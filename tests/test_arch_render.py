"""Tests for the arch renderers — mermaid sources + the pinned tracker."""

from mha.harnesses.arch import render
from mha.harnesses.arch.state import (
    ArchState,
    Brief,
    Component,
    Connection,
    DeployUnit,
    Entity,
    Flow,
    FlowStep,
    InfraFacet,
    Module,
    Obligation,
    OpenQuestion,
    Scale,
    ServiceFacet,
    StoreFacet,
)


def comp(id, kind="service", existing=False, facet=None):
    return Component(id=id, name=id.replace("-", " ").title(), kind=kind,
                     responsibility="r", trace=["g"], existing=existing, facet=facet)


def state_with(components=(), connections=(), flows=(), scope="internal"):
    st = ArchState()
    st.brief = Brief(goal="g", actors=["a"], scope=scope, scale=Scale(users="10k"),
                     consistency="eventual", availability="99.9")
    for c in components:
        st.components[c.id] = c
    st.connections = list(connections)
    st.flows = list(flows)
    return st


# ---------- toplevel flowchart ----------


def test_toplevel_shapes_and_edges():
    st = state_with(
        components=[comp("gw", "gateway"), comp("db", "store"), comp("q", "queue"),
                    comp("stripe", "external")],
        connections=[
            Connection(src="gw", dst="db", label="write", kind="sync"),
            Connection(src="gw", dst="q", label="emit", kind="async", mechanism="sqs"),
            Connection(src="q", dst="db", label="batch load", kind="batch"),
        ],
    )
    src = render.toplevel_mermaid(st)
    assert src.startswith("flowchart TD")
    assert 'db[("Db<br/><i>store</i>")]' in src
    assert 'q[["Q<br/><i>queue</i>"]]' in src
    assert 'stripe(("Stripe<br/><i>external</i>"))' in src
    assert 'gw --> |"write"| db' in src or 'gw -->|"write"| db' in src
    assert '-.->|"emit via sqs"| q' in src
    assert '==>|"batch load"| db' in src
    assert "classDef existing" not in src  # no brownfield components


def test_toplevel_existing_styled():
    st = state_with(components=[comp("legacy", existing=True), comp("new-svc")])
    src = render.toplevel_mermaid(st)
    assert "classDef existing" in src
    assert "class legacy existing" in src
    assert "new-svc" not in src.split("class ")[-1]


# ---------- flow sequence ----------


def test_flow_mermaid():
    flow = Flow(id="f", name="place order", kind="happy", steps=[
        FlowStep(src="ui", dst="api", action="POST /orders"),
        FlowStep(src="api", dst="db", action="INSERT", note="idempotent"),
    ])
    src = render.flow_mermaid(flow)
    assert src.splitlines()[0] == "sequenceDiagram"
    assert "participant ui" in src and "participant db" in src
    assert "ui->>api: POST /orders" in src
    assert "note over db: idempotent" in src


# ---------- facet diagrams ----------


def test_store_facet_er():
    c = comp("db", "store", facet=StoreFacet(
        entities=[Entity(name="orders", keys="id", fields=["id", "total amount"])]))
    src = render.facet_mermaid(c)
    assert src.startswith("erDiagram")
    assert "ORDERS {" in src
    assert "string total_amount" in src


def test_infra_facet_deployment():
    c = comp("deploy", "infra", facet=InfraFacet(
        units=[DeployUnit(name="web tier", components=["gw", "api"],
                          scaling_policy="cpu > 70%")],
        state_locality="stateless"))
    src = render.facet_mermaid(c)
    assert "subgraph web_tier" in src and "cpu > 70%" in src


def test_service_facet_modules_and_tableonly_facets():
    c = comp("svc", "service", facet=ServiceFacet(
        interface=["do_thing()"], modules=[Module(name="core", purpose="logic")]))
    assert "core: logic" in render.facet_mermaid(c)
    # interface-only service facet: tabular, no forced diagram
    c2 = comp("svc2", "service", facet=ServiceFacet(interface=["x"]))
    assert render.facet_mermaid(c2) is None


def test_render_all_structure():
    st = state_with(
        components=[comp("db", "store", facet=StoreFacet(
            entities=[Entity(name="e", keys="id")], access_patterns=["p"]))],
        flows=[Flow(id="f1", name="f", kind="happy",
                    steps=[FlowStep(src="db", dst="db", action="noop")])],
    )
    out = render.render_all(st)
    assert set(out) == {"toplevel", "flows", "facets"}
    assert "flowchart TD" in out["toplevel"]
    assert "f1" in out["flows"]
    assert out["facets"]["db"]["kind"] == "store"
    assert "erDiagram" in out["facets"]["db"]["mermaid"]


# ---------- tracker ----------


def test_tracker_prefix_and_phases():
    st = ArchState()
    t = render.tracker(st)
    assert t.startswith(render.TRACKER_PREFIX)
    assert "phase: intake" in t
    assert "goal" in t  # missing brief fields named

    st = state_with()
    st.phase = "propose"
    assert "top level still owes" in render.tracker(st)

    st.phase = "expand"
    st.obligations = [
        Obligation("svc", "service", "on the critical flow at high scale"),
        Obligation("db", "store", "stateful"),
    ]
    t = render.tracker(st)
    # risk order puts the store first despite list order
    assert t.index("db(store)") < t.index("svc(service)")
    assert 'expand("db")' in t

    st.obligations = []
    assert "challenge pass" in render.tracker(st)

    st.phase = "challenge"
    st.questions = [OpenQuestion(id="q1", question="retention?", blocking=True, source="judge")]
    t = render.tracker(st)
    assert "BLOCKING" in t and "q1" in t

    st.questions[0].resolution = "answered"
    assert "call `done` to request Finalize" in render.tracker(st)

    st.phase = "finalized"
    assert "session complete" in render.tracker(st)
