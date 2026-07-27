"""The context engine: branch-aware knowledge graph over the repo.

Wraps the `graphify` library (pip pkg `graphifyy`). Code = pure AST
extraction, zero LLM, no API keys. Docs/papers/images additionally go
through graphify's semantic (LLM) extraction when a backend is available:
explicit via `OX_KG_BACKEND` (any name in `graphify.llm.BACKENDS`, or
"none" to disable) or auto-detected from provider API keys in the
environment — no key, no LLM, same AST-only graph as before. Storage is
per-branch under `.ox/kg/<branch-slug>/graphify-out/` so switching
branches never corrupts the graph; `to_json(force=True)` overrides
graphify's #479 shrink-guard, which is legitimate here because each branch
owns its directory. Extraction caches (AST + semantic) live at
`<repo>/graphify-out/cache/` — content-hashed and branch-independent, shared
with any direct /graphify runs on the same repo.

Query matching is fully deterministic (decision #8): tokenize, split
camelCase/snake_case, singularize, fuzzy/substring match against the graph's
own vocabulary, IDF-rank — no LLM in the expansion step. Zero hits return
the nearest vocab tokens so a small model can self-correct.
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")

MAX_EXPANDED_TOKENS = 12
STOPWORDS = frozenset(
    "the and for with that this from into how does what where when why can are was "
    "were has have had not you your our its all any use used using work works".split()
)
NEAREST_ON_MISS = 10
SEMANTIC_TEXT_CATEGORIES = ("document", "paper")
BFS_DEPTH = 3
DFS_DEPTH = 6
_DFS_HINTS = ("how does", "reach", "path", "flow", "depend", "chain", "trace", "lead")


def tokenize(text: str) -> list[str]:
    """word → camelCase/snake_case parts → lowercase, length 3–30."""
    tokens = []
    for chunk in _WORD_RE.findall(text or ""):
        for part in _CAMEL_RE.findall(chunk) or [chunk]:
            t = part.lower()
            if 3 <= len(t) <= 30:
                tokens.append(t)
    return tokens


def singularize(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ses", "xes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def branch_slug(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        branch = ""
    if not branch:
        return "no-git"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "no-git"


def _node_file(nd: dict) -> str:
    """graphify puts the path in source_file; source_location may be
    line-only ("L12") or "path:L12" — take a path only if it has one."""
    f = str(nd.get("source_file") or "")
    if f:
        return f
    loc = str(nd.get("source_location") or "")
    head = loc.rsplit(":", 1)[0]
    return head if "/" in head or head.endswith(".py") else ""


def _norm_path(p: str) -> str:
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _same_file(a: str, b: str) -> bool:
    """Suffix-tolerant match: the graph may store absolute paths while the
    model supplies repo-relative ones (or vice versa)."""
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _norm_label(label: str) -> str:
    """'SessionRecorder' / 'new_run_id()' / '.close()' → comparable symbol name."""
    s = str(label).splitlines()[0].strip().lower()
    return s.removesuffix("()").lstrip(".")


@dataclass
class KGQueryResult:
    text: str
    hit_count: int
    expanded_tokens: list[str] = field(default_factory=list)
    mode: str = "bfs"


@dataclass
class KGStats:
    nodes: int
    edges: int
    action: str  # "built" | "updated" | "fresh" | "seeded"


class KG:
    def __init__(
        self,
        repo_root: Path,
        store_dir: Path | None = None,
        semantic_backend: str | None = None,
        semantic_model: str | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.semantic_backend = semantic_backend or os.environ.get("OX_KG_BACKEND") or None
        self.semantic_model = semantic_model or os.environ.get("OX_KG_MODEL") or None
        self.out_dir = (
            Path(store_dir)
            if store_dir
            else self.repo_root / ".ox" / "kg" / branch_slug(self.repo_root) / "graphify-out"
        )
        self.graph_path = self.out_dir / "graph.json"
        self.manifest_path = self.out_dir / "manifest.json"
        self._building_marker = self.out_dir / ".building"
        self._graph_cache: nx.Graph | None = None
        self._graph_mtime: float | None = None

    # ---------- lifecycle ----------

    def is_ready(self) -> bool:
        return self.graph_path.exists() and not self._building_marker.exists()

    def is_stale(self) -> bool:
        if not self.graph_path.exists():
            return True
        from graphify.detect import detect_incremental

        result = detect_incremental(self.repo_root, manifest_path=str(self.manifest_path))
        return result.get("new_total", 0) > 0 or bool(result.get("deleted_files"))

    def build(self) -> KGStats:
        """Full build: AST for code, semantic (LLM) for docs when a backend
        is available, cluster, export. Without a backend: AST-only, no keys."""
        from graphify.build import build_from_json
        from graphify.cluster import cluster
        from graphify.detect import detect, save_manifest
        from graphify.export import to_json
        from graphify.extract import collect_files, extract

        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._building_marker.touch()
        try:
            detection = detect(self.repo_root)
            extraction = self._merge_extractions(
                self._extract_code(detection["files"].get("code", []), collect_files, extract),
                self._extract_semantic(detection["files"]),
            )
            G = build_from_json(extraction, root=str(self.repo_root))
            if G.number_of_nodes() == 0:
                raise KGError("extraction produced an empty graph (no supported files?)")
            communities = cluster(G)
            # force=True: each branch owns its dir, so shrinking is legitimate (#479).
            to_json(G, communities, str(self.graph_path), force=True)
            save_manifest(detection["files"], manifest_path=str(self.manifest_path), root=self.repo_root)
        finally:
            self._building_marker.unlink(missing_ok=True)
        self._graph_cache = None
        return KGStats(nodes=G.number_of_nodes(), edges=G.number_of_edges(), action="built")

    def update(self) -> KGStats:
        """Incremental: re-extract changed code files, merge, prune deletions."""
        if not self.graph_path.exists():
            return self.build()
        from graphify.build import build_merge
        from graphify.cluster import cluster
        from graphify.detect import detect_incremental, save_manifest
        from graphify.export import to_json
        from graphify.extract import collect_files, extract

        result = detect_incremental(self.repo_root, manifest_path=str(self.manifest_path))
        changed = result.get("new_files", {})
        changed_code = changed.get("code", [])
        changed_semantic = any(
            changed.get(cat) for cat in (*SEMANTIC_TEXT_CATEGORIES, "image")
        )
        deleted = list(result.get("deleted_files", []))
        if not changed_code and not changed_semantic and not deleted:
            return KGStats(*self._counts(), action="fresh")

        extraction = self._merge_extractions(
            self._extract_code(changed_code, collect_files, extract),
            self._extract_semantic(changed),
        )
        # prune_sources is ONLY deleted files; build_merge's replace-on-re-extract
        # reconciles changed files (graphify #1344/#1178).
        G = build_merge(
            [extraction],
            graph_path=str(self.graph_path),
            prune_sources=deleted or None,
            root=str(self.repo_root),
        )
        communities = cluster(G)
        to_json(G, communities, str(self.graph_path), force=True)
        save_manifest(result["files"], manifest_path=str(self.manifest_path), root=self.repo_root)
        self._graph_cache = None
        return KGStats(nodes=G.number_of_nodes(), edges=G.number_of_edges(), action="updated")

    def seed(self, nodes: list[dict], edges: list[dict]) -> KGStats:
        """Merge hand-authored nodes into the graph, creating one if there is none.

        Extraction can only describe code that exists. The arch harness calls
        this at finalize so a greenfield `ox code` session can `kg_query` the
        architecture on turn one — the components, their contracts and what
        talks to what — instead of querying an empty repo and getting nothing.

        Written through graphify's own exporter, so the result is shaped exactly
        like a built graph and every reader stays oblivious. A later full
        `build()` drops these nodes, which is correct: by then the code they
        describe exists, and the graph should be earned rather than asserted.
        """
        from graphify.cluster import cluster
        from graphify.export import to_json

        G = self._load_graph().copy() if self.graph_path.exists() else nx.Graph()
        for node in nodes:
            attrs = {k: v for k, v in node.items() if k != "id"}
            G.add_node(node["id"], **attrs)
        for edge in edges:
            src, dst = edge.get("source"), edge.get("target")
            if src not in G or dst not in G:
                continue  # never leave a dangling edge behind
            G.add_edge(src, dst, **{k: v for k, v in edge.items() if k not in ("source", "target")})
        self.out_dir.mkdir(parents=True, exist_ok=True)
        communities = cluster(G) if G.number_of_nodes() else {}
        to_json(G, communities, str(self.graph_path), force=True)
        self._graph_cache = None
        return KGStats(nodes=G.number_of_nodes(), edges=G.number_of_edges(), action="seeded")

    def ensure(self) -> KGStats:
        """Build if missing, update if stale, no-op if fresh. Blocking.
        Always clears the .building marker (ensure_background sets it before
        the subprocess starts, and update() doesn't manage it)."""
        try:
            if not self.graph_path.exists():
                return self.build()
            return self.update()
        finally:
            self._building_marker.unlink(missing_ok=True)

    def ensure_background(self) -> subprocess.Popen | None:
        """Kick off ensure() in a subprocess and return immediately (decision #9).
        Returns the process, or None if the graph is already fresh."""
        if self.graph_path.exists() and not self.is_stale():
            return None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._building_marker.touch()  # visible immediately so is_ready() is False
        log = (self.out_dir / "build.log").open("w")
        return subprocess.Popen(
            [sys.executable, "-m", "ox.context.kg", str(self.repo_root), str(self.out_dir)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    @staticmethod
    def _extract_code(code_entries: list[str], collect_files, extract) -> dict:
        files: list[Path] = []
        for entry in code_entries:
            p = Path(entry)
            files.extend(collect_files(p) if p.is_dir() else [p])
        if not files:
            return {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
        return extract(files)

    @staticmethod
    def _empty_extraction() -> dict:
        return {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}

    def _resolve_semantic_backend(self) -> str | None:
        """Explicit backend name wins; "none"/"off" disables; unset falls back
        to graphify's env-key autodetection (no key anywhere → None)."""
        if self.semantic_backend:
            name = self.semantic_backend.lower()
            return None if name in ("none", "off") else self.semantic_backend
        from graphify.llm import detect_backend

        return detect_backend()

    def _extract_semantic(self, files_by_category: dict) -> dict:
        """Semantic (LLM) extraction for docs/papers/images via graphify's
        backend API — the same path the /graphify skill takes when an API key
        is present. No backend resolved → empty extraction (AST-only graph).

        The semantic cache lives at `<repo>/graphify-out/cache/semantic/`,
        the same content-hashed location the AST cache already uses (and that
        the /graphify skill shares), so it's branch-independent: unchanged
        docs are never re-extracted after a branch switch."""
        entries = [f for cat in SEMANTIC_TEXT_CATEGORIES for f in files_by_category.get(cat, [])]
        images = list(files_by_category.get("image", []))
        if not entries and not images:
            return self._empty_extraction()
        backend = self._resolve_semantic_backend()
        if backend is None:
            return self._empty_extraction()
        from graphify.cache import check_semantic_cache, save_semantic_cache
        from graphify.llm import BACKENDS, extract_corpus_parallel

        if BACKENDS.get(backend, {}).get("vision"):
            entries += images
        cached_nodes, cached_edges, cached_hyper, uncached = check_semantic_cache(
            entries, root=self.repo_root
        )
        new = self._empty_extraction()
        if uncached:
            # extract_corpus_parallel checkpoints each chunk into the cache as
            # it completes; this final save just makes the result authoritative.
            new = extract_corpus_parallel(
                uncached,
                backend=backend,
                model=self.semantic_model,
                root=self.repo_root,
            )
            save_semantic_cache(
                new.get("nodes", []),
                new.get("edges", []),
                new.get("hyperedges", []),
                root=self.repo_root,
            )
        return {
            "nodes": cached_nodes + new.get("nodes", []),
            "edges": cached_edges + new.get("edges", []),
            "hyperedges": cached_hyper + new.get("hyperedges", []),
            "input_tokens": new.get("input_tokens", 0),
            "output_tokens": new.get("output_tokens", 0),
        }

    @staticmethod
    def _merge_extractions(ast: dict, semantic: dict) -> dict:
        """AST nodes win on id collision (mirrors the skill's Part C merge)."""
        seen = {n["id"] for n in ast["nodes"]}
        nodes = list(ast["nodes"])
        for n in semantic["nodes"]:
            if n["id"] not in seen:
                seen.add(n["id"])
                nodes.append(n)
        return {
            "nodes": nodes,
            "edges": ast["edges"] + semantic["edges"],
            "hyperedges": ast.get("hyperedges", []) + semantic.get("hyperedges", []),
            "input_tokens": ast.get("input_tokens", 0) + semantic.get("input_tokens", 0),
            "output_tokens": ast.get("output_tokens", 0) + semantic.get("output_tokens", 0),
        }

    def _counts(self) -> tuple[int, int]:
        G = self._load_graph()
        return G.number_of_nodes(), G.number_of_edges()

    # ---------- query ----------

    def _load_graph(self) -> nx.Graph:
        mtime = self.graph_path.stat().st_mtime
        if self._graph_cache is None or self._graph_mtime != mtime:
            data = json.loads(self.graph_path.read_text(encoding="utf-8"))
            self._graph_cache = json_graph.node_link_graph(data, edges="links")
            self._graph_mtime = mtime
        return self._graph_cache

    def query(self, question: str, budget: int = 2000) -> KGQueryResult:
        if not self.is_ready():
            raise KGError("knowledge graph is not ready")
        G = self._load_graph()

        # vocabulary + document frequency over node labels
        df: Counter[str] = Counter()
        node_tokens: dict[str, set[str]] = {}
        for nid, nd in G.nodes(data=True):
            toks = set(tokenize(str(nd.get("label", nid))))
            node_tokens[nid] = toks
            df.update(toks)
        n_nodes = max(G.number_of_nodes(), 1)
        vocab = set(df)

        expanded = self._expand(question, vocab, df, n_nodes)
        if not expanded:
            nearest = self._nearest_vocab(question, vocab)
            return KGQueryResult(
                text=(
                    "No graph vocabulary matched this question. Nearest terms in the "
                    "codebase: " + ", ".join(nearest) + ". Retry kg_query rephrased "
                    "with these terms — the graph is working, this question just "
                    "missed its vocabulary. Use bash only for literal string content "
                    "that is not a code symbol."
                ),
                hit_count=0,
                expanded_tokens=[],
                mode="none",
            )

        def idf(t: str) -> float:
            return math.log(n_nodes / (1 + df.get(t, 0))) + 1.0

        scored = []
        for nid, toks in node_tokens.items():
            s = sum(idf(t) for t in expanded if t in toks)
            if s > 0:
                scored.append((s, str(nid)))
        scored.sort(key=lambda x: (-x[0], x[1]))
        starts = [nid for _, nid in scored[:3]]
        if not starts:
            nearest = self._nearest_vocab(question, vocab)
            return KGQueryResult(
                text=(
                    "No nodes matched. Nearest vocabulary: " + ", ".join(nearest)
                    + ". Retry kg_query with these terms."
                ),
                hit_count=0,
                expanded_tokens=expanded,
                mode="none",
            )

        mode = "dfs" if any(h in question.lower() for h in _DFS_HINTS) else "bfs"
        sub_nodes, sub_edges = self._traverse(G, starts, mode)

        def relevance(nid: str) -> float:
            return sum(idf(t) for t in expanded if t in node_tokens.get(nid, set()))

        lines = [
            f"[{mode.upper()} from: "
            + ", ".join(str(G.nodes[n].get("label", n)) for n in starts)
            + f" | matched terms: {' '.join(expanded)} | {len(sub_nodes)} nodes]"
        ]
        for nid in sorted(sub_nodes, key=lambda n: (-relevance(n), str(n))):
            nd = G.nodes[nid]
            loc = nd.get("source_location", "") or nd.get("source_file", "")
            lines.append(f"NODE {nd.get('label', nid)}" + (f"  [{loc}]" if loc else ""))
        for u, v in sub_edges:
            if u in sub_nodes and v in sub_nodes:
                ed = G[u][v]
                if isinstance(G, nx.MultiGraph):
                    ed = next(iter(ed.values()), {})
                rel = ed.get("relation", "related")
                lines.append(
                    f"EDGE {G.nodes[u].get('label', u)} --{rel}--> {G.nodes[v].get('label', v)}"
                )

        text = "\n".join(lines)
        char_budget = budget * 4
        if len(text) > char_budget:
            text = text[:char_budget] + f"\n... [truncated at ~{budget} tokens; pass a larger budget]"
        return KGQueryResult(text=text, hit_count=len(sub_nodes), expanded_tokens=expanded, mode=mode)

    def digest(self, max_files: int = 15, max_symbols: int = 6) -> str:
        """Compact orientation block for a system prompt: the files with the
        most symbols plus the highest-degree hub nodes — the pre-computed
        answer to the 'what is this codebase?' query every session starts with."""
        def short_label(nd: dict, nid) -> str:
            # labels can be whole docstrings; keep the first line, tightly
            return str(nd.get("label", nid)).splitlines()[0].strip()[:48]

        G = self._load_graph()
        by_file: dict[str, set[str]] = {}
        for nid, nd in G.nodes(data=True):
            f = _node_file(nd)
            if f:
                by_file.setdefault(f, set()).add(short_label(nd, nid))
        lines = ["[repo map — from the knowledge graph]"]
        for f, syms in sorted(by_file.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:max_files]:
            shown = ", ".join(sorted(syms)[:max_symbols])
            more = f", +{len(syms) - max_symbols} more" if len(syms) > max_symbols else ""
            lines.append(f"  {f}: {shown}{more}")
        hubs = sorted(G.degree, key=lambda kv: -kv[1])[:8]
        seen: list[str] = []
        for n, _ in hubs:
            lbl = short_label(G.nodes[n], n)
            if lbl not in seen:
                seen.append(lbl)
        if seen:
            lines.append("  key hubs: " + ", ".join(seen))
        return "\n".join(lines)

    def affected_files(self, files: list[str], depth: int = 2, limit: int = 8) -> list[str]:
        """Blast radius of touching `files`: other files whose symbols sit
        within `depth` graph hops of any symbol defined in them, ranked by how
        many connections land there. Used by the plan tool so a step can show
        what it may affect without the model reading everything."""
        if not self.is_ready():
            raise KGError("knowledge graph is not ready")
        G = self._load_graph()
        targets = [_norm_path(f) for f in files]
        file_of = {nid: _norm_path(_node_file(nd)) for nid, nd in G.nodes(data=True)}

        def is_target(f: str) -> bool:
            return bool(f) and any(_same_file(f, t) for t in targets)

        seeds = {nid for nid, f in file_of.items() if is_target(f)}
        # graphify attaches cross-file calls/references to bare unresolved-name
        # nodes (no file) instead of the definition node, with no edge between
        # the two — bridge that gap by label so consumers are reachable. Method
        # labels ('.close()') are skipped: too generic across files.
        seed_labels = {
            _norm_label(G.nodes[n].get("label", n))
            for n in seeds
            if not str(G.nodes[n].get("label", "")).startswith(".")
        }
        seed_labels.discard("")
        seeds |= {
            nid
            for nid, nd in G.nodes(data=True)
            if not file_of.get(nid) and _norm_label(nd.get("label", nid)) in seed_labels
        }
        visited = set(seeds)
        frontier = seeds
        hits: Counter[str] = Counter()
        for _ in range(depth):
            nxt: set[str] = set()
            for n in frontier:
                for nb in G.neighbors(n):
                    if nb in visited:
                        continue
                    visited.add(nb)
                    nxt.add(nb)
                    f = file_of.get(nb, "")
                    if f and not is_target(f):
                        hits[f] += 1
            frontier = nxt
        out: list[str] = []
        for f, _ in hits.most_common():
            if len(out) >= limit:
                break
            if not any(_same_file(f, kept) for kept in out):
                out.append(f)
        return out

    @staticmethod
    def _expand(question: str, vocab: set[str], df: Counter, n_nodes: int) -> list[str]:
        """Deterministic expansion: exact → singular → substring → fuzzy; IDF-ranked."""
        candidates: dict[str, float] = {}

        def add(token: str, weight: float) -> None:
            idf = math.log(n_nodes / (1 + df.get(token, 0))) + 1.0
            score = weight * idf
            if score > candidates.get(token, 0.0):
                candidates[token] = score

        for q in tokenize(question):
            if q in STOPWORDS:
                continue
            forms = {q, singularize(q)}
            matched = False
            for form in forms:
                if form in vocab:
                    add(form, 3.0)
                    matched = True
            if matched:
                continue
            for form in forms:
                if len(form) < 4:
                    continue
                for v in vocab:
                    if v in STOPWORDS:
                        continue
                    if form in v or (len(v) >= 4 and v in form):
                        add(v, 1.5)
                        matched = True
            if not matched:
                for close in difflib.get_close_matches(q, vocab, n=3, cutoff=0.8):
                    add(close, 1.0)

        ranked = sorted(candidates, key=lambda t: (-candidates[t], t))
        return ranked[:MAX_EXPANDED_TOKENS]

    @staticmethod
    def _nearest_vocab(question: str, vocab: set[str]) -> list[str]:
        nearest: list[str] = []
        for q in tokenize(question):
            for m in difflib.get_close_matches(q, vocab, n=3, cutoff=0.5):
                if m not in nearest:
                    nearest.append(m)
        if not nearest:  # question shares nothing; show most common structural terms
            nearest = sorted(vocab)[:NEAREST_ON_MISS]
        return nearest[:NEAREST_ON_MISS]

    @staticmethod
    def _traverse(
        G: nx.Graph, starts: list[str], mode: str
    ) -> tuple[set[str], list[tuple[str, str]]]:
        sub_nodes: set[str] = set(starts)
        sub_edges: list[tuple[str, str]] = []
        if mode == "dfs":
            visited: set[str] = set()
            stack = [(n, 0) for n in reversed(starts)]
            while stack:
                node, depth = stack.pop()
                if node in visited or depth > DFS_DEPTH:
                    continue
                visited.add(node)
                sub_nodes.add(node)
                for nb in G.neighbors(node):
                    if nb not in visited:
                        stack.append((nb, depth + 1))
                        sub_edges.append((node, nb))
        else:
            frontier = set(starts)
            for _ in range(BFS_DEPTH):
                nxt: set[str] = set()
                for n in frontier:
                    for nb in G.neighbors(n):
                        if nb not in sub_nodes:
                            nxt.add(nb)
                            sub_edges.append((n, nb))
                sub_nodes.update(nxt)
                frontier = nxt
        return sub_nodes, sub_edges


class KGError(Exception):
    pass


def _main() -> int:
    """Subprocess entrypoint for background builds: kg <repo_root> [out_dir]."""
    repo_root = Path(sys.argv[1])
    kg = KG(repo_root, store_dir=Path(sys.argv[2]) if len(sys.argv) > 2 else None)
    stats = kg.ensure()
    print(f"kg {stats.action}: {stats.nodes} nodes, {stats.edges} edges")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
