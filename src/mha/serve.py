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
or streamed token, whichever comes first.

Permission gating itself lives in mha.permissions and attaches at runner
construction, not here — a Server only supplies the broker (and, for a Repl
built without one, retro-fits the gate as a safety net). See that module for
why: gating in this file left `mha code`, the plain REPL, and every
lead-dispatched sub-session ungated.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from typing import Any, Callable, Protocol

from .engine.runner import repair_interrupted
from .engine.session import save_messages
from .llm.discovery import discover_models
from .permissions import (  # re-exported: importers still say mha.serve.GatedTool
    DIFF_CONTEXT_LINES,
    MAX_DIFF_LINES,
    GatedTool,
    PermissionBroker,
    _diff_lines,
    gate_tools,
    permission_payload,
)
from .repl import Repl
from .tools import Tool, ToolContext, ToolResult

_permission_payload = permission_payload  # back-compat alias


class _Interrupted(Exception):
    pass


class Handlers(Protocol):
    """What a transport delivers inbound messages to (implemented by Server)."""

    def on_user_input(self, text: str) -> None: ...
    def on_permission(self, req_id: int, approved: bool, feedback: str) -> None: ...
    def on_interrupt(self) -> None: ...
    def on_command(self, line: str) -> bool | None: ...
    def on_mutate(self, payload: dict[str, Any]) -> dict[str, Any]: ...


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


class Server:
    """The pump. Transport-agnostic: session logic only."""

    def __init__(
        self,
        repl: Repl,
        transport: Transport | None = None,
        broker: PermissionBroker | None = None,
    ):
        self.repl = repl
        self.transport = transport or StdioTransport()
        # cli.py builds the broker first (the runner has to be gated at
        # construction) and hands it in; bind now that the transport exists.
        self.broker = broker or PermissionBroker()
        self.broker.bind(self._emit)
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None

        runner = repl.runner
        # Safety net for a Repl built without a broker on its ctx (tests,
        # embedders). gate_tools skips anything already wrapped, so a runner
        # gated at build time is untouched here.
        runner.ctx.broker = self.broker
        for name, tool in list(runner.tools.items()):
            runner.tools[name] = gate_tools([tool], self.broker)[0]

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

    def on_mutate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A structured edit made in a UI rather than by the model.

        The pump has no idea what a mutation means. It knows only that a
        harness's state object may accept one, and asks; a session whose
        harness has no such notion (code, a plain REPL) says so and nothing
        breaks. The applying side is responsible for using the same validation
        path the model's tools use — see arch's mutate.py for why that matters.
        """
        target = getattr(self.repl.runner.ctx, "arch", None)
        apply = getattr(target, "apply_mutation", None)
        if apply is None:
            return {"ok": False, "error": "this session has no state a UI can edit"}
        try:
            result = apply(payload)
        except Exception as e:  # a refusal is an answer, not a dead HTTP thread
            return {"ok": False, "error": str(e)}
        return {"ok": True, **(result or {})}

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
