"""mha serve — the session pump plus its transports.

The pump (Server) owns session logic: turns on a worker thread, the
permission broker, interrupts, event tee-ing, transcript persistence. A
Transport owns only how bytes move between the pump and a UI. StdioTransport
is the JSON-lines protocol the TUI speaks (one JSON object per line):

  inbound  → {"type": "user_input", "text": "..."}
             {"type": "command", "line": "/model ..."}
             {"type": "permission_response", "id": 3, "approved": true,
              "feedback": "optional — rejection text returned to the loop"}
             {"type": "interrupt"}

  outbound ← {"type": "ready", "model": ..., "kg": ..., "run_id": ...}
             {"type": "harness_event", "event": "assistant"|"tool_result"|..., "data": {...}}
             {"type": "harness_event", "event": "assistant_delta", "data": {"text": "..."}}
             {"type": "permission_request", "id": 3, "kind": "edit"|"write"|..., ...}
             {"type": "turn_end", "status": ..., "summary": ..., "turns": ...}
             {"type": "model_list", "current": ..., "default": ..., "models": [...], "notes": [...]}
             {"type": "command_output", "text": "..."}
             {"type": "bye"}

HttpTransport (mha.http_transport) carries the same events over SSE + POSTs
for the arch harness's browser page.

Turns run in a worker thread so inbound stays responsive for permission
responses and interrupts. Interrupts take effect at the next harness event
or streamed token, whichever comes first. Permission gating wraps
the mutating tools (edit/write) — bash stays ungated because it is already
category-allowlisted to read-only commands (decision #10).
"""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import sys
import threading
from typing import Any, Callable, Protocol

from .engine.runner import repair_interrupted
from .engine.session import save_messages
from .llm.discovery import discover_models
from .repl import Repl
from .tools import Tool, ToolContext, ToolResult

PERMISSION_TOOLS = {"edit", "write"}
DIFF_CONTEXT_LINES = 2
MAX_DIFF_LINES = 40


class _Interrupted(Exception):
    pass


class Handlers(Protocol):
    """What a transport delivers inbound messages to (implemented by Server)."""

    def on_user_input(self, text: str) -> None: ...
    def on_permission(self, req_id: int, approved: bool, feedback: str) -> None: ...
    def on_interrupt(self) -> None: ...
    def on_command(self, line: str) -> bool | None: ...


class Transport(Protocol):
    """How bytes move between the pump and a UI. No session knowledge."""

    def emit(self, event: dict[str, Any]) -> None: ...
    def run(self, handlers: Handlers) -> None: ...


class StdioTransport:
    """JSON lines over stdin/stdout — the TUI's protocol, byte-compatible
    with the pre-split Server. Binds the real stdout at construction so
    redirect_stdout (used to capture Repl command output) can't steal it."""

    def __init__(self) -> None:
        self._out = sys.stdout
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def run(self, handlers: Handlers) -> None:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                self.emit({"type": "error", "message": f"bad JSON on stdin: {e}"})
                continue
            kind = msg.get("type")
            if kind == "user_input":
                handlers.on_user_input(str(msg.get("text", "")))
            elif kind == "permission_response":
                handlers.on_permission(
                    int(msg.get("id", 0)),
                    bool(msg.get("approved")),
                    str(msg.get("feedback", "") or ""),
                )
            elif kind == "interrupt":
                handlers.on_interrupt()
            elif kind == "command":
                if handlers.on_command(str(msg.get("line", ""))) is False:
                    break
            else:
                self.emit({"type": "error", "message": f"unknown message type: {kind!r}"})


class PermissionBroker:
    """Blocks a worker-thread tool call until the UI answers. The answer is
    (approved, feedback); feedback carries "Request changes" text on
    rejection of the arch gates and is empty otherwise."""

    def __init__(self, emit: Callable[..., None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, list[Any]]] = {}

    def request(self, payload: dict[str, Any]) -> tuple[bool, str]:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            done = threading.Event()
            slot: list[Any] = [False, ""]
            self._pending[req_id] = (done, slot)
        self._emit("permission_request", id=req_id, **payload)
        done.wait()
        with self._lock:
            self._pending.pop(req_id, None)
        return slot[0], slot[1]

    def resolve(self, req_id: int, approved: bool, feedback: str = "") -> None:
        with self._lock:
            entry = self._pending.get(req_id)
        if entry:
            done, slot = entry
            slot[0] = approved
            slot[1] = feedback
            done.set()

    def deny_all(self) -> None:
        with self._lock:
            entries = list(self._pending.values())
        for done, slot in entries:
            slot[0] = False
            slot[1] = ""
            done.set()


def _diff_lines(old: str, new: str, n: int = DIFF_CONTEXT_LINES) -> list[dict[str, str]]:
    kinds = {"+": "add", "-": "del", " ": "ctx"}
    out: list[dict[str, str]] = []
    for line in difflib.unified_diff(
        old.splitlines(), new.splitlines(), n=n, lineterm=""
    ):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append({"kind": kinds.get(line[:1], "ctx"), "text": line})
        if len(out) >= MAX_DIFF_LINES:
            out.append({"kind": "ctx", "text": "… (diff truncated)"})
            break
    return out


def _permission_payload(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if name == "edit":
        return {
            "kind": "edit",
            "file": args.get("path", "?"),
            "lines": _diff_lines(args.get("old_text", ""), args.get("new_text", "")),
        }
    if name == "write":
        path = args.get("path", "?")
        content = args.get("content", "")
        old = ""
        with contextlib.suppress(Exception):
            p = ctx.resolve_path(path)
            if p.is_file():
                old = p.read_text(encoding="utf-8", errors="replace")
        return {"kind": "write", "file": path, "lines": _diff_lines(old, content)}
    return {"kind": "bash", "cmd": args.get("command", name)}


class GatedTool(Tool):
    """Wraps a tool so execution waits for UI approval."""

    def __init__(self, inner: Tool, broker: PermissionBroker):
        self.inner = inner
        self.broker = broker
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        payload = _permission_payload(self.name, args, ctx)
        approved, _feedback = self.broker.request(payload)
        if not approved:
            ctx.emit("permission_denied", {"tool": self.name, "args": args})
            return ToolResult(
                output=(
                    f"The user DENIED permission for this {self.name}. Do not retry "
                    "the same change; ask the user or try a different approach."
                ),
                details={"denied": True},
                is_error=True,
            )
        return self.inner.execute(args, ctx)


class Server:
    """The pump. Transport-agnostic: session logic only."""

    def __init__(self, repl: Repl, transport: Transport | None = None):
        self.repl = repl
        self.transport = transport or StdioTransport()
        self.broker = PermissionBroker(self._emit)
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None

        runner = repl.runner
        # gate mutating tools
        for name in list(runner.tools):
            if name in PERMISSION_TOOLS:
                runner.tools[name] = GatedTool(runner.tools[name], self.broker)

        # tee harness events to the UI; honor interrupts between events
        recorder_event = repl.recorder.event

        def record(event_type: str, data: dict[str, Any]) -> None:
            recorder_event(event_type, data)
            self._emit("harness_event", event=event_type, data=data)
            if self.cancel.is_set():
                raise _Interrupted()

        runner.ctx.record = record

        # stream assistant text to the UI; deltas skip the session recorder
        # (the "assistant" event carries the full content) but still honor
        # interrupts so a long generation can be cancelled mid-token
        def on_delta(chunk: str | None) -> None:
            if self.cancel.is_set():
                raise _Interrupted()
            if chunk:  # "" is a wire-level cancel heartbeat, not display text
                self._emit("harness_event", event="assistant_delta", data={"text": chunk})

        runner.on_delta = on_delta

    def _emit(self, event_type: str, **data: Any) -> None:
        self.transport.emit({"type": event_type, **data})

    def ready_payload(self) -> dict[str, Any]:
        repl = self.repl
        return {
            "model": repl.runner.spec.spec,
            "kg": repl.kg is not None,
            "kg_ready": bool(repl.kg and repl.kg.is_ready()),
            "run_id": repl.run_id,
            "repo": str(repl.runner.ctx.repo_root),
            "skills": [
                {"name": s.name, "description": s.description, "source": s.source}
                for s in (repl.runner.ctx.skills or [])
            ],
        }

    def run(self) -> int:
        self._emit("ready", **self.ready_payload())
        self.transport.run(self)
        self.cancel.set()
        self.broker.deny_all()
        if self.worker:
            self.worker.join(timeout=5)
        self._emit("bye")
        return 0

    # ---- inbound handlers (the Handlers protocol) ----

    def on_user_input(self, text: str) -> None:
        self._start_turn(text)

    def on_permission(self, req_id: int, approved: bool, feedback: str) -> None:
        self.broker.resolve(req_id, approved, feedback)

    def on_interrupt(self) -> None:
        self.cancel.set()
        self.broker.deny_all()

    def on_command(self, line: str) -> bool | None:
        return self._command(line)

    # ---- session logic ----

    def _start_turn(self, text: str) -> None:
        if self.worker and self.worker.is_alive():
            self._emit("error", message="a turn is already running")
            return
        self.cancel.clear()

        def work() -> None:
            try:
                result = self.repl.runner.chat(self.repl.messages, text)
            except _Interrupted:
                repair_interrupted(self.repl.messages)
                self._emit("turn_end", status="interrupted", summary="", turns=0)
            except Exception as e:  # surface, don't die: the UI owns the terminal
                self._emit("turn_end", status="error", summary=str(e), turns=0)
            else:
                # persist the transcript so a /reload respawn can resume it
                # (the plain REPL does this too; serve never used to)
                save_messages(
                    [m.to_dict() for m in self.repl.messages],
                    self.repl.recorder.run_dir,
                )
                self._emit(
                    "turn_end",
                    status=result.status,
                    summary=result.summary,
                    turns=result.turns,
                )

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _command(self, line: str) -> bool | None:
        if self.worker and self.worker.is_alive():
            self._emit("command_output", text="busy: wait for the turn to finish")
            return None
        if line.strip() in ("/reload", "/reload-skills"):
            # The serve process can't reload its own code in place — the TUI
            # owns the process and respawns `mha serve` fresh from disk. We
            # hand it the current run_id so the respawn resumes this session
            # via --resume (transcript is persisted after every turn).
            self._emit("reload", run_id=self.repl.run_id)
            return None
        if line.strip() == "/model":
            # bare /model is the picker — the UI renders the selectable list
            # and answers with "/model <spec>"
            models, notes = discover_models(self.repl.registry)
            self._emit(
                "model_list",
                current=self.repl.runner.spec.spec,
                default=self.repl.registry.aliases.get("default"),
                models=[
                    {"spec": m.spec, "source": m.source, "context_window": m.context_window}
                    for m in models
                ],
                notes=notes,
            )
            return None
        if line.strip() == "/sessions":
            # bare /sessions is the picker — the UI renders the selectable list
            # and answers with "/continue <id>". Mirrors /model: the REPL has
            # the data, but a JSON bridge can't prompt; the TUI does the
            # interactive picking. We also handle an optional substring filter
            # by forwarding it to _list_sessions via a direct text match.
            sessions = self.repl._list_sessions()
            if not sessions:
                self._emit("command_output", text="no past sessions found")
                return None
            current_id = self.repl.run_id
            self._emit(
                "session_list",
                current=current_id,
                sessions=[
                    {"id": s["id"], "name": s["name"], "last_event": s["last_event"]}
                    for s in sessions
                ],
            )
            return None
        if line.startswith("/continue"):
            arg = line[len("/continue"):].strip()
            if not arg:
                # bare /continue: emit the same picker so the TUI can render
                # it. The TUI answers with "/continue <id>".
                sessions = self.repl._list_sessions()
                if not sessions:
                    self._emit("command_output", text="no past sessions found")
                    return None
                self._emit(
                    "session_list",
                    current=self.repl.run_id,
                    sessions=[
                        {"id": s["id"], "name": s["name"], "last_event": s["last_event"]}
                        for s in sessions
                    ],
                )
                return None
            # resume by id — delegate to Repl which loads messages and carries
            # the recorded model.
            self.repl._resume_session(arg)
            self._emit("state", model=self.repl.runner.spec.spec)
            return None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self.repl._command(line)
        self._emit("command_output", text=buf.getvalue().rstrip())
        self._emit("state", model=self.repl.runner.spec.spec)
        return result


def serve(repl: Repl) -> int:
    return Server(repl).run()
