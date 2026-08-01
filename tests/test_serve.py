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
    recorder = SessionRecorder(tmp_path / ".bird" / "sessions" / "t")
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event)
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    runner = Runner(
        spec=SPEC, client=FakeClient(script), registry=registry,
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


def run_server(monkeypatch, tmp_path, script):
    feeder, out = Feeder(), Out()
    monkeypatch.setattr("sys.stdin", feeder)
    monkeypatch.setattr("sys.stdout", out)
    server = Server(make_repl(tmp_path, script))
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
