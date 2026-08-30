"""Tests for the JSON-lines serve bridge (bird serve)."""

import json
import queue
import threading
import time

from bird.engine.runner import Runner
from bird.engine.session import SessionRecorder
from bird.llm.registry import ModelSpec, ProviderConfig, Registry
from bird.llm.types import LLMResponse, Message, ToolCall, Usage
from bird.repl import Repl
from bird.serve import GatedTool, Server, _diff_lines
from bird.harnesses.code import code_harness_tools
from bird.tools import Tool, ToolContext, ToolResult

SPEC = ModelSpec(
    spec="fake:model",
    provider=ProviderConfig(name="fake", base_url="http://x"),
    model="model",
    context_window=32768,
)

# OpenRouter's reasoning.effort accepts only high|medium|low, so its thinking
# picker must not offer "max" (see OPENROUTER_REASONING_EFFORT).
OPENROUTER_SPEC = ModelSpec(
    spec="openrouter:anthropic/claude-sonnet-4",
    provider=ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1"),
    model="anthropic/claude-sonnet-4",
    context_window=32768,
)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None, on_thinking=None):
        msg = self.script.pop(0)
        if on_delta is not None and msg.content:
            on_delta(msg.content)  # simulate streaming: one chunk, then end marker
            on_delta(None)
        if on_thinking is not None and getattr(msg, "thinking", None):
            on_thinking(msg.thinking)
            on_thinking(None)
        return LLMResponse(message=msg, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


def make_repl(tmp_path, script, skills=None, spec=SPEC):
    (tmp_path / "f.py").write_text("x = 1\n")
    recorder = SessionRecorder(tmp_path / ".bird" / "sessions" / "t")
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event, skills=skills)
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    runner = Runner(
        spec=spec, client=FakeClient(script), registry=registry,
        tools=code_harness_tools(with_kg=False), ctx=ctx,
    )
    return Repl(runner, registry, kg=None, recorder=recorder, run_id="t")


def edit_call(old, new):
    args = {"path": "f.py", "old_text": old, "new_text": new}
    return Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="1", name="edit", arguments=args, arguments_json=json.dumps(args))],
    )


# ---------- unit: diff + gate ----------


def test_diff_lines_kinds():
    lines = _diff_lines("a\nb\nc\n", "a\nB\nc\n")
    kinds = [(l["kind"], l["text"]) for l in lines]
    assert ("del", "-b") in kinds
    assert ("add", "+B") in kinds
    assert any(k == "ctx" for k, _ in kinds)


class Boom(Tool):
    name = "boom"
    description = "x"
    parameters = {"type": "object", "properties": {}}
    called = False

    def execute(self, args, ctx):
        self.called = True
        return ToolResult(output="ran")


class StubBroker:
    def __init__(self, answer, feedback=""):
        self.answer = answer
        self.feedback = feedback

    def request(self, payload):
        self.payload = payload
        return self.answer, self.feedback


def test_gated_tool_denied(tmp_path):
    inner = Boom()
    gated = GatedTool(inner, StubBroker(False))
    ctx = ToolContext(repo_root=tmp_path)
    result = gated.execute({}, ctx)
    assert result.is_error and "DENIED" in result.output
    assert inner.called is False


def test_gated_tool_approved(tmp_path):
    inner = Boom()
    gated = GatedTool(inner, StubBroker(True))
    result = gated.execute({}, ctx := ToolContext(repo_root=tmp_path))
    assert result.output == "ran" and inner.called


# ---------- integration: protocol over fake stdio ----------


class Feeder:
    """Blocking stdin stand-in driven by a queue."""

    def __init__(self):
        self.q = queue.Queue()

    def put(self, obj):
        self.q.put(json.dumps(obj) + "\n")

    def close(self):
        self.q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self.q.get()
        if item is None:
            raise StopIteration
        return item


class Out:
    """Captures Bridge output; lets the test wait for a message type."""

    def __init__(self):
        self.msgs = []
        self.cv = threading.Condition()

    def write(self, s):
        s = s.strip()
        if not s:
            return
        with self.cv:
            self.msgs.append(json.loads(s))
            self.cv.notify_all()

    def flush(self):
        pass

    def wait_for(self, type_, timeout=5.0):
        deadline = time.time() + timeout
        with self.cv:
            while True:
                for m in self.msgs:
                    if m["type"] == type_:
                        return m
                remaining = deadline - time.time()
                assert remaining > 0, f"timed out waiting for {type_}; got {self.msgs}"
                self.cv.wait(remaining)


def run_server(monkeypatch, tmp_path, script, skills=None, spec=SPEC):
    feeder, out = Feeder(), Out()
    monkeypatch.setattr("sys.stdin", feeder)
    monkeypatch.setattr("sys.stdout", out)
    server = Server(make_repl(tmp_path, script, skills, spec=spec))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return feeder, out, thread


def test_serve_reply_flow(monkeypatch, tmp_path):
    feeder, out, thread = run_server(
        monkeypatch, tmp_path, [Message(role="assistant", content="It is a Python file.")]
    )
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "what is f.py?"})
    end = out.wait_for("turn_end")
    assert end["status"] == "reply"
    assert end["summary"] == "It is a Python file."
    deltas = [
        m for m in out.msgs
        if m["type"] == "harness_event" and m["event"] == "assistant_delta"
    ]
    assert [d["data"]["text"] for d in deltas] == ["It is a Python file."]
    feeder.close()
    thread.join(timeout=5)
    out.wait_for("bye")


def test_serve_done_streams_summary_then_turn_end_matches(monkeypatch, tmp_path):
    """A `done` turn where the model streams its summary text alongside the
    done tool call is the duplicate-response precondition: the streamed
    assistant_delta content equals the done tool's summary, and turn_end
    carries that same summary as status=done. The TUI dedups on this shape —
    a regression here would re-introduce the doubled reply for thinking
    models, whose reasoning trace precedes the streamed content."""
    done_args = {"summary": "all done"}
    done_call = Message(
        role="assistant",
        content="all done",
        tool_calls=[ToolCall(id="1", name="done", arguments=done_args, arguments_json=json.dumps(done_args))],
    )
    feeder, out, thread = run_server(monkeypatch, tmp_path, [done_call])
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "finish it"})
    end = out.wait_for("turn_end")
    assert end["status"] == "done"
    assert end["summary"] == "all done"
    deltas = [
        m["data"]["text"]
        for m in out.msgs
        if m["type"] == "harness_event" and m["event"] == "assistant_delta"
    ]
    # the streamed content is the same text turn_end reports as the summary —
    # exactly the overlap the TUI must not render twice
    assert "".join(deltas) == "all done"
    feeder.close()
    thread.join(timeout=5)
    out.wait_for("bye")


def _skill(name="mr-description", body="write the MR"):
    from pathlib import Path

    from bird.skills import Skill

    return Skill(name=name, description="d", body=body, path=Path("x"), source="project")


def test_skill_command_runs_a_turn_and_never_echoes_the_reply(monkeypatch, tmp_path):
    """`/<skill>` is a model turn, not a UI command.

    It must reach the client the same way typed input does — streamed deltas
    plus a turn_end — and must NOT also arrive as command_output. Repl._turn
    prints the reply for the plain terminal REPL; when a command handler ran
    that turn under redirect_stdout, the capture came back as command_output
    and the UI drew the whole answer a second time (rendered once, raw once).
    """
    feeder, out, thread = run_server(
        monkeypatch, tmp_path,
        [Message(role="assistant", content="no branch changes to describe")],
        skills=[_skill()],
    )
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/mr-description describe my branch"})
    end = out.wait_for("turn_end")
    assert end["status"] == "reply"
    assert end["summary"] == "no branch changes to describe"

    deltas = [
        m["data"]["text"]
        for m in out.msgs
        if m["type"] == "harness_event" and m["event"] == "assistant_delta"
    ]
    assert "".join(deltas) == "no branch changes to describe"
    # the reply reached the UI exactly once — no command_output carrying it
    echoes = [m for m in out.msgs if m["type"] == "command_output"]
    assert echoes == [], f"reply echoed back as command_output: {echoes}"

    feeder.close()
    thread.join(timeout=5)
    out.wait_for("bye")


def test_skill_command_prompt_carries_body_and_args(monkeypatch, tmp_path):
    """The turn the server starts is the same prompt the Repl would build."""
    repl = make_repl(tmp_path, [], skills=[_skill(body="be concise")])
    with_args = repl.skill_prompt("mr-description", "describe my branch")
    assert "be concise" in with_args
    assert with_args.endswith("Task: describe my branch")
    assert "Task:" not in repl.skill_prompt("mr-description", "")
    assert repl.skill_prompt("nope", "") is None


def test_builtin_command_beats_a_skill_of_the_same_name(monkeypatch, tmp_path):
    """A skill named `model` must not shadow /model — no turn, just the picker."""
    feeder, out, thread = run_server(
        monkeypatch, tmp_path, [], skills=[_skill(name="model")],
    )
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/model"})
    out.wait_for("model_list")
    assert not any(m["type"] == "turn_end" for m in out.msgs)
    feeder.close()
    thread.join(timeout=5)
    out.wait_for("bye")


def test_turn_end_carries_cumulative_tokens(monkeypatch, tmp_path):
    feeder, out, thread = run_server(
        monkeypatch, tmp_path,
        [Message(role="assistant", content="first"), Message(role="assistant", content="second")],
    )
    out.wait_for("ready")

    feeder.put({"type": "user_input", "text": "one"})
    first = out.wait_for("turn_end")
    # FakeClient reports Usage(10, 5) per completion
    assert first["input_tokens"] == 10 and first["output_tokens"] == 5

    feeder.put({"type": "user_input", "text": "two"})
    # keep waiting until the *second* turn_end (wait_for returns the first)
    deadline = time.time() + 5
    while True:
        ends = [m for m in out.msgs if m["type"] == "turn_end"]
        if len(ends) >= 2:
            break
        assert time.time() < deadline, f"timed out waiting for second turn_end; got {out.msgs}"
        time.sleep(0.02)
    # session-cumulative: the second report is the running total, not a delta
    second = ends[-1]
    assert second["input_tokens"] == 20 and second["output_tokens"] == 10
    feeder.close()
    thread.join(timeout=5)
    out.wait_for("bye")


def test_serve_permission_approved(monkeypatch, tmp_path):
    feeder, out, thread = run_server(
        monkeypatch, tmp_path,
        [edit_call("x = 1", "x = 2"), Message(role="assistant", content="changed it")],
    )
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "bump x"})
    req = out.wait_for("permission_request")
    assert req["kind"] == "edit" and req["file"] == "f.py"
    assert {"kind": "add", "text": "+x = 2"} in req["lines"]
    feeder.put({"type": "permission_response", "id": req["id"], "approved": True})
    end = out.wait_for("turn_end")
    assert end["status"] == "reply"
    assert (tmp_path / "f.py").read_text() == "x = 2\n"
    feeder.close()
    thread.join(timeout=5)


def test_serve_permission_denied(monkeypatch, tmp_path):
    feeder, out, thread = run_server(
        monkeypatch, tmp_path,
        [edit_call("x = 1", "x = 2"), Message(role="assistant", content="ok, leaving it")],
    )
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "bump x"})
    req = out.wait_for("permission_request")
    feeder.put({"type": "permission_response", "id": req["id"], "approved": False})
    end = out.wait_for("turn_end")
    assert end["status"] == "reply"
    assert (tmp_path / "f.py").read_text() == "x = 1\n"  # unchanged
    feeder.close()
    thread.join(timeout=5)


def test_serve_model_list(monkeypatch, tmp_path):
    from bird.llm.discovery import DiscoveredModel

    monkeypatch.setattr(
        "bird.serve.discover_models",
        lambda registry: ([DiscoveredModel("fake:model", "configured", 32768)], ["a note"]),
    )
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/model"})
    msg = out.wait_for("model_list")
    assert msg["current"] == "fake:model"
    assert msg["default"] == "fake:model"
    assert msg["models"] == [{"spec": "fake:model", "source": "configured", "context_window": 32768}]
    assert msg["notes"] == ["a note"]
    feeder.close()
    thread.join(timeout=5)


def test_serve_think_list(monkeypatch, tmp_path):
    """bare /think emits a think_list event with the modes and current mode,
    mirroring bare /model's model_list — the TUI renders the picker and
    answers with '/think <mode>'. The mode list is provider-aware: OpenRouter
    has no "max" (its reasoning.effort accepts only high|medium|low)."""
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/think"})
    msg = out.wait_for("think_list")
    # no mode set on a fresh session → None (Ollama's auto/default behavior)
    assert msg["current"] is None
    assert msg["modes"] == ["off", "low", "medium", "high", "max"]
    feeder.close()
    thread.join(timeout=5)


def test_serve_think_list_openrouter_has_no_max(monkeypatch, tmp_path):
    """On an openrouter model the emitted modes drop "max" — its effort field
    accepts only high|medium|low (the adapter clamps max→high), so offering it
    would let the UI persist a value that silently means high."""
    feeder, out, thread = run_server(monkeypatch, tmp_path, [], spec=OPENROUTER_SPEC)
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/think"})
    msg = out.wait_for("think_list")
    assert msg["modes"] == ["off", "low", "medium", "high"]
    assert "max" not in msg["modes"]
    feeder.close()
    thread.join(timeout=5)


def test_serve_think_mode_falls_through_to_state(monkeypatch, tmp_path):
    """/think <mode> falls through to the generic _command path (which calls
    _cmd_think -> _set_think_mode) and emits a state event carrying the updated
    think_mode, same as /model <spec> does."""
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/think medium"})
    # the generic path prints "thinking: medium" then emits state
    cmd = out.wait_for("command_output")
    assert "medium" in cmd["text"]
    state = out.wait_for("state")
    assert state["model"] == "fake:model"
    assert state["think_mode"] == "medium"
    feeder.close()
    thread.join(timeout=5)


def test_serve_think_off_maps_to_none_label(monkeypatch, tmp_path):
    """`off` maps internally to reasoning_effort 'none' but the friendly label
    round-trips: setting /think off reports think_mode 'off' in state."""
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/think off"})
    out.wait_for("command_output")
    state = out.wait_for("state")
    assert state["think_mode"] == "off"
    # a subsequent bare /think reports the friendly label, not the internal one
    feeder.put({"type": "command", "line": "/think"})
    msg = out.wait_for("think_list")
    assert msg["current"] == "off"
    feeder.close()
    thread.join(timeout=5)


def test_serve_command(monkeypatch, tmp_path):
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/tools"})
    cmd = out.wait_for("command_output")
    assert "bash" in cmd["text"] and "edit" in cmd["text"]
    state = out.wait_for("state")
    assert state["model"] == "fake:model"
    feeder.put({"type": "command", "line": "/quit"})
    out.wait_for("bye")
    thread.join(timeout=5)


def test_serve_persists_transcript_after_turn(monkeypatch, tmp_path):
    """serve must persist messages.jsonl after a turn so a /reload respawn
    can resume the conversation (it never used to)."""
    from bird.engine.session import load_messages

    feeder, out, thread = run_server(
        monkeypatch, tmp_path, [Message(role="assistant", content="hello there")]
    )
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "hi"})
    out.wait_for("turn_end")
    # give the worker a beat to finish writing
    time.sleep(0.1)
    rows = load_messages(make_repl(tmp_path, []).recorder.run_dir)
    assert rows is not None and len(rows) >= 2
    roles = [r["role"] for r in rows]
    assert "user" in roles and "assistant" in roles
    feeder.close()
    thread.join(timeout=5)


def test_serve_reload_emits_run_id(monkeypatch, tmp_path):
    """/reload asks the UI to respawn serve, handing back the current run_id
    so the new process can --resume this session."""
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/reload"})
    msg = out.wait_for("reload")
    assert msg["run_id"] == "t"  # make_repl uses run_id="t"
    feeder.close()
    thread.join(timeout=5)


# ---------- transport split ----------


def test_permission_response_feedback_reaches_broker(monkeypatch, tmp_path):
    """A rejection's feedback string travels wire -> broker -> request()."""
    from bird.serve import PermissionBroker

    events = []
    broker = PermissionBroker(lambda t, **d: events.append({"type": t, **d}))
    got = {}

    def ask():
        got["answer"] = broker.request({"kind": "finalize", "summary": "s"})

    t = threading.Thread(target=ask)
    t.start()
    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.01)
    req = events[0]
    assert req["type"] == "permission_request" and req["kind"] == "finalize"
    broker.resolve(req["id"], False, feedback="drop the cache")
    t.join(timeout=5)
    assert got["answer"] == (False, "drop the cache")


class FakeTransport:
    """Collects emitted events; run() drives one scripted user turn."""

    def __init__(self):
        self.events = []
        self.done = threading.Event()

    def emit(self, event):
        self.events.append(event)
        if event["type"] == "turn_end":
            self.done.set()

    def run(self, handlers):
        handlers.on_user_input("hello?")
        assert self.done.wait(timeout=5)


def test_server_runs_on_custom_transport(tmp_path):
    """The pump is transport-agnostic: a fake transport gets the same event
    vocabulary stdio produces, with no stdio involved at all."""
    transport = FakeTransport()
    repl = make_repl(tmp_path, [Message(role="assistant", content="hi there")])
    server = Server(repl, transport=transport)
    assert server.run() == 0
    types = [e["type"] for e in transport.events]
    assert types[0] == "ready" and types[-1] == "bye"
    end = next(e for e in transport.events if e["type"] == "turn_end")
    assert end["status"] == "reply" and end["summary"] == "hi there"
    deltas = [
        e for e in transport.events
        if e["type"] == "harness_event" and e["event"] == "assistant_delta"
    ]
    assert [d["data"]["text"] for d in deltas] == ["hi there"]


# ---------- attachment ingestion ----------


class _AttachTransport:
    """Sends one message naming an image, then reaps the source file the way
    macOS reaps a screenshot preview's temp file."""

    def __init__(self, text, source):
        self.events = []
        self.text = text
        self.source = source
        self.done = threading.Event()

    def emit(self, event):
        self.events.append(event)
        if event["type"] == "turn_end":
            self.done.set()

    def run(self, handlers):
        handlers.on_user_input(self.text)
        self.source.unlink()  # gone before the model could ever have read it
        assert self.done.wait(timeout=5)


def test_server_ingests_a_dragged_screenshot_before_it_is_reaped(tmp_path):
    """End-to-end at the seam that matters: a quoted temp path arrives, the
    image is copied into the session, the model is handed the copy, and the
    original vanishing afterwards costs nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shot = tmp_path / "T" / "NSIRD_screencaptureui_x"  # outside the repo, like the real thing
    shot.mkdir(parents=True)
    src = shot / "Screenshot 2026-07-28 at 10.59.33 PM.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    repl = make_repl(repo, [Message(role="assistant", content="ok")])
    transport = _AttachTransport(f"'{src}', read this image?", src)
    assert Server(repl, transport=transport).run() == 0

    saved = [
        e for e in transport.events
        if e["type"] == "harness_event" and e["event"] == "attachment_saved"
    ]
    assert len(saved) == 1
    rel = saved[0]["data"]["path"]
    assert (repo / rel).is_file()          # survives the reaping
    assert not src.exists()

    # the model saw the copy's path, never the doomed temp one
    user_text = next(m.content for m in repl.messages if m.role == "user")
    assert rel in user_text
    assert str(src) not in user_text
    assert "read this image?" in user_text


# ------------------------------------------ the board as a way of talking


def _assistant(text):
    return Message(role="assistant", content=text, tool_calls=[])


def test_submitting_the_board_starts_a_turn_without_the_user_typing(tmp_path):
    """Drawing is talking. Without this the architect sits idle until you also
    type something, which makes the board a form beside the conversation
    rather than part of it."""
    from bird.harnesses.arch.session import ArchSession

    repl = make_repl(tmp_path, [_assistant("what does it own?")])
    server = Server(repl)
    arch = ArchSession(run_dir=tmp_path / "run")
    repl.runner.ctx.arch = arch
    arch.note_user_edit('drew a box "Rate limiter" (rate-limiter)')

    server.on_board_submit()
    server.worker.join(timeout=5)

    asked = [m.content for m in repl.messages if m.role == "user"]
    assert any("the user changed the board" in (c or "") for c in asked)
    assert any('drew a box "Rate limiter"' in (c or "") for c in asked)


def test_submitting_an_unchanged_board_says_nothing(tmp_path):
    from bird.harnesses.arch.session import ArchSession

    repl = make_repl(tmp_path, [])
    server = Server(repl)
    repl.runner.ctx.arch = ArchSession(run_dir=tmp_path / "run")

    server.on_board_submit()
    assert server.worker is None, "no edits, no turn"


def test_submitting_while_a_turn_runs_does_not_interrupt_it(tmp_path):
    """The edits reach a running turn through its pinned note; starting a
    second turn would have the architect answer the same gesture twice."""
    from bird.harnesses.arch.session import ArchSession

    release = threading.Event()

    class Blocking:
        def complete(self, *a, **kw):
            release.wait(timeout=5)
            return LLMResponse(message=_assistant("done"), usage=Usage(1, 1),
                               stop_reason="stop", model=SPEC.spec)

    repl = make_repl(tmp_path, [])
    repl.runner.client = Blocking()
    server = Server(repl)
    arch = ArchSession(run_dir=tmp_path / "run")
    repl.runner.ctx.arch = arch

    server.on_user_input("go")
    time.sleep(0.2)
    running = server.worker

    arch.note_user_edit("drew a wire api -> pg")
    server.on_board_submit()
    assert server.worker is running, "the running turn was left alone"
    assert arch.compose_activity_prompt() is not None, "and the edit is not lost"

    release.set()
    server.worker.join(timeout=5)


def test_a_harness_with_no_board_ignores_the_signal(tmp_path):
    """`bird serve --harness code` has no arch session; the route must not
    require every harness to grow one."""
    server = Server(make_repl(tmp_path, []))
    server.on_board_submit()
    assert server.worker is None


def test_what_was_typed_carries_what_it_pointed_at(tmp_path):
    """Selecting a box and asking "why this one?" has to arrive as one message
    that already knows which one. Without it the architect is guessing."""
    from bird.harnesses.arch.session import ArchSession
    from bird.harnesses.arch.state import Node

    repl = make_repl(tmp_path, [_assistant("ok")])
    server = Server(repl)
    arch = ArchSession(run_dir=tmp_path / "run")
    arch.state.nodes["idx"] = Node(
        id="idx", label="Search index", kind="store", responsibility="the vector index",
    )
    repl.runner.ctx.arch = arch

    server.on_user_input("why this one?", ["idx"])
    server.worker.join(timeout=5)

    said = [m.content for m in repl.messages if m.role == "user"]
    combined = next(c for c in said if "why this one?" in (c or ""))
    assert "the user is pointing at" in combined
    assert "- Search index" in combined
    assert "owns: the vector index" in combined, "the details, not just the id"


def test_pointing_at_nothing_adds_nothing(tmp_path):
    """The overwhelming majority of messages select nothing and must not grow a
    block saying so."""
    from bird.harnesses.arch.session import ArchSession

    repl = make_repl(tmp_path, [_assistant("ok")])
    server = Server(repl)
    repl.runner.ctx.arch = ArchSession(run_dir=tmp_path / "run")

    server.on_user_input("morning")
    server.worker.join(timeout=5)

    said = [m.content for m in repl.messages if m.role == "user"]
    assert any(c == "morning" for c in said), "the message is exactly what was typed"


def test_a_harness_with_no_board_ignores_a_selection(tmp_path):
    """`bird serve --harness code` has no arch session. A stray selection must
    not make it fail."""
    repl = make_repl(tmp_path, [_assistant("ok")])
    server = Server(repl)

    server.on_user_input("hello", ["idx"])
    server.worker.join(timeout=5)

    said = [m.content for m in repl.messages if m.role == "user"]
    assert any(c == "hello" for c in said)


def test_what_was_typed_carries_what_was_drawn(tmp_path):
    """One message, one turn. Splitting them would have the architect answer
    half of what you said at a time."""
    from bird.harnesses.arch.session import ArchSession

    repl = make_repl(tmp_path, [_assistant("ok")])
    server = Server(repl)
    arch = ArchSession(run_dir=tmp_path / "run")
    repl.runner.ctx.arch = arch
    arch.note_user_edit('drew a box "Rate limiter" (rate-limiter)')

    server.on_user_input("and what about backpressure?")
    server.worker.join(timeout=5)

    said = [m.content for m in repl.messages if m.role == "user"]
    combined = next(c for c in said if "backpressure" in (c or ""))
    assert 'drew a box "Rate limiter"' in combined, "the drawing rode along"


# ---- onboarding over the bridge ---------------------------------------------

def test_serve_setup_round_trip(monkeypatch, tmp_path):
    """`/setup` runs the walkthrough off the reader thread: questions arrive as
    prompt_request, the UI answers with prompt_response, and the chosen model
    is switched to before setup_end."""
    import bird.onboard as onboard_mod

    def fake_walkthrough(io, registry, **kw):
        key = io.ask_secret("OLLAMA_API_KEY (empty to skip)")
        io.say(f"key length {len(key)}")
        return io.choose("default model", [onboard_mod.Choice("fake:model", "fake:model")], current="fake:model")

    monkeypatch.setattr(onboard_mod, "walkthrough", fake_walkthrough)
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/setup"})
    req = out.wait_for("prompt_request")
    assert req["secret"] is True and "OLLAMA_API_KEY" in req["prompt"]
    feeder.put({"type": "prompt_response", "id": req["id"], "value": "sk-xyz"})
    while True:
        reqs = [m for m in out.msgs if m["type"] == "prompt_request" and "choices" in m]
        if reqs:
            break
        time.sleep(0.01)
    pick = reqs[0]
    assert pick["choices"][0]["value"] == "fake:model" and pick["current"] == "fake:model"
    feeder.put({"type": "prompt_response", "id": pick["id"], "value": None})
    out.wait_for("setup_end")
    texts = [m["text"] for m in out.msgs if m["type"] == "command_output"]
    assert "key length 6" in texts
    assert not any("sk-xyz" in json.dumps(m) for m in out.msgs)  # the secret never comes back out
    feeder.close()
    thread.join(timeout=5)


def test_serve_interrupt_cancels_a_pending_prompt(monkeypatch, tmp_path):
    import bird.onboard as onboard_mod

    seen = {}

    def fake_walkthrough(io, registry, **kw):
        seen["answer"] = io.ask_secret("OLLAMA_API_KEY")
        return None

    monkeypatch.setattr(onboard_mod, "walkthrough", fake_walkthrough)
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/setup"})
    out.wait_for("prompt_request")
    feeder.put({"type": "interrupt"})
    out.wait_for("setup_end")
    assert seen["answer"] == ""
    feeder.close()
    thread.join(timeout=5)
