"""The critique loop: the capture waiter, the critic, and the tools that
carry the fix-list back to the designer.

The loop's contract under test: every failure is a skip note on the auto path
(a create must never hang or fail because the loop could not run) and a
ToolError naming what is missing on the explicit path (a model that asked for
eyes should know there are none).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bird.harnesses.design import critique as critique_mod
from bird.harnesses.design import session as session_mod
from bird.harnesses.design.critique import Critic, never_list
from bird.harnesses.design.session import DesignSession
from bird.harnesses.design.state import Artboard, DesignState, Version
from bird.harnesses.design.tools import (
    DesignCreateTool,
    DesignCritiqueTool,
    DesignPlanTool,
)
from bird.llm.types import LLMResponse, Message, Usage
from bird.llm.wire.openai_compat import OpenAICompatClient
from bird.tools import ToolContext, ToolError

DOC = ('<!doctype html><html><head><title>Pod</title></head>'
       '<body><header class="hero"><h1>Signal</h1></header></body></html>')
PNG = "data:image/png;base64,AAAA"


class FakeCritic:
    """Stands in for critique.Critic on the session: records what it is shown,
    returns a fixed fix-list, or fails on demand."""

    model = "fake:vision"

    def __init__(self, note: str = "the hero reads as slop", fail: str | None = None):
        self.note = note
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def critique_render(self, session, artboard_id: str, png: str) -> str:
        self.calls.append((artboard_id, png))
        if self.fail:
            raise RuntimeError(self.fail)
        return self.note

    def critique_plan(self, session, plan: str) -> str:
        self.calls.append(("plan", plan))
        if self.fail:
            raise RuntimeError(self.fail)
        return self.note


class FakeVisionClient:
    """The wire client the real Critic talks to: records the call, replies."""

    def __init__(self, reply: str = "fix the hero"):
        self.reply = reply
        self.calls: list[tuple[object, list[Message], object]] = []

    def complete(self, spec, messages, tools=None, **kwargs):
        self.calls.append((spec, messages, tools))
        return LLMResponse(message=Message(role="assistant", content=self.reply),
                           usage=Usage(1, 1), stop_reason="stop", model=spec.spec)

    def abort(self) -> None:
        pass


@pytest.fixture
def session() -> DesignSession:
    s = DesignSession()
    s.start("a podcast landing page")
    return s


@pytest.fixture
def ctx(session) -> ToolContext:
    c = ToolContext(repo_root=Path("."))
    c.design = session
    return c


def _answer_with_png(session: DesignSession):
    """A capture hook that answers every request with a png, same thread."""
    return lambda p: session.resolve_capture({"id": p["id"], "png": PNG})


# ------------------------------------------------------------------ waiter

def test_request_capture_returns_the_png_the_page_sent(session):
    seen: dict = {}
    session.capture_hook = lambda p: (seen.update(p),
                                      session.resolve_capture({"id": p["id"], "png": PNG}))
    png, why = session.request_capture("hero", "v1")
    assert (png, why) == (PNG, "")
    assert seen["artboard"] == "hero" and seen["version"] == "v1"


def test_no_page_is_a_skip(session):
    png, why = session.request_capture("hero", "v1")  # capture_hook is None
    assert png is None and "no workbench" in why


def test_a_timeout_is_a_skip(session, monkeypatch):
    monkeypatch.setattr(session_mod, "CAPTURE_TIMEOUT", 0.05)
    session.capture_hook = lambda p: None  # nobody answers
    png, why = session.request_capture("hero", "v1")
    assert png is None and "capture budget" in why and "too heavy" in why


def test_the_request_carries_a_page_deadline_inside_the_waiter(session):
    seen: dict = {}

    def hook(p):
        seen.update(p)
        session.resolve_capture({"id": p["id"], "png": PNG})

    session.capture_hook = hook
    session.request_capture("hero", "v1")
    deadline = seen["deadline"]
    # the page's timer derives from the harness's budget and stays strictly
    # inside it — an answer at the same instant the waiter gives up would
    # land on a slot that is already closed
    assert deadline == session_mod.CAPTURE_TIMEOUT - session_mod.CAPTURE_PAGE_MARGIN
    assert deadline < session_mod.CAPTURE_TIMEOUT


def test_a_heavy_document_gets_a_longer_budget(session):
    session.add_artboard(DOC * 8000, "Hero")  # ~700KB: the capped regime
    aid = session.state.selected
    seen: dict = {}

    def hook(p):
        seen.update(p)
        session.resolve_capture({"id": p["id"], "png": PNG})

    session.capture_hook = hook
    session.request_capture(aid, "v1")
    budget = session_mod.capture_budget(len(session.read_html(aid)))
    assert budget == session_mod.CAPTURE_MAX_TIMEOUT
    assert seen["deadline"] == session_mod.CAPTURE_MAX_TIMEOUT - session_mod.CAPTURE_PAGE_MARGIN
    assert seen["deadline"] < budget


def test_capture_budget_grows_with_the_document_and_is_capped():
    assert session_mod.capture_budget(0) == session_mod.CAPTURE_TIMEOUT
    mid = session_mod.capture_budget(200_000)
    assert session_mod.CAPTURE_TIMEOUT < mid < session_mod.CAPTURE_MAX_TIMEOUT
    assert session_mod.capture_budget(10 ** 7) == session_mod.CAPTURE_MAX_TIMEOUT


def test_an_error_reply_is_a_skip_with_the_reason(session):
    session.capture_hook = lambda p: session.resolve_capture(
        {"id": p["id"], "error": "tainted canvas"})
    png, why = session.request_capture("hero", "v1")
    assert png is None and "tainted canvas" in why


def test_a_reply_to_a_timed_out_capture_is_dropped(session, monkeypatch):
    monkeypatch.setattr(session_mod, "CAPTURE_TIMEOUT", 0.05)
    session.capture_hook = lambda p: None
    png, _ = session.request_capture("hero", "v1")
    assert png is None
    assert session.resolve_capture({"id": "cap-1", "png": PNG})["ok"] is False


def test_resolve_with_nothing_waiting_says_so(session):
    assert session.resolve_capture({"id": "cap-1", "png": PNG})["ok"] is False


def test_cancel_capture_denies_a_pending_one(session):
    asked = threading.Event()

    def hook(payload):
        asked.set()

    session.capture_hook = hook
    out: dict = {}

    def ask():
        out["png"], out["why"] = session.request_capture("hero", "v1")

    t = threading.Thread(target=ask)
    t.start()
    assert asked.wait(timeout=2)
    session.cancel_capture("interrupted")
    t.join(timeout=2)
    assert out["png"] is None and "interrupted" in out["why"]


# ------------------------------------------------- the hook and the critic

def test_auto_critique_without_a_critic_is_a_skip_note(session):
    note = session.auto_critique("hero")
    assert note.startswith("not critiqued") and "vision" in note


def test_auto_critique_records_the_fix_list_against_the_version(session):
    session.add_artboard(DOC, "Hero")
    aid = session.state.selected
    session.critic = FakeCritic("the hero reads as slop")
    session.capture_hook = _answer_with_png(session)
    assert session.auto_critique(aid) == "the hero reads as slop"
    entry = session.state.critique_for(aid, session.state.artboards[aid].current)
    assert entry["critique"] == "the hero reads as slop"
    assert entry["model"] == "fake:vision"


def test_auto_critique_folds_a_capture_failure_into_a_skip_note(session):
    session.add_artboard(DOC, "Hero")
    session.critic = FakeCritic()  # but no capture_hook: the page is gone
    note = session.auto_critique(session.state.selected)
    assert note.startswith("not critiqued:") and "no workbench" in note


def test_auto_critique_survives_a_critic_that_raises(session):
    session.add_artboard(DOC, "Hero")
    session.critic = FakeCritic(fail="provider down")
    session.capture_hook = _answer_with_png(session)
    note = session.auto_critique(session.state.selected)
    assert note.startswith("not critiqued:") and "provider down" in note


def test_critique_now_raises_naming_the_reason(session):
    session.add_artboard(DOC, "Hero")
    session.critic = FakeCritic()
    with pytest.raises(ValueError, match="no workbench"):
        session.critique_now(session.state.selected)


def test_critique_now_needs_a_document(session):
    session.critic = FakeCritic()
    with pytest.raises(KeyError):
        session.critique_now("nope")


# ------------------------------------------------------- the Critic itself

def test_never_list_reads_the_builtin_skill():
    body = never_list()
    assert "system-ui" in body and "terracotta" in body


def test_critique_render_sends_rubric_screenshot_and_outline(session, tmp_path):
    session.add_artboard(DOC, "Hero")
    aid = session.state.selected
    session.state.plan = "one hero, one signature marquee"
    session.set_theme("linear-app")
    client = FakeVisionClient("the hero is slop")
    critic = Critic(client, SimpleNamespace(spec="fake:vision"), run_dir=tmp_path)
    assert critic.critique_render(session, aid, PNG) == "the hero is slop"
    spec, messages, tools = client.calls[0]
    assert tools is None  # the critic is a judge, not an agent
    rubric = messages[0].content[0].text
    assert "terracotta" in rubric, "the never-list is the rubric"
    assert "one hero, one signature marquee" in rubric, "the plan rides along"
    assert "header.hero" in rubric, "fixes must cite selectors the outline names"
    assert "the-active-design-system" in rubric, "the theme brief rides along"
    assert messages[0].content[1].image_url["url"] == PNG


def test_the_screenshot_and_critique_are_filed_in_the_run_dir(session, tmp_path):
    session.run_dir = tmp_path
    session.add_artboard(DOC, "Hero")
    aid = session.state.selected
    critic = Critic(FakeVisionClient("slop"), SimpleNamespace(spec="fake:vision"),
                    run_dir=tmp_path)
    critic.critique_render(session, aid, PNG)
    stem = f"{aid}-{session.state.artboards[aid].current}"
    assert (tmp_path / "critiques" / f"{stem}.md").read_text(encoding="utf-8") == "slop"
    assert (tmp_path / "critiques" / f"{stem}.png").exists()


def test_critique_plan_is_text_only(session):
    client = FakeVisionClient("the plan defaults to cream and serif")
    critic = Critic(client, SimpleNamespace(spec="fake:vision"))
    note = critic.critique_plan(session, "editorial direction, one marquee")
    assert note == "the plan defaults to cream and serif"
    _, messages, tools = client.calls[0]
    assert tools is None
    content = messages[0].content  # a plain string: no image part at all
    assert "editorial direction, one marquee" in content
    assert "terracotta" in content


def test_a_hung_critic_call_is_aborted_at_the_cap(monkeypatch):
    monkeypatch.setattr(critique_mod, "CRITIC_TIMEOUT", 0.1)

    class HungClient:
        """A provider gone quiet: blocks until abort() tears the request down,
        the same way the interrupt path unblocks a dead socket read."""

        def __init__(self):
            self.aborted = threading.Event()

        def abort(self):
            self.aborted.set()

        def complete(self, spec, messages, tools=None, **kwargs):
            if not self.aborted.wait(timeout=5):
                raise AssertionError("the watchdog never fired")
            raise RuntimeError("aborted by the watchdog")

    critic = Critic(HungClient(), SimpleNamespace(spec="fake:vision"))
    with pytest.raises(RuntimeError, match="watchdog"):
        critic._complete([Message(role="user", content="x")])


class _SlowWireClient(OpenAICompatClient):
    """The real wire client's abort flag (sticky by design), with a complete()
    that outlives the critic's cap and then answers normally — the live plan
    critique that ran 30.3s against the 30s watchdog."""

    def __init__(self, hold: float, interrupt_first: bool = False):
        super().__init__()
        self.hold = hold
        self.interrupt_first = interrupt_first
        self.reason_mid_call: str | None = None

    def complete(self, spec, messages, tools=None, **kwargs):
        if self.interrupt_first:
            self.abort()  # what serve.on_interrupt does: reason "user"
        time.sleep(self.hold)
        self.reason_mid_call = self.abort_reason
        return LLMResponse(message=Message(role="assistant", content="fix the hero"),
                           usage=Usage(1, 1), stop_reason="stop", model=spec.spec)


def test_a_late_critic_watchdog_does_not_leave_the_shared_client_aborted(monkeypatch):
    """The critic shares the designer's client. A watchdog that fired against a
    call that then completed must not leave the flag stuck: the designer's
    next request would die as "aborted" and the page would report an
    interrupt nobody made."""
    monkeypatch.setattr(critique_mod, "CRITIC_TIMEOUT", 0.05)
    client = _SlowWireClient(hold=0.3)
    try:
        critic = Critic(client, SimpleNamespace(spec="fake:vision"))
        note = critic._complete([Message(role="user", content="x")])
        assert note == "fix the hero"
        assert client.reason_mid_call == "critic"  # it did fire, under its own name
        assert client.aborted is False
        assert client.abort_reason is None
    finally:
        client.close()


def test_a_user_interrupt_during_the_critique_stays_stuck(monkeypatch):
    """The other direction: a real interrupt landing mid-critique owns the
    flag. The watchdog neither overwrites its reason nor clears it — the
    designer's turn must still stop as the user asked."""
    monkeypatch.setattr(critique_mod, "CRITIC_TIMEOUT", 0.05)
    client = _SlowWireClient(hold=0.3, interrupt_first=True)
    try:
        critic = Critic(client, SimpleNamespace(spec="fake:vision"))
        critic._complete([Message(role="user", content="x")])
        assert client.reason_mid_call == "user"
        assert client.aborted is True
        assert client.abort_reason == "user"
    finally:
        client.close()


# ------------------------------------------------------------------- tools

def test_design_plan_stores_the_plan_and_returns_the_critique(ctx):
    session = ctx.design
    session.critic = FakeCritic("name the signature element")
    result = DesignPlanTool().run({"plan": "editorial direction, one marquee"}, ctx)
    assert session.state.plan == "editorial direction, one marquee"
    assert "name the signature element" in result.output
    assert result.details["critique"] == "name the signature element"


def test_design_plan_stores_even_without_a_critic(ctx):
    session = ctx.design
    result = DesignPlanTool().run({"plan": "editorial"}, ctx)
    assert session.state.plan == "editorial"
    assert "not critiqued" in result.output
    assert result.details["critique"] is None


def test_design_plan_survives_a_critic_that_fails(ctx):
    session = ctx.design
    session.critic = FakeCritic(fail="provider down")
    result = DesignPlanTool().run({"plan": "editorial"}, ctx)
    assert session.state.plan == "editorial", "the plan's value does not depend on the critic"
    assert "provider down" in result.output


def test_design_plan_needs_plan_text(ctx):
    with pytest.raises(ToolError, match="needs the plan text"):
        DesignPlanTool().run({"plan": "  "}, ctx)


def test_design_plan_is_refused_after_finalize(ctx):
    session = ctx.design
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    session.finalize(session.state.selected)
    with pytest.raises(ToolError, match="read-only"):
        DesignPlanTool().run({"plan": "x"}, ctx)


def test_design_critique_without_a_critic_names_what_is_missing(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    with pytest.raises(ToolError, match="no vision model"):
        DesignCritiqueTool().run({}, ctx)


def test_design_critique_returns_the_fix_list_and_records_it(ctx):
    session = ctx.design
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    aid = session.state.selected
    session.critic = FakeCritic("the hero is slop")
    session.capture_hook = _answer_with_png(session)
    result = DesignCritiqueTool().run({}, ctx)
    assert "the hero is slop" in result.output
    assert result.details["critique"] == "the hero is slop"
    assert session.state.critique_for(aid, session.state.artboards[aid].current)


def test_design_critique_surfaces_a_capture_failure_as_an_error(ctx):
    session = ctx.design
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    session.critic = FakeCritic()  # no capture_hook: the explicit path refuses
    with pytest.raises(ToolError, match="no workbench"):
        DesignCritiqueTool().run({}, ctx)


def test_create_carries_the_auto_critique_in_its_result(ctx):
    session = ctx.design
    session.critic = FakeCritic("the hero is slop")
    session.capture_hook = _answer_with_png(session)
    result = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    assert "the hero is slop" in result.output
    assert result.details["critique"] == "the hero is slop"


def test_create_without_a_critic_says_not_critiqued(ctx):
    result = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    assert "not critiqued" in result.output
    assert "no vision model" in result.details["critique"]


def test_the_new_tools_wait_behind_the_intake_gate():
    s = DesignSession(intake_gate=True)
    s.start("a landing page")
    c = ToolContext(repo_root=Path("."))
    c.design = s
    with pytest.raises(ToolError, match="has not answered"):
        DesignPlanTool().run({"plan": "x"}, c)
    with pytest.raises(ToolError, match="has not answered"):
        DesignCritiqueTool().run({}, c)


# ------------------------------------------------------------------- state

def test_plan_and_critiques_survive_a_state_round_trip():
    st = DesignState()
    st.plan = "editorial"
    st.record_critique("hero", "v1", "slop", model="fake:vision")
    st2 = DesignState.from_dict(st.to_dict())
    assert st2.plan == "editorial"
    assert st2.critique_for("hero", "v1")["model"] == "fake:vision"
    assert st2.critique_for("hero", "v1")["critique"] == "slop"


def test_latest_critique_prefers_the_current_version_then_the_newest():
    st = DesignState()
    ab = Artboard(id="hero", title="Hero")
    ab.versions = [Version(id="v1", kind="generated", html_path="x"),
                   Version(id="v2", kind="edited", html_path="y")]
    st.artboards["hero"] = ab
    st.critiques["hero:v1"] = {"critique": "old", "at": "2026-01-01T00:00:00+00:00", "model": "m"}
    st.critiques["hero:v2"] = {"critique": "new", "at": "2026-01-02T00:00:00+00:00", "model": "m"}
    ab.current = "v2"
    assert st.latest_critique("hero")["critique"] == "new"
    ab.current = "v1"
    assert st.latest_critique("hero")["critique"] == "old"
    ab.current = "v3"  # a version with no critique: the newest wins
    assert st.latest_critique("hero")["critique"] == "new"
    assert st.latest_critique("missing") is None

# ------------------------------------------------ the rubric, after the polish

class QuestionCritic(FakeCritic):
    """A critic that takes the user's question, the way the real one does."""

    def critique_render(self, session, artboard_id: str, png: str, question=None) -> str:
        self.calls.append((artboard_id, png, question))
        if self.fail:
            raise RuntimeError(self.fail)
        return self.note


def test_never_list_is_the_never_sections_and_not_the_whole_skill():
    body = never_list()
    assert body.startswith("## Never")
    assert "terracotta" in body  # the second generation rides under it
    assert "Announce" not in body
    assert "## Instead" not in body and "## Theme discipline" not in body


def test_render_rubric_judges_the_theme_first_and_names_the_escape():
    rubric = critique_mod._render_rubric(
        never="- Inter as the identity font", theme_brief="# Theme: linear-app",
        plan="", outline="", artboard="x")
    assert rubric.index("<the-active-design-system>") < rubric.index("<the-never-list>")
    assert "1. The active design system" in rubric
    assert critique_mod.THEME_WINS in rubric
    assert "Motion budget" not in rubric


def test_render_rubric_puts_the_users_question_first_and_budgets_a_polish():
    rubric = critique_mod._render_rubric(
        never="", theme_brief="", plan="", outline="", artboard="x",
        question="is the hero too big?", polished=True)
    assert rubric.index("is the hero too big?") < rubric.index("Reply with a concrete fix-list")
    assert "Motion budget" in rubric and "600 ms" in rubric and "prefers-reduced-motion" in rubric


def test_plan_rubric_judges_the_theme_first_too():
    rubric = critique_mod._plan_rubric(never="N", theme_brief="T", plan="P")
    assert rubric.index("<the-active-design-system>") < rubric.index("<the-never-list>")
    assert critique_mod.THEME_WINS in rubric


def test_critique_render_forwards_the_question_and_the_polished_flag(session, tmp_path):
    session.add_artboard(DOC, "Hero")
    aid = session.state.selected
    client = FakeVisionClient("fine")
    critic = Critic(client, SimpleNamespace(spec="fake:vision"), run_dir=tmp_path)
    critic.critique_render(session, aid, PNG, question="does the hero fit?")
    text = client.calls[0][1][0].content[0].text
    assert "does the hero fit?" in text and "Motion budget" not in text
    session.showcase(aid)
    session.write_version(aid, session.read_html(aid), "polished", note="polish")
    critic.critique_render(session, aid, PNG)
    assert "Motion budget" in client.calls[1][1][0].content[0].text


def test_critique_now_forwards_a_question_only_when_one_was_asked(session):
    session.add_artboard(DOC, "Hero")
    aid = session.state.selected
    session.capture_hook = _answer_with_png(session)
    fake = session.critic = QuestionCritic("hero is centred")
    session.critique_now(aid)
    assert fake.calls[-1][2] is None
    session.critique_now(aid, question="is it centred?")
    assert fake.calls[-1][2] == "is it centred?"


def test_design_critique_tool_passes_the_users_question(ctx):
    session = ctx.design
    session.add_artboard(DOC, "Hero")
    session.capture_hook = _answer_with_png(session)
    fake = session.critic = QuestionCritic("yes, the hero is centred")
    result = DesignCritiqueTool().run({"question": "is the hero centred?"}, ctx)
    assert fake.calls[-1][2] == "is the hero centred?"
    assert "yes, the hero is centred" in result.output
    assert "look" in DesignCritiqueTool().description


def test_plan_critique_lands_in_state_for_the_page(ctx):
    session = ctx.design
    events: list[dict] = []
    session.on_state = events.append
    session.critic = FakeCritic("name the signature element")
    DesignPlanTool().run({"plan": "editorial direction, one marquee"}, ctx)
    assert session.state.plan_critique == "name the signature element"
    assert events[-1]["plan_critique"] == "name the signature element"


def test_theme_brief_is_the_digest_not_the_whole_brief(session):
    session.set_theme("apple")
    brief = critique_mod._theme_brief(session)
    assert brief.startswith("# Theme: apple")
    assert len(brief) < 4000  # the full DESIGN.md is ~18k chars


def test_long_documents_get_the_compact_outline(session):
    deep = "".join(
        f'<section class="s{i}"><h2>Title {i}</h2><div><div><div><div><span>x</span></div></div></div></div></section>'
        for i in range(40))
    session.add_artboard(f"<html><head><title>t</title></head><body>{deep}</body></html>", "Long")
    outline = critique_mod._outline_lines(session, session.state.selected)
    assert "span" not in outline  # depth 7, not a cited tag
    assert outline.count("h2") == 40  # headings survive at any depth
    assert "section.s39" in outline
    short = DesignSession()
    short.start("x")
    short.add_artboard(DOC, "Short")
    assert "h1" in critique_mod._outline_lines(short, "short")
