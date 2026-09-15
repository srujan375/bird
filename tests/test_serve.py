"""Tests for the JSON-lines serve bridge (bird serve)."""

import base64
import json
import queue
import threading
import time

from bird.engine.runner import Runner
from bird.engine.session import SessionRecorder
from bird.llm.registry import ModelSpec, ProviderConfig, Registry
from bird.llm.types import LLMResponse, Message, ToolCall, Usage
from bird.llm.wire.openai_compat import WireAborted
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

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None, on_thinking=None, **kwargs):
        msg = self.script.pop(0)
        if on_delta is not None and msg.content:
            on_delta(msg.content)  # simulate streaming: one chunk, then end marker
            on_delta(None)
        if on_thinking is not None and getattr(msg, "thinking", None):
            on_thinking(msg.thinking)
            on_thinking(None)
        return LLMResponse(message=msg, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


def make_repl(tmp_path, script, skills=None, spec=SPEC, client=None):
    (tmp_path / "f.py").write_text("x = 1\n")
    recorder = SessionRecorder(tmp_path / ".bird" / "sessions" / "t")
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event, skills=skills)
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    runner = Runner(
        spec=spec, client=client or FakeClient(script), registry=registry,
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

    def wait_for(self, type_, timeout=5.0, where=None):
        """First message of `type_` (optionally the first for which
        `where(msg)` holds — e.g. to skip the catalog's fetching frame)."""
        deadline = time.time() + timeout
        with self.cv:
            while True:
                for m in self.msgs:
                    if m["type"] == type_ and (where is None or where(m)):
                        return m
                remaining = deadline - time.time()
                assert remaining > 0, f"timed out waiting for {type_}; got {self.msgs}"
                self.cv.wait(remaining)


def run_server(monkeypatch, tmp_path, script, skills=None, spec=SPEC, client=None):
    feeder, out = Feeder(), Out()
    monkeypatch.setattr("sys.stdin", feeder)
    monkeypatch.setattr("sys.stdout", out)
    server = Server(make_repl(tmp_path, script, skills, spec=spec, client=client))
    out.server = server  # so a test can wait on server state, not on a sleep
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return feeder, out, thread


class BlockingClient:
    """Parks inside its first completion so a test can type mid-turn."""

    def __init__(self, script):
        self.script = list(script)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.seen = []  # the transcript as of each call, oldest first

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None, on_thinking=None, **kwargs):
        self.seen.append([(m.role, m.content or "") for m in messages])
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(5), "the test never released the first completion"
        msg = self.script.pop(0)
        return LLMResponse(message=msg, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


def read_call(id="1"):
    args = {"path": "f.py"}
    return Message(
        role="assistant", content="",
        tool_calls=[ToolCall(id=id, name="read", arguments=args, arguments_json=json.dumps(args))],
    )


# ---------- mid-turn input ----------


def test_input_during_a_turn_is_injected_not_refused(monkeypatch, tmp_path):
    """Typing while a run is working used to earn "a turn is already running".
    Now it parks on the server and the loop appends it at its next step, so
    the model reads it on the very next call instead of after the whole run."""
    client = BlockingClient([read_call(), Message(role="assistant", content="ok")])
    feeder, out, thread = run_server(monkeypatch, tmp_path, [], client=client)
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "start"})
    assert client.entered.wait(5)
    feeder.put({"type": "user_input", "text": "second thought"})
    out.wait_for("input_pending")
    client.release.set()

    end = out.wait_for("turn_end")
    assert end["status"] == "reply"
    landed = out.wait_for("harness_event", where=lambda m: m["event"] == "user_injected")
    assert landed["data"]["text"] == "second thought"
    # the model saw it on its NEXT call, not in a later turn
    assert ("user", "second thought") in client.seen[-1]
    # and the step boundary held: the injection sits after the tool result,
    # and nothing was wedged between the tool_calls message and that result
    seen = client.seen[-1]
    roles = [r for r, _ in seen]
    ti = roles.index("tool")
    ui = next(i for i, (r, c) in enumerate(seen) if r == "user" and c == "second thought")
    assert ui > ti, "the injection split the tool_calls/result pair"
    assert roles[ti - 1] == "assistant", "something landed between the call and its result"
    assert [m for m in out.msgs if m["type"] == "error"] == []
    feeder.close()
    thread.join(timeout=5)


def test_interrupt_hands_back_input_the_model_never_saw(monkeypatch, tmp_path):
    """An interrupted turn must not auto-fire what was parked behind it — the
    user just stopped something. serve hands it back and the TUI re-parks it
    as a held queue item that only an explicit return releases."""
    client = BlockingClient([Message(role="assistant", content="never used")])
    feeder, out, thread = run_server(monkeypatch, tmp_path, [], client=client)
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "start"})
    assert client.entered.wait(5)
    feeder.put({"type": "user_input", "text": "parked"})
    out.wait_for("input_pending")
    feeder.put({"type": "interrupt"})
    # the interrupt rides the transport thread; releasing the blocked completion
    # before it has been processed lets the turn finish normally and reply
    assert out.server.cancel.wait(5), "interrupt never reached the server"
    client.release.set()

    end = out.wait_for("turn_end")
    assert end["status"] == "interrupted"
    assert end["reason"] == "user"  # the page's "you stopped it" is earned here
    back = out.wait_for("input_unsent")
    assert back["texts"] == ["parked"]
    # the log answers "who stopped it": the interrupt lands as its own event
    # before the turn ends, naming the transport that delivered it
    rows = [
        json.loads(line)
        for line in (tmp_path / ".bird" / "sessions" / "t" / "events.jsonl")
        .read_text()
        .splitlines()
        if line.strip()
    ]
    hits = [r for r in rows if r["type"] == "interrupt"]
    assert hits, "the interrupt never reached the session log"
    assert hits[0]["data"] == {"source": "stdio"}
    feeder.close()
    thread.join(timeout=5)


class AbortedBySidecarClient:
    """A request torn down by something other than an interrupt — a sidecar's
    watchdog calling abort(reason="critic") — with the reason recorded the way
    OpenAICompatClient records it."""

    abort_reason = "critic"

    def complete(self, spec, messages, **kwargs):
        raise WireAborted("request to http://x aborted (critic)")

    def clear_abort(self):
        pass


def test_a_stop_nobody_asked_for_is_an_error_not_an_interrupt(monkeypatch, tmp_path):
    """The design board said "you interrupted it" for a turn the critic's
    stray abort had killed. A WireAborted with a reason this server never
    recorded is a failure with a culprit, not the user's click."""
    feeder, out, thread = run_server(monkeypatch, tmp_path, [], client=AbortedBySidecarClient())
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "start"})
    end = out.wait_for("turn_end")
    assert end["status"] == "error"
    assert end["reason"] == "critic"
    assert "critic" in end["summary"] and "not by you" in end["summary"]
    rows = [
        json.loads(line)
        for line in (tmp_path / ".bird" / "sessions" / "t" / "events.jsonl")
        .read_text()
        .splitlines()
        if line.strip()
    ]
    assert not [r for r in rows if r["type"] == "interrupt"]
    errors = [r for r in rows if r["type"] == "turn_error"]
    assert errors and errors[0]["data"]["reason"] == "critic"
    feeder.close()
    thread.join(timeout=5)


def test_server_teardown_reports_shutdown_not_the_user(monkeypatch, tmp_path):
    """The transport going away with a turn in flight ends the turn too, but
    that stop is the server's, and turn_end says so."""
    client = BlockingClient([Message(role="assistant", content="never shown")])
    feeder, out, thread = run_server(monkeypatch, tmp_path, [], client=client)
    out.wait_for("ready")
    feeder.put({"type": "user_input", "text": "start"})
    assert client.entered.wait(5)
    feeder.close()  # stdin ends: the transport returns, run() tears down
    assert out.server.cancel.wait(5), "teardown never cancelled the turn"
    client.release.set()
    end = out.wait_for("turn_end")
    assert end["status"] == "interrupted"
    assert end["reason"] == "shutdown"
    thread.join(timeout=5)


class RecordingClient:
    """Answers from a script and keeps the transcript of every call."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def complete(self, spec, messages, **kwargs):
        self.seen.append([(m.role, m.content or "") for m in messages])
        msg = self.script.pop(0)
        return LLMResponse(message=msg, usage=Usage(1, 1), stop_reason="stop", model=spec.spec)


def test_design_board_edits_travel_ahead_of_the_message(tmp_path):
    """A design session's inspector edits reach the designer the way the
    architect's drawn boxes do: composed in ahead of what was typed. The
    design page used to be treated as having no board at all."""
    from types import SimpleNamespace

    client = RecordingClient([Message(role="assistant", content="ok")])
    repl = make_repl(tmp_path, [], client=client)
    drained = []

    def compose():
        drained.append(1)
        return "[the user edited] hero title -> 'Bird'" if len(drained) == 1 else None

    repl.runner.ctx.design = SimpleNamespace(compose_activity_prompt=compose)
    transport = FakeTransport()
    assert Server(repl, transport=transport).run() == 0
    user = [c for r, c in client.seen[-1] if r == "user"][-1]
    assert user.startswith("[the user edited]") and user.endswith("hello?")


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
    out.wait_for("harness_list")
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


def test_serve_model_walk(monkeypatch, tmp_path):
    """bare /model emits the harness step (harness_list); /model <harness>
    emits that harness's model step (model_list) carrying each entry's stored
    thinking level plus the modes per provider, so the TUI runs the thinking
    step itself and answers with '/model <harness> <spec> [mode]'."""
    from bird.llm.discovery import DiscoveredModel

    monkeypatch.setattr(
        "bird.serve.discover_models",
        lambda registry: (
            [DiscoveredModel("fake:model", "configured", 32768), DiscoveredModel("fake:other", "configured", None)],
            ["a note"],
        ),
    )
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/model"})
    msg = out.wait_for("harness_list")
    assert msg["current"] == "code"
    by_name = {h["name"]: h for h in msg["harnesses"]}
    assert list(by_name) == ["code", "arch", "lead", "design"]
    assert by_name["code"] == {
        "name": "code", "alias": "default", "model": "fake:model",
        "think_mode": None, "shared_with": ["lead"],
    }
    assert by_name["design"]["alias"] == "designer" and by_name["design"]["model"] is None

    feeder.put({"type": "command", "line": "/model design"})
    msg = out.wait_for("model_list")
    assert msg["harness"] == "design" and msg["alias"] == "designer"
    assert msg["current"] is None and msg["default"] is None
    assert msg["models"] == [
        {"spec": "fake:model", "source": "configured", "context_window": 32768, "think_mode": None},
        {"spec": "fake:other", "source": "configured", "context_window": None, "think_mode": None},
    ]
    assert msg["notes"] == ["a note"]
    assert msg["think_modes"] == {"fake": ["off", "low", "medium", "high", "max"]}

    # the TUI's answer falls through to the Repl: the alias and thinking
    # level are set, the running (code) model is untouched
    reg = out.server.repl.registry
    reg.providers["fake"] = ProviderConfig(name="fake", base_url="http://x")
    feeder.put({"type": "command", "line": "/model design fake:other low"})
    cmd = out.wait_for("command_output")
    assert "design: fake:other" in cmd["text"] and "thinking: low" in cmd["text"]
    state = out.wait_for("state")
    assert state["model"] == "fake:model" and state["think_mode"] is None
    assert reg.aliases["designer"] == "fake:other"
    assert reg.models["fake:other"]["reasoning_effort"] == "low"
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


def test_serve_mcp_refresh_emits_catalog(monkeypatch, tmp_path):
    """mcp_refresh must emit an mcp_catalog event, not crash: the payload
    builder needs load_mcp_servers/McpError (and catalog_page) imported in
    serve.py — a NameError here used to kill the worker thread silently."""
    from bird.mcp.config import McpError

    monkeypatch.setattr(
        "bird.serve.catalog_page",
        lambda query: ([], {"total": 0, "cache_age": None}),
    )
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "mcp_catalog_refresh", "query": ""})
    msg = out.wait_for("mcp_catalog")
    assert msg["query"] == ""
    assert msg["connected"] == []  # no mcp.json in the temp repo
    assert msg["entries"] == []
    assert msg["registry_error"] is None
    feeder.close()
    thread.join(timeout=5)


def test_serve_bare_mcp_command_emits_catalog(monkeypatch, tmp_path):
    """Bare /mcp must emit an mcp_catalog event via the worker thread, not
    inline on the reader thread — a slow registry used to block the reader
    loop and the command appeared to do nothing."""
    monkeypatch.setattr(
        "bird.serve.catalog_page",
        lambda query: ([], {"total": 0, "cache_age": None}),
    )
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/mcp"})
    msg = out.wait_for("mcp_catalog")
    assert msg["query"] == ""
    assert msg["connected"] == []
    assert msg["entries"] == []
    feeder.close()
    thread.join(timeout=5)


def test_serve_bare_mcp_with_real_catalog_page_offline(monkeypatch, tmp_path):
    """Bare /mcp with the REAL catalog_page (no monkeypatch): offline, the
    registry fetch fails inside _get — which must surface as registry_error
    in an mcp_catalog message, never as silence. This is the regression test
    for the 'no output at all' bug: the worker thread must not die silently
    on a non-McpError, and the transport must flush the emit from that
    thread. The registry base is pointed at a dead local port so the failure
    is fast and deterministic."""
    monkeypatch.setattr("bird.mcp.discover.REGISTRY_BASE", "http://127.0.0.1:1")
    # an empty cache dir: a real ~/.bird cache from a past online /mcp would
    # otherwise satisfy the stale-cache fallback and the test would see entries
    monkeypatch.setattr("bird.mcp.discover.CACHE_DIR", str(tmp_path / "registry-cache"))
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "command", "line": "/mcp"})
    # the instant frame (connected servers + fetching marker) comes first so
    # the view can mount and take focus; the fetched page follows it
    first = out.wait_for("mcp_catalog")
    assert first["fetching"] is True and first["entries"] == []
    msg = out.wait_for("mcp_catalog", where=lambda m: not m.get("fetching"))
    assert msg["query"] == ""
    assert msg["connected"] == []
    assert msg["entries"] == []
    assert msg["registry_error"] and "cannot reach the MCP registry" in msg["registry_error"]
    feeder.close()
    thread.join(timeout=5)


def test_serve_mcp_refresh_reports_config_error(monkeypatch, tmp_path):
    """A broken mcp.json surfaces as an error entry in `connected`, and a
    registry failure lands in registry_error — the view branches on both."""
    from bird.mcp.config import McpError

    monkeypatch.setattr(
        "bird.serve.load_mcp_servers",
        lambda repo_root: (_ for _ in ()).throw(McpError("bad mcp.json")),
    )
    monkeypatch.setattr(
        "bird.serve.catalog_page",
        lambda query: (_ for _ in ()).throw(McpError("offline")),
    )
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "mcp_catalog_refresh", "query": ""})
    msg = out.wait_for("mcp_catalog", where=lambda m: not m.get("fetching"))
    assert msg["connected"] == [{"name": "", "error": "bad mcp.json"}]
    assert msg["registry_error"] == "offline"
    feeder.close()
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


def test_server_does_not_rebind_a_shared_broker(tmp_path):
    """A broker that already has a sink belongs to a parent session (the lead
    dispatching arch). Its requests must keep flowing to the parent's UI, not
    be redirected to the sub-session's transport."""
    from bird.serve import PermissionBroker

    parent_events = []
    broker = PermissionBroker(lambda t, **d: parent_events.append({"type": t, **d}))
    transport = FakeTransport()
    server = Server(make_repl(tmp_path, [], []), transport=transport, broker=broker)
    assert server.broker is broker

    t = threading.Thread(target=lambda: broker.request({"kind": "read_outside_repo"}))
    t.start()
    deadline = time.time() + 5
    while not parent_events and time.time() < deadline:
        time.sleep(0.01)
    assert parent_events and parent_events[0]["type"] == "permission_request"
    assert not [e for e in transport.events if e.get("type") == "permission_request"]
    broker.resolve(parent_events[0]["id"], True)
    t.join(timeout=5)

    # an unbound broker is still bound by the Server, as before
    fresh = PermissionBroker()
    Server(make_repl(tmp_path, [], []), transport=FakeTransport(), broker=fresh)
    assert fresh.bound


def test_serve_mcp_test_reports_unknown_server(monkeypatch, tmp_path):
    """mcp_test (the catalog's reconnect / retry) on a name that is not in
    mcp.json answers with a failed mcp_result rather than silence."""
    monkeypatch.setattr("bird.serve.load_mcp_servers", lambda repo_root: [])
    feeder, out, thread = run_server(monkeypatch, tmp_path, [])
    out.wait_for("ready")
    feeder.put({"type": "mcp_test", "name": "ghost"})
    msg = out.wait_for("mcp_result")
    assert msg["name"] == "ghost"
    assert msg["ok"] is False and msg["installed"] is False
    assert "no server 'ghost'" in msg["why"]
    feeder.close()
    thread.join(timeout=5)


# ---------- tool-call streaming ----------


class _ToolStreamClient:
    """A model writing one tool call: its arguments leave the wire in pieces,
    the way a design_create's html does while the designer is still typing it."""

    def __init__(self, pieces):
        self.pieces = list(pieces)
        self.turns = 0

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None,
                 on_delta=None, on_thinking=None, on_tool_delta=None, **kwargs):
        self.turns += 1
        if self.turns > 1:
            msg = Message(role="assistant", content="done", tool_calls=[])
            if on_delta is not None:
                on_delta("done")
                on_delta(None)
            return LLMResponse(message=msg, usage=Usage(1, 1), stop_reason="stop", model=spec.spec)
        if on_tool_delta is not None:
            for piece in self.pieces:
                on_tool_delta(0, "read", piece)
        if on_delta is not None:
            on_delta(None)
        args = json.loads("".join(self.pieces))
        msg = Message(
            role="assistant", content=None,
            tool_calls=[ToolCall(id="c1", name="read", arguments=args, arguments_json="".join(self.pieces))],
        )
        return LLMResponse(message=msg, usage=Usage(1, 1), stop_reason="tool_calls", model=spec.spec)


def test_server_streams_tool_call_arguments_as_tool_call_delta(tmp_path):
    """Every fragment of a tool call's arguments reaches the UI as its own
    event, in order, before the call's result — that is what lets the design
    page draw an artboard while the model is still writing it. Display-only:
    the recorder never sees the fragments (the `assistant` event carries the
    whole call), same as assistant_delta."""
    transport = FakeTransport()
    client = _ToolStreamClient(['{"pa', 'th": "f', '.py"}'])
    repl = make_repl(tmp_path, [], client=client)
    server = Server(repl, transport=transport)
    assert server.run() == 0
    events = transport.events
    deltas = [e for e in events if e["type"] == "harness_event" and e["event"] == "tool_call_delta"]
    assert [d["data"] for d in deltas] == [
        {"index": 0, "name": "read", "text": '{"pa'},
        {"index": 0, "name": "read", "text": 'th": "f'},
        {"index": 0, "name": "read", "text": '.py"}'},
    ]
    result_at = next(i for i, e in enumerate(events)
                     if e["type"] == "harness_event" and e["event"] == "tool_result")
    assert all(events.index(d) < result_at for d in deltas), "fragments precede the call's result"
    recorded = (tmp_path / ".bird" / "sessions" / "t" / "events.jsonl").read_text()
    assert "tool_call_delta" not in recorded, "fragments are display-only, never transcript"


# ---------- capture round trip ----------


def test_on_capture_resolves_a_design_sessions_waiter(tmp_path):
    """The pump duck-types onto ctx.design like on_mutate: it hands the page's
    png to whichever session owns the capture round trip and never learns
    what a screenshot is."""
    from bird.harnesses.design.session import DesignSession

    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    design = DesignSession()
    repl.runner.ctx.design = design
    requests: list[dict] = []
    design.capture_hook = lambda payload: requests.append(payload)
    out: dict = {}

    def ask():
        out["png"], out["why"] = design.request_capture("hero", "v1")

    t = threading.Thread(target=ask)
    t.start()
    while not requests:
        assert t.is_alive(), "the waiter never emitted its request"
        time.sleep(0.01)
    result = server.on_capture({"id": requests[0]["id"], "png": "data:image/png;base64,AA"})
    t.join(timeout=2)
    assert result["ok"] is True
    assert out["png"] == "data:image/png;base64,AA"


def test_on_capture_without_a_design_session_says_so(tmp_path):
    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    assert server.on_capture({"id": "cap-1", "png": "x"})["ok"] is False


def test_an_interrupt_cancels_a_pending_capture(tmp_path):
    """The turn that asked for the screenshot is going away, so the waiter is
    denied like a pending gate rather than left parked on a png nobody will
    send."""
    from bird.harnesses.design.session import DesignSession

    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    design = DesignSession()
    repl.runner.ctx.design = design
    asked = threading.Event()
    design.capture_hook = lambda payload: asked.set()
    out: dict = {}

    def ask():
        out["png"], out["why"] = design.request_capture("hero", "v1")

    t = threading.Thread(target=ask)
    t.start()
    assert asked.wait(timeout=2)
    server.on_interrupt()
    t.join(timeout=2)
    assert out["png"] is None and "interrupted" in out["why"]


# ---------- the upload route ----------

# Magic bytes + padding: detect_image_mime sniffs the head, so a real encoder
# is not needed to make a file the vision model would accept.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def test_on_upload_saves_into_the_session_and_emits_attachment_saved(tmp_path):
    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    out = server.on_upload(
        {"name": "Shot 1.png", "data": base64.b64encode(PNG_BYTES).decode()})
    assert out["ok"] is True
    saved = tmp_path / ".bird" / "sessions" / "t" / "attachments" / "shot-1.png"
    assert saved.is_file() and saved.read_bytes() == PNG_BYTES
    # the reference is repo-relative — the same shape ingest_images rewrites
    # named paths to, so the page can drop it into the message text and the
    # model sees one consistent spelling of where the attachment is
    assert out["path"] == ".bird/sessions/t/attachments/shot-1.png"
    assert out["size"] == len(PNG_BYTES)
    events = [e for e in server.transport.events if e.get("event") == "attachment_saved"]
    assert events and events[0]["data"]["path"] == out["path"]


def test_on_upload_refuses_non_images_and_bad_payloads(tmp_path):
    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    att_dir = tmp_path / ".bird" / "sessions" / "t" / "attachments"
    # not a raster image — the extension says text, and the shared detector
    # judges an upload exactly as read_image would judge the saved file
    out = server.on_upload(
        {"name": "notes.txt", "data": base64.b64encode(b"just text").decode()})
    assert out["ok"] is False and "raster" in out["error"]
    assert not list((tmp_path / ".bird" / "sessions" / "t" / "attachments").glob("*"))
    # undecodable base64, and no data at all
    assert server.on_upload({"name": "x.png", "data": "!!!"})["ok"] is False
    assert server.on_upload({"name": "x.png"})["ok"] is False
    if att_dir.is_dir():
        assert not list(att_dir.glob("*")), "a refused upload must leave no file behind"


def test_the_upload_reference_is_recognized_by_the_ingest_flow(tmp_path):
    """The page puts the returned reference in the message text; the pump's
    _ingest must leave it exactly as written — in-repo paths are already
    stable, and copying or mangling it would fork the two spellings."""
    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    out = server.on_upload(
        {"name": "shot.png", "data": base64.b64encode(PNG_BYTES).decode()})
    text = f"Take a look at {out['path']}"
    assert server._ingest(text) == text


# ---------- the showcase phase over /mutate ----------


def test_on_mutate_showcase_pushes_then_dispatches_the_polish_turn(tmp_path):
    """The page's pick gesture enters the phase and starts the polish turn —
    the answer_ask pattern. The push happens inside showcase(), so the page
    has swapped to the showcase view before the designer starts writing."""
    from bird.harnesses.design.session import DesignSession

    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    design = DesignSession()
    design.start("a landing page")
    design.add_artboard("<html><body><h1>Hello</h1></body></html>", "Hero")
    repl.runner.ctx.design = design
    order: list[tuple[str, str]] = []
    design.on_state = lambda e: order.append(("push", e["status"]))
    server.on_user_input = lambda text: order.append(("dispatch", text))
    result = server.on_mutate({"op": "showcase", "artboard": "hero"})
    assert result["ok"] is True
    assert design.status == "showcase" and design.state.showcase_artboard == "hero"
    assert [kind for kind, _ in order] == ["push", "dispatch"], "push, then dispatch"
    assert "design_polish" in order[1][1]


def test_on_mutate_showcase_exit_ends_the_phase(tmp_path):
    from bird.harnesses.design.session import DesignSession

    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    design = DesignSession()
    design.start("a landing page")
    design.add_artboard("<html><body><h1>Hello</h1></body></html>", "Hero")
    repl.runner.ctx.design = design
    design.showcase("hero")
    assert server.on_mutate({"op": "showcase_exit"})["ok"] is True
    assert design.status == "ready" and design.state.showcase_artboard == ""


def test_on_mutate_refuses_showcase_and_back_while_a_turn_runs(tmp_path):
    """A queued entry would dispatch a polish turn into a live one, and a
    mid-turn Back would pull the full-bleed view out from under a polish
    pass in flight. The page disables both buttons on the condition its
    finalize button uses; the pump holds the same line for any other caller."""
    from bird.harnesses.design.session import DesignSession

    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    design = DesignSession()
    design.start("a landing page")
    design.add_artboard("<html><body><h1>Hello</h1></body></html>", "Hero")
    repl.runner.ctx.design = design
    server._turn_active = True
    refused = server.on_mutate({"op": "showcase", "artboard": "hero"})
    assert refused["ok"] is False and "turn is running" in refused["error"]
    assert design.status == "ready", "the refusal left the phase untouched"
    design.showcase("hero")
    refused = server.on_mutate({"op": "showcase_exit"})
    assert refused["ok"] is False and "turn is running" in refused["error"]
    assert design.status == "showcase"


# ---------- deleting an artboard over /mutate ----------


def test_on_mutate_delete_artboard_removes_the_card(tmp_path):
    """The page's card control deletes through the same door the model's
    design_delete_artboard tool uses; the harness's selection falls back to
    the first remaining artboard."""
    from bird.harnesses.design.session import DesignSession

    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    design = DesignSession()
    design.start("a landing page")
    design.add_artboard("<html><body><h1>Hello</h1></body></html>", "Hero")
    design.add_artboard("<html><body><h1>Other</h1></body></html>", "Other")
    repl.runner.ctx.design = design
    assert server.on_mutate({"op": "delete_artboard", "artboard": "hero"})["ok"] is True
    assert "hero" not in design.state.artboards
    assert design.state.selected == "other"


def test_on_mutate_refuses_delete_while_a_turn_runs(tmp_path):
    """A card vanishing under a turn in flight would pull the document out
    from under the designer mid-edit — the same line the showcase entry
    holds, and the page's control is gone for the same condition."""
    from bird.harnesses.design.session import DesignSession

    repl = make_repl(tmp_path, [Message(role="assistant", content="hi")])
    server = Server(repl, transport=FakeTransport())
    design = DesignSession()
    design.start("a landing page")
    design.add_artboard("<html><body><h1>Hello</h1></body></html>", "Hero")
    repl.runner.ctx.design = design
    server._turn_active = True
    refused = server.on_mutate({"op": "delete_artboard", "artboard": "hero"})
    assert refused["ok"] is False and "turn is running" in refused["error"]
    assert "hero" in design.state.artboards, "the refusal left the board untouched"
