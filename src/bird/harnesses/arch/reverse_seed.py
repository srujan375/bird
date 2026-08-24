"""reverse_seed — deterministic KG subgraph → ArchState transform.

The reverse of `kg_seed`: instead of writing a design into the knowledge graph,
this reads the *as-built* code graph back out as boxes and edges so the board
can show the current architecture and the architect can propose changes against
what is actually there rather than against a guess.

It is a **pure transform** — no I/O, no model, no graph library. `scope_subgraph`
is the one function that touches the KG (it takes the `KG` object), and even
that only reads; everything downstream (`reverse_seed`) operates on a plain
`Subgraph` of node dicts + edge dicts and returns a `SeedResult`. That split is
what makes the transform unit-testable in isolation: hand it a fabricated
subgraph, assert the boxes/edges/inferences it produces.

The pipeline is hybrid by design (decision: "deterministic extraction + model
refinement"): the heuristics here do what is safe and confident — group symbols
by file, infer a `kind` from path/name signals, map call/import edges to
box-level edges, collapse mutual ones. Everything uncertain becomes a *gap* in
the inference log (low confidence), so the architect can correct it with
`canvas` rather than the harness silently producing a wrong diagram.

Imported boxes land at `depth="stub"` with `existing=True`: they are background,
not a design anyone did here, and the coverage checks skip them for that reason.

Reuses `kg.py`'s own vocabulary machinery (`tokenize`, `singularize`, `_expand`,
`_traverse`, `_node_file`, `_norm_path`, `_same_file`) so scoping matches exactly
what `kg_query` would match — no second vocabulary to drift.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ...context import kg as kgmod
from .state import Edge, Node

# ---------------------------------------------------------------- dataclasses


@dataclass
class Subgraph:
    """A slice of the KG: the nodes and edges `reverse_seed` transforms.

    Plain dicts (not graphify/node-link objects) so the transform has zero
    dependency on networkx — `scope_subgraph` produces them, tests fabricate
    them directly.
    """
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Inference:
    """One heuristic decision, for the transparency report.

    `confidence` is 'high' | 'medium' | 'low'. Low-confidence inferences are
    the gaps the architect should review first — the harness guessed, and it
    says so.
    """
    node_id: str
    field: str
    value: str
    confidence: str
    evidence: str


@dataclass
class SeedResult:
    """What `reverse_seed` returns: what to put on the board + what to report."""
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    inference_log: list[Inference] = field(default_factory=list)


# ----------------------------------------------------------------- scoping

# Default caps (decision: max_nodes=200, max_depth=3 matching kg.py's BFS_DEPTH).
DEFAULT_MAX_NODES = 200
DEFAULT_MAX_DEPTH = 3

# Edge relations that represent a real cross-box dependency (a call or an
# import). `contains`/`rationale_for` are intra-file structure — they never
# become edges between boxes.
DEPENDENCY_RELATIONS = frozenset({"calls", "imports", "imports_from", "references", "uses"})

# Node types/labels that are structural noise, not architecture. File nodes
# (label == basename, type code) carry no semantic kind; rationale/docstring
# nodes are prose. Symbols are what we group.
_FILE_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.(py|js|ts|go|rs|java|rb|md)$")


def scope_subgraph(
    kg: Any,
    scope_query: str,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Subgraph:
    """Tokenize/expand/match `scope_query` against the KG's own vocabulary, then
    BFS-traverse to extract the relevant slice. Reuses kg.py's `_expand` and
    `_traverse` so the match is identical to what `kg_query` would land on.

    Returns an empty Subgraph (no nodes) when the scope matches nothing — the
    caller turns that into a 'scope matched nothing' message, never a crash.
    """
    if not kg.is_ready():
        # The caller (import_state) checks is_ready first and gives a clear
        # message; this is a defensive guard so a pure read never raises opaquely.
        return Subgraph()
    G = kg._load_graph()
    if G.number_of_nodes() == 0:
        return Subgraph()

    # vocabulary + document frequency, exactly as kg.query builds it — same
    # call, so label/path indexing and IDF weighting can never drift apart
    df, node_tokens, path_tokens = kgmod.KG._vocabulary(G)
    n_nodes = max(G.number_of_nodes(), 1)
    vocab = set(df)

    expanded = kgmod.KG._expand(scope_query, vocab, df, n_nodes)
    if not expanded:
        return Subgraph()

    relevance = kgmod.KG._scorer(expanded, df, node_tokens, path_tokens, n_nodes)
    scored = [(s, str(nid)) for nid in node_tokens if (s := relevance(nid)) > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    starts = [nid for _, nid in scored[:kgmod.SEED_NODES]]
    if not starts:
        return Subgraph()

    # the node cap is this function's own (max_nodes), not kg_query's
    sub_nodes, sub_edges = kgmod.KG._traverse(G, starts, "bfs", relevance, max_nodes)

    # _traverse caps depth at BFS_DEPTH (3); honor a tighter max_depth too.
    if max_depth < kgmod.BFS_DEPTH:
        sub_nodes, sub_edges = _retraverse(G, starts, max_depth)

    # materialize node dicts + edge dicts, then enforce the node cap.
    #
    # Edges: take *every* edge whose both ends are in the subgraph, not just the
    # traversal edges. `_traverse` only records an edge when it discovers a
    # *new* node, so when several start nodes are already mutual neighbours
    # (common on a small, tightly-matched scope) the edges between them are
    # silently dropped — and those are exactly the cross-component calls the
    # reverse-seed cares about. Walking G.edges over the subgraph set recovers
    # them; the traversal edges are a subset, so nothing is invented.
    nodes = [{**G.nodes[n], "id": n} for n in sub_nodes]
    sub_set = set(sub_nodes)
    edges = []
    for u, v, ed in G.edges(data=True):
        if u in sub_set and v in sub_set:
            data = ed
            if hasattr(G, "is_multigraph") and G.is_multigraph():
                data = next(iter(ed.values()), {})
            edges.append({"source": u, "target": v, **data})

    # the ranked traversal stops at max_nodes itself, so reaching the cap is
    # the truncation signal; _retraverse (tight max_depth) is still uncapped
    # and can overshoot, which the block below trims.
    truncated = len(sub_nodes) >= max_nodes
    if len(nodes) > max_nodes:
        # keep the highest-scoring nodes (the starts and their nearest
        # neighbours). `scored` holds (score, id) — unpacking it the other way
        # round filled `keep` with floats, so nothing ever matched and the cap
        # fell through to plain traversal order.
        keep = {nid for _, nid in scored[:max_nodes]} | set(starts)
        if len(keep) < max_nodes:
            # fill from BFS order (insertion order of sub_nodes) until the cap
            for n in sub_nodes:
                if len(keep) >= max_nodes:
                    break
                keep.add(n)
        nodes = [{**G.nodes[n], "id": n} for n in sub_nodes if n in keep]
        keep_set = {nd["id"] for nd in nodes}
        edges = [e for e in edges if e["source"] in keep_set and e["target"] in keep_set]
        truncated = True

    sg = Subgraph(nodes=nodes, edges=edges)
    if truncated:
        # stamp the subgraph so reverse_seed can record the truncation inference;
        # carried on the Subgraph rather than a side-channel to keep it pure.
        sg._truncated = (max_depth, len(nodes))  # type: ignore[attr-defined]
    return sg


def _retraverse(G: Any, starts: list[str], max_depth: int) -> tuple[set[str], list[tuple[str, str]]]:
    """A shallower BFS than kg._traverse's fixed BFS_DEPTH, for tight max_depth."""
    sub_nodes: set[str] = set(starts)
    sub_edges: list[tuple[str, str]] = []
    frontier = set(starts)
    for _ in range(max_depth):
        nxt: set[str] = set()
        for n in frontier:
            for nb in G.neighbors(n):
                if nb not in sub_nodes:
                    nxt.add(nb)
                    sub_edges.append((n, nb))
        sub_nodes.update(nxt)
        frontier = nxt
    return sub_nodes, sub_edges


# ----------------------------------------------------------- kind inference

# path/name signals → component kind, ordered most-specific first. A hit is
# high confidence only when the signal is unambiguous (one kind matches);
# multiple/weak matches drop to medium/low.
_KIND_SIGNALS: list[tuple[str, tuple[str, ...]]] = [
    # (kind, path/name fragments)
    # gateway/cache/job folded into api/store/service when the kind vocabulary
    # shrank to the eight the board draws — the signals are still worth having,
    # they just resolve to the surviving neighbour.
    ("api", ("api", "endpoint", "route", "handler", "controller", "resource",
             "view", "gateway", "proxy", "ingress")),
    ("store", ("store", "repo", "repository", "db", "database", "model", "schema",
               "migration", "orm", "dao", "cache", "redis")),
    ("queue", ("queue", "bus", "stream", "kafka", "pubsub", "topic", "broker", "worker")),
    ("service", ("job", "cron", "scheduler", "batch", "runner", "service", "worker_pool")),
    ("ui", ("ui", "view", "component", "page", "template", "render", "frontend", "client")),
    ("llm", ("llm", "prompt", "embedding", "rag", "agent", "inference")),
    ("infra", ("infra", "deploy", "terraform", "k8s", "docker", "compose", "config")),
    ("external", ("external", "thirdparty", "vendor", "stripe", "s3", "sns")),
]


def _infer_kind(file_path: str, symbols: list[str]) -> tuple[str, str, str]:
    """(kind, confidence, evidence) from path + symbol-name signals.

    High = exactly one kind's signals matched and at least one is specific.
    Medium = one kind matched but only via generic terms, or path strongly
             implies a kind but symbols don't corroborate.
    Low  = nothing matched → default to 'service' and flag for review.
    """
    haystack = " ".join([file_path] + symbols).lower()
    hits: list[tuple[str, list[str]]] = []
    for kind, frags in _KIND_SIGNALS:
        matched = [f for f in frags if f in haystack]
        if matched:
            hits.append((kind, matched))
    if not hits:
        return ("service", "low", "no path/name signal; defaulted to service")
    if len(hits) == 1:
        kind, matched = hits[0]
        # 'model' alone is ambiguous (could be a domain model, not a store) —
        # only high if a store-specific term corroborates.
        store_specific = {"store", "repo", "repository", "db", "database", "schema",
                          "migration", "orm", "dao", "cache", "redis"}
        if kind == "store" and not (store_specific & set(matched)):
            return (kind, "medium", f"path/name hints: {', '.join(matched)}")
        return (kind, "high", f"path/name signals: {', '.join(matched)}")
    # multiple kinds matched — pick the one with the most specific signals,
    # medium confidence (the model should confirm).
    hits.sort(key=lambda kv: -len(kv[1]))
    kind, matched = hits[0]
    return (kind, "medium", f"ambiguous signals ({len(hits)} kinds); picked {kind} from {', '.join(matched)}")


def _symbol_name(label: str) -> str:
    """'format_activity()' / '.close()' → 'format_activity'."""
    s = str(label).splitlines()[0].strip()
    s = s.removesuffix("()").lstrip(".")
    return s


# ----------------------------------------------------------- the transform

def reverse_seed(subgraph: Subgraph, scope: str) -> SeedResult:
    """Pure transform: scoped KG nodes+edges → board boxes, edges and an
    inference log. No I/O, no model.

    Grouping: one box per source file (the unit a human reads as a "module").
    Symbols with no file (bare resolved names like `Path`, `Popen`) are dropped
    — they're cross-file glue, not boxes. Edges between symbols in different
    files become edges between those files' boxes; intra-file edges
    (`contains`, `rationale_for`) never do.
    """
    inferences: list[Inference] = []

    # ---- truncation notice (stamped by scope_subgraph) ----
    truncated = getattr(subgraph, "_truncated", None)
    if truncated:
        depth, n = truncated
        inferences.append(Inference(
            node_id="*", field="scope", value="truncated",
            confidence="medium",
            evidence=f"subgraph truncated at depth {depth} ({n} nodes) — re-scope more tightly if this missed something",
        ))

    # ---- group symbols by file → components ----
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for nd in subgraph.nodes:
        f = kgmod._norm_path(kgmod._node_file(nd))
        if not f:
            continue  # bare resolved name, no file — not a component
        # skip file-baseline nodes (label is just the basename) — they carry no
        # kind signal beyond the path, and grouping their *symbols* is enough.
        by_file[f].append(nd)

    boxes: list[Node] = []
    file_to_cid: dict[str, str] = {}
    # kg node id → box id, so edges (which carry kg node ids) can be lifted to
    # the box level. A node with no file maps to None and is dropped.
    node_to_cid: dict[str, str | None] = {}
    for f, syms in sorted(by_file.items()):
        cid = _file_to_cid(f)
        file_to_cid[f] = cid
        for s in syms:
            node_to_cid[str(s.get("id", ""))] = cid
        labels = [str(s.get("label", s.get("id", ""))) for s in syms]
        # the file's own label (basename) is noise for kind inference; use symbols
        sym_names = [_symbol_name(l) for l in labels if not _FILE_LABEL_RE.match(l)]
        kind, conf, evidence = _infer_kind(f, sym_names)
        boxes.append(Node(
            id=cid,
            label=_file_to_name(f),
            kind=kind,
            responsibility="",  # unknown — a gap, not a guess
            depth="stub",
            existing=True,
            notes=f"imported from the repo (scope: {scope}); {f}",
        ))
        inferences.append(Inference(
            node_id=cid, field="kind", value=kind, confidence=conf, evidence=evidence,
        ))
        # responsibility is always a gap for an imported box — the graph doesn't
        # carry "what it does" in one sentence.
        inferences.append(Inference(
            node_id=cid, field="responsibility", value="(none)",
            confidence="low",
            evidence=f"no responsibility inferred from code symbols in {f}; set it with `canvas`",
        ))

    # ---- map kg edges → board edges (collapse mutual, dedupe by pair) ----
    board_edges, edge_inf = _map_edges(subgraph.edges, node_to_cid)
    inferences.extend(edge_inf)

    return SeedResult(nodes=boxes, edges=board_edges, inference_log=inferences)


def _file_to_cid(file_path: str) -> str:
    """A stable kebab-case box id from a file path.

    `src/bird/context/kg.py` → `context-kg`. Drops common top-level dirs
    (src/, lib/, app/, tests/) so the id is the meaningful tail.
    """
    p = kgmod._norm_path(file_path)
    parts = [pt for pt in p.split("/") if pt and pt not in ("src", "lib", "app", "internal")]
    # drop the extension on the last segment
    if parts:
        parts[-1] = re.sub(r"\.[^.]+$", "", parts[-1])
    tail = parts[-2:] if len(parts) >= 2 else parts  # last two segments are usually enough
    raw = "-".join(tail) if tail else re.sub(r"[^a-z0-9]+", "-", p).strip("-")
    cid = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not cid:
        cid = "comp"
    # ensure it starts with a letter (state.validate_node requires it)
    if not cid[0].isalpha():
        cid = "c-" + cid
    return cid


def _file_to_name(file_path: str) -> str:
    """A human name from a file path: the basename without extension."""
    p = kgmod._norm_path(file_path)
    base = p.rsplit("/", 1)[-1]
    return re.sub(r"\.[^.]+$", "", base) or p


def _map_edges(
    edges: list[dict[str, Any]],
    node_to_cid: dict[str, str | None],
) -> tuple[list[Edge], list[Inference]]:
    """Collapse KG call/import edges to box-level edges.

    - drop intra-file edges (same box on both ends)
    - dedupe by (src, dst) pair, counting the underlying edges
    - detect mutual edges (A→B and B→A) and collapse to one 'mutual dependency'
    """
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_relations: dict[tuple[str, str], set[str]] = defaultdict(set)
    for e in edges:
        rel = str(e.get("relation", ""))
        if rel not in DEPENDENCY_RELATIONS:
            continue
        src_c = node_to_cid.get(str(e.get("source", "")))
        dst_c = node_to_cid.get(str(e.get("target", "")))
        if not src_c or not dst_c or src_c == dst_c:
            continue  # bare name (no file) or intra-file — not a cross-component edge
        pair_counts[(src_c, dst_c)] += 1
        pair_relations[(src_c, dst_c)].add(rel)

    # detect mutual pairs
    mutual: set[frozenset[str]] = set()
    for (a, b) in list(pair_counts):
        if a == b:
            continue
        if (b, a) in pair_counts and frozenset((a, b)) not in mutual:
            mutual.add(frozenset((a, b)))

    board_edges: list[Edge] = []
    inferences: list[Inference] = []
    seen_pairs: set[frozenset[str]] = set()
    for (src, dst), count in sorted(pair_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        key = frozenset((src, dst))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        if key in mutual:
            # collapse bidirectional into one edge, oriented src→dst
            board_edges.append(Edge(src=src, dst=dst, label="mutual dependency", kind="sync"))
            inferences.append(Inference(
                node_id=src, field="edge", value=f"{src}↔{dst}",
                confidence="medium",
                evidence=f"bidirectional call/import between {src} and {dst} collapsed to one mutual dependency",
            ))
        else:
            rels = sorted(pair_relations[(src, dst)])
            label = "calls" if "calls" in rels else rels[0] if rels else "depends on"
            board_edges.append(Edge(src=src, dst=dst, label=label, kind="sync"))
            inferences.append(Inference(
                node_id=src, field="edge", value=f"{src}→{dst}",
                confidence="medium",
                evidence=f"{count} {label} edge(s) between {src} and {dst}",
            ))
    return board_edges, inferences