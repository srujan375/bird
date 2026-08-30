"""The architect's notebook: six tools, and what each of them refuses.

The line these tests hold is that a tool refuses only what is *broken*. An
unfinished design, a missing brief, a decision with nothing weighed against it —
all of those succeed, and come back with an observation rather than an error.
"""

from __future__ import annotations

import json

import pytest

from bird.harnesses.arch.session import ArchSession
from bird.harnesses.arch.tools import arch_harness_tools
from bird.tools import ToolContext


@pytest.fixture
def board(tmp_path):
    """(call, session) — `call(tool, **args)` runs a tool against a live board."""
    session = ArchSession(run_dir=tmp_path / "run")
    ctx = ToolContext(repo_root=tmp_path, arch=session)
    tools = {t.name: t for t in arch_harness_tools()}

    # positional-only: `approach` has a `name` argument of its own, and a
    # keyword parameter here would shadow it
    def call(tool_name, /, **args):
        return tools[tool_name].execute(args, ctx)

    return call, session


# ---------------------------------------------------------------- canvas


def test_a_whole_shape_lands_in_one_call(board):
    """Six calls to put up a diagram is six round-trips the user waits through."""
    call, session = board
    res = call("canvas",
               nodes=[{"label": "API", "kind": "api"}, {"label": "Orders", "kind": "store"}],
               edges=[{"src": "api", "dst": "orders", "label": "writes"}])
    assert not res.is_error, res.output
    assert set(session.state.nodes) == {"api", "orders"}
    assert len(session.state.edges) == 1


def test_a_missing_edge_endpoint_is_created_as_a_stub(board):
    """You are at a whiteboard: draw the flow first, name the boxes after."""
    call, session = board
    res = call("canvas", edges=[{"src": "api", "dst": "stripe", "label": "charges"}])
    assert not res.is_error, res.output
    assert set(session.state.nodes) == {"api", "stripe"}
    assert session.state.nodes["stripe"].depth == "stub"
    assert "auto-created stubs" in res.output


def test_an_id_is_derived_from_the_label_when_none_is_given(board):
    call, session = board
    call("canvas", nodes=[{"label": "Order Store"}])
    assert "order-store" in session.state.nodes


def test_a_partial_spec_updates_only_what_it_names(board):
    call, session = board
    call("canvas", nodes=[{"id": "api", "label": "API", "kind": "api",
                           "responsibility": "fronts everything"}])
    call("canvas", nodes=[{"id": "api", "tech": "FastAPI"}])
    node = session.state.nodes["api"]
    assert node.tech == "FastAPI"
    assert node.responsibility == "fronts everything", "an unnamed field is not cleared"
    assert node.kind == "api"


def test_depth_moves_both_ways(board):
    """Collapsing a box whose detail stopped earning its place is a real design
    move, not an undo."""
    call, session = board
    call("canvas", nodes=[{"id": "api", "label": "API", "depth": "detailed",
                           "detail": "lots of words"}])
    call("canvas", nodes=[{"id": "api", "depth": "stub"}])
    assert session.state.nodes["api"].depth == "stub"


def test_redrawing_an_edge_relabels_it_instead_of_stacking_a_second(board):
    call, session = board
    call("canvas", edges=[{"src": "a", "dst": "b", "label": "calls"}])
    call("canvas", edges=[{"src": "a", "dst": "b", "label": "calls, with a retry"}])
    assert len(session.state.edges) == 1
    assert session.state.edges[0].label == "calls, with a retry"


def test_removing_a_box_takes_its_edges_with_it(board):
    call, session = board
    call("canvas", edges=[{"src": "a", "dst": "b"}, {"src": "b", "dst": "c"}])
    res = call("canvas", remove=["b"])
    assert not res.is_error, res.output
    assert set(session.state.nodes) == {"a", "c"}
    assert session.state.edges == []
    assert "2 edge(s)" in res.output


def test_removing_a_single_edge_leaves_the_boxes(board):
    call, session = board
    call("canvas", edges=[{"src": "a", "dst": "b"}])
    call("canvas", remove=["a>b"])
    assert set(session.state.nodes) == {"a", "b"}
    assert session.state.edges == []


def test_an_empty_canvas_call_is_refused(board):
    call, _ = board
    assert call("canvas").is_error


def test_a_node_with_no_label_or_id_is_refused(board):
    call, _ = board
    res = call("canvas", nodes=[{"kind": "store"}])
    assert res.is_error and "needs a label" in res.output


def test_an_unknown_kind_reaches_the_model_verbatim(board):
    call, _ = board
    res = call("canvas", nodes=[{"label": "DB", "kind": "database"}])
    assert res.is_error and "unknown kind" in res.output


# -------------------------------------------------------------- approach


def test_naming_an_approach_then_labelling_boxes_with_it(board):
    call, session = board
    call("approach", name="queue-first", summary="durable queue, workers drain it")
    res = call("canvas", nodes=[{"label": "Queue", "kind": "queue",
                                 "approaches": ["queue-first"]}])
    assert not res.is_error, res.output
    assert session.state.nodes["queue"].approaches == ["queue-first"]


def test_a_box_labelled_with_an_unnamed_approach_is_refused(board):
    call, _ = board
    res = call("canvas", nodes=[{"label": "Queue", "approaches": ["typo-first"]}])
    assert res.is_error and "unknown approach" in res.output


def test_greying_an_approach_needs_the_reason_it_lost(board):
    call, _ = board
    call("approach", name="queue-first")
    res = call("approach", id="queue-first", status="greyed")
    assert res.is_error and "reason it lost" in res.output


def test_a_greyed_approach_stays_on_the_board_with_its_reason(board):
    call, session = board
    call("approach", name="queue-first")
    res = call("approach", id="queue-first", status="greyed",
               rejected_reason="the volume never justifies a broker")
    assert not res.is_error, res.output
    app = session.state.approaches["queue-first"]
    assert app.status == "greyed"
    assert "never justifies" in app.rejected_reason
    assert "queue-first" in session.state.approaches, "greyed is not deleted"


# ---------------------------------------------------------------- decide


def test_a_decision_records_what_it_beat(board):
    call, session = board
    res = call("decide", topic="compute", choice="lambda",
               against=["always-on service"], why="cheaper at this volume")
    assert not res.is_error, res.output
    dec = session.state.decisions[0]
    assert [o.name for o in dec.options] == ["lambda", "always-on service"]
    assert dec.rationale == "cheaper at this volume"


def test_against_accepts_bare_names_or_objects_with_pros_and_cons(board):
    """Jot lightly or elaborate — the point is that neither is a form."""
    call, session = board
    call("decide", topic="queue", choice="in-process",
         against=[{"name": "SQS", "cons": ["a broker to operate"], "pros": ["durable"]}])
    rival = session.state.decisions[0].options[1]
    assert rival.name == "SQS"
    assert rival.cons == ["a broker to operate"]
    assert rival.pros == ["durable"]


def test_a_pragmatic_choice_is_recorded_as_a_verdict(board):
    call, session = board
    call("decide", topic="delivery", choice="in-process retry",
         why="ships this week",
         pragmatic="loses in-flight retries on restart; at this volume you re-fire by hand")
    assert "re-fire by hand" in session.state.decisions[0].pragmatism_note


def test_a_user_choice_with_no_rival_is_told_so_but_not_refused(board):
    call, session = board
    res = call("decide", topic="queue", choice="SQS", source="user")
    assert not res.is_error
    assert "nothing was weighed" in res.output
    assert len(session.state.decisions) == 1


def test_amending_a_decision_replaces_it_rather_than_stacking(board):
    call, session = board
    call("decide", topic="compute", choice="lambda")
    call("decide", id="d1", topic="compute", choice="fargate", why="changed my mind")
    assert len(session.state.decisions) == 1
    assert session.state.decisions[0].choice == "fargate"


def test_amending_an_unknown_decision_is_refused(board):
    call, _ = board
    res = call("decide", id="d9", topic="x", choice="y")
    assert res.is_error and "no decision" in res.output


# -------------------------------------------------------------- question


def test_parking_a_question_and_answering_it_later(board):
    call, session = board
    res = call("question", question="Which region?", recommendation="us-east-1, closest to you")
    assert not res.is_error, res.output
    assert session.state.questions[0].status == "open"
    call("question", id="q1", answer="eu-west-1")
    assert session.state.questions[0].status == "answered"
    assert session.state.questions[0].answer == "eu-west-1"


def test_a_question_with_no_recommendation_is_accepted_and_called_out(board):
    """Handing the user a blank is the failure mode; refusing the tool would
    just lose the question."""
    call, session = board
    res = call("question", question="What are your latency requirements?")
    assert not res.is_error
    assert "no recommendation" in res.output
    assert len(session.state.questions) == 1


def test_answering_an_unknown_question_is_refused(board):
    call, _ = board
    res = call("question", id="q7", answer="sure")
    assert res.is_error and "no question" in res.output


# ----------------------------------------------------------------- brief


def test_the_brief_accretes_one_field_at_a_time(board):
    call, session = board
    call("brief", goal="relay webhooks")
    call("brief", scale="a few hundred a day, spiky")
    assert session.state.brief.goal == "relay webhooks"
    assert session.state.brief.scale == "a few hundred a day, spiky"


def test_an_empty_brief_call_is_refused(board):
    call, _ = board
    assert call("brief").is_error


# --------------------------------------------------------------- handoff


def test_handoff_writes_the_bundle_and_ends_the_session(board, tmp_path):
    call, session = board
    call("canvas", nodes=[{"label": "API", "kind": "api"}])
    res = call("handoff", summary="one box, and they said that's enough")
    assert not res.is_error, res.output
    assert res.details["done"] is True, "the engine terminates on this"
    assert session.state.handed_off
    md = tmp_path / "run" / "bundle" / "architecture.md"
    assert md.is_file()


def test_handoff_never_refuses_because_the_design_is_unfinished(board):
    """Whether the design is done is the user's judgement, not the harness's."""
    call, session = board
    call("canvas", nodes=[{"label": "API", "kind": "api"}])
    call("question", question="anything about the schema?")
    res = call("handoff", summary="they're done")
    assert not res.is_error
    assert "1 question(s) travel with it" in res.output


def test_handoff_with_an_empty_board_is_refused(board):
    call, _ = board
    res = call("handoff", summary="nothing here")
    assert res.is_error and "nothing on the board" in res.output


def test_everything_is_locked_once_the_design_is_handed_off(board):
    call, _ = board
    call("canvas", nodes=[{"label": "API", "kind": "api"}])
    call("handoff", summary="done")
    for name, args in (
        ("canvas", {"nodes": [{"label": "Late"}]}),
        ("decide", {"topic": "x", "choice": "y"}),
        ("brief", {"goal": "g"}),
        ("question", {"question": "q"}),
        ("approach", {"name": "a"}),
    ):
        res = call(name, **args)
        assert res.is_error and "handed off" in res.output, name


# ------------------------------------------------------------- the shape


def test_the_toolset_is_small(board):
    """Twenty tools was the old harness's data-entry surface. The recording
    surface is six; the rest is fact-finding."""
    recording = {"canvas", "approach", "decide", "question", "brief", "handoff"}
    names = {t.name for t in arch_harness_tools()}
    assert recording <= names
    assert not names & {"variant", "node", "link", "splice", "promote", "component",
                        "connect", "flow", "expand", "concern", "offer", "ask",
                        "answer", "amend_toplevel", "done"}


def test_the_architect_cannot_touch_the_repo():
    names = {t.name for t in arch_harness_tools()}
    assert not names & {"edit", "write", "bash"}


def test_every_tool_schema_is_json_serializable():
    """They go on the wire to the model verbatim."""
    for tool in arch_harness_tools():
        json.dumps(tool.spec().parameters)


def test_arch_tools_refuse_outside_an_arch_session(tmp_path):
    ctx = ToolContext(repo_root=tmp_path)  # no ctx.arch
    tools = {t.name: t for t in arch_harness_tools()}
    res = tools["canvas"].execute({"nodes": [{"label": "x"}]}, ctx)
    assert res.is_error and "not an architecture session" in res.output


def test_canvas_merges_facts_and_replaces_items(session_and_ctx=None):
    from bird.harnesses.arch.tools import _upsert_node
    from bird.harnesses.arch.session import ArchSession
    import inspect
    # build a bare session the way the other tool tests do, if a helper exists
    try:
        from tests.test_arch_tools import _session as mk  # type: ignore
        session = mk()
    except Exception:
        session = None
    if session is None:
        from bird.harnesses.arch.state import ArchState
        class S:  # minimal stand-in: _upsert_node only touches .state
            state = ArchState()
        session = S()
    auto = []
    _upsert_node(session, {"id": "api", "label": "API", "kind": "api",
                           "facts": {"protocol": "http", "auth": "session"},
                           "items": ["/state", {"k": "POST", "v": "/mutate", "d": "one op"}]}, auto)
    n = session.state.nodes["api"]
    assert n.facts == {"protocol": "http", "auth": "session"}
    assert [i.k for i in n.items] == ["GET", "POST"]
    _upsert_node(session, {"id": "api", "facts": {"auth": ""}, "items": [{"v": "/events"}]}, auto)
    n = session.state.nodes["api"]
    assert n.facts == {"protocol": "http"}
    assert [i.v for i in n.items] == ["/events"]
