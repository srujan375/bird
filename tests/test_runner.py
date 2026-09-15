import json
import threading
import time

import pytest

from bird.engine.runner import READONLY_CALL_TOTAL_CAP, Runner
from bird.llm.registry import ModelSpec, ProviderConfig, Registry
from bird.llm.types import LLMResponse, Message, ToolCall, Usage
from bird.llm.wire.openai_compat import WireAborted
from bird.harnesses.code import code_harness_tools
from bird.tools import Tool, ToolContext, ToolResult

SPEC = ModelSpec(
    spec="fake:model",
    provider=ProviderConfig(name="fake", base_url="http://x"),
    model="model",
    context_window=32768,
)
REGISTRY = Registry(providers={}, models={}, aliases={})


class FakeClient:
    """Returns scripted assistant messages in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None, on_thinking=None, **kwargs):
        self.calls += 1
        if on_delta is not None and self.script[0].content:
            on_delta(self.script[0].content)
            on_delta(None)
        msg = self.script.pop(0)
        return LLMResponse(message=msg, usage=Usage(100, 10), stop_reason="stop", model=spec.spec)


def tc(name, args, id="c1"):
    j = json.dumps(args) if isinstance(args, dict) else args
    return ToolCall.from_raw(id, name, j)


def assistant(content=None, calls=()):
    return Message(role="assistant", content=content, tool_calls=list(calls))


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n")
    return tmp_path


@pytest.fixture
def make_runner(repo):
    def _make(script, **kw):
        events = []
        ctx = ToolContext(repo_root=repo, record=lambda t, d: events.append((t, d)))
        r = Runner(
            spec=SPEC,
            client=FakeClient(script),
            registry=REGISTRY,
            tools=code_harness_tools(with_kg=False),
            ctx=ctx,
            **kw,
        )
        r.events = events
        return r

    return _make


def test_happy_path_read_then_done(make_runner):
    r = make_runner([
        assistant(calls=[tc("read", {"path": "f.py"})]),
        assistant(calls=[tc("done", {"summary": "read it"})]),
    ])
    result = r.run("read f.py")
    assert result.status == "done"
    assert result.summary == "read it"
    assert result.turns == 2
    assert result.usage.input_tokens == 200


def test_invalid_call_gets_helpful_error_then_recovers(make_runner):
    r = make_runner([
        assistant(calls=[tc("read", {"path": 123})]),  # path must be a string
        assistant(calls=[tc("read", {"path": "f.py"})]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    result = r.run("go")
    assert result.status == "done"
    types = [t for t, _ in r.events]
    assert "invalid_tool_call" in types


def test_invalid_calls_exhaust_retries_abort(make_runner):
    bad = lambda i: assistant(calls=[tc("read", {"path": 123}, id=f"c{i}")])
    r = make_runner([bad(1), bad(2), bad(3)])
    result = r.run("go")
    assert result.status == "aborted_invalid_tool"
    assert result.turns == 3  # initial + 2 retries


def test_text_only_streak_aborts(make_runner):
    r = make_runner([
        assistant(content="thinking..."),
        assistant(content="still thinking..."),
        assistant(content="hmm..."),
    ])
    result = r.run("go")
    assert result.status == "aborted_stuck"
    assert "without a tool call" in result.summary


def test_repeated_message_aborts(make_runner):
    same = assistant(calls=[tc("read", {"path": "f.py"})])
    r = make_runner([same, same])
    result = r.run("go")
    assert result.status == "aborted_stuck"
    assert "repeated" in result.summary


def test_same_tool_loop_aborts(make_runner):
    # identical call each turn but different ids so messages aren't verbatim-equal
    r = make_runner([
        assistant(calls=[tc("read", {"path": "f.py"}, id=f"c{i}")]) for i in range(1, 4)
    ])
    result = r.run("go")
    assert result.status == "aborted_stuck"
    assert "repeated 3x" in result.summary


def test_max_turns(make_runner, repo):
    script = []
    for i in range(10):
        path = f"g{i}.py"
        (repo / path).write_text("y = 2\n")
        script.append(assistant(calls=[tc("read", {"path": path}, id=f"c{i}")]))
    r = make_runner(script, max_turns=5)
    result = r.run("go")
    assert result.status == "max_turns"
    assert result.turns == 5


def test_unknown_tool_is_validation_error(make_runner):
    r = make_runner([
        assistant(calls=[tc("frobnicate", {"pattern": "x"})]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    result = r.run("go")
    assert result.status == "done"
    # the model saw a helpful error naming available tools
    err_events = [d for t, d in r.events if t == "invalid_tool_call"]
    assert err_events and "Unknown tool" in err_events[0]["error"]


def test_duplicate_read_returns_note_not_content(make_runner):
    r = make_runner([
        assistant(calls=[tc("read", {"path": "f.py"}, id="c1")]),
        assistant(calls=[tc("read", {"path": "f.py"}, id="c2")]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    result = r.run("go")
    assert result.status == "done"
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert tool_msgs[0].content == "x = 1\n"
    assert "already in the conversation above" in tool_msgs[1].content
    assert any(t == "read_deduped" for t, _ in r.events)


def test_reread_after_change_returns_content(make_runner, repo):
    r = make_runner([
        assistant(calls=[tc("read", {"path": "f.py"}, id="c1")]),
        assistant(calls=[tc("write", {"path": "f.py", "content": "x = 2\n"}, id="c2")]),
        assistant(calls=[tc("read", {"path": "f.py"}, id="c3")]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    result = r.run("go")
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert tool_msgs[2].content == "x = 2\n"  # changed file → full content again
    assert not any(t == "read_deduped" for t, _ in r.events)


def test_repeated_readonly_calls_abort(make_runner, repo):
    (repo / "g.py").write_text("y = 2\n")
    # alternate two reads so neither the consecutive same-call guard nor the
    # verbatim-repeat guard fires; the cumulative cap must catch the spin
    script = []
    for i in range(14):
        path = "f.py" if i % 2 == 0 else "g.py"
        script.append(assistant(calls=[tc("read", {"path": path}, id=f"c{i}")]))
    r = make_runner(script)
    result = r.run("go")
    assert result.status == "aborted_stuck"
    assert "read-only call repeated" in result.summary
    assert any(t == "abort" and d["reason"] == "repeated_readonly_call" for t, d in r.events)


def test_explore_nudge_injected(make_runner, repo):
    script = []
    for i in range(7):
        path = f"e{i}.py"
        (repo / path).write_text("z = 3\n")
        script.append(assistant(calls=[tc("read", {"path": path}, id=f"c{i}")]))
    script.append(assistant(calls=[tc("done", {"summary": "ok"})]))
    r = make_runner(script)
    result = r.run("go")
    assert result.status == "done"
    assert any(t == "explore_nudge" for t, _ in r.events)
    assert any(
        m.role == "user" and m.content and "turns of reading" in m.content
        for m in result.messages
    )


def test_write_resets_explore_streak(make_runner, repo):
    script = []
    for i in range(5):
        path = f"w{i}.py"
        (repo / path).write_text("z = 3\n")
        script.append(assistant(calls=[tc("read", {"path": path}, id=f"c{i}")]))
    script.append(assistant(calls=[tc("write", {"path": "new.py", "content": "n = 1\n"}, id="cw")]))
    script.append(assistant(calls=[tc("done", {"summary": "ok"})]))
    r = make_runner(script)
    result = r.run("go")
    assert result.status == "done"
    assert not any(t == "explore_nudge" for t, _ in r.events)


def test_system_prompt_grounds_repo_root(make_runner, repo):
    r = make_runner([assistant(calls=[tc("done", {"summary": "ok"})])])
    result = r.run("go")
    system = result.messages[0]
    assert system.role == "system"
    assert str(repo) in system.content
    assert "f.py" in system.content  # shallow-tree fallback when no KG


# --- project-level custom instructions (.bird/instructions.md / CLAUDE.md) ---

def _system_prompt(make_runner, repo):
    """Run a trivial task and return the assembled system prompt string."""
    r = make_runner([assistant(calls=[tc("done", {"summary": "ok"})])])
    result = r.run("go")
    return result.messages[0].content


def test_bird_instructions_injected(make_runner, repo):
    (repo / ".bird").mkdir()
    (repo / ".bird" / "instructions.md").write_text("PROJECT-RULE: use tabs not spaces")
    prompt = _system_prompt(make_runner, repo)
    assert "PROJECT-RULE: use tabs not spaces" in prompt


def test_claude_md_injected_when_no_bird_instructions(make_runner, repo):
    (repo / "CLAUDE.md").write_text("PROJECT-RULE: prefer composition over inheritance")
    prompt = _system_prompt(make_runner, repo)
    assert "PROJECT-RULE: prefer composition over inheritance" in prompt


def test_bird_instructions_takes_precedence_over_claude_md(make_runner, repo):
    (repo / ".bird").mkdir()
    (repo / ".bird" / "instructions.md").write_text("BIRD-WINS")
    (repo / "CLAUDE.md").write_text("CLAUDE-LOSES")
    prompt = _system_prompt(make_runner, repo)
    assert "BIRD-WINS" in prompt
    assert "CLAUDE-LOSES" not in prompt


def test_no_project_instructions_leaves_prompt_unchanged(make_runner, repo):
    prompt = _system_prompt(make_runner, repo)
    # no extra blank part is added — the harness instructions and repo-root
    # block are still present, and there's no stray double-newline artifact
    assert "Repository root:" in prompt
    assert str(repo) in prompt


def test_project_instructions_truncated_past_8kb(make_runner, repo):
    (repo / "CLAUDE.md").write_text("X" * 9000)
    prompt = _system_prompt(make_runner, repo)
    assert "[project instructions truncated — file exceeded 8KB limit]" in prompt
    # the body is capped near 8KB, not the full 9000 chars
    assert prompt.count("X") < 9000


def test_project_instructions_after_harness_before_repo_root(make_runner, repo):
    (repo / "CLAUDE.md").write_text("PROJECT-MARKER")
    prompt = _system_prompt(make_runner, repo)
    harness_idx = prompt.index("All tool paths are relative to this root.")
    # the harness instructions block ends just before the project marker block;
    # the project marker must come before the repo-root notice
    project_idx = prompt.index("PROJECT-MARKER")
    repo_root_idx = prompt.index("Repository root:")
    assert project_idx < harness_idx
    assert project_idx < repo_root_idx


# --- @path import resolution in project instructions ---

def test_import_inlines_claude_md(make_runner, repo):
    (repo / ".bird").mkdir()
    (repo / ".bird" / "instructions.md").write_text("Top.\n@CLAUDE.md\nBottom.\n")
    (repo / "CLAUDE.md").write_text("IMPORTED-RULE: prefer composition")
    prompt = _system_prompt(make_runner, repo)
    assert "Top." in prompt
    assert "Bottom." in prompt
    assert "IMPORTED-RULE: prefer composition" in prompt
    # the @CLAUDE.md directive line itself is gone, replaced by the content
    assert "@CLAUDE.md" not in prompt


def test_import_resolves_nested_path_relative_to_repo_root(make_runner, repo):
    (repo / ".bird").mkdir()
    (repo / "docs").mkdir()
    (repo / ".bird" / "instructions.md").write_text("Intro\n@docs/standards.md\n")
    (repo / "docs" / "standards.md").write_text("STANDARD: no tabs in markdown")
    prompt = _system_prompt(make_runner, repo)
    assert "STANDARD: no tabs in markdown" in prompt
    assert "@docs/standards.md" not in prompt


def test_import_missing_file_replaced_with_comment(make_runner, repo):
    (repo / ".bird").mkdir()
    (repo / ".bird" / "instructions.md").write_text("Intro\n@does/not/exist.md\nOutro\n")
    prompt = _system_prompt(make_runner, repo)
    assert "<!-- import not found: @does/not/exist.md -->" in prompt
    assert "@does/not/exist.md" not in prompt.replace(
        "<!-- import not found: @does/not/exist.md -->", ""
    )
    # no crash — the surrounding text is still present
    assert "Intro" in prompt
    assert "Outro" in prompt


def test_import_pushing_over_8kb_is_truncated(make_runner, repo):
    (repo / ".bird").mkdir()
    # the top-level file is small, but the imported file is huge — the cap
    # applies AFTER import resolution, so the combined content is truncated
    (repo / ".bird" / "instructions.md").write_text("header\n@big.md\n")
    (repo / "big.md").write_text("Y" * 9000)
    prompt = _system_prompt(make_runner, repo)
    assert "[project instructions truncated — file exceeded 8KB limit]" in prompt
    assert prompt.count("Y") < 9000


def test_import_is_one_level_only(make_runner, repo):
    (repo / ".bird").mkdir()
    # the imported file contains its own @line — it must NOT be resolved
    (repo / ".bird" / "instructions.md").write_text("Start\n@inner.md\n")
    (repo / "inner.md").write_text("INNER-TEXT\n@unresolved.md\n")
    prompt = _system_prompt(make_runner, repo)
    assert "INNER-TEXT" in prompt
    # the nested @line survives as literal text (one level only)
    assert "@unresolved.md" in prompt
    assert "<!-- import not found: @unresolved.md -->" not in prompt


def test_inline_at_in_prose_is_not_an_import(make_runner, repo):
    (repo / ".bird").mkdir()
    (repo / ".bird" / "instructions.md").write_text(
        "see @CLAUDE.md for details\n"
    )
    (repo / "CLAUDE.md").write_text("SHOULD-NOT-APPEAR")
    prompt = _system_prompt(make_runner, repo)
    # the prose line is left untouched; the @CLAUDE.md file is NOT inlined
    assert "see @CLAUDE.md for details" in prompt
    assert "SHOULD-NOT-APPEAR" not in prompt


class StubKG:
    """Duck-typed ctx.kg: always ready, answers every query."""

    def is_ready(self):
        return True

    def digest(self):
        return "[repo map]"

    def query(self, question, budget=2000):
        from bird.context.kg import KGQueryResult

        return KGQueryResult(text="NODE x [f.py:1]", hit_count=1)


@pytest.fixture
def make_kg_runner(repo):
    def _make(script, **kw):
        events = []
        ctx = ToolContext(repo_root=repo, kg=StubKG(), record=lambda t, d: events.append((t, d)))
        r = Runner(
            spec=SPEC,
            client=FakeClient(script),
            registry=REGISTRY,
            tools=code_harness_tools(with_kg=True),
            ctx=ctx,
            **kw,
        )
        r.events = events
        return r

    return _make


def test_kg_drift_nudge_after_repeated_bash_searches(make_kg_runner):
    r = make_kg_runner([
        assistant(calls=[tc("bash", {"command": f"grep -rn x{i} ."}, id=f"c{i}")])
        for i in range(3)
    ] + [assistant(calls=[tc("done", {"summary": "ok"})])])
    result = r.run("go")
    assert result.status == "done"
    assert any(t == "kg_drift_nudge" for t, _ in r.events)
    assert any(
        m.role == "user" and m.content and "shelling out to search" in m.content
        for m in result.messages
    )


def test_kg_query_resets_drift_counter(make_kg_runner):
    r = make_kg_runner([
        assistant(calls=[tc("bash", {"command": "grep -rn a ."}, id="c1")]),
        assistant(calls=[tc("bash", {"command": "rg b"}, id="c2")]),
        assistant(calls=[tc("kg_query", {"question": "where is x defined"}, id="c3")]),
        assistant(calls=[tc("bash", {"command": "grep -rn c ."}, id="c4")]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    result = r.run("go")
    assert result.status == "done"
    assert not any(t == "kg_drift_nudge" for t, _ in r.events)


def test_non_search_bash_does_not_count_as_drift(make_kg_runner):
    r = make_kg_runner([
        assistant(calls=[tc("bash", {"command": "pytest -q"}, id="c1")]),
        assistant(calls=[tc("bash", {"command": "git status"}, id="c2")]),
        assistant(calls=[tc("bash", {"command": "ls src"}, id="c3")]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    result = r.run("go")
    assert result.status == "done"
    assert not any(t == "kg_drift_nudge" for t, _ in r.events)


def test_no_drift_nudge_without_kg_tool(make_runner):
    # control arm: no kg_query tool → bash search is the only option, never nudge
    r = make_runner([
        assistant(calls=[tc("bash", {"command": f"grep -rn x{i} ."}, id=f"c{i}")])
        for i in range(4)
    ] + [assistant(calls=[tc("done", {"summary": "ok"})])])
    result = r.run("go")
    assert result.status == "done"
    assert not any(t == "kg_drift_nudge" for t, _ in r.events)


def test_repair_interrupted_answers_dangling_calls():
    from bird.engine.runner import repair_interrupted

    messages = [
        Message(role="user", content="go"),
        assistant(content="reading", calls=[tc("read", {"path": "a.py"}, id="c1"),
                                            tc("read", {"path": "b.py"}, id="c2")]),
        Message(role="tool", content="stuff", tool_call_id="c1"),
    ]
    repair_interrupted(messages)
    assert messages[-1].role == "tool"
    assert messages[-1].tool_call_id == "c2"
    assert "interrupted" in messages[-1].content
    # idempotent: nothing dangling now
    n = len(messages)
    repair_interrupted(messages)
    assert len(messages) == n


def test_repair_interrupted_noop_on_clean_transcript():
    from bird.engine.runner import repair_interrupted

    messages = [Message(role="user", content="go"), assistant(content="hi")]
    repair_interrupted(messages)
    assert len(messages) == 2


# ---------- harness tuning params (arch harness reuses the engine) ----------


def test_custom_tracker_pinned_once_and_refreshed(make_runner):
    """A non-plan tracker provider is pinned into the transcript each turn,
    with exactly one live copy (strip + re-append)."""
    r = make_runner(
        [assistant(content="hello"), assistant(content="again")],
        tracker=lambda ctx: "[arch tracker] phase: propose",
        tracker_prefix="[arch tracker",
    )
    messages = []
    r.chat(messages, "hi")
    assert sum(1 for m in messages if (m.content or "").startswith("[arch tracker")) == 1
    r.chat(messages, "more")
    assert sum(1 for m in messages if (m.content or "").startswith("[arch tracker")) == 1


def test_custom_mutating_tools_suppress_explore_nudge(repo, make_runner):
    """When the harness declares its own mutating tools, calls to them reset
    the explore streak — no bogus 'make an edit/write NOW' nudges."""
    for i in range(7):
        (repo / f"m{i}.py").write_text("y = 1\n")
    reads = [assistant(calls=[tc("read", {"path": f"m{i}.py"}, id=f"c{i}")]) for i in range(7)]
    script = reads + [assistant(calls=[tc("done", {"summary": "ok"}, id="cd")])]

    r = make_runner(list(script), mutating_tools={"read"}, explore_nudge="[system notice] {n} CUSTOM")
    result = r.run("look around")
    assert not any("CUSTOM" in (m.content or "") for m in result.messages)

    r2 = make_runner(list(script), explore_nudge="[system notice] {n} CUSTOM")
    result2 = r2.run("look around")
    assert any("CUSTOM" in (m.content or "") for m in result2.messages)


# --- verification ledger: the runner stamps it, `done` reads it ---

def _gated(make_runner, script):
    r = make_runner(script)
    r.ctx.require_verification = True
    return r


def test_done_is_blocked_until_a_check_passes(make_runner):
    """End to end through the runner: the model edits, calls done, gets told to
    run a check, runs it, and only then finishes."""
    script = [
        assistant(calls=[tc("edit", {"path": "f.py", "old_text": "x = 1", "new_text": "x = 2"}, id="c1")]),
        assistant(calls=[tc("done", {"summary": "changed x"}, id="c2")]),
        assistant(calls=[tc("bash", {"command": "python -m pytest --version"}, id="c3")]),
        assistant(calls=[tc("done", {"summary": "changed x"}, id="c4")]),
    ]
    r = _gated(make_runner, script)
    result = r.run("bump x")

    assert result.status == "done"
    assert any(t == "done_blocked_unverified" for t, _ in r.events)
    blocked = next(m for m in result.messages if m.tool_call_id == "c2")
    assert "f.py" in blocked.content
    assert r.ctx.unverified_paths == []  # the passing check cleared the ledger


def test_editing_after_a_green_check_reopens_the_gate(make_runner):
    script = [
        assistant(calls=[tc("edit", {"path": "f.py", "old_text": "x = 1", "new_text": "x = 2"}, id="c1")]),
        assistant(calls=[tc("bash", {"command": "python -m pytest --version"}, id="c2")]),
        assistant(calls=[tc("edit", {"path": "f.py", "old_text": "x = 2", "new_text": "x = 3"}, id="c3")]),
        assistant(calls=[tc("done", {"summary": "changed x"}, id="c4")]),
        assistant(calls=[tc("bash", {"command": "python -m pytest --version"}, id="c5")]),
        assistant(calls=[tc("done", {"summary": "changed x"}, id="c6")]),
    ]
    r = _gated(make_runner, script)
    result = r.run("bump x twice")

    assert result.status == "done"
    blocked = next(m for m in result.messages if m.tool_call_id == "c4")
    assert "ran BEFORE these edits" in blocked.content


def test_ungated_harness_keeps_the_old_done(make_runner):
    """require_verification off (lead, arch, library use) — unchanged behaviour."""
    script = [
        assistant(calls=[tc("edit", {"path": "f.py", "old_text": "x = 1", "new_text": "x = 2"}, id="c1")]),
        assistant(calls=[tc("done", {"summary": "changed x"}, id="c2")]),
    ]
    r = make_runner(script)
    assert r.run("bump x").status == "done"
    assert not any(t == "done_blocked_unverified" for t, _ in r.events)


# ---------- mid-turn input ----------


def test_pending_input_lands_at_the_step_boundary(make_runner):
    """Text typed mid-run is appended at the top of the NEXT step — after the
    previous step's tool results. Never between a tool_calls message and its
    results: that shape is one every provider rejects, which is why the drain
    cannot happen between individual calls."""
    script = [
        assistant(calls=[tc("read", {"path": "f.py"})]),
        assistant(content="switching to g.py"),
    ]
    r = make_runner(script)
    drains = [[], ["actually, use g.py"]]  # nothing typed yet at step 1
    r.ctx.pending_input = lambda: drains.pop(0) if drains else []

    messages = []
    result = r.chat(messages, "read f.py")
    assert result.status == "reply"

    ai = next(i for i, m in enumerate(messages) if m.role == "assistant" and m.tool_calls)
    assert messages[ai + 1].role == "tool", "injection split a tool_calls/result pair"
    ti = next(i for i, m in enumerate(messages) if m.role == "tool")
    ii = next(i for i, m in enumerate(messages) if m.role == "user" and "use g.py" in (m.content or ""))
    assert ii > ti
    assert any(t == "user_injected" and d["text"] == "actually, use g.py" for t, d in r.events)


def test_same_tool_loop_guard_fires_without_an_injection(make_runner):
    """Baseline for the test below: three identical calls in a row is the
    stuck-guard trip, and nothing rescues it."""
    script = [assistant(calls=[tc("read", {"path": "f.py"}, id=f"c{i}")]) for i in range(3)]
    script.append(assistant(content="never reached"))
    assert make_runner(script).chat([], "start").status == "aborted_stuck"


def test_injection_resets_the_stuck_guards(make_runner):
    """A course correction is not drift. The guards count repetition within one
    task; the task just changed, so carrying the counters across would abort the
    run the user was actively steering."""
    script = [assistant(calls=[tc("read", {"path": "f.py"}, id=f"c{i}")]) for i in range(3)]
    script.append(assistant(content="finished"))
    r = make_runner(script)
    drains = [[], [], ["different approach please"]]
    r.ctx.pending_input = lambda: drains.pop(0) if drains else []
    result = r.chat([], "start")
    assert result.status == "reply" and result.summary == "finished"


def test_no_turn_cap_by_default(make_runner):
    """The default loop is unbounded — a long job that keeps making progress is
    never cut off. Only an explicit ceiling can produce a max_turns result."""
    script = [assistant(calls=[tc("read", {"path": "f.py"}, id=f"c{i}")]) for i in range(50)]
    script.append(assistant(content="done at last"))
    r = make_runner(script)
    # vary the calls so the stuck guards stay quiet; the point is the cap
    for i, m in enumerate(script[:-1]):
        m.tool_calls[0].arguments_json = json.dumps({"path": "f.py", "_": i})
    assert r.max_turns is None
    result = r.chat([], "start")
    assert result.status == "reply" and result.turns == 51


def test_drift_guard_counts_the_grep_tool(make_runner):
    """The guard watched `bash` alone. bird ships a first-class `grep`, so a run
    that replaced kg_query with it drifted in total silence — one logged session
    made 19 grep/glob calls, zero kg_query calls, and fired zero nudges."""
    from types import SimpleNamespace

    script = [assistant(calls=[tc("grep", {"pattern": f"x{i}"}, id=f"g{i}")]) for i in range(3)]
    script.append(assistant(content="stopped looking"))
    r = make_runner(script)
    r.ctx.kg = SimpleNamespace(is_ready=lambda: True)
    r.tools["kg_query"] = object()  # presence is all the guard checks
    r.chat([], "start")
    assert any(t == "kg_drift_nudge" for t, _ in r.events)


def test_drift_guard_ignores_glob(make_runner):
    """`glob` answers filename questions, which the graph cannot — using it is
    not drift, and nudging toward kg_query there would be wrong advice."""
    from types import SimpleNamespace

    script = [assistant(calls=[tc("glob", {"pattern": f"**/*{i}*"}, id=f"g{i}")]) for i in range(3)]
    script.append(assistant(content="stopped looking"))
    r = make_runner(script)
    r.ctx.kg = SimpleNamespace(is_ready=lambda: True)
    r.tools["kg_query"] = object()
    r.chat([], "start")
    assert not any(t == "kg_drift_nudge" for t, _ in r.events)


# ---------- loop guards that exact-match checks miss ----------


def test_degenerate_repetition_aborts(make_runner):
    """The real collapse: one grep repeated ~200x inside a single bash command,
    corrupting as it went so no two turns matched. Every other guard compares
    whole messages or call signatures for EXACT equality, so all of them slid
    off it; only the shell parser tripping over an unbalanced quote stopped it."""
    seg = 'grep -n "import" src/branding.ts | head -3'
    collapsed = "; ".join([seg] * 40)
    r = make_runner([assistant(calls=[tc("bash", {"command": collapsed})])])
    result = r.run("go")
    assert result.status == "aborted_stuck"
    assert "collapsed into repetition" in result.summary
    assert any(t == "abort" and d.get("reason") == "degenerate_repetition" for t, d in r.events)


def test_a_long_legitimate_command_is_not_degenerate(make_runner):
    """Threshold has to clear real chained commands — a build script with a
    dozen DIFFERENT steps must not read as collapse."""
    cmd = "; ".join(f"echo step{i} && ls dir{i}" for i in range(20))
    r = make_runner([
        assistant(calls=[tc("bash", {"command": cmd})]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    assert r.run("go").status == "done"


def test_writing_a_document_with_code_blocks_is_not_degenerate(make_runner):
    """The false positive this guard shipped with: a markdown document opens and
    closes nine fenced code blocks, so `\u0060\u0060\u0060` is the most common segment nine
    times over, and a perfectly healthy `write` was aborted as collapse."""
    doc = "# Notes\n\n" + "".join(
        f"## Section {i}\n\nSome prose about section {i} here.\n\n"
        f"\u0060\u0060\u0060python\nvalue_{i} = compute_{i}(argument)\n\u0060\u0060\u0060\n\n"
        for i in range(9)
    )
    r = make_runner([
        assistant(calls=[tc("write", {"path": "notes.md", "content": doc})]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    assert r.run("go").status == "done"


def test_a_short_fragment_that_takes_over_the_argument_is_degenerate(make_runner):
    """The fragment-length floor must not open an escape hatch: a segment too
    short to be substantive still reads as collapse once it IS the argument."""
    collapsed = "; ".join(["ls -la"] * 60)
    r = make_runner([assistant(calls=[tc("bash", {"command": collapsed})])])
    result = r.run("go")
    assert result.status == "aborted_stuck"
    assert "collapsed into repetition" in result.summary


def test_a_test_file_that_repeats_its_setup_line_is_not_degenerate(make_runner):
    """The false positive that taught the lead to slice tasks into one file
    each: every test opens with the same setup line, so the count-only guard
    read `root = _repo(tmp_path, monkeypatch)` as collapse. Replayed over 5041
    logged tool-calling turns, that version aborted five such writes against
    two real collapses. Twenty-five copies here clear the threshold on their
    own; what saves the file is that they are spread through real content."""
    body = "".join(
        f"def test_case_{i}(tmp_path, monkeypatch):\n"
        f"    root = _repo(tmp_path, monkeypatch)\n"
        f"    result = run_thing(root, option_{i}=True)\n"
        f"    assert result.status == 'done'\n\n"
        for i in range(25)
    )
    r = make_runner([
        assistant(calls=[tc("write", {"path": "tests/test_thing.py", "content": body})]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    assert r.run("go").status == "done"


def test_a_collapse_that_corrupts_as_it_goes_is_still_degenerate(make_runner):
    """The real thing: one fragment repeated back-to-back, with a corrupted
    copy every few repeats — dense, even though not contiguous."""
    seg = 'grep -n "import" src/branding.ts | head -3'
    parts = []
    for i in range(30):
        parts.append(seg)
        if i % 5 == 4:
            parts.append(seg.replace("branding", "brancding"))
    r = make_runner([assistant(calls=[tc("bash", {"command": "; ".join(parts)})])])
    result = r.run("go")
    assert result.status == "aborted_stuck"
    assert any(t == "abort" and d.get("reason") == "degenerate_repetition" for t, d in r.events)


def test_counts_toward_readonly_cap_separates_searching_from_checking():
    """bash was exempt from the cumulative cap wholesale, on the reasoning that
    a test run may legitimately repeat between edits. True of `pytest`; not of
    the same `grep` six times over, which one logged session did uncounted.
    bash.py already draws that line — this is the predicate that uses it."""
    from bird.engine.runner import _counts_toward_readonly_cap

    def call(name, args):
        return tc(name, args, id="x")

    # searching: repeating it means spinning
    assert _counts_toward_readonly_cap(call("grep", {"pattern": "x"}))
    assert _counts_toward_readonly_cap(call("glob", {"pattern": "**/*.py"}))
    assert _counts_toward_readonly_cap(call("read", {"path": "f.py"}))
    assert _counts_toward_readonly_cap(call("bash", {"command": "grep -n import f.py | head -3"}))
    assert _counts_toward_readonly_cap(call("bash", {"command": "sed -n 1,50p f.py"}))
    # checking and changing: repeating it is work
    assert not _counts_toward_readonly_cap(call("bash", {"command": "pytest -q"}))
    assert not _counts_toward_readonly_cap(call("bash", {"command": "npm test"}))
    assert not _counts_toward_readonly_cap(call("edit", {"path": "f.py"}))


def test_interleaved_pure_search_bash_trips_the_cumulative_cap(make_runner):
    """The cumulative cap exists for repeats the consecutive guard cannot see —
    the same search coming back again and again with other calls in between.
    Before this change bash was invisible to it entirely."""
    script = []
    for i in range(READONLY_CALL_TOTAL_CAP + 1):
        script.append(assistant(calls=[tc("bash", {"command": "grep -n x f.py"}, id=f"g{i}")]))
        script.append(assistant(calls=[tc("bash", {"command": f"echo {i}"}, id=f"e{i}")]))
    script.append(assistant(calls=[tc("done", {"summary": "ok"})]))
    r = make_runner(script)
    result = r.run("go")
    assert result.status == "aborted_stuck"
    assert any(t == "abort" and d.get("reason") == "repeated_readonly_call" for t, d in r.events)
    assert "grep -n x f.py" in result.summary


# --- turn watchdog: the wall-clock budget that ends a trickling stream ---


class HangingClient:
    """complete() blocks the way a provider dripping keep-alives does: no
    chunk ever arrives, so the wire's per-read timeout never fires. The
    watchdog's abort() is the only thing that can end the call, and it raises
    WireAborted exactly as the real wire does."""

    def __init__(self):
        self.abort_calls: list[str] = []
        self.aborted = False
        self.abort_reason: str | None = None
        self._wake = threading.Event()

    def abort(self, reason: str = "user"):
        self.abort_calls.append(reason)
        self.aborted = True
        self.abort_reason = reason
        self._wake.set()

    def clear_abort(self):
        self.aborted = False
        self.abort_reason = None

    def complete(self, spec, messages, **kwargs):
        self._wake.wait(timeout=10)
        raise WireAborted(
            f"request aborted ({self.abort_calls[-1] if self.abort_calls else 'unknown'})"
        )


class InterruptedClient:
    """A user interrupt: WireAborted with no watchdog behind it."""

    def __init__(self):
        self.aborted = True
        self.abort_reason = "user"

    def abort(self, reason: str = "user"):
        self.aborted = True
        self.abort_reason = reason

    def clear_abort(self):
        self.aborted = False
        self.abort_reason = None

    def complete(self, spec, messages, **kwargs):
        raise WireAborted("request to http://x aborted (user)")


class SlowClient:
    """complete() outlives the budget but lands anyway — the watchdog fired
    mid-call and the response arrived right behind it."""

    def __init__(self, script):
        self.script = list(script)
        self.aborted = False
        self.abort_reason = None

    def abort(self, reason: str = "user"):
        self.aborted = True
        self.abort_reason = reason

    def clear_abort(self):
        self.aborted = False
        self.abort_reason = None

    def complete(self, spec, messages, **kwargs):
        time.sleep(0.2)  # the 0.05s budget expires mid-call
        msg = self.script.pop(0)
        return LLMResponse(message=msg, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


def _runner_with(repo, client, events, budget=None):
    """make_runner pins FakeClient; the watchdog tests need clients that hang,
    interrupt, or land late, so they build the Runner directly."""
    ctx = ToolContext(repo_root=repo, record=lambda t, d: events.append((t, d)))
    kw = {} if budget is None else {"turn_budget_seconds": budget}
    return Runner(
        spec=SPEC,
        client=client,
        registry=REGISTRY,
        tools=code_harness_tools(with_kg=False),
        ctx=ctx,
        **kw,
    )


def test_watchdog_ends_a_turn_that_never_completes(repo):
    """The live design session hung at turn 4 for hours in perfect silence: a
    provider that drips chunks never trips the wire's per-read timeout, and
    nothing else watched the clock. The budget must tear the stream down,
    surface a timeout error (not a silent retry, not a user-style interrupt),
    and leave a turn_timeout event so the log names what happened."""
    from bird.llm.wire.openai_compat import WireError

    client = HangingClient()
    events: list[tuple[str, dict]] = []
    r = _runner_with(repo, client, events, budget=0.05)
    with pytest.raises(WireError, match="timed out"):
        r.run("go")
    assert client.abort_calls == ["watchdog"]
    assert any(t == "turn_timeout" for t, _ in events)


def test_a_user_interrupt_is_not_reported_as_a_timeout(repo):
    """serve.py catches WireAborted as "interrupted"; the watchdog conversion
    must apply only when the budget actually fired, or every cancel would
    come back as a phantom timeout error nobody asked for."""
    from bird.llm.wire.openai_compat import WireAborted

    client = InterruptedClient()
    events: list[tuple[str, dict]] = []
    r = _runner_with(repo, client, events)  # default budget: never fires here
    with pytest.raises(WireAborted):  # NOT WireError — the cause stays a cancel
        r.run("go")
    assert not any(t == "turn_timeout" for t, _ in events)


def test_a_watchdog_that_fires_after_the_response_landed_is_harmless(repo):
    """The timer can expire while the last chunk is being read: the response
    is fine, but abort()'s flag sticks by design (an interrupt between
    requests must not be lost). A stuck WATCHDOG flag is the one case that
    must be cleared — the next turn did nothing wrong and inherits nothing."""
    client = SlowClient([
        assistant(calls=[tc("read", {"path": "f.py"})]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    events: list[tuple[str, dict]] = []
    r = _runner_with(repo, client, events, budget=0.05)
    result = r.run("go")
    assert result.status == "done"
    assert client.aborted is False, "the late watchdog flag was cleared"
    assert not any(t == "turn_timeout" for t, _ in events)


# --- explore nudge opt-out ---


def test_an_empty_explore_nudge_switches_it_off(make_runner, repo):
    """The design harness passes "": reading three artboards is its work, and
    the code copy it used to inherit fired 22 times at designers doing so."""
    script = []
    for i in range(7):
        (repo / f"q{i}.py").write_text("z = 3\n")
        script.append(assistant(calls=[tc("read", {"path": f"q{i}.py"}, id=f"c{i}")]))
    script.append(assistant(calls=[tc("done", {"summary": "ok"}, id="cd")]))
    r = make_runner(script, explore_nudge="")
    result = r.run("go")
    assert result.status == "done"
    assert not any(t == "explore_nudge" for t, _ in r.events)
    assert not any("turns of reading" in (m.content or "") for m in result.messages)


def test_none_keeps_the_default_explore_nudge(make_runner, repo):
    script = []
    for i in range(7):
        (repo / f"n{i}.py").write_text("z = 3\n")
        script.append(assistant(calls=[tc("read", {"path": f"n{i}.py"}, id=f"c{i}")]))
    script.append(assistant(calls=[tc("done", {"summary": "ok"}, id="cd")]))
    r = make_runner(script, explore_nudge=None)
    r.run("go")
    assert any(t == "explore_nudge" for t, _ in r.events)


# --- transcript argument elision ---


class Stash(Tool):
    """Stands in for design_create: keeps the document, asks the runner to
    drop it from the transcript."""

    name = "stash"
    description = "keep a document"
    parameters = {"type": "object", "properties": {
        "id": {"type": "string"}, "html": {"type": "string"}}, "required": ["id", "html"]}

    def execute(self, args, ctx):
        return ToolResult(
            output=f"stored {args['id']}",
            details={"elide_arguments": {"html": f"[html elided — {len(args['html'])} chars, stored as {args['id']} v1]"}},
        )


class SeeingClient(FakeClient):
    """Keeps the wire form of the last transcript it was sent."""

    def complete(self, spec, messages, **kwargs):
        self.last_wire = [m.to_openai() for m in messages]
        return super().complete(spec, messages, **kwargs)


def _elider(repo, script):
    events = []
    ctx = ToolContext(repo_root=repo, record=lambda t, d: events.append((t, d)))
    client = SeeingClient(script)
    r = Runner(spec=SPEC, client=client, registry=REGISTRY,
               tools=code_harness_tools(with_kg=False) + [Stash()], ctx=ctx)
    return r, client, events


def test_a_tool_can_elide_its_arguments_from_the_transcript(repo):
    """The logged 112-turn design session re-sent six artboards' html on every
    turn. After the call runs, the transcript keeps a stub; the session log,
    written before the call ran, keeps the real document."""
    big = "<html><body>" + "x" * 2000 + "</body></html>"
    r, client, events = _elider(repo, [
        assistant(calls=[tc("stash", {"id": "a", "html": big}, id="c1")]),
        assistant(calls=[tc("done", {"summary": "ok"}, id="c2")]),
    ])
    result = r.run("go")
    assert result.status == "done"
    call = next(m for m in result.messages if m.role == "assistant" and m.tool_calls).tool_calls[0]
    assert call.arguments == {"id": "a", "html": "[html elided — 2026 chars, stored as a v1]"}
    assert big not in call.arguments_json and '"id": "a"' in call.arguments_json
    # the next model call saw the stub, not the document
    wire_call = next(m for m in client.last_wire if m.get("tool_calls"))["tool_calls"][0]
    assert "elided" in wire_call["function"]["arguments"] and big not in wire_call["function"]["arguments"]
    # the log kept the real thing
    logged = next(d for t, d in events if t == "assistant" and d["tool_calls"])
    assert big in logged["tool_calls"][0]["arguments_json"]


def test_elision_does_not_fool_the_same_call_guard(repo):
    """Distinct calls that elide to the same stub must not read as a loop."""
    calls = [
        assistant(calls=[tc("stash", {"id": "a", "html": f"<p>{i}</p>"}, id=f"c{i}")])
        for i in range(5)
    ]
    r, _, events = _elider(repo, calls + [assistant(calls=[tc("done", {"summary": "ok"}, id="cd")])])
    result = r.run("go")
    assert result.status == "done"
    assert not any(t == "abort" for t, _ in events)


def test_unparseable_arguments_are_left_alone(repo):
    """Nothing safe to rewrite: the raw string stays as the model sent it."""
    from bird.engine.runner import _elide_arguments

    call = ToolCall(id="x", name="stash", arguments=None, arguments_json="{not json")
    _elide_arguments(call, {"elide_arguments": {"html": "[gone]"}})
    assert call.arguments_json == "{not json" and call.arguments is None
    call = ToolCall.from_raw("y", "stash", json.dumps({"id": "a"}))
    _elide_arguments(call, {"elide_arguments": {"html": "[gone]"}})  # no such argument
    assert call.arguments == {"id": "a"}
