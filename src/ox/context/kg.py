"""The context engine: branch-aware knowledge graph over the repo.

Wraps the `graphify` library (pip pkg `graphifyy`). Code = pure AST
extraction, zero LLM, no API keys. Docs/papers/images additionally go
through graphify's semantic (LLM) extraction, aimed by the `kg` alias in
models.json: ox's provider is pointed at whichever graphify backend speaks
its wire protocol, so the graph is built by the same models the harness
runs on. `OX_KG_BACKEND`/`OX_KG_MODEL` still override the alias and "none"
disables it; with neither, we fall back to graphify's env-key
autodetection, which recognizes only first-party keys (GEMINI_API_KEY,
ANTHROPIC_API_KEY, ...). That fallback is why the alias exists: an ox
configured entirely through OpenRouter/Ollama set none of those keys, so
autodetection returned None and the LLM half of the graph silently never
ran. Storage is
per-branch under `.ox/kg/<branch-slug>/graphify-out/` so switching
branches never corrupts the graph; `to_json(force=True)` overrides
graphify's #479 shrink-guard, which is legitimate here because each branch
owns its directory. Extraction caches (AST + semantic) live at
`<repo>/graphify-out/cache/` — content-hashed and branch-independent, shared
with any direct /graphify runs on the same repo.

Query matching is fully deterministic (decision #8): tokenize, split
camelCase/snake_case, singularize, fuzzy/substring match against the graph's
own vocabulary, IDF-rank — no LLM in the expansion step. Zero hits return
the nearest vocab tokens so a small model can self-correct. Both labels and
file paths are indexed, ranking survives into the traversal (best-first,
capped), and every result line carries a `file:line` location the read tool
can take verbatim — the three things that made search look broken from the
model's side.
"""

from __future__ import annotations

import difflib
import heapq
import itertools
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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
KG_ALIAS = "kg"  # models.json role that names the semantic-extraction model
# ox provider → the graphify backend that speaks its wire protocol. Every ox
# provider is OpenAI-compatible, so "openai" is the right default for a
# provider added to models.json later; only the URL and key differ, and both
# are supplied from the registry rather than graphify's env lookups.
GRAPHIFY_BACKEND_FOR_PROVIDER = {"ollama": "ollama", "openrouter": "openai"}

# ---- retrieval shape ----
# Seeds are the nodes the query itself matched; everything else is reached by
# traversal. Three seeds was too few to carry the ranking (one ambiguous label
# and the answer was already out), and the traversal that followed was
# unranked and uncapped — a query on a common term returned a fifth of the
# graph and the char budget then threw most of it away at random.
SEED_NODES = 8
MAX_RESULT_NODES = 60
BFS_DEPTH = 3
DFS_DEPTH = 6
# How much of a node's score a neighbour inherits. DFS questions ("how does X
# reach Y") are about distance, so they decay slower.
BFS_DECAY = 0.5
DFS_DECAY = 0.8
# A node this connected is a fine answer but a terrible doorway: expanding one
# 400-degree bundle node pulls in the whole repo.
HUB_EXPAND_DEGREE = 40
# A symbol *named* `session` beats one that merely lives in `session.py`.
PATH_TOKEN_WEIGHT = 0.4
MAX_LABEL_CHARS = 80  # labels can be whole docstrings

# Traversal mode. These must be *phrases*: bare "path" used to be in this
# list, so "where is the file path resolved" — an ordinary lookup — ran a
# depth-6 DFS over the graph.
_DFS_PHRASES = (
    "how does", "how do", "how is", "end to end", "end-to-end",
    "path from", "path to", "path between", "reach", "reaches",
    "call chain", "chain of", "flows through", "flow through",
    "depends on", "depend on", "downstream", "upstream",
    "trace", "traces", "leads to", "lead to", "connected to",
)
_DFS_RE = re.compile("|".join(rf"\b{re.escape(p)}\b" for p in _DFS_PHRASES))

# Build output is not source. A committed bundle is minified, so its symbols
# are single letters that poison the vocabulary every query is matched
# against, and its file node becomes the graph's highest-degree hub — any
# traversal touching it inhales the repo. In this repo one such bundle was
# 16% of all nodes and the top hub at degree 392. Filtered at extraction, so
# the junk never enters the graph rather than being hidden at render time.
ARTIFACT_DIRS = frozenset({
    "node_modules", "dist", "build", "out", "target", "coverage", "htmlcov",
    ".venv", "venv", "__pycache__", "site-packages", ".mypy_cache", ".pytest_cache",
    ".ox", "graphify-out", ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
})
ARTIFACT_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum",
})
_MINIFIED_SUFFIXES = (".min.js", ".min.css", ".min.mjs", ".bundle.js")
# vite/webpack content-hashed emits: index-Dv8sdJDj.js, main.4f3a2b1c.css
_HASHED_ASSET_RE = re.compile(r"[.-][A-Za-z0-9_-]{8,}\.(js|mjs|cjs|css)$")
# Naming rules miss a bundle emitted under a plain name; line length doesn't.
_SNIFF_EXTS = frozenset({".js", ".mjs", ".cjs", ".css", ".ts", ".tsx", ".jsx"})
_LONG_LINE_CHARS = 500
_LONG_LINES_FOR_MINIFIED = 3
_SNIFF_LINES = 50


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


def _short_label(nd: dict, nid, limit: int = MAX_LABEL_CHARS) -> str:
    """Labels can be whole docstrings; keep the first line, clipped."""
    return str(nd.get("label", nid)).splitlines()[0].strip()[:limit]


_LINE_RE = re.compile(r"L(\d+)\s*$")


def _node_loc(nd: dict) -> str:
    """'src/ox/tools/base.py:64' — a location the `read` tool can take verbatim.

    graphify puts the path in `source_file` and the line, *alone*, in
    `source_location` ("L64"); some graphs use "path:L64". Query output used
    to print `source_location or source_file`, and since source_location is
    almost never empty the path was discarded on every line — the model got a
    bare line number, guessed the file, and the read failed. Nodes with no
    file at all (unresolved cross-file names, and the arch harness's
    `design:<id>` seeds) keep their raw location: it is all they have.
    """
    f = _node_file(nd)
    raw = str(nd.get("source_location") or "")
    if not f:
        return raw
    m = _LINE_RE.search(raw)
    if m:
        return f"{f}:{m.group(1)}"
    return f"{f} ({raw})" if raw else f


def _looks_minified(path: Path) -> bool:
    """A generated bundle wears its shape on the outside: a handful of lines,
    each thousands of characters long. Cheap enough to run per candidate file
    (one open, first 50 lines) and it catches bundles no naming rule would."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = list(itertools.islice(fh, _SNIFF_LINES))
    except OSError:
        return False
    return sum(1 for line in head if len(line) > _LONG_LINE_CHARS) >= _LONG_LINES_FOR_MINIFIED


def is_artifact(path: Path) -> bool:
    """True for generated files — build output, lockfiles, minified bundles.

    These are checked-in *products*, not source. Indexing them costs nothing
    but noise: nobody asks the graph a question whose answer is `dist/`.
    """
    if set(path.parts) & ARTIFACT_DIRS:
        return True
    name = path.name
    if name in ARTIFACT_NAMES or name.endswith(_MINIFIED_SUFFIXES):
        return True
    if _HASHED_ASSET_RE.search(name):
        return True
    return path.suffix in _SNIFF_EXTS and _looks_minified(path)


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


@dataclass
class SemanticBackend:
    """A resolved target for semantic extraction: which graphify backend, and
    (when it came from models.json) the provider URL and key to aim it at.

    `base_url`/`api_key` are None for a backend named via `OX_KG_BACKEND`,
    which keeps that path exactly as it was — graphify reads its own env vars.
    """

    name: str  # a key in graphify.llm.BACKENDS
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None


class KG:
    def __init__(
        self,
        repo_root: Path,
        store_dir: Path | None = None,
        semantic_backend: str | None = None,
        semantic_model: str | None = None,
        models_json: str | Path | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.semantic_backend = semantic_backend or os.environ.get("OX_KG_BACKEND") or None
        self.semantic_model = semantic_model or os.environ.get("OX_KG_MODEL") or None
        self.models_json = str(models_json) if models_json else None
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
        self._artifacts_cache: tuple[float | None, list[str]] | None = None

    # ---------- lifecycle ----------

    def is_ready(self) -> bool:
        return self.graph_path.exists() and not self._building_marker.exists()

    def is_stale(self) -> bool:
        if not self.graph_path.exists():
            return True
        from graphify.detect import detect_incremental

        result = detect_incremental(self.repo_root, manifest_path=str(self.manifest_path))
        if result.get("new_total", 0) > 0 or bool(result.get("deleted_files")):
            return True
        # A graph built before the artifact filter is stale even when nothing
        # changed — without this the prune in update() is never reached and
        # the bundle survives until someone rebuilds by hand.
        return bool(self._indexed_artifacts())

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
        # A graph built before the artifact filter existed still carries the
        # bundle; drop it here so the fix reaches existing repos.
        stale_artifacts = self._indexed_artifacts()
        if not changed_code and not changed_semantic and not deleted and not stale_artifacts:
            return KGStats(*self._counts(), action="fresh")

        extraction = self._merge_extractions(
            self._extract_code(changed_code, collect_files, extract),
            self._extract_semantic(changed),
        )
        # prune_sources is deleted files plus anything the artifact filter now
        # rejects; build_merge's replace-on-re-extract reconciles changed files
        # (graphify #1344/#1178).
        G = build_merge(
            [extraction],
            graph_path=str(self.graph_path),
            prune_sources=(deleted + stale_artifacts) or None,
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
        cmd = [sys.executable, "-m", "ox.context.kg", str(self.repo_root), str(self.out_dir)]
        if self.models_json:
            cmd.append(self.models_json)  # the child resolves `kg` itself
        return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    @staticmethod
    def _extract_code(code_entries: list[str], collect_files, extract) -> dict:
        files: list[Path] = []
        for entry in code_entries:
            p = Path(entry)
            files.extend(collect_files(p) if p.is_dir() else [p])
        files = [f for f in files if not is_artifact(f)]
        if not files:
            return {"nodes": [], "edges": [], "input_tokens": 0, "output_tokens": 0}
        return extract(files)

    def _indexed_artifacts(self) -> list[str]:
        """Source files already in the graph that the artifact filter now
        rejects, so an existing graph cleans itself on the next update rather
        than carrying a minified bundle until someone thinks to rebuild.

        Memoised against the graph's mtime: `is_stale` calls this on every
        session start, and the minified sniff opens files.
        """
        if not self.graph_path.exists():
            return []
        try:
            G = self._load_graph()
        except (OSError, ValueError):
            return []
        if self._artifacts_cache is not None and self._artifacts_cache[0] == self._graph_mtime:
            return self._artifacts_cache[1]
        stale: set[str] = set()
        for _, nd in G.nodes(data=True):
            f = _node_file(nd)
            if f and f not in stale and is_artifact(self.repo_root / f):
                stale.add(f)
        found = sorted(stale)
        self._artifacts_cache = (self._graph_mtime, found)
        return found

    @staticmethod
    def _empty_extraction() -> dict:
        return {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}

    def _resolve_semantic_backend(self) -> SemanticBackend | None:
        """Explicit backend name wins; "none"/"off" disables; otherwise the
        `kg` alias in models.json decides, and only with no alias do we fall
        back to graphify's env-key autodetection."""
        if self.semantic_backend:
            name = self.semantic_backend.lower()
            if name in ("none", "off"):
                return None
            return SemanticBackend(name=self.semantic_backend, model=self.semantic_model)
        from_alias = self._backend_from_alias()
        if from_alias is not None:
            return from_alias
        from graphify.llm import detect_backend

        detected = detect_backend()
        return SemanticBackend(name=detected, model=self.semantic_model) if detected else None

    def _backend_from_alias(self) -> SemanticBackend | None:
        """models.json's `kg` alias → a graphify backend aimed at ox's provider.

        Returns None quietly when the alias is absent (nothing was asked for),
        but says so on stderr when the alias is present and unusable: a missing
        key silently degrading to an AST-only graph is the failure mode this
        whole path exists to fix, so it must never look like success.
        """
        from ..llm.registry import Registry, RegistryError

        try:
            registry = Registry.load(self.models_json)
        except (OSError, ValueError) as e:
            print(f"[ox kg] cannot read models.json ({e}) — semantic extraction off", file=sys.stderr)
            return None
        if KG_ALIAS not in registry.aliases:
            return None
        try:
            spec = registry.resolve(KG_ALIAS)
        except RegistryError as e:
            print(f"[ox kg] {e} — semantic extraction off", file=sys.stderr)
            return None
        if not spec.provider.api_key:
            print(
                f"[ox kg] '{KG_ALIAS}' alias is {spec.spec} but "
                f"{spec.provider.api_key_env} is unset — building an AST-only graph",
                file=sys.stderr,
            )
            return None
        return SemanticBackend(
            name=GRAPHIFY_BACKEND_FOR_PROVIDER.get(spec.provider.name, "openai"),
            model=self.semantic_model or spec.model,  # OX_KG_MODEL still wins
            api_key=spec.provider.api_key,
            base_url=spec.provider.base_url,
        )

    @staticmethod
    def _aim_backend(backend: SemanticBackend) -> bool:
        """Point graphify's backend entry at ox's provider. False = don't send.

        graphify captures each backend's base_url from the environment at
        import time, so an ox provider URL can only reach it by being written
        back into `BACKENDS` — setting OLLAMA_BASE_URL here would be read too
        late. The corpus and the API key both travel to whatever that URL
        names, so it goes through graphify's own exfiltration guard first, and
        a rejected URL disables extraction rather than falling through to
        graphify's default (localhost, or the wrong vendor entirely).
        """
        from graphify.llm import BACKENDS, provider_base_url_ok

        if backend.name not in BACKENDS:
            print(
                f"[ox kg] no graphify backend named {backend.name!r} "
                f"(known: {sorted(BACKENDS)}) — semantic extraction off",
                file=sys.stderr,
            )
            return False
        if backend.base_url:
            if not provider_base_url_ok(backend.base_url, f"ox:{backend.name}"):
                return False
            BACKENDS[backend.name]["base_url"] = backend.base_url
        return True

    def _extract_semantic(self, files_by_category: dict) -> dict:
        """Semantic (LLM) extraction for docs/papers/images via graphify's
        backend API — the same path the /graphify skill takes when an API key
        is present. No backend resolved → empty extraction (AST-only graph).

        The semantic cache lives at `<repo>/graphify-out/cache/semantic/`,
        the same content-hashed location the AST cache already uses (and that
        the /graphify skill shares), so it's branch-independent: unchanged
        docs are never re-extracted after a branch switch."""
        entries = [
            f
            for cat in SEMANTIC_TEXT_CATEGORIES
            for f in files_by_category.get(cat, [])
            if not is_artifact(Path(f))
        ]
        images = [f for f in files_by_category.get("image", []) if not is_artifact(Path(f))]
        if not entries and not images:
            return self._empty_extraction()
        backend = self._resolve_semantic_backend()
        if backend is None:
            return self._empty_extraction()
        from graphify.cache import check_semantic_cache, save_semantic_cache
        from graphify.llm import BACKENDS, extract_corpus_parallel

        if not self._aim_backend(backend):
            return self._empty_extraction()
        # Images ride along only where graphify will actually send pixels; on a
        # text-only backend they'd render as bare path references, and a chunk
        # the provider rejects takes its docs down with it.
        if BACKENDS[backend.name].get("vision"):
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
                backend=backend.name,
                api_key=backend.api_key,  # None → graphify falls back to its env key
                model=backend.model,
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

    @staticmethod
    def _vocabulary(G: nx.Graph) -> tuple[Counter[str], dict[str, set[str]], dict[str, set[str]]]:
        """Document frequency plus per-node label and *path* tokens.

        Paths are indexed because scoring ran on labels alone: a question
        phrased as a file or directory ("what is in tools/files.py") could not
        match anything, and two of the forty nodes labelled `.run()` were
        indistinguishable to the ranker. Kept separate from label tokens so a
        path match can be weighted below a name match.
        """
        df: Counter[str] = Counter()
        node_tokens: dict[str, set[str]] = {}
        path_tokens: dict[str, set[str]] = {}
        for nid, nd in G.nodes(data=True):
            toks = set(tokenize(str(nd.get("label", nid))))
            ptoks = set(tokenize(_node_file(nd))) - toks
            node_tokens[nid] = toks
            path_tokens[nid] = ptoks
            df.update(toks | ptoks)
        return df, node_tokens, path_tokens

    @staticmethod
    def _scorer(
        expanded: list[str],
        df: Counter[str],
        node_tokens: dict[str, set[str]],
        path_tokens: dict[str, set[str]],
        n_nodes: int,
    ) -> Callable[[str], float]:
        """IDF relevance of a node against the expanded query terms."""
        idf = {t: math.log(n_nodes / (1 + df.get(t, 0))) + 1.0 for t in expanded}

        def relevance(nid: str) -> float:
            labels = node_tokens.get(nid) or ()
            paths = path_tokens.get(nid) or ()
            return sum(idf[t] for t in expanded if t in labels) + PATH_TOKEN_WEIGHT * sum(
                idf[t] for t in expanded if t in paths
            )

        return relevance

    def query(self, question: str, budget: int = 2000) -> KGQueryResult:
        if not self.is_ready():
            raise KGError("knowledge graph is not ready")
        G = self._load_graph()

        df, node_tokens, path_tokens = self._vocabulary(G)
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

        relevance = self._scorer(expanded, df, node_tokens, path_tokens, n_nodes)

        scored = [(s, str(nid)) for nid in node_tokens if (s := relevance(nid)) > 0]
        scored.sort(key=lambda x: (-x[0], x[1]))
        starts = [nid for _, nid in scored[:SEED_NODES]]
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

        mode = "dfs" if _DFS_RE.search(question.lower()) else "bfs"
        sub_nodes, sub_edges = self._traverse(G, starts, mode, relevance, MAX_RESULT_NODES)

        lines = [
            f"[{mode.upper()} from: "
            + ", ".join(_short_label(G.nodes[n], n) for n in starts[:3])
            + f" | matched terms: {' '.join(expanded)} | {len(sub_nodes)} nodes]"
        ]
        for nid in sorted(sub_nodes, key=lambda n: (-relevance(n), str(n))):
            nd = G.nodes[nid]
            loc = _node_loc(nd)
            lines.append(f"NODE {_short_label(nd, nid)}" + (f"  [{loc}]" if loc else ""))
        for u, v in sub_edges:
            if u in sub_nodes and v in sub_nodes:
                ed = G[u][v]
                if isinstance(G, nx.MultiGraph):
                    ed = next(iter(ed.values()), {})
                rel = ed.get("relation", "related")
                lines.append(
                    f"EDGE {_short_label(G.nodes[u], u)} --{rel}--> {_short_label(G.nodes[v], v)}"
                )
        if len(sub_nodes) >= MAX_RESULT_NODES:
            lines.append(
                f"[capped at {MAX_RESULT_NODES} highest-scoring nodes — ask a narrower "
                "question if the answer is not here]"
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
            return _short_label(nd, nid, 48)  # tighter than query output

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
        G: nx.Graph,
        starts: list[str],
        mode: str,
        relevance: Callable[[str], float] | None = None,
        limit: int = MAX_RESULT_NODES,
    ) -> tuple[set[str], list[tuple[str, str]]]:
        """Best-first expansion from the seeds, capped at `limit` nodes.

        The previous version flooded: an unranked, uncapped BFS/DFS to a fixed
        depth. Ranking decided the three seeds and then stopped mattering, so
        "where is resolve_path defined" came back with 523 nodes — a fifth of
        the graph — of which the char budget kept an arbitrary prefix. Here
        the frontier is a priority queue ordered by the node's own relevance
        plus a decayed share of the parent's, so the cap keeps the *best*
        nodes rather than the first ones the walk happened to reach.

        A hub is still returned when it matches; it is just not expanded
        *through*. One 392-degree bundle node is enough to pull in the repo,
        and the nodes behind it are related to each other, not to the query.
        """
        score_of = relevance or (lambda _n: 0.0)
        decay = DFS_DECAY if mode == "dfs" else BFS_DECAY
        depth_cap = DFS_DEPTH if mode == "dfs" else BFS_DEPTH
        sub_nodes: set[str] = set(starts)
        sub_edges: list[tuple[str, str]] = []
        # (-score, depth, tiebreak, node) — the str tiebreak keeps heapq from
        # ever comparing node objects themselves.
        heap: list[tuple[float, int, str, Any]] = [
            (-score_of(n), 0, str(n), n) for n in starts
        ]
        heapq.heapify(heap)
        while heap and len(sub_nodes) < limit:
            neg_score, depth, _, node = heapq.heappop(heap)
            if depth >= depth_cap:
                continue
            if depth and G.degree(node) > HUB_EXPAND_DEGREE:
                continue
            inherited = -neg_score * decay
            for nb in sorted(G.neighbors(node), key=lambda n: (-score_of(n), str(n))):
                if nb in sub_nodes:
                    continue
                sub_nodes.add(nb)
                sub_edges.append((node, nb))
                heapq.heappush(heap, (-(score_of(nb) + inherited), depth + 1, str(nb), nb))
                if len(sub_nodes) >= limit:
                    break
        return sub_nodes, sub_edges


class KGError(Exception):
    pass


def _main() -> int:
    """Subprocess entrypoint for background builds:
    kg <repo_root> [out_dir] [models_json]."""
    from dotenv import load_dotenv

    # The build resolves the `kg` alias itself, so it needs the provider key.
    # A spawned child inherits it from the parent's load_dotenv(); this is for
    # `python -m ox.context.kg` run by hand. Never overrides what's already set.
    load_dotenv()
    repo_root = Path(sys.argv[1])
    kg = KG(
        repo_root,
        store_dir=Path(sys.argv[2]) if len(sys.argv) > 2 else None,
        models_json=sys.argv[3] if len(sys.argv) > 3 else None,
    )
    stats = kg.ensure()
    print(f"kg {stats.action}: {stats.nodes} nodes, {stats.edges} edges")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
