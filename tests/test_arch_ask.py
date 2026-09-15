"""Questions the architect parks, as pickers on the board's conversation.

A question with options is answered in one click; a question without is
answered in prose, in the message box. Both settle the same question, and only
the earliest open one is ever on the table.
"""

from types import SimpleNamespace

import pytest

from bird.harnesses.arch.session import ArchSession, PICK_PREFIX
from bird.harnesses.arch.state import ArchState
from bird.harnesses.arch.tools import QuestionTool
from bird.serve import Server
from bird.tools import ToolContext, ToolError

ROWS = [
    {"label": "In-process", "cost": "One box. Loses queued work on a crash."},
    {"label": "Redis", "cost": "One more thing to run and watch.", "rec": True},
]


def _ctx(session: ArchSession) -> ToolContext:
    ctx = ToolContext(repo_root=None)
    ctx.arch = session
    return ctx


def _asked(**extra) -> tuple[ArchSession, ToolContext]:
    session = ArchSession()
    ctx = _ctx(session)
    args = {"question": "Where does the queue live?", "recommendation": "in-process for now",
            "summary": "Queue home", "options": ROWS}
    args.update(extra)
    QuestionTool().run(args, ctx)
    return session, ctx


def _server_with(arch: ArchSession) -> Server:
    """A Server carrying only what on_answer touches, plus a record of the
    turns it started."""
    server = Server.__new__(Server)
    server.repl = SimpleNamespace(runner=SimpleNamespace(ctx=SimpleNamespace(arch=arch)))
    server.started = []
    server.on_user_input = lambda text, subjects=(): server.started.append(text)
    return server


# ---- parking one ----


def test_the_options_reach_the_page_as_a_picker():
    """They did not before: the tool took `options` and dropped them on the
    floor for every newly parked question."""
    session, _ = _asked()
    ask = session.state_event()["ask"]
    assert ask["id"] == "q1" and ask["variant"] == "option-cards"
    assert ask["prompt"] == "Where does the queue live?"
    assert ask["summaryLabel"] == "Queue home"
    assert ask["confirm"] == {"template": "Go with {v}", "empty": "Pick an answer"}
    assert [o["label"] for o in ask["options"]] == ["In-process", "Redis"]
    assert ask["options"][1]["rec"] is True


def test_a_question_asked_in_prose_puts_no_picker_on_the_table():
    session = ArchSession()
    QuestionTool().run({"question": "How many users?", "recommendation": "a few hundred"}, _ctx(session))
    assert session.state_event()["ask"] is None
    assert session.state.questions[0].open


def test_only_the_earliest_open_question_is_on_the_table():
    session, ctx = _asked()
    QuestionTool().run({"question": "Which datastore?", "options": ROWS}, ctx)
    assert session.state_event()["ask"]["id"] == "q1"
    session.answer_ask({"id": "q1", "value": "Redis"})
    assert session.state_event()["ask"]["id"] == "q2", "the next appears when the first is answered"


def test_a_picker_the_page_could_not_render_is_refused_at_the_tool():
    session = ArchSession()
    with pytest.raises(ToolError, match="at least 2 options"):
        QuestionTool().run({"question": "one row?", "options": [{"label": "yes"}]}, _ctx(session))
    assert not session.state.questions, "and nothing is parked"


def test_two_recommendations_is_neither():
    session = ArchSession()
    rows = [dict(r, rec=True) for r in ROWS]
    with pytest.raises(ToolError, match="at most one option"):
        QuestionTool().run({"question": "Where?", "options": rows}, _ctx(session))


def test_options_can_be_added_to_a_question_already_parked():
    session = ArchSession()
    ctx = _ctx(session)
    QuestionTool().run({"question": "Where does the queue live?"}, ctx)
    assert session.state_event()["ask"] is None
    QuestionTool().run({"id": "q1", "options": ROWS, "summary": "Queue home"}, ctx)
    assert session.state_event()["ask"]["options"][0]["label"] == "In-process"


# ---- answering one ----


def test_answering_settles_the_question_and_records_the_label():
    session, _ = _asked()
    out = session.answer_ask({"id": "q1", "value": "Redis"})
    q = session.state.question_by_id("q1")
    assert q.status == "answered" and q.answer == "Redis"
    assert out["label"] == "Redis"
    assert session.state_event()["ask"] is None


def test_the_turn_it_sends_is_a_pick_not_words_the_user_typed():
    session, _ = _asked()
    out = session.answer_ask({"id": "q1", "value": "In-process"})
    assert out["input"].startswith(PICK_PREFIX)
    assert "- In-process" in out["input"]
    assert "in answer to q1: Where does the queue live?" in out["input"], (
        "the question rides along so the architect cannot bind the answer to the wrong one"
    )


def test_a_row_that_was_not_offered_is_refused():
    session, _ = _asked()
    with pytest.raises(ValueError, match="no option 'Kafka'"):
        session.answer_ask({"id": "q1", "value": "Kafka"})
    assert session.state.question_by_id("q1").open


def test_answering_an_unknown_question_names_the_ones_it_has():
    session, _ = _asked()
    with pytest.raises(ValueError, match="known: q1"):
        session.answer_ask({"id": "q9", "value": "Redis"})


def test_an_answered_question_survives_a_state_round_trip():
    session, _ = _asked()
    session.answer_ask({"id": "q1", "value": "Redis"})
    back = ArchState.from_dict(session.state.to_dict())
    q = back.question_by_id("q1")
    assert q.answer == "Redis" and q.summary == "Queue home"
    assert [o.label for o in q.options] == ["In-process", "Redis"]
    assert q.options[1].description == "One more thing to run and watch."


# ---- the pump ----


def test_on_answer_routes_to_the_arch_session_and_starts_the_turn():
    session, _ = _asked()
    server = _server_with(session)
    out = server.on_answer({"id": "q1", "value": "Redis"})
    assert out["ok"] and out["label"] == "Redis"
    assert server.started and server.started[0].startswith(PICK_PREFIX)
    assert session.state.question_by_id("q1").status == "answered"


def test_on_answer_refuses_without_taking_the_question_off_the_table():
    session, _ = _asked()
    server = _server_with(session)
    out = server.on_answer({"id": "q1", "value": "Kafka"})
    assert out["ok"] is False and "no option" in out["error"]
    assert server.started == [] and session.state_event()["ask"]["id"] == "q1"
