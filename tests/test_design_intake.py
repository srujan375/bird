"""The design intake: the questions asked before anything is drawn.

The gate is the point. A session with a person at the workbench asks for the
direction first, holds the brief behind it, and refuses `design_create` until
it is answered — three artboards in the wrong fidelity are three artboards
nobody asked for. A session with nobody in the room (every other test in this
suite, and every headless caller) never asks and never blocks.
"""

from types import SimpleNamespace

import pytest

from bird.harnesses.design import intake
from bird.harnesses.design.session import DesignSession, STATUS_ASKING, STATUS_GENERATING
from bird.harnesses.design.state import DesignState
from bird.harnesses.design.tools import DesignCreateTool
from bird.serve import Server
from bird.tools import ToolContext, ToolError

HERO = "<html><body><h1>Hello</h1></body></html>"


def _gated(prompt: str = "a landing page for a podcast") -> DesignSession:
    s = DesignSession(intake_gate=True)
    s.start(prompt)
    return s


def _server_with(design: DesignSession) -> Server:
    """A Server carrying only what on_answer touches, plus a record of the
    turns it started — the pump is the only thing allowed to start one."""
    server = Server.__new__(Server)
    server.repl = SimpleNamespace(runner=SimpleNamespace(ctx=SimpleNamespace(design=design)))
    server.started = []
    server.on_user_input = lambda text, subjects=(): server.started.append(text)
    return server


# ---- the gate ----


def test_a_session_with_nobody_in_the_room_never_asks():
    """The default. Every headless caller — and every other test — starts
    generating, because a picker nobody can answer is a hang."""
    s = DesignSession()
    s.start("a landing page")
    assert s.pending_ask() is None
    assert s.status == STATUS_GENERATING
    s.guard_intake()  # does not raise


def test_the_workbench_asks_for_the_direction_first():
    s = _gated()
    ask = s.pending_ask()
    assert ask is not None and ask.id == intake.FIDELITY
    assert s.status == STATUS_ASKING, "nothing is being generated behind the question"
    assert [o.value for o in ask.options] == [intake.WIREFRAME, intake.HIGH_FIDELITY]
    assert all(o.description for o in ask.options), "the rows say what each costs"
    assert ask.variant == "option-cards"


def test_design_create_is_refused_while_the_question_is_open():
    s = _gated()
    ctx = ToolContext(repo_root=None)
    ctx.design = s
    with pytest.raises(ToolError, match="has not answered"):
        DesignCreateTool().run({"html": HERO, "title": "Hero"}, ctx)
    assert not s.state.artboards


def test_design_create_is_allowed_once_the_intake_is_settled():
    s = _gated()
    s.answer_ask({"id": intake.FIDELITY, "value": intake.WIREFRAME})
    ctx = ToolContext(repo_root=None)
    ctx.design = s
    out = DesignCreateTool().run({"html": HERO, "title": "Hero"}, ctx)
    assert not out.is_error and "hero" in s.state.artboards


# ---- one question at a time ----


def test_high_fidelity_raises_the_design_system_question_and_only_then():
    s = _gated()
    assert s.pending_ask().id == intake.FIDELITY
    result = s.answer_ask({"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    assert "input" not in result, "the brief waits for the second question"
    nxt = s.pending_ask()
    assert nxt is not None and nxt.id == intake.DESIGN_SYSTEM
    assert 2 <= len(nxt.options) <= 4, "four cards is the ceiling"
    assert nxt.options[-1].value == intake.DESIGNER_CHOICE, "and one row hands it back"


def test_a_wireframe_is_never_asked_which_design_system():
    """The wireframe theme *is* the greyscale. Asking which design system to
    sketch in is a question with no consequence."""
    s = _gated()
    result = s.answer_ask({"id": intake.FIDELITY, "value": intake.WIREFRAME})
    assert s.pending_ask() is None
    assert s.state.intake.by_id(intake.DESIGN_SYSTEM) is None
    assert result["input"] == (
        "Direction: Wireframe. Design system: wireframe. "
        "Brief: a landing page for a podcast"
    )


def test_the_last_answer_composes_the_opening_message():
    s = _gated()
    s.answer_ask({"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    theme = s.pending_ask().options[0].value
    result = s.answer_ask({"id": intake.DESIGN_SYSTEM, "value": theme})
    assert result["input"] == (
        f"Direction: High-fidelity. Design system: {theme}. "
        "Brief: a landing page for a podcast"
    )
    assert s.status == STATUS_GENERATING


def test_the_designers_choice_leaves_the_design_system_unspecified():
    """Which is what the design instructions read as "offer me the list"."""
    s = _gated()
    s.answer_ask({"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    result = s.answer_ask({"id": intake.DESIGN_SYSTEM, "value": intake.DESIGNER_CHOICE})
    assert "Design system: unspecified." in result["input"]


def test_changing_the_direction_retires_the_question_it_raised():
    """Change on an answered row is a revision: the design-system question was
    only ever there because of the high-fidelity answer."""
    s = _gated()
    s.answer_ask({"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    assert s.state.intake.by_id(intake.DESIGN_SYSTEM) is not None
    result = s.answer_ask({"id": intake.FIDELITY, "value": intake.WIREFRAME})
    assert s.state.intake.by_id(intake.DESIGN_SYSTEM) is None
    assert s.state.intake.by_id(intake.FIDELITY).revised
    assert "Direction: Wireframe." in result["input"]


def test_a_brief_less_session_still_composes_a_message():
    s = _gated(prompt="")
    result = s.answer_ask({"id": intake.FIDELITY, "value": intake.WIREFRAME})
    assert result["input"] == "Direction: Wireframe. Design system: wireframe."


# ---- what the page is told ----


def test_the_state_push_carries_the_pending_question_and_the_record():
    s = _gated()
    ev = s.state_event()
    assert ev["status"] == "asking"
    assert ev["ask"]["id"] == intake.FIDELITY
    assert [a["id"] for a in ev["intake"]] == [intake.FIDELITY]
    assert ev["intake_locked"] is False

    s.answer_ask({"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    ev = s.state_event()
    assert ev["ask"]["id"] == intake.DESIGN_SYSTEM, "the next one, not both"
    assert [a["id"] for a in ev["intake"]] == [intake.FIDELITY, intake.DESIGN_SYSTEM]
    assert ev["intake"][0]["answered"] is True
    assert ev["intake_locked"] is False, "still changeable — nothing has been drawn"

    s.answer_ask({"id": intake.DESIGN_SYSTEM, "value": intake.DESIGNER_CHOICE})
    ev = s.state_event()
    assert ev["ask"] is None
    assert ev["intake_locked"] is True, "the brief has gone out; the answers are history"


def test_an_ungated_session_pushes_no_question():
    ev = DesignSession().state_event()
    assert ev["ask"] is None and ev["intake"] == [] and ev["intake_locked"] is False


def test_the_intake_survives_a_state_round_trip():
    s = _gated()
    s.answer_ask({"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    back = DesignState.from_dict(s.state.to_dict())
    assert back.intake.value(intake.FIDELITY) == intake.HIGH_FIDELITY
    assert back.intake.pending().id == intake.DESIGN_SYSTEM


# ---- the pump ----


def test_on_answer_routes_to_the_design_session_and_starts_the_turn():
    s = _gated()
    server = _server_with(s)
    out = server.on_answer({"id": intake.FIDELITY, "value": intake.WIREFRAME})
    assert out["ok"] and out["answered"] == intake.FIDELITY and out["label"] == "Wireframe"
    assert "input" not in out, "the turn text is the pump's business, not the page's"
    assert server.started == [
        "Direction: Wireframe. Design system: wireframe. Brief: a landing page for a podcast"
    ]


def test_on_answer_starts_no_turn_while_a_question_is_still_open():
    s = _gated()
    server = _server_with(s)
    server.on_answer({"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    assert server.started == [], "the designer waits for the second answer"


def test_on_answer_refuses_a_row_that_was_not_offered():
    s = _gated()
    out = _server_with(s).on_answer({"id": intake.FIDELITY, "value": "sketchy"})
    assert out["ok"] is False and "no option" in out["error"]
    assert s.pending_ask().id == intake.FIDELITY, "and the question stays on the table"


def test_on_answer_says_so_when_there_is_nothing_to_answer():
    out = _server_with(DesignSession()).on_answer({"id": "fidelity", "value": "wireframe"})
    assert out["ok"] is False


# ---- the shortlist ----


def test_the_design_system_rows_are_real_installed_themes():
    ask = intake.design_system_ask()
    assert ask is not None
    from bird.harnesses.design import themes

    installed = set(themes.list_themes())
    named = [o.value for o in ask.options if o.value != intake.DESIGNER_CHOICE]
    assert named and set(named) <= installed
    assert "wireframe" not in named, "a wireframe is a direction, not a design system"
    assert all(o.description for o in ask.options), "a name alone does not say what it looks like"


def test_theme_slugs_are_titled_for_a_person_to_read():
    assert intake._title("linear-app") == "Linear"
    assert intake._title("swiss-minimal") == "Swiss Minimal"
    assert intake._title("notion") == "Notion"
