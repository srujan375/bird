"""Asking before designing, and answering the user's own choices.

These cover the failure this harness exists to prevent: a design built for
numbers nobody supplied. The mechanisms are deliberately *advisory* — nothing
here blocks a session — so every test asserts on what the model is TOLD, which
is the only lever an advisory system has.
"""

import pytest

from bird.harnesses.arch import render
from bird.harnesses.arch.session import ArchSession
from bird.harnesses.arch.state import ArchState, Brief, Component, Decision, Option
from bird.harnesses.arch.tools import (
    AskTool,
    ComponentTool,
    DecideTool,
    ExpandTool,
    OfferTool,
)
from bird.tools import ToolContext

from .test_arch_tools import FakeBroker, err, make_ctx, ok


# ---------- an unstated scope is a question, not a licence to build big ----------


def test_unstated_scope_means_the_smallest_thing_that_could_work():
    assert Brief().scope_assumed() is True
    assert Brief().effective_scope() == "prototype"
    stated = Brief(scope="high_scale")
    assert stated.scope_assumed() is False
    assert stated.effective_scope() == "high_scale"


def test_an_assumed_scope_is_never_treated_as_production():
    """effective_scope() must not leak into the production checks: assuming
    prototype is the point, and assuming *production* would be the bug."""
    assert ArchState().scope_is_production() is False


def test_the_tracker_says_the_scope_was_assumed(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    assert "ASSUMED" in render.tracker(session.state)
    session.state.brief.scope = "production"
    assert "ASSUMED" not in render.tracker(session.state)
    assert "scope=production (stated)" in render.tracker(session.state)


def test_the_tracker_pins_the_numbers_not_a_boolean(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    assert "numbers: NONE STATED" in render.tracker(session.state)
    session.state.brief.scale.writes_per_sec = "40/s"
    session.state.brief.consistency = "eventual"
    line = render.tracker(session.state)
    assert "writes/s=40/s" in line and "consistency=eventual" in line


def test_brief_debt_is_reported_once_a_design_exists(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    # nothing drawn yet: the brief being empty is not yet a debt
    assert "components on the canvas and the brief is still missing" not in render.tracker(
        session.state
    )
    ok(ComponentTool(), ctx, id="api", kind="api", responsibility="serve")
    assert "components on the canvas and the brief is still missing" in render.tracker(
        session.state
    )


# ---------- offer: the answer, one tap away ----------


def test_offer_writes_the_answer_straight_into_the_brief(tmp_path):
    broker = FakeBroker([(True, "~1k users")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    ok(OfferTool(), ctx, question="Roughly how many users?",
       options=["~1k users", "~100k", "~1M+"], target="brief.scale.users")
    assert session.state.brief.scale.users == "~1k users"
    q = session.state.questions[0]
    assert (q.answer, q.resolution, q.target) == ("~1k users", "answered", "brief.scale.users")
    assert broker.requests[0]["kind"] == "offer"
    assert broker.requests[0]["options"] == ["~1k users", "~100k", "~1M+"]


def test_offer_only_accepts_a_real_scope_for_the_scope_field(tmp_path):
    """Free text is a fine answer to most questions and a corrupt value for
    `scope`, which four other checks branch on."""
    broker = FakeBroker([(True, "quite big honestly")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    ok(OfferTool(), ctx, question="What scope?", options=["prototype", "production"],
       target="brief.scope")
    assert session.state.brief.scope == ""
    assert session.state.questions[0].answer == "quite big honestly"


def test_a_dismissed_offer_defers_the_question_instead_of_guessing(tmp_path):
    broker = FakeBroker([(False, "")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    res = ok(OfferTool(), ctx, question="How many users?", options=["~1k", "~1M"],
             target="brief.scale.users")
    assert session.state.brief.scale.users is None
    assert session.state.questions[0].resolution == "deferred"
    assert "assuming" in res.output


def test_an_unanswered_offer_never_becomes_an_answer(tmp_path):
    """No broker (headless, evals) auto-approves with empty feedback. That must
    read as 'nobody answered', not as agreement with option one — otherwise the
    harness invents the very numbers it exists to ask for."""
    ctx, session, _ = make_ctx(tmp_path, broker=None)
    res = ok(OfferTool(), ctx, question="How many users?", options=["~1k", "~1M"],
             target="brief.scale.users")
    assert session.state.brief.scale.users is None
    assert session.state.questions[0].resolution == "deferred"
    assert "No one answered" in res.output


def test_offer_refuses_to_be_a_statement(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    assert "at least 2 options" in err(OfferTool(), ctx, question="ok?", options=["yes"])
    assert "at most" in err(OfferTool(), ctx, question="pick", options=list("abcde"))


# ---------- a question hangs on the thing it is about ----------


def test_expand_warns_when_you_go_deep_on_an_unanswered_question(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    ok(ComponentTool(), ctx, id="events", kind="store", responsibility="keep events")
    ok(AskTool(), ctx, question="How long must events be kept?", blocking=False,
       target="events")
    res = ok(ExpandTool(), ctx, component_id="events",
             entities=[{"name": "Event", "keys": "id", "fields": ["id"]}])
    assert "you asked about events and have no answer yet" in res.output
    assert "How long must events be kept?" in res.output


def test_expand_is_quiet_when_nothing_is_outstanding(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    ok(ComponentTool(), ctx, id="events", kind="store", responsibility="keep events")
    res = ok(ExpandTool(), ctx, component_id="events",
             entities=[{"name": "Event", "keys": "id", "fields": ["id"]}])
    assert "have no answer yet" not in res.output


# ---------- the user's own choice gets a verdict ----------


def test_a_user_choice_with_nothing_weighed_against_it_is_reported_back(tmp_path):
    """The SQS case: the user names a technology, it lands as a one-option
    decision, and nobody ever checked whether it was right at their scale."""
    ctx, session, _ = make_ctx(tmp_path)
    res = ok(DecideTool(), ctx, topic="Message queue", category="communication",
             choice="SQS", rationale="the user asked for it", source="user",
             options=[{"name": "SQS"}])
    assert "the user's own choice and nothing was weighed against it" in res.output
    assert [d.id for d in session.state.unweighed_user_choices()] == ["d1"]
    assert "user choices with no alternative weighed" in render.tracker(session.state)


def test_weighing_a_real_alternative_settles_it(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    res = ok(DecideTool(), ctx, topic="Message queue", category="communication",
             choice="SQS", rationale="burst profile justifies it at their numbers",
             source="user",
             options=[{"name": "SQS", "pros": ["absorbs bursts"]},
                      {"name": "pg_cron", "cons": ["no burst headroom"]}])
    assert "nothing was weighed against it" not in res.output
    assert session.state.unweighed_user_choices() == []
    assert "user choices with no alternative weighed" not in render.tracker(session.state)


def test_the_harness_only_chases_choices_the_user_made(tmp_path):
    """A one-option decision the *architect* made is already reported as thin;
    it must not also be chased as an unanswered user request."""
    ctx, session, _ = make_ctx(tmp_path)
    ok(DecideTool(), ctx, topic="Message queue", category="communication",
       choice="SQS", rationale="it is what I know", options=[{"name": "SQS"}])
    assert session.state.unweighed_user_choices() == []


def test_decision_provenance_survives_a_roundtrip():
    st = ArchState()
    st.decisions.append(Decision(id="d1", topic="Queue", category="communication",
                                 options=[Option(name="SQS")], choice="SQS",
                                 rationale="asked", source="user"))
    assert ArchState.from_dict(st.to_dict()).decisions[0].source == "user"


def test_a_pre_overhaul_state_file_still_loads():
    """`source` is new; a state file written before it must not explode, and
    must not be mistaken for a user choice."""
    old = ArchState().to_dict()
    old["decisions"] = [{"id": "d1", "topic": "Queue", "category": "communication",
                         "options": [{"name": "SQS"}], "choice": "SQS", "rationale": "r"}]
    old["questions"] = [{"id": "q1", "question": "how many?", "blocking": False,
                         "source": "model"}]
    state = ArchState.from_dict(old)
    assert state.decisions[0].source == "model"
    assert state.questions[0].target is None
    assert state.unweighed_user_choices() == []


# ---------- the wire, not a fake ----------


def test_an_offer_travels_the_real_broker_wire(tmp_path):
    """tool -> request_gate -> PermissionBroker -> emitted event -> resolve.

    The unit tests above fake the broker, so they cannot catch a payload the
    page cannot read. This asserts the event shape the UI's PermissionEvent
    type actually destructures: kind/question/options, plus the id it answers
    with."""
    import threading

    from bird.permissions import PermissionBroker

    emitted: list[dict] = []
    broker = PermissionBroker(emit=lambda name, **kw: emitted.append({"name": name, **kw}))
    ctx, session, _ = make_ctx(tmp_path, broker=broker)

    result: list = []
    t = threading.Thread(
        target=lambda: result.append(
            OfferTool().execute(
                {"question": "Roughly how many users?",
                 "options": ["~1k users", "~100k", "~1M+"],
                 "target": "brief.scale.users"},
                ctx,
            )
        ),
        daemon=True,
    )
    t.start()

    deadline = __import__("time").time() + 5
    while not emitted and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)
    assert emitted, "the offer never reached the transport"

    req = emitted[0]
    assert req["name"] == "permission_request"
    assert req["kind"] == "offer"
    assert req["question"] == "Roughly how many users?"
    assert req["options"] == ["~1k users", "~100k", "~1M+"]
    assert req["target"] == "brief.scale.users"

    # the page answers with the chosen option as the feedback string
    broker.resolve(req["id"], True, "~1k users")
    t.join(timeout=5)
    assert not t.is_alive(), "the tool never unblocked after the user answered"
    assert session.state.brief.scale.users == "~1k users"
    assert "prototype" in render.tracker(session.state)


def test_closing_the_page_mid_offer_does_not_hang_the_session(tmp_path):
    """deny_all() is what a disconnecting page triggers. An offer caught by it
    must come back as deferred, not as an answer and not as a wedged turn."""
    import threading
    import time

    from bird.permissions import PermissionBroker

    emitted: list[dict] = []
    broker = PermissionBroker(emit=lambda name, **kw: emitted.append({"name": name, **kw}))
    ctx, session, _ = make_ctx(tmp_path, broker=broker)

    result: list = []
    t = threading.Thread(
        target=lambda: result.append(
            OfferTool().execute(
                {"question": "How many?", "options": ["~1k", "~1M"]}, ctx
            )
        ),
        daemon=True,
    )
    t.start()
    deadline = time.time() + 5
    while not emitted and time.time() < deadline:
        time.sleep(0.01)

    broker.deny_all()
    t.join(timeout=5)
    assert not t.is_alive()
    assert session.state.questions[0].resolution == "deferred"
    assert not result[0].is_error
