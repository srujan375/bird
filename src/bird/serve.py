"""bird serve — the session pump plus its transports.

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
             {"type": "turn_end", "status": ..., "summary": ..., "turns": ...,
              "input_tokens": ..., "output_tokens": ...}
             {"type": "total_usage", "input_tokens": ..., "output_tokens": ...}
             {"type": "model_list", "current": ..., "default": ..., "models": [...], "notes": [...]}
             {"type": "command_output", "text": "..."}
             {"type": "bye"}

HttpTransport (bird.http_transport) carries the same events over SSE + POSTs
for the arch harness's browser page.

Turns run in a worker thread so inbound stays responsive for permission
responses and interrupts. Interrupts take effect at the next harness event
or streamed token, whichever comes first.

Permission gating itself lives in bird.permissions and attaches at runner
construction, not here — a Server only supplies the broker (and, for a Repl
built without one, retro-fits the gate as a safety net). See that module for
why: gating in this file left `bird code`, the plain REPL, and every
lead-dispatched sub-session ungated.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from collections.abc import Sequence
from typing import Any, Callable, Protocol

from .attachments import ingest_images
from .engine.runner import repair_interrupted
from .engine.session import save_messages
from .llm.discovery import discover_models
from .llm.types import Usage
from .llm.wire.openai_compat import WireAborted
from .onboard import Prompter, TransportIO
from .permissions import (  # re-exported: importers still say bird.serve.GatedTool
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

    def on_user_input(self, text: str, subjects: Sequence[str] = ()) -> None: ...
    def on_permission(self, req_id: int, approved: bool, feedback: str) -> None: ...
    def on_prompt(self, req_id: int, value: str | None) -> None: ...
    def on_interrupt(self) -> None: ...
    def on_command(self, line: str) -> bool | None: ...
    def on_mutate(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def on_board_submit(self) -> None: ...


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
            elif kind == "prompt_response":
                value = msg.get("value")
                handlers.on_prompt(int(msg.get("id", 0)), None if value is None else str(value))
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
        # questions the setup walkthrough asks the UI (keys, model pick):
        # same blocking round-trip as permissions
        self.prompter = Prompter(self._emit)

        runner = repl.runner
        # Safety net for a Repl built without a broker on its ctx (tests,
        # embedders). gate_tools skips anything already wrapped, so a runner
        # gated at build time is untouched here.
        runner.ctx.broker = self.broker
        for name, tool in list(runner.tools.items()):
            runner.tools[name] = gate_tools([tool], self.broker)[0]

        # Session-cumulative token spend across every harness this session
        # runs. A Server's own runner contributes nothing here — its per-turn
        # usage lands in every turn_end carried by `_start_turn` — but the
        # harnesses it spawns mid-turn report themselves: the lead's `code`
        # fork pushes its RunResult deltas via a `usage_notify` record event
        # (it knows about this total because the fork carries it — see
        # harnesses/lead/tools.py).
        self.usage = Usage()

        # tee harness events to the UI; honor interrupts between events
        recorder_event = repl.recorder.event

        def record(event_type: str, data: dict[str, Any]) -> None:
            if event_type == "usage_notify":
                # A session-local notification, not transcript material: fold
                # the deltas in and publish the running total, but never tee
                # to the recorder or the UI as a harness_event (double-count
                # risk aside, no transcript wants fake tokens on the record).
                self.usage += Usage(
                    int(data.get("input_tokens", 0) or 0),
                    int(data.get("output_tokens", 0) or 0),
                )
                self._emit(
                    "total_usage",
                    input_tokens=self.usage.input_tokens,
                    output_tokens=self.usage.output_tokens,
                )
                if self.cancel.is_set():
                    raise _Interrupted()
                return
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
                # Repl._turn re-prints the reply unless it knows the text
                # already reached the user. Taking over on_delta without
                # setting this is what made a `/<skill>` turn print its answer
                # a second time; keep the flag honest for any caller that
                # still routes a turn through the Repl.
                self.repl._streamed = True
                self._emit("harness_event", event="assistant_delta", data={"text": chunk})

        # stream the reasoning trace (Ollama thinking models) to the UI the
        # same way: display-only, skipping the recorder (the recorder-bound
        # "thinking" event carries the full text), honoring interrupts on
        # every token so a long thought can be cancelled mid-stream
        def on_thinking(chunk: str | None) -> None:
            if self.cancel.is_set():
                raise _Interrupted()
            if chunk:  # "" heartbeat / None sentinel are wire-level, not display
                self._emit("harness_event", event="thinking_delta", data={"text": chunk})

        runner.on_delta = on_delta
        runner.on_thinking = on_thinking

    def _emit(self, event_type: str, **data: Any) -> None:
        self.transport.emit({"type": event_type, **data})

    @staticmethod
    def _alias_spec(repl: Any, alias: str) -> str | None:
        """The model behind a registry alias, or None if it does not resolve —
        a session with no critic must not claim one."""
        try:
            return repl.runner.registry.resolve(alias).spec
        except Exception:
            return None

    def ready_payload(self) -> dict[str, Any]:
        repl = self.repl
        payload: dict[str, Any] = {
            "model": repl.runner.spec.spec,
            # the page prints "12.4k / 40k" on every turn divider: compaction
            # fires at 90% of this, so the denominator has to be on screen
            # before it does rather than explained after the fact
            "context_window": repl.runner.spec.context_window,
            "kg": repl.kg is not None,
            "kg_ready": bool(repl.kg and repl.kg.is_ready()),
            "run_id": repl.run_id,
            "repo": str(repl.runner.ctx.repo_root),
            # the friendly thinking-mode label (off/low/medium/high/max) or
            # None when no mode is set (Ollama's auto/default behavior). The
            # TUI shows it next to the model name; the plain REPL doesn't.
            "think_mode": repl._think_label(),
            "skills": [
                {"name": s.name, "description": s.description, "source": s.source}
                for s in (repl.runner.ctx.skills or [])
            ],
        }
        # A respawn (--resume) re-opens the same session; the spend so far
        # was reported by the previous pump's last turn_end, so seed it back
        # into this one instead of the UI showing 0 / 0 until the next turn.
        # Absent when zero: a fresh ready from a server that never knew this
        # field looks identical to one that spent nothing.
        if self.usage.input_tokens or self.usage.output_tokens:
            payload["input_tokens"] = self.usage.input_tokens
            payload["output_tokens"] = self.usage.output_tokens
        return payload

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

    def on_user_input(self, text: str, subjects: Sequence[str] = ()) -> None:
        # Anything drawn since the last turn travels with what was typed: they
        # are one message, and splitting them into two turns would have the
        # architect answer half of it at a time.
        #
        # `subjects` is what the page had selected when Send was pressed. It
        # goes in ahead of the words so "why this one?" arrives already knowing
        # which one — the selection is the page's, and a question that leans on
        # it is unanswerable without it.
        drawn = self._board_edits()
        pointed = self._board_focus(subjects)
        typed = self._ingest(text)
        parts = [p for p in (drawn, pointed, typed) if p]
        self._start_turn("\n\n".join(parts))

    def on_board_submit(self) -> None:
        """Send what the user drew, because they said to.

        Drawing is talking, but only the user knows when they have finished a
        sentence. Inferring it from a pause spends a model call on an
        unfinished thought and gets an answer to something nobody had finished
        saying — so this happens when they ask for it, and not before.
        """
        if self.worker and self.worker.is_alive():
            self._emit("error", message="a turn is already running")
            return
        prompt = self._board_edits()
        if prompt:
            self._start_turn(prompt)

    def _board_edits(self) -> str | None:
        """What the user has drawn that the architect has not been shown."""
        arch = getattr(self.repl.runner.ctx, "arch", None)
        return getattr(arch, "compose_activity_prompt", lambda: None)()

    def _board_focus(self, subjects: Sequence[str]) -> str | None:
        """What the user had selected, described. getattr like _board_edits:
        a harness with no board has nothing to point at."""
        if not subjects:
            return None
        arch = getattr(self.repl.runner.ctx, "arch", None)
        describe = getattr(arch, "describe_subjects", None)
        return describe(subjects) if describe is not None else None

    def on_permission(self, req_id: int, approved: bool, feedback: str) -> None:
        self.broker.resolve(req_id, approved, feedback)

    def on_prompt(self, req_id: int, value: str | None) -> None:
        self.prompter.resolve(req_id, value)

    def on_interrupt(self) -> None:
        self.cancel.set()
        self.broker.deny_all()
        self.prompter.cancel_all()
        # the flag above is only seen when a chunk arrives; a provider that
        # has gone quiet leaves the worker blocked in a socket read that
        # nothing else can wake — tear the request down from here
        abort = getattr(self.repl.runner.client, "abort", None)
        if abort is not None:
            abort()

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

    def _ingest(self, text: str) -> str:
        """Copy any image the user just named into the session, and point the
        text at the copy. Runs on the transport thread, before the turn starts:
        a screenshot's temp file has to be captured while it still exists, and
        by the time the worker thread is asking for approval it may not."""
        run_dir = getattr(self.repl.recorder, "run_dir", None)
        try:
            rewritten, found = ingest_images(text, run_dir, self.repl.runner.ctx.repo_root)
        except Exception as e:  # ingestion is a convenience; never lose the turn
            self._emit("harness_event", event="attachment_failed", data={"error": str(e)})
            return text
        for a in found:
            self._emit(
                "harness_event",
                event="attachment_saved",
                data={"path": a.path, "size": a.size, "original": a.original},
            )
        return rewritten

    def _start_turn(self, text: str) -> None:
        if self.worker and self.worker.is_alive():
            self._emit("error", message="a turn is already running")
            return
        self.cancel.clear()
        clear_abort = getattr(self.repl.runner.client, "clear_abort", None)
        if clear_abort is not None:
            clear_abort()

        def work() -> None:
            try:
                result = self.repl.runner.chat(self.repl.messages, text)
            except (_Interrupted, WireAborted):
                repair_interrupted(self.repl.messages)
                self._emit("turn_end", status="interrupted", summary="", turns=0)
            except Exception as e:  # surface, don't die: the UI owns the terminal
                summary = str(e)
                if "401" in summary or "Unauthorized" in summary or "not reachable" in summary:
                    summary += " — /setup configures a key or picks a local model; /doctor explains"
                self._emit("turn_end", status="error", summary=summary, turns=0)
            else:
                # persist the transcript so a /reload respawn can resume it
                # (the plain REPL does this too; serve never used to)
                save_messages(
                    [m.to_dict() for m in self.repl.messages],
                    self.repl.recorder.run_dir,
                )
                # What this turn actually spent, session-cumulative. Mid-turn
                # sessions (the lead's dispatches) already
                # pushed their deltas onto server.usage while the loop was
                # running — the remainder, if any, is this runner's own loop.
                self.usage = self.usage + result.usage
                self._emit(
                    "turn_end",
                    status=result.status,
                    summary=result.summary,
                    turns=result.turns,
                    input_tokens=self.usage.input_tokens,
                    output_tokens=self.usage.output_tokens,
                )

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _start_setup(self, keys_only: str | None = None) -> None:
        if self.worker and self.worker.is_alive():
            self._emit("error", message="a turn is already running")
            return
        self.cancel.clear()
        tio = TransportIO(self._emit, self.prompter)

        def work() -> None:
            try:
                if keys_only:
                    self._run_repl_command(lambda: self.repl._cmd_keys(f"set {keys_only}", io=tio))
                else:
                    self.repl._cmd_setup(io=tio)
            except Exception as e:  # noqa: BLE001
                self._emit("error", message=f"setup failed: {e}")
            finally:
                self._emit("state", model=self.repl.runner.spec.spec, think_mode=self.repl._think_label())
                self._emit("setup_end")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _run_repl_command(self, fn) -> None:
        """Run a Repl command whose output is print()ed, forwarding it as
        command_output."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        if buf.getvalue().strip():
            self._emit("command_output", text=buf.getvalue().rstrip())

    def _command(self, line: str) -> bool | None:
        if self.worker and self.worker.is_alive():
            self._emit("command_output", text="busy: wait for the turn to finish")
            return None
        # `/<skill>` is a model turn wearing a slash, so it runs through
        # _start_turn like typed input — NOT through the redirect_stdout path
        # at the bottom of this method. Two reasons, both load-bearing:
        # the turn's reply is already streamed to the UI as assistant_delta,
        # so capturing Repl._turn's print of the same text and echoing it as
        # command_output showed the whole answer twice (once rendered, once
        # raw); and running it inline blocks the transport's reader loop, so
        # a permission prompt or an interrupt mid-skill could never arrive.
        # Built-ins keep priority — a skill named "model" must not shadow
        # /model, same rule the Repl's own dispatch chain enforces.
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        if cmd not in self.repl.BUILTIN_COMMANDS and self.repl._is_skill_command(cmd):
            arg = self._ingest(parts[1].strip()) if len(parts) > 1 else ""
            prompt = self.repl.skill_prompt(cmd[1:], arg)
            if prompt is None:  # raced a skill reload; say so instead of hanging
                self._emit("command_output", text=f"no skill named {cmd[1:]!r}")
            else:
                self._start_turn(prompt)
            return None
        if line.strip() in ("/reload", "/reload-skills"):
            # The serve process can't reload its own code in place — the TUI
            # owns the process and respawns `bird serve` fresh from disk. We
            # hand it the current run_id so the respawn resumes this session
            # via --resume (transcript is persisted after every turn).
            self._emit("reload", run_id=self.repl.run_id)
            return None
        if line.strip() == "/setup":
            # the walkthrough asks questions, so it runs off the reader
            # thread (which has to stay free to deliver the answers) — the
            # same reason a model turn does
            self._start_setup()
            return None
        if line.startswith("/keys set") and len(line.split()) == 3:
            # `/keys set NAME` with no value: ask for it masked, off-thread
            name = line.split()[2]
            self._start_setup(keys_only=name)
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
        if line.strip() == "/think":
            # bare /think is the picker — the UI renders the selectable list
            # and answers with "/think <mode>". Mirrors /model: the REPL has
            # the modes, but a JSON bridge can't prompt; the TUI does the
            # interactive picking. /think <mode> falls through to the generic
            # _command path (which calls _cmd_think -> _set_think_mode).
            self._emit(
                "think_list",
                current=self.repl._think_label(),
                modes=list(self.repl.think_modes()),
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
            self._emit("state", model=self.repl.runner.spec.spec, think_mode=self.repl._think_label())
            return None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self.repl._command(line)
        self._emit("command_output", text=buf.getvalue().rstrip())
        self._emit("state", model=self.repl.runner.spec.spec, think_mode=self.repl._think_label())
        return result


def serve(repl: Repl) -> int:
    return Server(repl).run()
