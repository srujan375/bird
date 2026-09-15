"""The context engine: branch-aware knowledge graph over the repo.

Wraps the `graphify` library (pip pkg `graphifyy`). Code = pure AST
extraction, zero LLM, no API keys. Docs/papers/images additionally go
through graphify's semantic (LLM) extraction, aimed by the `kg` alias in
models.json: bird's provider is pointed at whichever graphify backend speaks
its wire protocol, so the graph is built by the same models the harness
runs on. `BIRD_KG_BACKEND`/`BIRD_KG_MODEL` still override the alias and "none"
disables it; with neither, we fall back to graphify's env-key
autodetection, which recognizes only first-party keys (GEMINI_API_KEY,
ANTHROPIC_API_KEY, ...). That fallback is why the alias exists: an bird
configured entirely through OpenRouter/Ollama set none of those keys, so
autodetection returned None and the LLM half of the graph silently never
ran. Storage is
per-branch under `.bird/kg/<branch-slug>/graphify-out/` so switching
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
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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

# ---- questions the graph is the wrong tool for ----
# A code graph knows symbols, files and the edges between them. It does not
# know the git index, and it cannot enumerate filenames by pattern. Asked
# anyway it does not fail — expansion finds *some* matching token, the
# traversal fills its node cap, and sixty confident-looking nodes come back.
# One logged session asked "list all files whose path contains mcp" five times,
# reworded each time, and got sixty nodes every time. Recognising the shape of
# the question and naming the right tool is the only honest answer.
_OUT_OF_SCOPE = (
    (
        re.compile(
            r"\bgit (status|diff|stash|working)\b|\buntracked\b|\buncommitted\b"
            r"|\bworking (tree|directory|copy)\b|\bstaged\b|\bunstaged\b"
            r"|\brecently (added|modified|changed|created)\b"
        ),
        "the state of the git working tree",
        "bash: `git status`, `git diff --name-only`",
    ),
    (
        re.compile(
            r"\blist (all|any|every|the) files?\b"
            r"|\bfiles? whose (name|path|filename)\b"
            r"|\bfiles?\b.{0,40}\bin (its|their|the) (name|path|filename)\b"
            r"|\b(any|all|every) files? (named|called)\b"
            r"|\bfilenames?\b.{0,30}\b(contain|match)"
            r"|\bfiles? .{0,20}\b(contain|containing|matching)\b.{0,30}\bin (the )?(name|path|filename)\b"
        ),
        "which files exist by name",
        'the `glob` tool, e.g. glob {"pattern": "**/*mcp*"}',
    ),
)
# Mentioning a directory the extractor filters out. These are exactly the paths
# the graph cannot answer about (see ARTIFACT_DIRS), which is what makes them
# the paths grep has to cover.
_ARTIFACT_MENTION = re.compile(
    r"\bnode_modules\b|\bsite-packages\b|\bdist/|\bbuild/|\bvendor\b"
    r"|\b(dependency|dependencies|third[- ]party|installed package)\b"
)


def _out_of_scope(question: str) -> str | None:
    """A redirect naming the right tool, or None if the graph should try."""
    q = question.lower()
    for pattern, subject, tool in _OUT_OF_SCOPE:
        if pattern.search(q):
            return (
                f"The knowledge graph does not index {subject} — it maps code "
                f"structure (symbols, files, and the edges between them). This "
                f"question is answered by {tool}. Asking it here would return "
                f"unrelated nodes, not a wrong-looking error, so it is being "
                f"refused instead."
            )
    if _ARTIFACT_MENTION.search(q):
        return (
            "The knowledge graph deliberately does not index generated or "
            "installed directories (node_modules, dist, build, site-packages) — "
            "their contents are minified or vendored and would swamp the graph. "
            "Search them directly instead: `grep` with a path inside the "
            "directory, or `glob` to find the files first. kg_query still "
            "answers questions about this repository's own source."
        )
    return None
SEMANTIC_TEXT_CATEGORIES = ("document", "paper")
# A stale verdict is re-used until the graph is rebuilt; a fresh one is
# re-checked after this long. is_stale() walks the repo (~180ms here) against a
# ~20ms query, so it can never run per call.
STALENESS_TTL_SECONDS = 30.0
STALE_NOTICE = (
    "[STALE GRAPH — files have changed since this graph was built. The symbol "
    "locations below may be out of date: treat every [path:line] as a hint, and "
    "`read` the file to confirm before you edit it. Structure (what calls what) "
    "is still broadly right; exact lines are not.]"
)

KG_ALIAS = "kg"  # models.json role that names the semantic-extraction model
# bird provider → the graphify backend that speaks its wire protocol. Every bird
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
    ".bird", "graphify-out", ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
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
    """'src/bird/tools/base.py:64' — a location the `read` tool can take verbatim.

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


# Extensions the repo map treats as program source. Manifests, docs, styles and
# lockfiles are indexed (package.json alone contributes 85 nodes here) but a
# map that leads with them answers "what is this codebase" with its metadata.
_SOURCE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".rb", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".scala",
    ".php", ".ex", ".exs", ".sh",
}
_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs"}
# Edges that describe structure (a file contains a symbol, a class has a
# method) or annotation (a rationale note hangs off a symbol). They inflate a
# node's degree without saying anything about who USES it — a file with 178
# children would otherwise be every map's top hub.
_STRUCTURAL_RELATIONS = {"contains", "method", "rationale_for"}
# Pragma-ish first lines that are not a purpose statement.
_NOT_A_PURPOSE = ("#!", "# -*-", "# coding", "// @ts-", "/* eslint", "// eslint", "\"use strict\"", "'use strict'")


def _is_source_path(p: str) -> bool:
    return os.path.splitext(p)[1].lower() in _SOURCE_EXTS


def _is_test_path(p: str) -> bool:
    parts = _norm_path(p).split("/")
    if set(parts[:-1]) & _TEST_DIRS:
        return True
    name = parts[-1].lower()
    stem = name.rsplit(".", 1)[0]
    return (
        name.startswith(("test_", "conftest"))
        or stem.endswith(("_test", ".test", ".spec", "_spec"))
    )


def _file_purpose(path, limit: int = 96) -> str:
    """The author's one-line answer to "what is this file": the first sentence
    of a module docstring or of a leading comment block. Empty when there is
    none or the file cannot be read — the map then falls back to symbols
    alone. Reads at most 2KB, so pricing fifteen of these into a system prompt
    is a rounding error next to one model call.

    The whole first paragraph is gathered before the sentence is cut, because
    a docstring's first line routinely ends mid-thought ("the setup
    walkthrough, key management and the") and the period is on line two."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(2048)
    except OSError:
        return ""
    raw = [ln.strip() for ln in head.splitlines()]
    i = 0
    while i < len(raw) and (not raw[i] or raw[i].startswith(_NOT_A_PURPOSE)):
        i += 1
    if i >= len(raw):
        return ""
    first = raw[i]
    para: list[str] = []
    if first.startswith(('"""', "\'\'\'")):
        q, rest = first[:3], first[3:]
        if q in rest:
            para.append(rest.split(q)[0])
        else:
            if rest.strip():
                para.append(rest)
            for ln in raw[i + 1:]:
                if not ln:
                    break
                if q in ln:
                    tail = ln.split(q)[0]
                    if tail.strip():
                        para.append(tail)
                    break
                para.append(ln)
    elif first.startswith("/*"):
        rest = first.lstrip("/*")
        if "*/" in rest:
            para.append(rest.split("*/")[0])
        else:
            if rest.strip():
                para.append(rest)
            for ln in raw[i + 1:]:
                body = ln.lstrip("*").strip()
                if not body:
                    break
                if "*/" in ln:
                    tail = ln.split("*/")[0].lstrip("*")
                    if tail.strip():
                        para.append(tail)
                    break
                para.append(body)
    elif first.startswith(("//", "#")):
        for ln in raw[i:]:
            if not ln.startswith(("//", "#")):
                break
            body = ln.lstrip("/#").strip()
            if not body:
                break
            para.append(body)
    else:
        return ""
    text = " ".join(x.strip() for x in para).strip()
    if not text or text.startswith(("import ", "from ", "export ", "package ", "use ")):
        return ""
    cut = text.find(". ")
    if 0 < cut < limit:
        text = text[:cut + 1]
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


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
    # the graph no longer matches the working tree — answers are still useful
    # for structure, but line numbers may point at code that has since moved
    stale: bool = False
    # "exact"  at least one query term is a symbol/path token in the graph
    # "weak"   seeds came only from substring/fuzzy expansion — the traversal
    #          ran, but nothing in the question actually names anything indexed
    # "none"   no seeds at all; "out_of_scope" the graph is the wrong tool
    # Retrieval that always fills MAX_RESULT_NODES makes hit_count useless as a
    # quality signal (it was 60 for all 26 queries of one logged session,
    # including "check git status"). This is the field that can say "I don't
    # know", so it is what the session log should be judged on.
    confidence: str = "exact"


@dataclass
class KGStats:
    nodes: int
    edges: int
    action: str  # "built" | "updated" | "fresh" | "seeded"


@dataclass
class SemanticBackend:
    """A resolved target for semantic extraction: which graphify backend, and
    (when it came from models.json) the provider URL and key to aim it at.

    `base_url`/`api_key` are None for a backend named via `BIRD_KG_BACKEND`,
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
        self.semantic_backend = semantic_backend or os.environ.get("BIRD_KG_BACKEND") or None
        self.semantic_model = semantic_model or os.environ.get("BIRD_KG_MODEL") or None
        self.models_json = str(models_json) if models_json else None
        self.out_dir = (
            Path(store_dir)
            if store_dir
            else self.repo_root / ".bird" / "kg" / branch_slug(self.repo_root) / "graphify-out"
        )
        self.graph_path = self.out_dir / "graph.json"
        self.manifest_path = self.out_dir / "manifest.json"
        self._building_marker = self.out_dir / ".building"
        self._graph_cache: nx.Graph | None = None
        self._graph_mtime: float | None = None
        self._artifacts_cache: tuple[float | None, list[str]] | None = None
        self._stale_cache: tuple[float, float, bool] | None = None  # (graph mtime, checked at, verdict)

    # ---------- lifecycle ----------

    def is_ready(self) -> bool:
        return self.graph_path.exists() and not self._building_marker.exists()

    def _is_stale_cached(self) -> bool:
        """is_stale() with a cache, because the query path cannot afford it raw.

        Readiness has never meant freshness: `is_ready()` asks only whether a
        graph exists, so a graph built before a hundred edits answers with the
        same confidence as one built a second ago. That is how a query returns
        `[main.ts:L217]` for a symbol that moved to L229 — a citation the model
        has no way to distrust, pointing at real code in the wrong place.

        Cached two ways. A stale verdict stands until the graph itself is
        rebuilt, because staleness only grows as files are edited. A fresh
        verdict is re-checked after STALENESS_TTL_SECONDS, so a burst of
        queries in one turn pays for the repo walk once.
        """
        try:
            mtime = self.graph_path.stat().st_mtime if self.graph_path.exists() else 0.0
        except OSError:
            return False
        now = time.monotonic()
        if self._stale_cache is not None:
            cached_mtime, checked_at, verdict = self._stale_cache
            if cached_mtime == mtime and (verdict or now - checked_at < STALENESS_TTL_SECONDS):
                return verdict
        try:
            verdict = self.is_stale()
        except Exception:  # a freshness hint must never take a query down
            return False
        self._stale_cache = (mtime, now, verdict)
        return verdict

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
            code_entries = detection["files"].get("code", [])
            extraction = self._canonicalize_extraction(
                self._merge_extractions(
                    self._extract_code(code_entries, collect_files, extract),
                    self._extract_semantic(detection["files"]),
                ),
                code_entries,
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

        # Canonicalise before merging: a narrow change set makes the extractor
        # strip a deeper prefix, which renames every node in the changed files
        # and turns this update into an append instead of a replace.
        extraction = self._canonicalize_extraction(
            self._merge_extractions(
                self._extract_code(changed_code, collect_files, extract),
                self._extract_semantic(changed),
            ),
            changed_code,
        )
        # prune_sources is deleted files plus anything the artifact filter now
        # rejects; build_merge's replace-on-re-extract reconciles changed files
        # (graphify #1344/#1178).
        # Ghost spellings of the files we just re-extracted: build_merge would
        # never touch them (they are not in the new chunk, so nothing replaces
        # them), so name them explicitly and let an old graph clean itself.
        ghosts = self._ghost_source_files(
            {str(n.get("source_file") or "") for n in extraction.get("nodes") or []} - {""}
        )
        if ghosts:
            self._graph_cache = None  # the prune invalidates whatever we just read
        G = build_merge(
            [extraction],
            graph_path=str(self.graph_path),
            prune_sources=(deleted + stale_artifacts + ghosts) or None,
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
        this at finalize so a greenfield `bird code` session can `kg_query` the
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
        cmd = [sys.executable, "-m", "bird.context.kg", str(self.repo_root), str(self.out_dir)]
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
        """models.json's `kg` alias → a graphify backend aimed at bird's provider.

        Returns None quietly when the alias is absent (nothing was asked for),
        but says so on stderr when the alias is present and unusable: a missing
        key silently degrading to an AST-only graph is the failure mode this
        whole path exists to fix, so it must never look like success.
        """
        from ..llm.registry import Registry, RegistryError

        try:
            registry = Registry.load(self.models_json)
        except (OSError, ValueError) as e:
            print(f"[bird kg] cannot read models.json ({e}) — semantic extraction off", file=sys.stderr)
            return None
        if KG_ALIAS not in registry.aliases:
            return None
        try:
            spec = registry.resolve(KG_ALIAS)
        except RegistryError as e:
            print(f"[bird kg] {e} — semantic extraction off", file=sys.stderr)
            return None
        if not spec.provider.api_key:
            print(
                f"[bird kg] '{KG_ALIAS}' alias is {spec.spec} but "
                f"{spec.provider.api_key_env} is unset — building an AST-only graph",
                file=sys.stderr,
            )
            return None
        return SemanticBackend(
            name=GRAPHIFY_BACKEND_FOR_PROVIDER.get(spec.provider.name, "openai"),
            model=self.semantic_model or spec.model,  # BIRD_KG_MODEL still wins
            api_key=spec.provider.api_key,
            base_url=spec.provider.base_url,
        )

    @staticmethod
    def _aim_backend(backend: SemanticBackend) -> bool:
        """Point graphify's backend entry at bird's provider. False = don't send.

        graphify captures each backend's base_url from the environment at
        import time, so an bird provider URL can only reach it by being written
        back into `BACKENDS` — setting OLLAMA_BASE_URL here would be read too
        late. The corpus and the API key both travel to whatever that URL
        names, so it goes through graphify's own exfiltration guard first, and
        a rejected URL disables extraction rather than falling through to
        graphify's default (localhost, or the wrong vendor entirely).
        """
        from graphify.llm import BACKENDS, provider_base_url_ok

        if backend.name not in BACKENDS:
            print(
                f"[bird kg] no graphify backend named {backend.name!r} "
                f"(known: {sorted(BACKENDS)}) — semantic extraction off",
                file=sys.stderr,
            )
            return False
        if backend.base_url:
            if not provider_base_url_ok(backend.base_url, f"bird:{backend.name}"):
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
            G = json_graph.node_link_graph(data, edges="links")
            self._graph_cache = self._canonicalize(G)
            self._graph_mtime = mtime
        return self._graph_cache

    def _repo_files(self) -> dict[str, list[str]]:
        """Real repo-relative paths, indexed by basename.

        Built once per graph load and only consulted for paths the graph got
        wrong, so the walk cost is paid once and only when it buys something.
        """
        index: dict[str, list[str]] = {}
        for path in self.repo_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(self.repo_root)
            except ValueError:
                continue
            if any(part in ARTIFACT_DIRS for part in rel.parts):
                continue
            index.setdefault(rel.name, []).append(rel.as_posix())
        return index

    def _path_resolver(self, hints: list[str] | None = None):
        """raw path as recorded -> real repo-relative path, when exactly one matches.

        `hints` are the files actually being extracted; they are consulted first
        because they are certain. Anything else — an import that reaches outside
        the batch — falls back to a repo walk. An ambiguous suffix (two
        `index.ts` in different packages) is left exactly as it was: guessing
        would answer confidently about the wrong file, which is worse than a
        duplicate.
        """
        hint_list = list(hints or [])
        index: dict[str, list[str]] | None = None
        cache: dict[str, str] = {}

        def resolve(raw: str) -> str:
            if not raw:
                return raw
            if raw in cache:
                return cache[raw]
            nonlocal index
            rel = raw.lstrip("./")
            out = raw
            if (self.repo_root / rel).is_file():
                out = rel
            else:
                hits = [c for c in hint_list if c == rel or c.endswith("/" + rel)]
                if len(hits) != 1:
                    if index is None:
                        index = self._repo_files()
                    hits = [
                        c for c in index.get(PurePosixPath(rel).name, [])
                        if c == rel or c.endswith("/" + rel)
                    ]
                if len(hits) == 1:
                    out = hits[0]
            cache[raw] = out
            return out

        return resolve

    @staticmethod
    def _id_prefix(rel: str) -> str:
        """The node-id prefix graphify derives from a file path: the path minus
        extension, slugified.

        Delegated to graphify's own `normalize_id` rather than reimplemented.
        A rewritten id has to land exactly where graphify's next merge looks
        for it — `dedup._id_prefixes` documents the `<path>_<entity>` scheme —
        so the two must never drift apart. (Verified identical to a local
        implementation across all 299 paths in this repo's graph.)
        """
        from graphify.ids import normalize_id

        stem = PurePosixPath(rel)
        if stem.suffix:
            stem = stem.with_suffix("")
        return normalize_id(str(stem))

    def _canonicalize_extraction(self, extraction: dict, inputs: list) -> dict:
        """Make extraction paths repo-relative BEFORE the graph is built.

        graphify's extractor strips the common prefix of whatever file list it
        is handed, so the same file is recorded under a different path — and so
        a different node id — depending on how wide the batch was. A full build
        sees the whole repo and records `tui/src/main.ts`; an update that
        touched only the TUI records `src/main.ts`; one that touched only
        `tui/src/` records `main.ts`. build_merge matches on id, so every
        narrower update ADDED a fresh copy of every node in the changed files
        instead of replacing them.

        That is how one function became three nodes carrying three line numbers,
        two of them stale, and how 41% of this repo's graph became duplicates
        that competed for the same result budget while pointing `read` at files
        that do not exist. Fixed here rather than at load so the graph is right
        on disk and the next update recognises what it already has.
        """
        nodes = extraction.get("nodes") or []
        if not nodes:
            return extraction
        hints: list[str] = []
        for entry in inputs or []:
            try:
                hints.append(Path(entry).resolve().relative_to(self.repo_root).as_posix())
            except (ValueError, OSError):
                continue
        resolve = self._path_resolver(hints)

        remap: dict[str, str] = {}
        for nd in nodes:
            raw = str(nd.get("source_file") or "")
            canon = resolve(raw)
            if not canon or canon == raw:
                continue
            nd["source_file"] = canon
            if str(nd.get("label") or "") == raw:  # file/module nodes are labelled by path
                nd["label"] = canon
            old_p, new_p = self._id_prefix(raw), self._id_prefix(canon)
            nid = str(nd.get("id") or "")
            if old_p and new_p and (nid == old_p or nid.startswith(old_p + "_")):
                new_id = new_p + nid[len(old_p):]
                if new_id != nid:
                    remap[nid] = new_id
                    nd["id"] = new_id

        if remap:
            for coll in ("edges", "hyperedges"):
                for e in extraction.get(coll) or []:
                    for key in ("source", "target"):
                        if e.get(key) in remap:
                            e[key] = remap[e[key]]
                    for key in ("nodes", "members"):
                        v = e.get(key)
                        if isinstance(v, list):
                            e[key] = [remap.get(x, x) for x in v]
            seen: set = set()
            deduped = []
            for nd in nodes:  # a rewrite can land two nodes on one id
                if nd.get("id") in seen:
                    continue
                seen.add(nd.get("id"))
                deduped.append(nd)
            extraction["nodes"] = deduped
        return extraction

    def _ghost_source_files(self, canonical: set[str]) -> list[str]:
        """Stale path spellings of the files being re-extracted, still in the graph.

        build_merge's replace-set is keyed on source_file, so it only drops the
        nodes filed under the path the NEW chunk carries. Canonicalising the
        chunk stops the bleeding, but a graph that already holds the same file
        under a stripped-prefix spelling keeps those nodes forever — they are
        never re-extracted, so nothing ever replaces them.

        Listing them as prune_sources lets an existing graph heal itself on the
        next update instead of needing a full rebuild. build_merge subtracts
        re-extracted files from the prune set, and these spellings are by
        definition NOT in the new chunk, so the subtraction cannot eat them.

        Reads the raw file rather than `_load_graph`, whose repair pass has
        already resolved these away — the point here is to find them on disk.
        """
        if not self.graph_path.exists() or not canonical:
            return []
        try:
            data = json.loads(self.graph_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        present = {
            str(n.get("source_file") or "") for n in data.get("nodes") or []
        }
        present.discard("")
        ghosts: set[str] = set()
        for rel in canonical:
            parts = PurePosixPath(rel).parts
            for i in range(1, len(parts)):
                alt = "/".join(parts[i:])
                # only a spelling that is IN the graph, is not this path, and
                # names no real file — a genuine `src/index.ts` elsewhere in the
                # repo is somebody's actual source, not a ghost
                if alt != rel and alt in present and not (self.repo_root / alt).is_file():
                    ghosts.add(alt)
        return sorted(ghosts)

    def _canonicalize(self, G: nx.Graph) -> nx.Graph:
        """Repair paths the extractor recorded from the wrong root, then merge
        the duplicate nodes that repair exposes.

        graphify indexes sub-projects from their own directory as well as from
        the repo root, so one file arrives under several spellings. On this repo
        `tui/src/main.ts` was also indexed as `src/main.ts` and `main.ts` —
        neither of which exists — giving three nodes per symbol with three
        different line numbers, two of them stale. 41% of all nodes were
        duplicates, and every one of them competed for the same
        MAX_RESULT_NODES budget while pointing `read` at files it would reject.

        Correctness first: a spelling is only rewritten when exactly one real
        file matches it. An ambiguous suffix (two `index.ts` in different
        packages) is left exactly as it was — a wrong merge would answer
        confidently about the wrong file, which is worse than a duplicate.
        """
        canon_file = self._path_resolver()

        # The key carries file_type and _origin, not just (file, label). An
        # AST-extracted dependency `react` and a semantically-extracted concept
        # `react` in the same package.json are two DIFFERENT assertions about
        # the repo, and collapsing them would delete the semantic layer this
        # graph pays a model to produce. Only like merges with like.
        groups: dict[tuple[str, str, str, str], list] = {}
        for nid, nd in G.nodes(data=True):
            f = canon_file(str(nd.get("source_file") or ""))
            label = str(nd.get("label", nid))
            # source_location is part of the identity, not decoration. Seven
            # distinct `.run()` methods share one file and one label in
            # arch/tools.py; four `.to_openai()` share llm/types.py. Merging on
            # (file, label) alone fuses them into one node and destroys exactly
            # what a "which one?" query needs. The scope bug this repair exists
            # for duplicates a node at the SAME line under a different path
            # spelling, so keeping the line is both safe and sufficient.
            kind = (
                str(nd.get("file_type") or ""),
                str(nd.get("_origin") or ""),
                str(nd.get("source_location") or ""),
            )
            # A file/module node carries its own path AS its label, so the
            # rewrite has to reach the label too — otherwise the phantom
            # spellings survive as three separate file nodes all pointing at
            # one real file, and a hit on one of them sends `read` somewhere
            # that does not exist.
            if f and label != f and PurePosixPath(label).suffix and canon_file(label) == f:
                label = f
            if not f:
                # No path means no path-spelling duplicate — this is an
                # external or builtin reference (`Any`, `Path`, `Exception`)
                # that graphify records once per importing file. Merging those
                # is not a repair, it is a different graph: it would fuse 31
                # per-file references into one degree-31 hub and change what
                # every query about them returns. Out of scope; leave alone.
                groups.setdefault(("", label, *kind, nid), []).append(nid)
                continue
            groups.setdefault((f, label, *kind), []).append(nid)

        mapping: dict = {}
        survivors: dict = {}
        changed = False
        for (f, label, *_rest), members in groups.items():
            if len(members) > 1:
                # Keep the node whose ORIGINAL path was already the real one:
                # its source_location came from the build that saw the true
                # file, so its line numbers are the ones worth keeping.
                members = sorted(
                    members,
                    key=lambda n: (
                        str(G.nodes[n].get("source_file") or "") != f,
                        str(G.nodes[n].get("label", "")) != label,
                        str(n),
                    ),
                )
                changed = True
            keep = members[0]
            attrs = dict(G.nodes[keep])
            if f and attrs.get("source_file") != f:
                attrs["source_file"] = f
                changed = True
            if attrs.get("label") != label:
                attrs["label"] = label
                attrs["norm_label"] = label
                changed = True
            survivors[keep] = attrs
            for n in members:
                mapping[n] = keep

        if not changed:
            return G
        H = nx.relabel_nodes(G, mapping, copy=True)
        for nid, attrs in survivors.items():
            if nid in H:
                H.nodes[nid].update(attrs)
        # merging two nodes turns an edge between them into a self-loop, which
        # is not a relationship anybody asked about
        H.remove_edges_from(list(nx.selfloop_edges(H)))
        return H

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
        redirect = _out_of_scope(question)
        if redirect:
            return KGQueryResult(
                text=redirect, hit_count=0, expanded_tokens=[],
                mode="out_of_scope", confidence="out_of_scope",
            )
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
                confidence="none",
            )

        # Did the question actually NAME anything in the graph? Expansion also
        # matches by substring and fuzzy edit distance, which is what keeps
        # recall usable — and also what lets an unrelated question find a seed
        # and come back with a full cap of nodes. The distinction has to reach
        # the caller, because from the outside the two look identical.
        exact = self._exact_matches(question, vocab)
        confidence = "exact" if exact else "weak"

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
                confidence="none",
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
                f"[traversal filled its {MAX_RESULT_NODES}-node cap, so this is the "
                "neighbourhood of the seeds above, not a ranked answer — narrow the "
                "question if what you need is not here]"
            )

        stale = self._is_stale_cached()

        if confidence == "weak":
            nearest = self._nearest_vocab(question, vocab)
            lines.insert(0, (
                "[LOW CONFIDENCE — no term in your question names anything in the "
                "graph; these seeds came from partial/fuzzy matches, so the nodes "
                "below may be unrelated. Closest indexed terms: "
                + ", ".join(nearest) + ". Either re-ask using those terms, or — if "
                "you are looking for literal text, a filename, or something outside "
                "this repo's own source — use grep/glob instead. Do not re-ask this "
                "same question reworded a third time.]"
            ))

        if stale:
            # first line, so it survives the budget clip below and qualifies
            # everything under it rather than trailing off the end
            lines.insert(0, STALE_NOTICE)

        text = "\n".join(lines)
        char_budget = budget * 4
        if len(text) > char_budget:
            text = text[:char_budget] + f"\n... [truncated at ~{budget} tokens; pass a larger budget]"
        return KGQueryResult(
            text=text, hit_count=len(sub_nodes), expanded_tokens=expanded,
            mode=mode, confidence=confidence, stale=stale,
        )

    def digest(self, max_files: int = 15, max_symbols: int = 5) -> str:
        """Compact orientation block for a system prompt — the pre-computed
        answer to the "what is this codebase?" question every session starts
        with.

        Source files first, ranked by how many symbols they define; test files
        are summarised on one line. The old map ranked every file the same
        way, so on this repo six of its top nine entries were test files — it
        told the model where the tests were, not what the program was. Each
        file leads with its own first docstring or comment line (the author's
        one-sentence answer), and the symbols shown are the file's most-USED
        ones by call/import degree, not the alphabetically first six. Only
        `code` nodes count as symbols: rationale, concept and document nodes
        are what turned the old map into a list of docstring fragments."""
        G = self._load_graph()
        # Cross-file use-degree per symbol. The graph is undirected, so "who
        # imports whom" is gone by the time it loads; what survives is how many
        # OTHER files' symbols a symbol touches. Intra-file edges are skipped —
        # a call within a module says nothing about the module's place in the
        # repo.
        file_of = {nid: _norm_path(_node_file(nd)) for nid, nd in G.nodes(data=True)}
        use_degree: dict = {}
        touches: dict[str, set[str]] = {}  # file -> other files its symbols connect to
        for u, v, d in G.edges(data=True):
            if str(d.get("relation") or "") in _STRUCTURAL_RELATIONS:
                continue
            fu, fv = file_of.get(u) or "", file_of.get(v) or ""
            if fu and fu == fv:
                continue
            use_degree[u] = use_degree.get(u, 0) + 1
            use_degree[v] = use_degree.get(v, 0) + 1
            if fu and fv:
                touches.setdefault(fu, set()).add(fv)
                touches.setdefault(fv, set()).add(fu)

        by_file: dict[str, list[tuple[int, str, str]]] = {}
        for nid, nd in G.nodes(data=True):
            if str(nd.get("file_type") or "code") != "code":
                continue
            f = _norm_path(_node_file(nd))
            if not f or not _is_source_path(f):
                continue
            label = _short_label(nd, nid, 40)
            if _norm_path(label) == f:
                continue  # the file's own node, not a symbol in it
            by_file.setdefault(f, []).append((use_degree.get(nid, 0), label, nid))

        source = {f: s for f, s in by_file.items() if not _is_test_path(f)}
        tests = [f for f in by_file if _is_test_path(f)]
        lines = ["[repo map — from the knowledge graph]"]
        # A file's rank is how many OTHER files its symbols connect to. That is
        # direction-free (the graph is undirected), size-free (a 178-symbol
        # TUI component file touching four files sits below a 20-line module
        # touching forty), and it is what surfaces the entry points and the
        # shared abstractions — cli, serve, the tool base — which is what "what
        # is this codebase" means. Ties break on squared symbol degree, which
        # tells "one class three files import" (1 × 3² = 9) apart from "three
        # functions that each import it" (3 × 1² = 3); then on size.
        ranked = sorted(
            source.items(),
            key=lambda kv: (
                -len(touches.get(kv[0], ())),
                -sum(d * d for d, _, _ in kv[1]),
                -len(kv[1]),
                kv[0],
            ),
        )[:max_files]
        for f, syms in ranked:
            # top-level names (classes, functions) before `.method()` entries,
            # each group by use-degree — the shape of a module is its exports
            top = [lbl for _, lbl, _ in sorted(syms, key=lambda x: (x[1].startswith('.'), -x[0], x[1]))]
            shown = ", ".join(top[:max_symbols])
            more = f", +{len(top) - max_symbols} more" if len(top) > max_symbols else ""
            purpose = _file_purpose(self.repo_root / f)
            head = f"  {f}" + (f" — {purpose}" if purpose else "")
            lines.append(f"{head} ({shown}{more})")
        if len(source) > len(ranked):
            lines.append(f"  … and {len(source) - len(ranked)} more source files")
        if tests:
            names = sorted(tests, key=lambda f: (-len(by_file[f]), f))
            shown = ", ".join(n.rsplit("/", 1)[-1] for n in names[:6])
            tail = ", …" if len(tests) > 6 else ""
            plural = "s" if len(tests) != 1 else ""
            lines.append(f"  tests: {len(tests)} file{plural} ({shown}{tail})")

        # hubs: the most-used symbols repo-wide, each tagged with its file so
        # the name is something `read` can act on
        hubs: list[str] = []
        for nid, _ in sorted(use_degree.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            nd = G.nodes[nid]
            if str(nd.get("file_type") or "code") != "code":
                continue
            f = _norm_path(_node_file(nd))
            lbl = _short_label(nd, nid, 40)
            if not f or _norm_path(lbl) == f or not _is_source_path(f) or _is_test_path(f):
                continue
            tag = f"{lbl} [{f.rsplit('/', 1)[-1]}]"
            if tag not in hubs:
                hubs.append(tag)
            if len(hubs) >= 8:
                break
        if hubs:
            lines.append("  key hubs: " + ", ".join(hubs))
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
    def _exact_matches(question: str, vocab: set[str]) -> list[str]:
        """Query terms that are verbatim graph vocabulary (modulo plurals).

        The strong tier of `_expand`, isolated: `_expand` folds exact,
        substring and fuzzy hits into one ranked list, and by the time it
        returns, a question that named a real symbol is indistinguishable from
        one that merely shares four letters with one.
        """
        out: list[str] = []
        for q in tokenize(question):
            if q in STOPWORDS:
                continue
            for form in (q, singularize(q)):
                if form in vocab:
                    out.append(form)
                    break
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
    # `python -m bird.context.kg` run by hand. Never overrides what's already set.
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
