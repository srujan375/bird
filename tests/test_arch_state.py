"""Tests for ArchState — validation, gates, obligations, serialization."""

import json

import pytest

from ox.harnesses.arch.state import (
    ApiFacet,
    ArchState,
    Brief,
    Component,
    Connection,
    Decision,
    Endpoint,
    Entity,
    Flow,
    FlowStep,
    Obligation,
    OpenQuestion,
    Option,
    Scale,
    StoreFacet,
)


def comp(id, kind="service", **kw):
    kw.setdefault("name", id)
    kw.setdefault("responsibility", f"{id} does things")
    kw.setdefault("trace", ["goal"])
    if kind == "store":
        kw.setdefault("data_owned", "its data")
    return Component(id=id, kind=kind, **kw)


def small_state(scope="internal"):
    st = ArchState()
    st.brief = Brief(goal="ship it", actors=["user"], scope=scope)
    if scope in ("production", "high_scale"):
        st.brief.scale = Scale(users="10k MAU")
        st.brief.consistency = "eventual"
        st.brief.availability = "99.9"
    return st


# ---------- brief gate ----------


def test_brief_missing_baseline():
    assert Brief().missing() == ["goal", "actors", "scope"]


def test_brief_missing_production_extras():
    b = Brief(goal="g", actors=["a"], scope="production")
    assert b.missing() == ["scale", "consistency", "availability"]
    b.scale = Scale(users="10k")
    b.consistency = "strong"
    b.availability = "99.9"
    assert b.missing() == []


def test_brief_internal_needs_no_extras():
    assert Brief(goal="g", actors=["a"], scope="internal").missing() == []


# ---------- component validation ----------


def test_component_id_must_be_kebab():
    st = small_state()
    with pytest.raises(ValueError, match="kebab-case"):
        st.validate_component(comp("Worker_Pool"), updating=False)


def test_thin_component_is_accepted_and_reported_as_a_gap():
    """The inversion: a component with no trace is recorded, not refused."""
    st = small_state()
    c = comp("api", trace=[])
    st.validate_component(c, updating=False)  # does not raise
    assert any("YAGNI" in g for g in st.component_gaps(c))


def test_store_without_data_owned_is_a_gap_not_an_error():
    st = small_state()
    c = comp("db", kind="store")
    c.data_owned = None
    st.validate_component(c, updating=False)
    assert any("data_owned" in g for g in st.component_gaps(c))
    c.data_owned = "orders"
    assert not any("data_owned" in g for g in st.component_gaps(c))


def test_failure_notes_gap_only_at_production_scope():
    assert not small_state().component_gaps(comp("svc"))
    st = small_state("production")
    assert any("failure_notes" in g for g in st.component_gaps(comp("svc")))
    assert not st.component_gaps(comp("svc", failure_notes="retries, then 503"))


def test_malformed_id_and_unknown_kind_still_raise():
    """Structural breakage stays a hard error — it would corrupt the graph."""
    st = small_state()
    with pytest.raises(ValueError, match="kebab-case"):
        st.validate_component(comp("Not An Id"), updating=False)
    bad = comp("svc")
    bad.kind = "wormhole"
    with pytest.raises(ValueError, match="unknown kind"):
        st.validate_component(bad, updating=False)


# ---------- connection / flow / decision validation ----------


def test_connection_refs_must_exist():
    st = small_state()
    st.components["a"] = comp("a")
    with pytest.raises(ValueError, match="unknown component 'b'"):
        st.validate_connection(Connection(src="a", dst="b", label="x", kind="sync"))


def test_async_without_mechanism_is_a_gap():
    st = small_state()
    st.components["a"] = comp("a")
    st.components["b"] = comp("b")
    loose = Connection(src="a", dst="b", label="x", kind="async")
    st.validate_connection(loose)  # does not raise
    assert any("mechanism" in g for g in st.connection_gaps(loose))
    named = Connection(src="a", dst="b", label="x", kind="async", mechanism="rabbitmq")
    assert not st.connection_gaps(named)


def test_connection_failure_mode_gap_at_production_scope():
    st = small_state("production")
    st.components["a"] = comp("a")
    st.components["b"] = comp("b")
    conn = Connection(src="a", dst="b", label="x", kind="sync")
    st.validate_connection(conn)
    assert any("failure_mode" in g for g in st.connection_gaps(conn))


def test_flow_steps_must_reference_components():
    st = small_state()
    st.components["a"] = comp("a")
    with pytest.raises(ValueError, match="unknown component"):
        st.validate_flow(Flow(id="f", name="f", kind="happy",
                              steps=[FlowStep(src="a", dst="ghost", action="GET /x")]))


def test_single_option_decision_is_a_gap_but_bad_choice_still_raises():
    st = small_state()
    lonely = Decision(id="d", topic="t", category="storage",
                      options=[Option(name="pg")], choice="pg", rationale="r")
    st.validate_decision(lonely)  # recorded — you can decide without a bake-off
    assert any("without alternatives" in g for g in st.decision_gaps(lonely))
    with pytest.raises(ValueError, match="must match"):
        st.validate_decision(Decision(id="d", topic="t", category="storage",
                                      options=[Option(name="pg"), Option(name="mysql")],
                                      choice="sqlite", rationale="r"))


def test_references_to_lists_danglers():
    st = small_state()
    st.components["a"] = comp("a")
    st.components["b"] = comp("b")
    st.connections.append(Connection(src="a", dst="b", label="x", kind="sync"))
    st.flows.append(Flow(id="f1", name="f", kind="happy",
                         steps=[FlowStep(src="a", dst="b", action="do")]))
    refs = st.references_to("b")
    assert "connection a -> b" in refs and "flow 'f1'" in refs
    assert st.references_to("a")  # both sides count


# ---------- obligations ----------


def build(scope, kinds, critical_ids=()):
    st = small_state(scope)
    for i, kind in enumerate(kinds):
        cid = f"c{i}-{kind}"
        st.components[cid] = comp(cid, kind=kind, failure_notes="handled")
    if critical_ids:
        ids = list(critical_ids)
        st.flows.append(Flow(id="f", name="main", kind="happy",
                             steps=[FlowStep(src=ids[0], dst=ids[-1], action="go")]))
    return st


def test_prototype_owes_nothing():
    st = build("prototype", ["store", "api", "queue", "service"])
    st.compute_obligations()
    assert st.obligations == []


def test_internal_owes_store_and_critical_api():
    st = build("internal", ["store", "api", "service"])
    st.compute_obligations()
    owed = {o.component_id for o in st.obligations}
    assert owed == {"c0-store"}  # api not on a critical flow
    st.flows.append(Flow(id="f", name="main", kind="happy",
                         steps=[FlowStep(src="c1-api", dst="c2-service", action="GET /")]))
    st.compute_obligations()
    owed = {o.component_id for o in st.obligations}
    assert owed == {"c0-store", "c1-api"}


def test_production_owes_contracts_not_service_internals():
    st = build("production", ["store", "api", "queue", "llm", "service", "external"])
    st.compute_obligations()
    owed = {o.component_id: o.facet for o in st.obligations}
    assert owed == {
        "c0-store": "store", "c1-api": "api", "c2-queue": "queue", "c3-llm": "llm",
    }


def test_high_scale_adds_infra_and_critical_services():
    st = build("high_scale", ["infra", "service", "service"])
    st.flows.append(Flow(id="f", name="main", kind="happy",
                         steps=[FlowStep(src="c1-service", dst="c2-service", action="rpc")]))
    st.compute_obligations()
    owed = {o.component_id for o in st.obligations}
    assert "c0-infra" in owed and "c1-service" in owed and "c2-service" in owed


def test_existing_components_never_owe():
    st = build("production", ["store"])
    st.components["c0-store"].existing = True
    st.compute_obligations()
    assert st.obligations == []


def test_recompute_preserves_done_and_waived():
    st = build("production", ["store", "api"])
    st.compute_obligations()
    st.obligations[0].status = "done"
    st.compute_obligations()
    by_id = {o.component_id: o.status for o in st.obligations}
    assert by_id["c0-store"] == "done" and by_id["c1-api"] == "pending"


# ---------- gates ----------


def test_toplevel_missing():
    st = small_state()
    assert len(st.toplevel_missing()) == 3
    st.components["a"] = comp("a")
    st.components["b"] = comp("b")
    st.flows.append(Flow(id="f", name="main", kind="happy",
                         steps=[FlowStep(src="a", dst="b", action="go")]))
    st.decisions.append(Decision(id="d", topic="t", category="storage",
                                 options=[Option(name="x"), Option(name="y")],
                                 choice="x", rationale="r"))
    assert st.toplevel_missing() == []


def test_blocking_questions():
    st = small_state()
    st.questions.append(OpenQuestion(id="q1", question="?", blocking=True, source="model"))
    st.questions.append(OpenQuestion(id="q2", question="?", blocking=False, source="model"))
    assert [q.id for q in st.blocking_questions()] == ["q1"]
    st.questions[0].resolution = "answered"
    assert st.blocking_questions() == []


# ---------- serialization ----------


def test_json_round_trip_with_facets():
    st = build("production", ["store", "api"])
    st.components["c0-store"].facet = StoreFacet(
        entities=[Entity(name="orders", keys="id", fields=["id", "total"], indexes=["user_id"])],
        access_patterns=["orders by user, newest first"],
        retention="180d",
    )
    st.components["c1-api"].facet = ApiFacet(
        endpoints=[Endpoint(route="/orders", method="POST", request="{...}",
                            response="{id}", auth="bearer", errors=["409"])]
    )
    st.connections.append(Connection(src="c1-api", dst="c0-store", label="write",
                                     kind="sync", failure_mode="503"))
    st.questions.append(OpenQuestion(id="q", question="?", blocking=True, source="judge"))
    st.compute_obligations()
    st.amendments.append(__import__("ox.harnesses.arch.state", fromlist=["Amendment"]).Amendment(
        turn=1, description="renamed", structural=False))

    wire = json.dumps(st.to_dict())
    back = ArchState.from_dict(json.loads(wire))
    assert back.brief.scope == "production"
    assert back.components["c0-store"].facet.entities[0].name == "orders"
    assert back.components["c0-store"].facet.facet_kind == "store"
    assert back.components["c1-api"].facet.endpoints[0].route == "/orders"
    assert back.connections[0].failure_mode == "503"
    assert back.questions[0].blocking is True
    assert {o.component_id for o in back.obligations} == {"c0-store", "c1-api"}
    assert back.amendments[0].description == "renamed"
    # round-trip is loss-free
    assert back.to_dict() == st.to_dict()


def test_a_pre_overhaul_state_file_still_loads():
    """`intake` and `challenge` were removed once nothing advanced into them.
    A session saved before that must still open — mapped to where it would be
    now, not left holding a phase no code understands."""
    old = {
        "mode": "system",
        "phase": "intake",
        "brief": {"goal": "ship it", "actors": ["user"], "scope": "internal"},
        "components": {},
        "connections": [], "flows": [], "decisions": [],
        "questions": [], "concerns": [], "obligations": [], "amendments": [],
    }
    assert ArchState.from_dict(old).phase == "brainstorm"

    old["phase"] = "challenge"
    old["components"] = {"db": {"id": "db", "name": "db", "kind": "store",
                                "responsibility": "rows"}}
    migrated = ArchState.from_dict(old)
    assert migrated.phase == "expand"
    assert migrated.components["db"].kind == "store"
    # and the migrated value is what gets written back — the old name is gone
    assert migrated.to_dict()["phase"] == "expand"


def test_gaps_by_subject_groups_thinness_for_the_page():
    """The page marks the thin node itself, so the rules stay server-side."""
    st = small_state("production")
    st.components["api"] = comp("api", trace=[])
    st.components["db"] = comp("db", kind="store")
    st.components["db"].data_owned = None
    st.connections.append(Connection(src="api", dst="db", label="w", kind="async"))
    by = st.gaps_by_subject()
    assert any("trace" in g for g in by["api"])
    assert any("data_owned" in g for g in by["db"])
    assert any("mechanism" in g for g in by["api->db"])
    # the flat form the tracker and bundle use is derived from the same source
    assert sorted(st.gaps()) == sorted(
        f"{subj}: {g}" for subj, gaps in by.items() for g in gaps
    )
