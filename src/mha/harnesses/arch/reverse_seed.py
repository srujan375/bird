"""reverse_seed — deterministic KG subgraph → ArchState transform.

The reverse of `kg_seed`: instead of writing a design into the knowledge graph,
this reads the *as-built* code graph back out as an ArchState so the render
layer can display the current architecture and the model can propose
modifications against the loaded state.

It is a **pure transform** — no I/O, no model, no graph library. `scope_subgraph`
is the one function that touches the KG (it takes the `KG` object), and even
that only reads; everything downstream (`reverse_seed`) operates on a plain
`Subgraph` of node dicts + edge dicts and returns a `SeedResult`. That split is
what makes the transform unit-testable in isolation: hand it a fabricated
subgraph, assert the components/connections/inferences it produces.

The pipeline is hybrid by design (decision: "deterministic extraction + model
refinement"): the heuristics here do what is safe and confident — group symbols
by file, infer a `kind` from path/name signals, map call/import edges to
component-level connections, collapse mutual edges, extract a thin facet when
the kind is obvious. Everything uncertain becomes a *gap* in the inference log
(low confidence), so the model can correct it with the existing tools rather than
the harness silently producing a wrong diagram.

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
from .state import (
    KINDS,
    ApiFacet,
    Component,
    Connection,
    Endpoint,
    Entity,
    InfraFacet,
    LlmFacet,
    QueueFacet,
    ServiceFacet,
    StoreFacet,
)

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
    the gaps the model should review first — the harness guessed, and it says so.
    """
    component_id: str
    field: str
    value: str
    confidence: str
    evidence: str


@dataclass
class SeedResult:
    """What `reverse_seed` returns: what to write into ArchState + what to report."""
    components: list[Component] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    inference_log: list[Inference] = field(default_factory=list)


# ----------------------------------------------------------------- scoping

# Default caps (decision: max_nodes=200, max_depth=3 matching kg.py's BFS_DEPTH).
DEFAULT_MAX_NODES = 200
DEFAULT_MAX_DEPTH = 3

# Edge relations that represent a real cross-component dependency (a call or
# an import). `contains`/`rationale_for` are intra-file structure — they never
# become connections between components.
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

    # vocabulary + document frequency, exactly as kg.query builds it
    df: Counter[str] = Counter()
    node_tokens: dict[str, set[str]] = {}
    for nid, nd in G.nodes(data=True):
        toks = set(kgmod.tokenize(str(nd.get("label", nid))))
        node_tokens[nid] = toks
        df.update(toks)
    n_nodes = max(G.number_of_nodes(), 1)
    vocab = set(df)

    expanded = kgmod.KG._expand(scope_query, vocab, df, n_nodes)
    if not expanded:
        return Subgraph()

    def idf(t: str) -> float:
        import math
        return math.log(n_nodes / (1 + df.get(t, 0))) + 1.0

    scored = []
    for nid, toks in node_tokens.items():
        s = sum(idf(t) for t in expanded if t in toks)
        if s > 0:
            scored.append((s, str(nid)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    starts = [nid for _, nid in scored[:3]]
    if not starts:
        return Subgraph()

    sub_nodes, sub_edges = kgmod.KG._traverse(G, starts, "bfs")

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

    truncated = False
    if len(nodes) > max_nodes:
        # keep the highest-scoring nodes (the starts and their nearest neighbours)
        keep = {n for n, _ in scored[:max_nodes]} | set(starts)
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
    ("api", ("api", "endpoint", "route", "handler", "controller", "resource", "view")),
    ("gateway", ("gateway", "proxy", "ingress", "edge")),
    ("store", ("store", "repo", "repository", "db", "database", "model", "schema", "migration", "orm", "dao")),
    ("cache", ("cache", "redis", "memo")),
    ("queue", ("queue", "bus", "stream", "kafka", "pubsub", "topic", "broker", "worker")),
    ("job", ("job", "cron", "scheduler", "task", "batch", "runner")),
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
        store_specific = {"store", "repo", "repository", "db", "database", "schema", "migration", "orm", "dao"}
        if kind == "store" and not (store_specific & set(matched)):
            return (kind, "medium", f"path/name hints: {', '.join(matched)}")
        return (kind, "high", f"path/name signals: {', '.join(matched)}")
    # multiple kinds matched — pick the one with the most specific signals,
    # medium confidence (the model should confirm).
    hits.sort(key=lambda kv: -len(kv[1]))
    kind, matched = hits[0]
    return (kind, "medium", f"ambiguous signals ({len(hits)} kinds); picked {kind} from {', '.join(matched)}")


# ----------------------------------------------------------- facet extraction

def _extract_facet(kind: str, symbols: list[str], file_path: str) -> tuple[Any, list[Inference]]:
    """A thin facet when the kind is obvious; None (a gap) otherwise.

    Only `store` and `api` get a facet here — they're the kinds whose shape is
    inferable from symbol names (an Entity class, a route handler). Service/
    queue/llm/infra facets need semantic detail the graph doesn't carry, so
    they stay black boxes and the inference log says why.
    """
    cid_evidence = file_path
    inferences: list[Inference] = []
    if kind == "store":
        # entity-like symbols (CamelCase, not all-caps) become Entity stubs.
        entities = []
        for sym in symbols:
            name = _symbol_name(sym)
            if not name or not name[0].isupper():
                continue
            if name.isupper():  # a constant, not an entity
                continue
            entities.append(Entity(name=name, keys="unknown"))
        if entities:
            inferences.append(Inference(
                component_id="", field="facet.entities", value=f"{len(entities)} entities",
                confidence="low", evidence=f"inferred CamelCase classes in {cid_evidence}",
            ))
            return StoreFacet(entities=entities), inferences
        return None, [Inference(
            component_id="", field="facet", value="none",
            confidence="low", evidence=f"store with no inferable entities in {cid_evidence}",
        )]
    if kind == "api":
        # route-like symbols (get_/post_/put_/delete_ or names containing a path)
        endpoints = []
        # match the HTTP verb as a word, optionally followed by `_` (the common
        # handler-naming convention, e.g. `get_orders`); a trailing `\b` alone
        # would miss `get_orders` since `_` is a word character.
        route_re = re.compile(r"\b(get|post|put|patch|delete|head|options)(?=_|\b|$)", re.I)
        for sym in symbols:
            name = _symbol_name(sym)
            if not name:
                continue
            m = route_re.search(name)
            if m:
                endpoints.append(Endpoint(
                    route="?", method=m.group(1).upper(),
                    request="?", response="?", auth="?",
                ))
        if endpoints:
            inferences.append(Inference(
                component_id="", field="facet.endpoints", value=f"{len(endpoints)} endpoints",
                confidence="low", evidence=f"inferred handler names in {cid_evidence}",
            ))
            return ApiFacet(endpoints=endpoints), inferences
        return None, [Inference(
            component_id="", field="facet", value="none",
            confidence="low", evidence=f"api with no inferable endpoints in {cid_evidence}",
        )]
    return None, []


def _symbol_name(label: str) -> str:
    """'format_activity()' / '.close()' → 'format_activity'."""
    s = str(label).splitlines()[0].strip()
    s = s.removesuffix("()").lstrip(".")
    return s


# ----------------------------------------------------------- the transform

def reverse_seed(subgraph: Subgraph, scope: str) -> SeedResult:
    """Pure transform: scoped KG nodes+edges → ArchState components, connections,
    facets + an inference log. No I/O, no model.

    Grouping: one component per source file (the unit a human reads as a
    "module"). Symbols with no file (bare resolved names like `Path`, `Popen`)
    are dropped — they're cross-file glue, not components. Edges between symbols
    in different files become connections between those files' components;
    intra-file edges (`contains`, `rationale_for`) never do.
    """
    inferences: list[Inference] = []

    # ---- truncation notice (stamped by scope_subgraph) ----
    truncated = getattr(subgraph, "_truncated", None)
    if truncated:
        depth, n = truncated
        inferences.append(Inference(
            component_id="*", field="scope", value="truncated",
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

    components: list[Component] = []
    file_to_cid: dict[str, str] = {}
    # node id → component id, so edges (which carry node ids) can be lifted to
    # the component level. A node with no file maps to None and is dropped.
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
        name = _file_to_name(f)
        facet, facet_inf = _extract_facet(kind, sym_names, f)
        comp = Component(
            id=cid,
            name=name,
            kind=kind,
            responsibility="",  # unknown — a gap, not a guess
            trace=[],
            existing=True,
            tech=None,
            facet=facet,
            origin=f"imported:{scope}",
        )
        components.append(comp)
        inferences.append(Inference(
            component_id=cid, field="kind", value=kind, confidence=conf, evidence=evidence,
        ))
        for fi in facet_inf:
            fi.component_id = cid
            inferences.append(fi)
        # responsibility is always a gap for imported components — the graph
        # doesn't carry "what it does" in one sentence.
        inferences.append(Inference(
            component_id=cid, field="responsibility", value="(none)",
            confidence="low",
            evidence=f"no responsibility inferred from code symbols in {f}; set it with `component`",
        ))

    # ---- map edges → connections (collapse mutual, dedupe by pair) ----
    connections, conn_inf = _map_connections(subgraph.edges, node_to_cid)
    inferences.extend(conn_inf)

    return SeedResult(
        components=components,
        connections=connections,
        inference_log=inferences,
    )


def _file_to_cid(file_path: str) -> str:
    """A stable kebab-case component id from a file path.

    `src/mha/context/kg.py` → `context-kg`. Drops common top-level dirs
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
    # ensure it starts with a letter (state.validate_component requires it)
    if not cid[0].isalpha():
        cid = "c-" + cid
    return cid


def _file_to_name(file_path: str) -> str:
    """A human name from a file path: the basename without extension."""
    p = kgmod._norm_path(file_path)
    base = p.rsplit("/", 1)[-1]
    return re.sub(r"\.[^.]+$", "", base) or p


def _map_connections(
    edges: list[dict[str, Any]],
    node_to_cid: dict[str, str | None],
) -> tuple[list[Connection], list[Inference]]:
    """Collapse KG call/import edges to component-level connections.

    - drop intra-file edges (same component on both ends)
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

    connections: list[Connection] = []
    inferences: list[Inference] = []
    seen_pairs: set[frozenset[str]] = set()
    for (src, dst), count in sorted(pair_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        key = frozenset((src, dst))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        if key in mutual:
            # collapse bidirectional into one connection, oriented src→dst
            connections.append(Connection(
                src=src, dst=dst, label="mutual dependency", kind="sync",
            ))
            inferences.append(Inference(
                component_id=src, field="connection", value=f"{src}↔{dst}",
                confidence="medium",
                evidence=f"bidirectional call/import between {src} and {dst} collapsed to one mutual dependency",
            ))
        else:
            rels = sorted(pair_relations[(src, dst)])
            label = "calls" if "calls" in rels else rels[0] if rels else "depends on"
            connections.append(Connection(
                src=src, dst=dst, label=label, kind="sync",
            ))
            inferences.append(Inference(
                component_id=src, field="connection", value=f"{src}→{dst}",
                confidence="medium",
                evidence=f"{count} {label} edge(s) between {src} and {dst}",
            ))
    return connections, inferences