"""Tests for the reverse-seed capability: code KG → ArchState.

Two layers, mirroring the implementation's split:

- `reverse_seed` is a **pure transform** on a fabricated `Subgraph` (node dicts
  + edge dicts). These tests hand it synthetic graphs and assert the
  components/connections/facets/inferences it produces — no KG, no I/O.
- `ImportStateTool` is the integration: it scopes a real `KG` (built here via
  `kg.seed`, the same path finalize uses) and writes the result into an
  `ArchState`, then the existing render/gate machinery must accept it.

The transparency contract is the point: every guess is logged with a
confidence, and low-confidence inferences are the gaps the model reviews.
"""

from __future__ import annotations

import pytest

from mha.context.kg import KG
from mha.harnesses.arch.reverse_seed import (
    DEPENDENCY_RELATIONS,
    Subgraph,
    _extract_facet,
    _file_to_cid,
    _file_to_name,
    _infer_kind,
    _symbol_name,
    reverse_seed,
    scope_subgraph,
)
from mha.harnesses.arch.session import ArchSession
from mha.harnesses.arch.state import ArchState, ApiFacet, StoreFacet
from mha.harnesses.arch.tools import (
    ArchDoneTool,
    ComponentTool,
    ConnectTool,
    ImportStateTool,
)
from mha.tools import ToolContext


# ----------------------------------------------------------- pure transform


def _node(nid, label, file, ntype="function"):
    return {"id": nid, "label": label, "type": ntype, "source_file": file}


def test_symbol_name_strips_call_punctuation():
    assert _symbol_name("format_activity()") == "format_activity"
    assert _symbol_name(".close()") == "close"
    assert _symbol_name("OrderRepo") == "OrderRepo"


def test_file_to_cid_is_kebab_and_stable():
    assert _file_to_cid("src/mha/context/kg.py") == "context-kg"
    # drops common top-level dirs, strips extension
    assert _file_to_cid("lib/store/orders_repo.py") == "store-orders-repo"
    # idempotent + starts with a letter (validate_component requires it)
    cid = _file_to_cid("app/1thing.py")
    assert cid[0].isalpha()


def test_file_to_name_is_basename_without_extension():
    assert _file_to_name("src/mha/context/kg.py") == "kg"
    assert _file_to_name("lib/store/orders_repo.py") == "orders_repo"


def test_infer_kind_high_confidence_on_specific_signal():
    kind, conf, _ev = _infer_kind("src/store/orders_repo.py", ["OrderRepo", "save"])
    assert kind == "store"
    assert conf == "high"


def test_infer_kind_low_confidence_defaults_to_service():
    kind, conf, ev = _infer_kind("src/util/helpers.py", ["fmt", "dump"])
    assert kind == "service"
    assert conf == "low"
    assert "service" in ev


def test_infer_kind_ambiguous_drops_to_medium():
    # 'model' alone is ambiguous (domain model vs store) → medium
    kind, conf, _ev = _infer_kind("src/domain/model.py", ["UserModel"])
    assert kind == "store"
    assert conf == "medium"


def test_infer_kind_api_from_handler_names():
    kind, conf, _ev = _infer_kind("src/api/orders.py", ["get_orders", "create_order"])
    assert kind == "api"
    assert conf == "high"


def test_extract_facet_store_infers_camelcase_entities():
    facet, inf = _extract_facet("store", ["OrderRepo", "Order", "LineItem", "MAX"], "orders_repo.py")
    assert isinstance(facet, StoreFacet)
    names = [e.name for e in facet.entities]
    assert "Order" in names and "LineItem" in names
    assert "MAX" not in names  # all-caps constant, not an entity
    # the heuristic keeps every CamelCase class (it can't tell a repo from an
    # entity from name shape alone) — that over-inclusion is a transparent gap,
    # logged as low confidence, for the model to prune.
    assert "Order" in names and "LineItem" in names
    # entity keys are unknown — a transparent gap, not a guess
    assert all(e.keys == "unknown" for e in facet.entities)
    assert any(i.field == "facet.entities" and i.confidence == "low" for i in inf)


def test_extract_facet_api_infers_endpoints_from_verbs():
    facet, inf = _extract_facet("api", ["get_orders", "post_order", "helper"], "orders.py")
    assert isinstance(facet, ApiFacet)
    methods = {e.method for e in facet.endpoints}
    assert "GET" in methods and "POST" in methods
    # routes are unknown — a transparent gap
    assert all(e.route == "?" for e in facet.endpoints)
    assert any(i.field == "facet.endpoints" and i.confidence == "low" for i in inf)


def test_extract_facet_service_is_a_gap():
    facet, inf = _extract_facet("service", ["do_work"], "worker.py")
    assert facet is None
    assert inf == []  # service facets need semantic detail the graph lacks


def test_reverse_seed_groups_symbols_by_file_into_components():
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            _node("n2", "OrderRepo", "src/store/orders_repo.py", ntype="class"),
            _node("n3", "Order", "src/store/orders_repo.py", ntype="class"),
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "calls"}],
    )
    r = reverse_seed(sg, "orders")
    cids = {c.id for c in r.components}
    assert cids == {"api-orders", "store-orders-repo"}
    for c in r.components:
        assert c.existing is True
        assert c.origin == "imported:orders"


def test_reverse_seed_maps_call_edges_to_connections():
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            _node("n2", "OrderRepo", "src/store/orders_repo.py", ntype="class"),
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "calls"}],
    )
    r = reverse_seed(sg, "orders")
    assert len(r.connections) == 1
    conn = r.connections[0]
    assert conn.src == "api-orders"
    assert conn.dst == "store-orders-repo"
    assert conn.kind == "sync"
    assert conn.label == "calls"


def test_reverse_seed_drops_intra_file_edges():
    """`contains`/intra-file calls never become connections."""
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            _node("n2", "_validate()", "src/api/orders.py"),
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "calls"}],
    )
    r = reverse_seed(sg, "orders")
    assert len(r.components) == 1
    assert r.connections == []  # same file → not cross-component


def test_reverse_seed_drops_non_dependency_relations():
    assert "contains" not in DEPENDENCY_RELATIONS
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            _node("n2", "OrderRepo", "src/store/orders_repo.py", ntype="class"),
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "contains"}],
    )
    r = reverse_seed(sg, "orders")
    assert r.connections == []


def test_reverse_seed_collapses_mutual_edges():
    sg = Subgraph(
        nodes=[
            _node("n1", "a()", "src/svc/a.py"),
            _node("n2", "b()", "src/svc/b.py"),
        ],
        edges=[
            {"source": "n1", "target": "n2", "relation": "calls"},
            {"source": "n2", "target": "n1", "relation": "calls"},
        ],
    )
    r = reverse_seed(sg, "svc")
    assert len(r.connections) == 1
    assert "mutual" in r.connections[0].label
    assert any("mutual" in i.evidence for i in r.inference_log)


def test_reverse_seed_drops_bare_resolved_names_without_a_file():
    """A node with no source_file (e.g. a resolved `Path`) is glue, not a
    component, and edges touching it are dropped."""
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            {"id": "n2", "label": "Path", "type": "resolved"},  # no source_file
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "imports"}],
    )
    r = reverse_seed(sg, "orders")
    assert [c.id for c in r.components] == ["api-orders"]
    assert r.connections == []


def test_reverse_seed_logs_responsibility_as_a_gap():
    sg = Subgraph(nodes=[_node("n1", "do_work()", "src/svc/worker.py")])
    r = reverse_seed(sg, "worker")
    resp = [i for i in r.inference_log if i.field == "responsibility"]
    assert resp and resp[0].confidence == "low"
    assert r.components[0].responsibility == ""  # a gap, not a guess


def test_reverse_seed_reports_truncation_when_subgraph_is_stamped():
    sg = Subgraph(nodes=[_node("n1", "do_work()", "src/svc/worker.py")])
    sg._truncated = (2, 50)  # type: ignore[attr-defined]
    r = reverse_seed(sg, "worker")
    trunc = [i for i in r.inference_log if i.field == "scope" and i.value == "truncated"]
    assert trunc and "truncated at depth 2" in trunc[0].evidence


def test_reverse_seed_empty_subgraph_yields_empty_result():
    r = reverse_seed(Subgraph(), "nothing")
    assert r.components == [] and r.connections == []
    assert r.inference_log == []


# ------------------------------------------------- import_state integration


def _make_ctx(tmp_path, kg=None):
    session = ArchSession(state=ArchState(), run_dir=tmp_path / "run")
    ctx = ToolContext(repo_root=tmp_path, arch=session, kg=kg)
    return ctx, session


def _seed_kg(tmp_path, nodes, edges):
    """Build a real KG graph in tmp_path via kg.seed (the finalize path)."""
    kg = KG(repo_root=tmp_path, store_dir=tmp_path / "kg")
    kg.seed(nodes, edges)
    assert kg.is_ready()
    return kg


def _code_nodes():
    """A tiny as-built graph: an api calling a store, with an entity class."""
    return [
        {"id": "api_orders", "label": "get_orders() — list orders", "type": "function",
         "source_file": "src/api/orders.py"},
        {"id": "store_repo", "label": "OrderRepo — persists orders", "type": "class",
         "source_file": "src/store/orders_repo.py"},
        {"id": "entity_order", "label": "Order — order entity", "type": "class",
         "source_file": "src/store/orders_repo.py"},
    ], [
        {"source": "api_orders", "target": "store_repo", "relation": "calls"},
        {"source": "store_repo", "target": "entity_order", "relation": "contains"},
    ]


def test_import_state_loads_components_and_connections(tmp_path):
    nodes, edges = _code_nodes()
    kg = _seed_kg(tmp_path, nodes, edges)
    ctx, session = _make_ctx(tmp_path, kg=kg)

    res = ImportStateTool().execute({"scope": "orders"}, ctx)
    assert not res.is_error, res.output
    state = session.state
    assert len(state.components) >= 2
    assert any(c.kind == "api" for c in state.components.values())
    assert any(c.kind == "store" for c in state.components.values())
    # the call edge became a connection; the contains edge did not
    assert len(state.connections) == 1
    assert state.connections[0].kind == "sync"
    # loaded state moves out of brainstorm so the model can refine
    assert state.phase == "propose"
    # every loaded component is marked existing (brownfield, not owed)
    assert all(c.existing for c in state.components.values())


def test_import_state_transparency_report_lists_low_confidence_gaps(tmp_path):
    nodes, edges = _code_nodes()
    kg = _seed_kg(tmp_path, nodes, edges)
    ctx, session = _make_ctx(tmp_path, kg=kg)

    res = ImportStateTool().execute({"scope": "orders"}, ctx)
    assert not res.is_error, res.output
    details = res.details
    assert details["loaded"] >= 2
    assert "inferences" in details
    low = [i for i in details["inferences"] if i["confidence"] == "low"]
    assert low, "responsibility/kind gaps must be reported as low-confidence"
    assert "review" in res.output.lower() or "low-confidence" in res.output.lower()


def test_import_state_refuses_to_clobber_an_existing_design(tmp_path):
    nodes, edges = _code_nodes()
    kg = _seed_kg(tmp_path, nodes, edges)
    ctx, session = _make_ctx(tmp_path, kg=kg)
    # model already started designing
    ComponentTool().execute(
        {"id": "svc", "kind": "service", "responsibility": "r", "trace": ["g"]}, ctx
    )

    res = ImportStateTool().execute({"scope": "orders"}, ctx)
    assert res.is_error
    assert "one-shot" in res.output or "already has" in res.output


def test_import_state_requires_a_ready_kg(tmp_path):
    ctx, session = _make_ctx(tmp_path, kg=None)
    res = ImportStateTool().execute({"scope": "orders"}, ctx)
    assert res.is_error
    assert "knowledge graph" in res.output.lower()


def test_import_state_scope_matching_nothing_is_an_error(tmp_path):
    nodes, edges = _code_nodes()
    kg = _seed_kg(tmp_path, nodes, edges)
    ctx, _session = _make_ctx(tmp_path, kg=kg)
    res = ImportStateTool().execute({"scope": "zzznopezzz"}, ctx)
    assert res.is_error
    assert "no KG nodes" in res.output or "matched no" in res.output


def test_import_state_loaded_design_is_editable_with_existing_tools(tmp_path):
    """The whole point: loaded state renders and is editable with the same
    tools a hand-written design uses."""
    nodes, edges = _code_nodes()
    kg = _seed_kg(tmp_path, nodes, edges)
    ctx, session = _make_ctx(tmp_path, kg=kg)
    ImportStateTool().execute({"scope": "orders"}, ctx)

    # tighten a responsibility (a gap the report flagged)
    cid = next(c for c in session.state.components.values() if c.kind == "api").id
    res = ComponentTool().execute(
        {"id": cid, "responsibility": "serves the orders REST API", "trace": ["goal"]}, ctx
    )
    assert not res.is_error, res.output
    assert session.state.components[cid].responsibility == "serves the orders REST API"

    # add a new connection between loaded components
    ids = sorted(session.state.components)
    res = ConnectTool().execute(
        {"src": ids[0], "dst": ids[1], "label": "reads", "kind": "sync"}, ctx
    )
    assert not res.is_error, res.output


def test_import_state_then_finalize_still_seeds_kg(tmp_path):
    """The existing arch→KG seed must still work on a design that started as a
    reverse-seed import. End-to-end: import → fill brief → done (top level) →
    done (finalize) → kg has design nodes."""
    from mha.harnesses.arch.tools import BriefTool

    class _Broker:
        """Approves every gate (top-level, then finalize)."""
        def __init__(self):
            self.requests = []
        def request(self, payload):
            self.requests.append(payload)
            return (True, "")

    nodes, edges = _code_nodes()
    kg = _seed_kg(tmp_path, nodes, edges)
    broker = _Broker()
    session = ArchSession(state=ArchState(), run_dir=tmp_path / "run", broker=broker)
    ctx = ToolContext(repo_root=tmp_path, arch=session, kg=kg)
    ImportStateTool().execute({"scope": "orders"}, ctx)
    BriefTool().execute(
        {"goal": "ship orders", "actors": ["user"], "scope": "internal"}, ctx
    )

    # gate 1: top-level approval → expand
    res = ArchDoneTool().execute({"summary": "top level ready"}, ctx)
    assert not res.is_error, res.output
    assert session.state.phase == "expand"
    assert broker.requests[0]["kind"] == "toplevel_approval"

    # gate 2: finalize → finalized, and the arch→KG seed runs
    res = ArchDoneTool().execute({"summary": "finalized"}, ctx)
    assert not res.is_error, res.output
    assert session.state.phase == "finalized"
    assert broker.requests[1]["kind"] == "finalize"
    # the finalize seed merged design nodes into the same graph the import read
    G = kg._load_graph()
    assert any(
        "design:" in str(nd.get("source_location", ""))
        for _, nd in G.nodes(data=True)
    )


# ------------------------------------------------- scope_subgraph on a KG


def test_scope_subgraph_returns_empty_when_kg_not_ready(tmp_path):
    kg = KG(repo_root=tmp_path, store_dir=tmp_path / "kg")
    assert not kg.is_ready()
    sg = scope_subgraph(kg, "orders")
    assert sg.nodes == []


def test_scope_subgraph_extracts_a_slice_from_a_ready_kg(tmp_path):
    nodes, edges = _code_nodes()
    kg = _seed_kg(tmp_path, nodes, edges)
    sg = scope_subgraph(kg, "orders")
    assert len(sg.nodes) >= 2
    # edges carry their relation through
    rels = {e.get("relation") for e in sg.edges}
    assert "calls" in rels