import json

import pytest

from bird.engine.runner import Runner
from bird.llm.registry import ModelSpec, ProviderConfig, Registry
from bird.llm.types import LLMResponse, Message, ToolCall, Usage
from bird.harnesses.code import code_harness_tools
from bird.tools import ToolContext

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

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
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
        assistant(calls=[tc("read", {})]),  # missing required path
        assistant(calls=[tc("read", {"path": "f.py"})]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    result = r.run("go")
    assert result.status == "done"
    types = [t for t, _ in r.events]
    assert "invalid_tool_call" in types


def test_invalid_calls_exhaust_retries_abort(make_runner):
    bad = lambda i: assistant(calls=[tc("read", {}, id=f"c{i}")])
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
        assistant(calls=[tc("grep", {"pattern": "x"})]),
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
        m.role == "user" and m.content and "kg_query is the primary search tool" in m.content
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
