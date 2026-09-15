"""The lead's `design` dispatch tool: routes visual/UI work to the design
harness's Workbench and hands the finalized DESIGN.md bundle to `code` via
ctx.last_bundle — the same lateral seam architect->code uses."""

import pytest

from bird.harnesses.design.session import DesignSession
from bird.harnesses.lead import lead_harness_tools
from bird.harnesses.lead.tools import DesignTool
from bird.llm.registry import ProviderConfig, Registry
from bird.tools import ToolContext, ToolError

REG = Registry(
    providers={"fake": ProviderConfig(name="fake", base_url="http://x")},
    models={},
    aliases={"default": "fake:model", "architect": "fake:model"},
)

HERO = "<html><body><h1>Hello</h1></body></html>"


def _ctx(tmp_path, **kw):
    return ToolContext(repo_root=tmp_path, registry=REG, run_dir=tmp_path, **kw)


# ---------------------------------------------------------------- toolset

def test_lead_toolset_contains_design():
    names = {t.name for t in lead_harness_tools(with_kg=False, with_web=False)}
    assert {"architect", "design", "code"} <= names


# ----------------------------------------------------------- refusal path

def test_design_tool_refuses_when_the_user_never_finalized(tmp_path, monkeypatch):
    """Closing the page without finalize is the user declining — the lead must
    not read that as a finished design and must not send anything to code."""
    def fake(*, run_dir, **kw):
        s = DesignSession()  # memory-only, run_dir=None
        s.start("a landing page for a podcast")
        s.add_artboard(HERO, "Hero")
        return s  # never finalized

    monkeypatch.setattr("bird.harnesses.design.run.run_design_interactive", fake)
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolError, match="Do NOT proceed to code") as excinfo:
        DesignTool().run({"task": "a landing page for a podcast"}, ctx)
    assert ctx.last_bundle is None  # nothing to hand to the builder
    # the refusal says what was left on the board and how to get back to it:
    # the old "they may have closed the page" guessed, and its "reopen" had
    # nothing behind it
    text = str(excinfo.value)
    assert "1 artboard(s)" in text and "page was closed" in text
    assert 'resume="' in text and str(tmp_path) in text


def test_design_tool_reopens_a_saved_session_when_asked(tmp_path, monkeypatch):
    """`resume` names an earlier run dir; the tool hands it through and runs
    in THAT dir instead of minting a new one."""
    old = tmp_path / "design-old"
    old.mkdir()
    (old / "design_state.json").write_text("{}", encoding="utf-8")
    seen = {}

    def fake(*, run_dir, resume=None, **kw):
        seen["run_dir"], seen["resume"] = run_dir, resume
        s = DesignSession()
        s.start("a landing page for a podcast")
        return s

    monkeypatch.setattr("bird.harnesses.design.run.run_design_interactive", fake)
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolError, match="Do NOT proceed to code"):
        DesignTool().run({"task": "a landing page for a podcast", "resume": str(old)}, ctx)
    assert seen == {"run_dir": old, "resume": old}


def test_design_tool_refuses_a_resume_dir_with_no_board(tmp_path):
    with pytest.raises(ToolError, match="nothing to resume"):
        DesignTool().run({"task": "x", "resume": str(tmp_path / "nope")}, _ctx(tmp_path))


# -------------------------------------------------------------- happy path

def test_design_tool_stashes_the_bundle_and_tells_the_lead_to_call_code(tmp_path, monkeypatch):
    """The fake stands in for the browser and nothing else: the session is
    real and run_dir-backed, so finalize writes the bundle the way it will in
    production. Handing the writer to the test is how the seam went unnoticed
    while `read_text` had no file to open."""
    seen = {}

    def fake(*, run_dir, prompt, broker=None, **kw):
        seen["prompt"] = prompt
        seen["broker"] = broker
        seen["kw"] = kw
        s = DesignSession(run_dir=run_dir)
        s.start(prompt)
        s.add_artboard(HERO, "Hero")
        s.finalize("hero")  # writes bundle/DESIGN.md, as the real one does
        return s

    monkeypatch.setattr("bird.harnesses.design.run.run_design_interactive", fake)
    events = []
    sentinel = object()
    ctx = ToolContext(repo_root=tmp_path, registry=REG, run_dir=tmp_path,
                      broker=sentinel, record=lambda t, d: events.append((t, d)))
    res = DesignTool().run({"task": "a landing page for a podcast"}, ctx)

    # the design harness's contract: prompt= (not task=), no store, the lead's broker
    assert seen["prompt"] == "a landing page for a podcast"
    assert "store" not in seen["kw"]
    assert seen["broker"] is sentinel
    # the dispatch went out before the session ran
    dispatches = [d for t, d in events if t == "dispatch"]
    assert dispatches and dispatches[0]["harness"] == "design"
    # the receipt tells the lead what to do next
    assert "Now call `code`" in res.output
    assert res.details["harness"] == "design"
    assert res.details["finalized"] is True
    assert res.details["artboard"] == "hero"
    # the stash is the raw DESIGN.md — no arch seed header wrapped around it
    assert ctx.last_bundle.startswith("# Design — Hero")
    assert HERO in ctx.last_bundle
    assert "Architecture handoff" not in ctx.last_bundle


def test_design_tool_refuses_when_finalize_left_no_bundle(tmp_path, monkeypatch):
    """A finalized session with nothing on disk is a wiring failure, not a
    design. Say so instead of dying on the read."""
    def fake(*, run_dir, prompt, **kw):
        s = DesignSession()  # memory-only: finalize writes no bundle
        s.start(prompt)
        s.add_artboard(HERO, "Hero")
        s.finalize("hero")
        return s

    monkeypatch.setattr("bird.harnesses.design.run.run_design_interactive", fake)
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolError, match="no bundle was written"):
        DesignTool().run({"task": "a landing page"}, ctx)
    assert ctx.last_bundle is None