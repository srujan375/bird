import json

import pytest

from mha.harness.runner import Runner
from mha.harness.session import SessionRecorder, load_messages
from mha.llm.registry import ModelSpec, ProviderConfig, Registry
from mha.llm.types import LLMResponse, Message, ToolCall, Usage
from mha.repl import Repl
from mha.tools import ToolContext, code_harness_tools

SPEC = ModelSpec(
    spec="fake:model",
    provider=ProviderConfig(name="fake", base_url="http://x"),
    model="model",
    context_window=32768,
)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
        msg = self.script.pop(0)
        if on_delta is not None and msg.content:
            on_delta(msg.content)  # simulate streaming: one chunk, then end marker
            on_delta(None)
        return LLMResponse(message=msg, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


def make_repl(tmp_path, script):
    (tmp_path / "f.py").write_text("x = 1\n")
    recorder = SessionRecorder(tmp_path / ".mha" / "sessions" / "t")
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event)
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    runner = Runner(
        spec=SPEC, client=FakeClient(script), registry=registry,
        tools=code_harness_tools(with_kg=False), ctx=ctx,
    )
    return Repl(runner, registry, kg=None, recorder=recorder, run_id="t")


def feed(monkeypatch, lines):
    it = iter(lines)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_quit_exits(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    feed(monkeypatch, ["/quit"])
    assert repl.run() == 0


def test_eof_exits(tmp_path, monkeypatch):
    repl = make_repl(tmp_path, [])

    def raise_eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert repl.run() == 0


def test_help_and_tools_and_session(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    feed(monkeypatch, ["/help", "/tools", "/session", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "/model" in out
    assert "read" in out and "bash" in out
    assert "events.jsonl" in out


def test_chat_text_reply(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [Message(role="assistant", content="It is a Python file.")])
    feed(monkeypatch, ["what is f.py?", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert out.count("It is a Python file.") == 1  # streamed once, not re-printed
    assert len(repl.messages) == 3  # system + user + assistant


def test_chat_task_with_done(tmp_path, monkeypatch, capsys):
    tc = ToolCall.from_raw("c1", "done", json.dumps({"summary": "all good"}))
    repl = make_repl(tmp_path, [Message(role="assistant", tool_calls=[tc])])
    feed(monkeypatch, ["do the thing", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "› done all good" in out  # activity header for the tool call
    assert "✓ all good" in out


def test_activity_headers_for_tool_calls(tmp_path, monkeypatch, capsys):
    read_call = ToolCall.from_raw("c1", "read", json.dumps({"path": "f.py"}))
    bad_call = ToolCall.from_raw("c2", "read", json.dumps({"path": "missing.py"}))
    repl = make_repl(tmp_path, [
        Message(role="assistant", tool_calls=[read_call]),
        Message(role="assistant", tool_calls=[bad_call]),
        Message(role="assistant", content="f.py sets x."),
    ])
    feed(monkeypatch, ["what is f.py?", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "› read f.py" in out
    assert "✕ read failed" in out
    assert "f.py sets x." in out


def test_conversation_persists_across_turns(tmp_path, monkeypatch):
    repl = make_repl(tmp_path, [
        Message(role="assistant", content="first"),
        Message(role="assistant", content="second"),
    ])
    feed(monkeypatch, ["one", "two", "/quit"])
    repl.run()
    roles = [m.role for m in repl.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]


def test_turn_persists_full_history_after_plan(tmp_path, monkeypatch):
    """The looping bug: with a plan active the runner used to rebind its
    message list, so everything after plan creation vanished from the repl's
    history (and from the transcript saved for /continue). All three
    assistant turns must survive, and the finished plan's tracker must not."""
    plan_call = ToolCall.from_raw(
        "c1", "plan", json.dumps({"steps": [{"title": "One", "files": ["f.py"]}]})
    )
    upd_call = ToolCall.from_raw("c2", "plan_update", json.dumps({"step": 1, "status": "done"}))
    done_call = ToolCall.from_raw("c3", "done", json.dumps({"summary": "shipped"}))
    repl = make_repl(tmp_path, [
        Message(role="assistant", tool_calls=[plan_call]),
        Message(role="assistant", tool_calls=[upd_call]),
        Message(role="assistant", tool_calls=[done_call]),
    ])
    feed(monkeypatch, ["do the thing", "/quit"])
    repl.run()
    assert sum(1 for m in repl.messages if m.role == "assistant") == 3
    rows = load_messages(repl.recorder.run_dir)
    assert sum(1 for r in rows if r["role"] == "assistant") == 3
    # the pinned (user-role) tracker is gone; tool results keep their renders
    assert not any(
        m.role == "user" and (m.content or "").startswith("[plan tracker")
        for m in repl.messages
    )


def test_clear_resets_conversation(tmp_path, monkeypatch):
    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    feed(monkeypatch, ["hello", "/clear", "/quit"])
    repl.run()
    assert repl.messages == []


def test_model_show_and_bad_switch(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    feed(monkeypatch, ["/model", "/model nope:model", "/model nonsense", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "fake:model" in out
    assert "error:" in out  # nope:model → unknown provider
    assert "no available model matches 'nonsense'" in out  # bare word → filter


class Tty:
    """stdin stand-in that claims to be a terminal (input() is monkeypatched
    separately, so the picker's prompt is answered by feed())."""

    @staticmethod
    def isatty():
        return True


def test_model_picker_selects_and_sets_default(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    reg = repl.registry
    reg.providers["fake"] = ProviderConfig(name="fake", base_url="http://x")
    reg.models["fake:model"] = {"context_window": 32768}
    reg.models["fake:other"] = {"context_window": 65536}
    monkeypatch.setattr("sys.stdin", Tty())
    feed(monkeypatch, ["/model", "2", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "1. fake:model" in out and "2. fake:other" in out
    assert repl.runner.spec.spec == "fake:other"
    assert repl.runner.spec.context_window == 65536
    assert reg.aliases["default"] == "fake:other"
    assert "default updated for this session" in out  # no models.json to persist to


def test_unknown_command(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    feed(monkeypatch, ["/wat", "/quit"])
    repl.run()
    assert "unknown command" in capsys.readouterr().out


def test_kg_disabled_message(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    feed(monkeypatch, ["/kg status", "/quit"])
    repl.run()
    assert "disabled" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Continue-session feature: /rename, auto-resume prompt, model carryover.
# ---------------------------------------------------------------------------

def _write_past_session(sessions_dir: Path, run_id: str, *, model: str | None,
                          name: str | None, messages: list[Message]) -> Path:
    """Materialize a complete past session directory on disk: events.jsonl
    (required by find_most_recent_session), session.json (the metadata that
    carries the model + name), and messages.jsonl (what /continue loads)."""
    run_dir = sessions_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"seq": 1, "ts": 0.0, "type": "run_start",
                     "data": {"task": messages[0].content if messages else "",
                              "model": model or ""}}) + "\n",
        encoding="utf-8",
    )
    meta = {}
    if model is not None:
        meta["model"] = model
    if name is not None:
        meta["name"] = name
    if meta:
        (run_dir / "session.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    with open(run_dir / "messages.jsonl", "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m.to_dict()) + "\n")
    # set mtime to something clearly older than the current session will be
    import os
    os.utime(run_dir, (1000000000.0, 1000000000.0))
    return run_dir


def test_rename_persists_and_shows_in_banner(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    feed(monkeypatch, ["/rename fix-login-bug", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "renamed to: fix-login-bug" in out
    # metadata file on disk is what /sessions reads
    meta = json.loads((repl.recorder.run_dir / "session.json").read_text())
    assert meta["name"] == "fix-login-bug"
    # next REPL's banner uses the persisted name
    repl2 = make_repl(tmp_path, [])
    # copy the meta file into the new recorder's run_dir
    (repl2.recorder.run_dir / "session.json").write_text(json.dumps(meta) + "\n")
    feed(monkeypatch, ["/quit"])
    repl2.run()
    out2 = capsys.readouterr().out
    assert "fix-login-bug" in out2  # banner now includes the human label


def test_rename_requires_name(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    feed(monkeypatch, ["/rename", "/quit"])
    repl.run()
    assert "usage: /rename <name>" in capsys.readouterr().out


def test_continue_loads_messages_and_carries_model(tmp_path, monkeypatch, capsys):
    """Resuming a past session must (a) load its message history and (b)
    switch the runner to the model that session was on — otherwise the LLM
    differs from the one that produced the resumed messages, which is the
    whole point of /continue."""
    repl = make_repl(tmp_path, [])
    reg = repl.registry
    reg.providers["fake"] = ProviderConfig(name="fake", base_url="http://x")
    reg.models["fake:model"] = {"context_window": 32768}
    reg.models["fake:other"] = {"context_window": 65536}
    past = _write_past_session(
        repl.recorder.run_dir.parent,
        "2024-01-01-other",
        model="fake:other",
        name="fix login bug",
        messages=[
            Message(role="user", content="how do I fix the login?"),
            Message(role="assistant", content="add a try/except."),
        ],
    )
    feed(monkeypatch, [f"/continue {past.name}", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "resumed session" in out
    assert "loaded 2 messages" in out
    assert repl.runner.spec.spec == "fake:other"
    assert repl.runner.spec.context_window == 65536
    # the two resumed messages plus the system prompt seeded on the next turn
    # (which never ran because we /quit) → just the two from disk
    assert len(repl.messages) == 2
    assert repl.messages[0].content == "how do I fix the login?"


def test_continue_unknown_session_does_not_explode(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    feed(monkeypatch, ["/continue nope-no-such", "/quit"])
    repl.run()
    assert "no session matches" in capsys.readouterr().out
    assert repl.messages == []  # failed resume must leave messages untouched


def test_auto_resume_prompt_accepts_and_loads(tmp_path, monkeypatch, capsys):
    """On startup with a tty, if a past session exists the user is asked
    whether to resume. Saying yes loads its messages and switches model."""
    repl = make_repl(tmp_path, [])
    reg = repl.registry
    reg.providers["fake"] = ProviderConfig(name="fake", base_url="http://x")
    reg.models["fake:model"] = {"context_window": 32768}
    reg.models["fake:other"] = {"context_window": 65536}
    _write_past_session(
        repl.recorder.run_dir.parent,
        "2024-01-01-other",
        model="fake:other",
        name="fix login bug",
        messages=[Message(role="user", content="hi")],
    )
    monkeypatch.setattr("sys.stdin", Tty())
    # answers: y (resume) → /session proves the REPL stayed alive → /quit
    feed(monkeypatch, ["y", "/session", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "continue previous session?" in out
    assert "fix login bug" in out
    assert "resumed 2024-01-01-other" in out
    assert "events.jsonl" in out  # accepting the resume must NOT exit the REPL
    assert repl.runner.spec.spec == "fake:other"
    assert len(repl.messages) == 1


def test_auto_resume_prompt_decline_starts_fresh(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    _write_past_session(
        repl.recorder.run_dir.parent,
        "2024-01-01-other",
        model="fake:other",
        name="old thing",
        messages=[Message(role="user", content="hi")],
    )
    monkeypatch.setattr("sys.stdin", Tty())
    feed(monkeypatch, ["n", "/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "continue previous session?" in out
    assert "starting a fresh session" in out
    # declined → no messages loaded, default model kept
    assert repl.messages == []
    assert repl.runner.spec.spec == "fake:model"


def test_auto_resume_prompt_skipped_without_tty(tmp_path, monkeypatch, capsys):
    """CI / piped stdin must never get hung on a prompt. Auto-resume is
    opt-in by tty; /continue is the non-tty escape hatch."""
    repl = make_repl(tmp_path, [])
    _write_past_session(
        repl.recorder.run_dir.parent,
        "2024-01-01-other",
        model="fake:other",
        name="old",
        messages=[Message(role="user", content="hi")],
    )
    # no Tty() — stdin reports not-a-tty
    feed(monkeypatch, ["/quit"])
    repl.run()
    out = capsys.readouterr().out
    assert "continue previous session?" not in out
    assert repl.messages == []


def test_auto_resume_prompt_skipped_with_no_past_sessions(tmp_path, monkeypatch, capsys):
    repl = make_repl(tmp_path, [])
    monkeypatch.setattr("sys.stdin", Tty())
    feed(monkeypatch, ["/quit"])
    repl.run()
    assert "continue previous session?" not in capsys.readouterr().out
