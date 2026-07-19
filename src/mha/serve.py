"""mha serve — JSON-lines bridge for external UIs (the TUI).

Protocol (one JSON object per line):

  stdin  → {"type": "user_input", "text": "..."}
           {"type": "command", "line": "/model ..."}
           {"type": "permission_response", "id": 3, "approved": true}
           {"type": "interrupt"}

  stdout ← {"type": "ready", "model": ..., "kg": ..., "run_id": ...}
           {"type": "harness_event", "event": "assistant"|"tool_result"|..., "data": {...}}
           {"type": "harness_event", "event": "assistant_delta", "data": {"text": "..."}}
           {"type": "permission_request", "id": 3, "kind": "edit"|"write"|"bash", ...}
           {"type": "turn_end", "status": ..., "summary": ..., "turns": ...}
           {"type": "model_list", "current": ..., "default": ..., "models": [...], "notes": [...]}
           {"type": "command_output", "text": "..."}
           {"type": "bye"}

Turns run in a worker thread so stdin stays responsive for permission
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
from typing import Any

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


class Bridge:
    """Thread-safe JSON-lines writer bound to the real stdout, immune to
    redirect_stdout (used to capture Repl command output)."""

    def __init__(self) -> None:
        self._out = sys.stdout
        self._lock = threading.Lock()

    def emit(self, event_type: str, **data: Any) -> None:
        line = json.dumps({"type": event_type, **data}, ensure_ascii=False, default=str)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()


class PermissionBroker:
    """Blocks a worker-thread tool call until the UI answers."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, list[bool]]] = {}

    def request(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            done = threading.Event()
            slot: list[bool] = [False]
            self._pending[req_id] = (done, slot)
        self.bridge.emit("permission_request", id=req_id, **payload)
        done.wait()
        with self._lock:
            self._pending.pop(req_id, None)
        return slot[0]

    def resolve(self, req_id: int, approved: bool) -> None:
        with self._lock:
            entry = self._pending.get(req_id)
        if entry:
            done, slot = entry
            slot[0] = approved
            done.set()

    def deny_all(self) -> None:
        with self._lock:
            entries = list(self._pending.values())
        for done, slot in entries:
            slot[0] = False
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
        if not self.broker.request(payload):
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
    def __init__(self, repl: Repl):
        self.repl = repl
        self.bridge = Bridge()
        self.broker = PermissionBroker(self.bridge)
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
            self.bridge.emit("harness_event", event=event_type, data=data)
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
                self.bridge.emit("harness_event", event="assistant_delta", data={"text": chunk})

        runner.on_delta = on_delta

    def run(self) -> int:
        repl, bridge = self.repl, self.bridge
        bridge.emit(
            "ready",
            model=repl.runner.spec.spec,
            kg=repl.kg is not None,
            kg_ready=bool(repl.kg and repl.kg.is_ready()),
            run_id=repl.run_id,
            repo=str(repl.runner.ctx.repo_root),
            skills=[
                {"name": s.name, "description": s.description, "source": s.source}
                for s in (repl.runner.ctx.skills or [])
            ],
        )
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                bridge.emit("error", message=f"bad JSON on stdin: {e}")
                continue
            kind = msg.get("type")
            if kind == "user_input":
                self._start_turn(str(msg.get("text", "")))
            elif kind == "permission_response":
                self.broker.resolve(int(msg.get("id", 0)), bool(msg.get("approved")))
            elif kind == "interrupt":
                self.cancel.set()
                self.broker.deny_all()
            elif kind == "command":
                if self._command(str(msg.get("line", ""))) is False:
                    break
            else:
                bridge.emit("error", message=f"unknown message type: {kind!r}")
        self.cancel.set()
        self.broker.deny_all()
        if self.worker:
            self.worker.join(timeout=5)
        bridge.emit("bye")
        return 0

    def _start_turn(self, text: str) -> None:
        if self.worker and self.worker.is_alive():
            self.bridge.emit("error", message="a turn is already running")
            return
        self.cancel.clear()

        def work() -> None:
            try:
                result = self.repl.runner.chat(self.repl.messages, text)
            except _Interrupted:
                repair_interrupted(self.repl.messages)
                self.bridge.emit("turn_end", status="interrupted", summary="", turns=0)
            except Exception as e:  # surface, don't die: the UI owns the terminal
                self.bridge.emit("turn_end", status="error", summary=str(e), turns=0)
            else:
                # persist the transcript so a /reload respawn can resume it
                # (the plain REPL does this too; serve never used to)
                save_messages(
                    [m.to_dict() for m in self.repl.messages],
                    self.repl.recorder.run_dir,
                )
                self.bridge.emit(
                    "turn_end",
                    status=result.status,
                    summary=result.summary,
                    turns=result.turns,
                )

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _command(self, line: str) -> bool | None:
        if self.worker and self.worker.is_alive():
            self.bridge.emit("command_output", text="busy: wait for the turn to finish")
            return None
        if line.strip() in ("/reload", "/reload-skills"):
            # The serve process can't reload its own code in place — the TUI
            # owns the process and respawns `mha serve` fresh from disk. We
            # hand it the current run_id so the respawn resumes this session
            # via --resume (transcript is persisted after every turn).
            self.bridge.emit("reload", run_id=self.repl.run_id)
            return None
        if line.strip() == "/model":
            # bare /model is the picker — the UI renders the selectable list
            # and answers with "/model <spec>"
            models, notes = discover_models(self.repl.registry)
            self.bridge.emit(
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
                self.bridge.emit("command_output", text="no past sessions found")
                return None
            current_id = self.repl.run_id
            self.bridge.emit(
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
                    self.bridge.emit("command_output", text="no past sessions found")
                    return None
                self.bridge.emit(
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
            self.bridge.emit("state", model=self.repl.runner.spec.spec)
            return None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self.repl._command(line)
        self.bridge.emit("command_output", text=buf.getvalue().rstrip())
        self.bridge.emit("state", model=self.repl.runner.spec.spec)
        return result


def serve(repl: Repl) -> int:
    return Server(repl).run()
