"""Tests for the reverse-seed capability: code KG → the board.

Two layers, mirroring the implementation's split:

- `reverse_seed` is a **pure transform** on a fabricated `Subgraph` (node dicts
  + edge dicts). These tests hand it synthetic graphs and assert the boxes,
  edges and inferences it produces — no KG, no I/O.
- `import_repo` is the integration: it scopes a real `KG` (built here via
  `kg.seed`, the same path handoff uses) and puts the result on the board,
  which the render layer and the architect's own tools must then accept.

The transparency contract is the point: every guess is logged with a
confidence, and low-confidence inferences are the gaps the architect reviews.
"""

from __future__ import annotations

import pytest

from bird.context.kg import KG
from bird.harnesses.arch.reverse_seed import (
    DEPENDENCY_RELATIONS,
    Subgraph,
    _file_to_cid,
    _file_to_name,
    _infer_kind,
    _symbol_name,
    reverse_seed,
    scope_subgraph,
)
from bird.harnesses.arch.session import ArchSession
from bird.harnesses.arch.state import ArchState
from bird.harnesses.arch.tools import (
    BriefTool,
    CanvasTool,
    HandoffTool,
    ImportRepoTool,
)
from bird.tools import ToolContext


# ----------------------------------------------------------- pure transform


def _node(nid, label, file, ntype="function"):
    return {"id": nid, "label": label, "type": ntype, "source_file": file}


def test_symbol_name_strips_call_punctuation():
    assert _symbol_name("format_activity()") == "format_activity"
    assert _symbol_name(".close()") == "close"
    assert _symbol_name("OrderRepo") == "OrderRepo"


def test_file_to_cid_is_kebab_and_stable():
    assert _file_to_cid("src/bird/context/kg.py") == "context-kg"
    # drops common top-level dirs, strips extension
    assert _file_to_cid("lib/store/orders_repo.py") == "store-orders-repo"
    # idempotent + starts with a letter (validate_component requires it)
    cid = _file_to_cid("app/1thing.py")
    assert cid[0].isalpha()


def test_file_to_name_is_basename_without_extension():
    assert _file_to_name("src/bird/context/kg.py") == "kg"
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


def test_reverse_seed_groups_symbols_by_file_into_boxes():
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            _node("n2", "OrderRepo", "src/store/orders_repo.py", ntype="class"),
            _node("n3", "Order", "src/store/orders_repo.py", ntype="class"),
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "calls"}],
    )
    r = reverse_seed(sg, "orders")
    assert {c.id for c in r.nodes} == {"api-orders", "store-orders-repo"}
    for box in r.nodes:
        assert box.existing is True, "imported code is background, not a proposal"
        assert box.depth == "stub"
        assert "orders" in box.notes


def test_reverse_seed_maps_call_edges_to_board_edges():
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            _node("n2", "OrderRepo", "src/store/orders_repo.py", ntype="class"),
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "calls"}],
    )
    r = reverse_seed(sg, "orders")
    assert len(r.edges) == 1
    edge = r.edges[0]
    assert edge.src == "api-orders"
    assert edge.dst == "store-orders-repo"
    assert edge.kind == "sync"
    assert edge.label == "calls"


def test_reverse_seed_drops_intra_file_edges():
    """`contains`/intra-file calls never become edges between boxes."""
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            _node("n2", "_validate()", "src/api/orders.py"),
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "calls"}],
    )
    r = reverse_seed(sg, "orders")
    assert len(r.nodes) == 1
    assert r.edges == []  # same file → not a cross-box dependency


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
    assert r.edges == []


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
    assert len(r.edges) == 1
    assert "mutual" in r.edges[0].label
    assert any("mutual" in i.evidence for i in r.inference_log)


def test_reverse_seed_drops_bare_resolved_names_without_a_file():
    """A node with no source_file (e.g. a resolved `Path`) is glue, not a box,
    and edges touching it are dropped."""
    sg = Subgraph(
        nodes=[
            _node("n1", "get_orders()", "src/api/orders.py"),
            {"id": "n2", "label": "Path", "type": "resolved"},  # no source_file
        ],
        edges=[{"source": "n1", "target": "n2", "relation": "imports"}],
    )
    r = reverse_seed(sg, "orders")
    assert [c.id for c in r.nodes] == ["api-orders"]
    assert r.edges == []


def test_reverse_seed_logs_responsibility_as_a_gap():
    sg = Subgraph(nodes=[_node("n1", "do_work()", "src/svc/worker.py")])
    r = reverse_seed(sg, "worker")
    resp = [i for i in r.inference_log if i.field == "responsibility"]
    assert resp and resp[0].confidence == "low"
    assert r.nodes[0].responsibility == ""  # a gap, not a guess


def test_reverse_seed_reports_truncation_when_subgraph_is_stamped():
    sg = Subgraph(nodes=[_node("n1", "do_work()", "src/svc/worker.py")])
    sg._truncated = (2, 50)  # type: ignore[attr-defined]
    r = reverse_seed(sg, "worker")
    trunc = [i for i in r.inference_log if i.field == "scope" and i.value == "truncated"]
    assert trunc and "truncated at depth 2" in trunc[0].evidence


def test_reverse_seed_empty_subgraph_yields_empty_result():
    r = reverse_seed(Subgraph(), "nothing")
    assert r.nodes == [] and r.edges == []
    assert r.inference_log == []


# -------------------------------------------------- import_repo integration


def _make_ctx(tmp_path, kg=None):
    session = ArchSession(state=ArchState(), run_dir=tmp_path / "run")
    ctx = ToolContext(repo_root=tmp_path, arch=session, kg=kg)
    return ctx, session


def _seed_kg(tmp_path, nodes, edges):
    """Build a real KG graph in tmp_path via kg.seed (the handoff path)."""
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


def test_import_repo_puts_the_existing_system_on_the_board(tmp_path):
    nodes, edges = _code_nodes()
    ctx, session = _make_ctx(tmp_path, kg=_seed_kg(tmp_path, nodes, edges))

    res = ImportRepoTool().execute({"scope": "orders"}, ctx)
    assert not res.is_error, res.output
    state = session.state
    assert len(state.nodes) >= 2
    assert any(n.kind == "api" for n in state.nodes.values())
    assert any(n.kind == "store" for n in state.nodes.values())
    # the call edge became a board edge; the contains edge did not
    assert len(state.edges) == 1
    assert state.edges[0].kind == "sync"


def test_imported_boxes_are_background_not_a_proposal(tmp_path):
    """They are what exists. The coverage checks skip them for that reason —
    nobody designed them here, so nothing about them is a gap."""
    from bird.harnesses.arch import derive

    nodes, edges = _code_nodes()
    ctx, session = _make_ctx(tmp_path, kg=_seed_kg(tmp_path, nodes, edges))
    ImportRepoTool().execute({"scope": "orders"}, ctx)

    assert all(n.existing for n in session.state.nodes.values())
    assert derive.coverage(session.state) == []
    assert derive.askable(session.state) == []


def test_import_repo_reports_its_low_confidence_guesses(tmp_path):
    nodes, edges = _code_nodes()
    ctx, _ = _make_ctx(tmp_path, kg=_seed_kg(tmp_path, nodes, edges))

    res = ImportRepoTool().execute({"scope": "orders"}, ctx)
    assert not res.is_error, res.output
    low = [i for i in res.details["inferences"] if i["confidence"] == "low"]
    assert low, "responsibility/kind gaps must be reported as low-confidence"
    assert "correct them with `canvas`" in res.output


def test_import_repo_refuses_to_clobber_a_board_in_progress(tmp_path):
    nodes, edges = _code_nodes()
    ctx, _ = _make_ctx(tmp_path, kg=_seed_kg(tmp_path, nodes, edges))
    CanvasTool().execute({"nodes": [{"label": "New service"}]}, ctx)

    res = ImportRepoTool().execute({"scope": "orders"}, ctx)
    assert res.is_error
    assert "one-shot" in res.output


def test_import_repo_requires_a_ready_kg(tmp_path):
    ctx, _ = _make_ctx(tmp_path, kg=None)
    res = ImportRepoTool().execute({"scope": "orders"}, ctx)
    assert res.is_error
    assert "knowledge graph" in res.output.lower()


def test_import_repo_scope_matching_nothing_is_an_error(tmp_path):
    nodes, edges = _code_nodes()
    ctx, _ = _make_ctx(tmp_path, kg=_seed_kg(tmp_path, nodes, edges))
    res = ImportRepoTool().execute({"scope": "zzznopezzz"}, ctx)
    assert res.is_error
    assert "matched no nodes" in res.output


def test_an_imported_board_is_editable_with_the_ordinary_tools(tmp_path):
    """The whole point: loaded state is the same graph a hand-drawn board is,
    and `canvas` corrects it."""
    nodes, edges = _code_nodes()
    ctx, session = _make_ctx(tmp_path, kg=_seed_kg(tmp_path, nodes, edges))
    ImportRepoTool().execute({"scope": "orders"}, ctx)

    nid = next(n for n in session.state.nodes.values() if n.kind == "api").id
    res = CanvasTool().execute(
        {"nodes": [{"id": nid, "responsibility": "serves the orders REST API"}]}, ctx
    )
    assert not res.is_error, res.output
    assert session.state.nodes[nid].responsibility == "serves the orders REST API"

    ids = sorted(session.state.nodes)
    res = CanvasTool().execute(
        {"edges": [{"src": ids[0], "dst": ids[1], "label": "reads"}]}, ctx
    )
    assert not res.is_error, res.output


def test_import_then_handoff_still_seeds_the_graph(tmp_path):
    """The arch→KG seed must work on a design that started as an import."""
    nodes, edges = _code_nodes()
    kg = _seed_kg(tmp_path, nodes, edges)
    ctx, session = _make_ctx(tmp_path, kg=kg)
    ImportRepoTool().execute({"scope": "orders"}, ctx)
    BriefTool().execute({"goal": "ship orders"}, ctx)

    res = HandoffTool().execute({"summary": "as-is plus one change"}, ctx)
    assert not res.is_error, res.output
    assert session.state.handed_off
    G = kg._load_graph()
    assert any(
        "design:" in str(nd.get("source_location", "")) for _, nd in G.nodes(data=True)
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