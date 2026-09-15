"""The design process skills: design-craft and design-review ship as builtins,
and the design harness's instructions wire them into the flow.

The loader tests read the real packaged files (the same policy
test_design_themes.py applies to themes: a builtin that loses its body fails
here, not on a user's screen). The instructions assertions read the file via
the registry's instructions_path, the same path the runner feeds the model.
"""

from __future__ import annotations

from pathlib import Path

from bird.harnesses.design.run import run_design_interactive
from bird.harnesses.registry import get
from bird.skills import load_skills

CRAFT = "design-craft"
REVIEW = "design-review"


def _builtin(name: str):
    skills = load_skills(Path("."))
    return next((s for s in skills if s.name == name and s.source == "builtin"), None)


def test_design_craft_skill_loads():
    sk = _builtin(CRAFT)
    assert sk is not None, f"{CRAFT} not found among builtin skills"
    assert sk.description.startswith("Use when")
    assert sk.body.strip()


def test_design_review_skill_loads():
    sk = _builtin(REVIEW)
    assert sk is not None, f"{REVIEW} not found among builtin skills"
    assert sk.description.startswith("Use when")
    assert sk.body.strip()


def test_design_craft_body_carries_the_anti_slop_rules():
    body = _builtin(CRAFT).body
    # the banned list and the wireframe exception are the load-bearing parts
    assert "system-ui" in body
    assert "Learn more" in body
    assert "wireframe" in body
    # framework-default accents — what a model reaches for when it picks a
    # color itself; the ban names the hexes so the critic can check a render
    assert "#6366f1" in body
    assert "#3B82F6" in body
    # and the escape hatch: the theme wins, so a purple-accent theme is not
    # slop — the bans bind only on colors the model chose
    assert "Do-nots win" in body
    # the critic reads the never-list by these headings (critique.never_list)
    assert "## Never (reads as AI-slop)" in body
    assert "### The second generation" in body
    assert "linear-app" in body


def test_design_craft_body_carries_the_second_generation_never_list():
    """The defaults AI output reached for after the first list landed. The
    critic reads this same file as its rubric, so a refresh that lands here
    lands in the critique too — one list, two readers."""
    body = _builtin(CRAFT).body
    assert "terracotta" in body
    assert "acid-green" in body
    assert "hairline" in body
    assert "kicker" in body
    assert "Drop caps" in body


def test_design_review_body_carries_the_gate():
    body = _builtin(REVIEW).body
    assert "Visual hierarchy" in body
    assert "Innovation" in body
    assert "design_finalize" in body


def test_design_review_body_carries_the_subtraction_pass():
    body = _builtin(REVIEW).body
    assert "subtraction" in body.lower()
    # softened on 2026-09-04: name the decoration, remove it only if the page
    # reads the same without it — the user edits this document too
    assert "name the one decoration" in body
    assert "only if the page reads the same" in body


def test_design_instructions_wire_both_skills_and_the_answered_fidelity():
    text = get("design").instructions_path.read_text(encoding="utf-8")
    assert CRAFT in text
    assert REVIEW in text
    # The fidelity is no longer the model's question to ask: the workbench puts
    # it to the user as a picker before the session starts (design/intake.py)
    # and states the answer in the first message. The instructions have to say
    # so, or the designer opens by asking something already answered.
    assert "Direction: <Wireframe|High-fidelity>" in text
    assert "never re-ask them" in text
    assert "unspecified" in text, "and what the designer's-choice answer means"


def test_the_design_session_wiring_loads_the_skill_index(tmp_path, monkeypatch):
    """Wiring-level: run_design_interactive builds its own ToolContext, and it
    used to skip `skills` — the designer's skill tool then answered
    {"available": []} for design-craft and the whole session ran without the
    house style (the live session whose plan critic caught what the skill
    would have prevented). The ctx handed to build_runner must carry the
    loaded index, the same way cli._make_runner does for code/lead."""
    from types import SimpleNamespace

    from bird.harnesses.design import run as run_mod
    from bird.llm.registry import ProviderConfig, Registry

    registry = Registry(
        providers={"fake": ProviderConfig(name="fake", base_url="http://x")},
        models={}, aliases={"designer": "fake:model"},
    )
    captured: dict = {}

    class FakeTransport:
        url = "http://127.0.0.1:9999/"

        def __init__(self, **kwargs):
            pass

        def emit(self, event):
            pass

        def shutdown(self):
            pass

    class FakeServer:
        def __init__(self, repl, transport=None, broker=None):
            pass

        def on_user_input(self, text):
            pass

        def run(self):
            pass

    def fake_build(_harness, **kw):
        captured["ctx"] = kw["ctx"]
        return SimpleNamespace(ctx=kw["ctx"])

    monkeypatch.setattr("bird.http_transport.HttpTransport", FakeTransport)
    monkeypatch.setattr("bird.serve.Server", FakeServer)
    monkeypatch.setattr("bird.repl.Repl", lambda *a, **k: object())
    monkeypatch.setattr(run_mod, "build_runner", fake_build)
    monkeypatch.setattr("time.sleep", lambda _s: None)  # the SSE drain wait

    run_design_interactive(
        repo_root=tmp_path, prompt="a landing page", registry=registry,
        client=None, run_dir=tmp_path / "design-run", no_open=True,
    )
    names = {s.name for s in (captured["ctx"].skills or [])}
    assert CRAFT in names, "design-craft must resolve in a design session"
    assert REVIEW in names, "design-review must resolve in a design session"


def test_the_skill_tool_inside_a_design_session_lists_and_loads_craft(tmp_path):
    """The toolset half of the wiring: with the ctx the design session now
    builds, the skill tool in design_edit_tools returns the full body on a
    hit — not the {"available": []} the live session got — and lists the
    builtins on a miss."""
    from bird.harnesses.design.tools import design_edit_tools
    from bird.tools import ToolContext
    from bird.tools.skill import SkillTool

    ctx = ToolContext(repo_root=tmp_path, skills=load_skills(tmp_path))
    skill = next(t for t in design_edit_tools() if t.name == "skill")

    loaded = skill.execute({"name": CRAFT}, ctx)
    assert not loaded.is_error
    assert loaded.output.strip(), "the full body, not an empty available list"
    assert loaded.details["source"] == "builtin"

    loaded = skill.execute({"name": REVIEW}, ctx)
    assert not loaded.is_error and loaded.details["source"] == "builtin"

    miss = skill.execute({"name": "no-such-skill"}, ctx)
    assert miss.is_error
    assert CRAFT in miss.output, "the helpful miss lists what IS available"