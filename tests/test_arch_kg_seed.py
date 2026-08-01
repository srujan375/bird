"""The KG seed: the design, queryable before the code exists.

The point is not that nodes land in a file — it is that `kg_query` answers a
builder's question on turn one of a greenfield session. So these tests ask the
real KG real questions, through the same path the code harness uses.
"""

import json

import pytest

from bird.context.kg import KG
from bird.harnesses.arch.kg_seed import build_seed, seed_kg
from bird.harnesses.arch.state import (
    ApiFacet,
    ArchState,
    Component,
    Connection,
    Endpoint,
    Entity,
    Flow,
    FlowStep,
    QueueFacet,
    QueueMessage,
    StoreFacet,
)


def design() -> ArchState:
    st = ArchState(phase="expand")
    st.brief.goal = "capture orders"
    st.components["order-api"] = Component(
        id="order-api", name="order-api", kind="api",
        responsibility="the public order surface", trace=["capture orders"],
        facet=ApiFacet(endpoints=[
            Endpoint(route="/orders", method="POST", request="{items, address}",
                     response="{id, status}", auth="session cookie", errors=["422"]),
        ]),
    )
    st.components["order-db"] = Component(
        id="order-db", name="order-db", kind="store",
        responsibility="durable order state", trace=["capture orders"],
        data_owned="orders, lines, payment intents",
        facet=StoreFacet(
            entities=[Entity(name="order", keys="id",
                             fields=["id", "user_id", "status", "total_cents"])],
            access_patterns=["orders by user, newest first"],
        ),
    )
    st.components["bus"] = Component(
        id="bus", name="events-bus", kind="queue", responsibility="carries domain events",
        facet=QueueFacet(messages=[
            QueueMessage(name="OrderPlaced", schema="{order_id, total_cents}",
                         ordering="per order_id", delivery="at-least-once", dlq_policy="5 tries"),
        ]),
    )
    st.connections.append(Connection(src="order-api", dst="order-db", label="writes", kind="sync"))
    st.connections.append(Connection(src="order-api", dst="bus", label="publishes",
                                     kind="async", mechanism="nats jetstream"))
    st.flows.append(Flow(id="place-order", name="place order", kind="happy", steps=[
        FlowStep(src="order-api", dst="order-db", action="INSERT order"),
        FlowStep(src="order-api", dst="bus", action="publish OrderPlaced"),
    ]))
    return st


# ---------------------------------------------------------------- the shape


def test_every_component_and_its_internals_become_nodes():
    nodes, edges = build_seed(design())
    labels = {n["id"]: n["label"] for n in nodes}

    assert "arch_order_db" in labels
    assert "durable order state" in labels["arch_order_db"]
    # the words a builder would search for are IN the label — query() matches
    # on labels alone, so an id-only node would be unreachable
    assert "total_cents" in labels["arch_order_db_entity_order"]
    assert "/orders" in labels["arch_order_api_endpoint_post_orders"]
    assert "at-least-once" in labels["arch_bus_message_orderplaced"]
    assert all(n["_origin"] == "arch" for n in nodes)
    # a hit must read as "the design said this", not as discovered code —
    # source_location is what kg_query prints beside the node
    assert all(n["source_file"].endswith(".md") for n in nodes)
    assert all(n["source_location"].startswith("design:") for n in nodes)


def test_connections_and_flows_become_edges():
    nodes, edges = build_seed(design())
    pairs = {(e["source"], e["target"], e["relation"]) for e in edges}

    assert ("arch_order_api", "arch_order_db", "writes") in pairs
    assert ("arch_order_api", "arch_bus", "publishes") in pairs
    assert ("arch_order_db", "arch_order_db_entity_order", "defines_entity") in pairs
    assert ("arch_flow_place_order", "arch_order_db", "step_in_flow") in pairs
    # the async edge keeps its mechanism, which is half of what makes it real
    async_edge = next(e for e in edges if e["relation"] == "publishes")
    assert "nats jetstream" in async_edge["context"]


def test_no_edge_dangles():
    """A connection to a component that vanished must not leave a half-edge —
    node_link_graph would silently invent the missing node."""
    st = design()
    st.connections.append(Connection(src="order-api", dst="ghost", label="calls", kind="sync"))
    nodes, edges = build_seed(st)
    ids = {n["id"] for n in nodes}
    assert all(e["source"] in ids and e["target"] in ids for e in edges)


def test_a_black_box_component_still_lands():
    st = ArchState()
    st.components["ext"] = Component(id="ext", name="stripe", kind="external",
                                     responsibility="takes the money")
    nodes, _ = build_seed(st)
    assert [n["id"] for n in nodes] == ["arch_ext"]


# ------------------------------------------------------- through the real KG


@pytest.fixture
def kg(tmp_path):
    return KG(repo_root=tmp_path, store_dir=tmp_path / "kg-out")


def test_seeding_creates_a_graph_a_greenfield_session_can_query(kg):
    """The whole point: no code on disk, and turn one can still ask."""
    assert not kg.is_ready()
    report = seed_kg(kg, design())
    assert "seeded the knowledge graph" in report
    assert kg.is_ready()

    answer = kg.query("where are orders stored").text
    assert "order-db" in answer
    assert "durable order state" in answer

    answer = kg.query("what publishes OrderPlaced").text
    assert "OrderPlaced" in answer
    assert "events-bus" in answer or "bus" in answer


def test_seeding_merges_into_an_existing_graph_instead_of_replacing_it(kg):
    """A brownfield repo already has a graph; the design joins it."""
    kg.seed(
        [{"id": "src_pay", "label": "charge_card", "type": "function",
          "source_file": "src/pay.py"}],
        [],
    )
    seed_kg(kg, design())
    graph = json.loads(kg.graph_path.read_text())
    ids = {n["id"] for n in graph["nodes"]}
    assert "src_pay" in ids and "arch_order_db" in ids
    assert "charge_card" in kg.query("charge card").text


def test_a_broken_graph_never_fails_the_finalize(kg, monkeypatch):
    """The design is already on disk by then. A graph that won't take the seed
    is a worse next session, not a failed architecture."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(kg, "seed", boom)
    report = seed_kg(kg, design())
    assert "not seeded" in report and "disk on fire" in report


def test_no_kg_is_not_an_error():
    assert seed_kg(None, design()) == ""


def test_finalize_seeds_the_graph(tmp_path):
    """Through the real gate: `done` writes the bundle and the graph together,
    because a builder needs both — the prose to read and the graph to ask."""
    from bird.harnesses.arch.session import ArchSession
    from bird.harnesses.arch.tools import ArchDoneTool
    from bird.tools import ToolContext

    class Broker:
        def request(self, payload):
            return True, ""

    run_dir = tmp_path / "run"
    kg = KG(repo_root=tmp_path, store_dir=tmp_path / "kg-out")
    session = ArchSession(state=design(), run_dir=run_dir, broker=Broker(),
                          on_state=lambda e: None)
    ctx = ToolContext(repo_root=tmp_path, arch=session, kg=kg)

    res = ArchDoneTool().execute({"summary": "ship it"}, ctx)
    assert not res.is_error, res.output
    assert session.state.phase == "finalized"
    assert "seeded the knowledge graph" in res.output
    assert (run_dir / "bundle" / "architecture.md").is_file()
    # the graph points back at the bundle it was seeded from
    assert kg.is_ready()
    assert "order-db" in kg.query("where are orders stored").text
