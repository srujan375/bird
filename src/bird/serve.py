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
             {"type": "harness_event", "event": "tool_call_delta",
              "data": {"index": 0, "name": "design_create", "text": "..."}}
             {"type": "permission_request", "id": 3, "kind": "edit"|"write"|..., ...}
             {"type": "turn_end", "status": ..., "summary": ..., "turns": ...,
              "input_tokens": ..., "output_tokens": ...}
             {"type": "total_usage", "input_tokens": ..., "output_tokens": ...}
             {"type": "harness_list", "current": ..., "harnesses": [{"name", "alias", "model", "think_mode", "shared_with"}]}
             {"type": "model_list", "harness": ..., "alias": ..., "current": ..., "default": ...,
              "models": [{"spec", "source", "context_window", "think_mode"}], "notes": [...],
              "think_modes": {provider: [...]}}
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

import base64
import contextlib
import io
import json
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Protocol

from .attachments import ATTACHMENTS_DIRNAME, _slugify, _unique_dest, ingest_images
from .engine.runner import repair_interrupted
from .engine.session import save_messages
from .llm.discovery import discover_models
from .llm.types import Usage
from .llm.wire.openai_compat import WireAborted
from .mcp.config import McpError, load_mcp_servers, parse_servers
from .mcp.discover import catalog_page
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
from .tools.files import MAX_IMAGE_BYTES, detect_image_mime

_permission_payload = permission_payload  # back-compat alias


class _Interrupted(Exception):
    pass


class Handlers(Protocol):
    """What a transport delivers inbound messages to (implemented by Server)."""

    def on_user_input(self, text: str, subjects: Sequence[str] = ()) -> None: ...
    def on_permission(self, req_id: int, approved: bool, feedback: str) -> None: ...
    def on_prompt(self, req_id: int, value: str | None) -> None: ...
    # source: transport-level detail for the session log's interrupt event —
    # which route the stop arrived on ("stdio", "/interrupt")
    def on_interrupt(self, source: str = "unknown") -> None: ...
    def on_command(self, line: str) -> bool | None: ...
    def on_mutate(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def on_answer(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def on_capture(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    # an image the page uploaded (a paste or drop in the composer); served
    # getattr-style, so a Handlers without it answers 404 rather than crashing
    def on_upload(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def on_board_submit(self) -> None: ...
    def on_mcp_refresh(self, query: str) -> None: ...
    def on_mcp_install(self, name: str) -> None: ...
    def on_mcp_remove(self, name: str) -> None: ...
    def on_mcp_test(self, name: str) -> None: ...


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
                handlers.on_interrupt("stdio")
            elif kind == "mcp_catalog_refresh":
                handlers.on_mcp_refresh(str(msg.get("query", "")))
            elif kind == "mcp_install":
                handlers.on_mcp_install(str(msg.get("name", "")))
            elif kind == "mcp_remove":
                handlers.on_mcp_remove(str(msg.get("name", "")))
            elif kind == "mcp_test":
                handlers.on_mcp_test(str(msg.get("name", "")))
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
        # A broker that already has a sink is SHARED (a sub-session running
        # on its parent's broker): leave it pointed at the UI that answers
        # gates. Rebinding it to this transport is how the lead's arch page —
        # which has no permission UI — used to swallow read_outside_repo
        # requests and hang the whole session.
        if not getattr(self.broker, "bound", False):
            self.broker.bind(self._emit)
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        # questions the setup walkthrough asks the UI (keys, model pick):
        # same blocking round-trip as permissions
        self.prompter = Prompter(self._emit)
        # Mid-turn user input: text typed while a run is working parks here and
        # the runner drains it at the top of its next step. It lives on THIS
        # side of the wire on purpose — a client polled at each step boundary
        # would answer after the step had already started, so the message would
        # always land a step later than the one that asked for it.
        self._pending: list[str] = []
        self._pending_lock = threading.Lock()
        # True only while a model turn owns the worker (not setup, not the mcp
        # catalog). Guarded by _pending_lock so admitting an injection and
        # ending the turn can't interleave: nothing is ever parked in a buffer
        # that nobody is left to drain.
        self._turn_active = False
        # Who stopped the turn in flight, for the turn_end it produces: "user"
        # once on_interrupt lands, "shutdown" once run() is tearing down.
        # Anything else that tears a request down — a sidecar's abort, a flag
        # left armed — is NOT an interrupt and is not reported as one: the
        # page used to say "you interrupted it" for a stop the user never
        # made. Reset per turn in _start_turn.
        self._interrupted_by: str | None = None
        self._shutting_down = False

        runner = repl.runner
        # Safety net for a Repl built without a broker on its ctx (tests,
        # embedders). gate_tools skips anything already wrapped, so a runner
        # gated at build time is untouched here.
        runner.ctx.broker = self.broker
        runner.ctx.pending_input = self._drain_pending
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

        # stream a tool call's arguments the same way: display-only (the
        # `assistant` event carries every call whole), honoring interrupts on
        # every fragment. This is what lets the design page draw an artboard
        # while the designer is still writing it — `index` is the call's slot
        # in the message, `name` the function as far as it has been announced,
        # `text` the next piece of its arguments_json.
        def on_tool_delta(index: int, name: str, chunk: str) -> None:
            if self.cancel.is_set():
                raise _Interrupted()
            self._emit(
                "harness_event", event="tool_call_delta",
                data={"index": index, "name": name, "text": chunk},
            )

        runner.on_delta = on_delta
        runner.on_thinking = on_thinking
        runner.on_tool_delta = on_tool_delta

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
        self._shutting_down = True
        self.cancel.set()
        self.broker.deny_all()
        self.prompter.cancel_all()
        # same as an interrupt: a capture the page will never answer must not
        # hold the worker (and the join below) on a png nobody will send
        self._cancel_design_capture("shutdown")
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
        joined = "\n\n".join(parts)
        if not joined:
            return
        with self._pending_lock:
            parked = self._turn_active
            if parked:
                self._pending.append(joined)
        if parked:
            # not a new turn — the running one picks this up at its next step
            self._emit("input_pending", text=joined)
            return
        self._start_turn(joined)

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
        """What the user has done on the board that the model has not been
        shown — boxes drawn for the architect, or inspector edits made to a
        design artboard. getattr like _board_focus: a harness with no board
        has nothing to report, and the design page used to be treated that
        way even though its user edits the very document the designer works
        on (a "now match the other card" arrived against a document the model
        did not know had changed)."""
        for attr in ("arch", "design"):
            target = getattr(self.repl.runner.ctx, attr, None)
            compose = getattr(target, "compose_activity_prompt", None)
            if compose is not None:
                return compose()
        return None

    def _board_focus(self, subjects: Sequence[str]) -> str | None:
        """What the user had selected, described. getattr like _board_edits:
        a harness with no board has nothing to point at."""
        if not subjects:
            return None
        for attr in ("arch", "design"):
            target = getattr(self.repl.runner.ctx, attr, None)
            describe = getattr(target, "describe_subjects", None)
            if describe is not None:
                return describe(subjects)
        return None

    def on_permission(self, req_id: int, approved: bool, feedback: str) -> None:
        self.broker.resolve(req_id, approved, feedback)

    def on_prompt(self, req_id: int, value: str | None) -> None:
        self.prompter.resolve(req_id, value)

    def on_interrupt(self, source: str = "unknown") -> None:
        # the log must be able to answer "who stopped it": a turn that ends
        # with turn_end(status=interrupted) and no record of the interrupt
        # landing reads as the model failing on its own — exactly the
        # confusion a mis-aimed click produces. One event per interrupt, with
        # whatever transport detail the caller has; server shutdown does not
        # route through here.
        self.repl.recorder.event("interrupt", {"source": source})
        self._interrupted_by = "user"  # before the flag: the worker reads both
        self.cancel.set()
        self.broker.deny_all()
        self.prompter.cancel_all()
        # a capture waiter parked on the page is the same kind of pending
        # round trip as a gate: the turn that asked is going away, so deny it
        # rather than leave the tool blocked on a png nobody will send
        self._cancel_design_capture("interrupted")
        # the flag above is only seen when a chunk arrives; a provider that
        # has gone quiet leaves the worker blocked in a socket read that
        # nothing else can wake — tear the request down from here
        abort = getattr(self.repl.runner.client, "abort", None)
        if abort is not None:
            abort()

    def _cancel_design_capture(self, reason: str) -> None:
        design = getattr(self.repl.runner.ctx, "design", None)
        cancel = getattr(design, "cancel_capture", None)
        if cancel is not None:
            cancel(reason)

    def _stop_reason(self) -> str:
        """Who ended the turn that just raised _Interrupted / WireAborted.

        The two stops this server makes are recorded as they happen (see
        _interrupted_by). Anything else is read off the client's abort flag:
        a named reason ("critic", "watchdog") is the culprit; a bare or
        "user"-reasoned abort nobody here recorded is "unknown" — still not
        the user, and reported as a failure rather than pinned on them."""
        if self._interrupted_by is not None:
            return self._interrupted_by
        if self._shutting_down:
            return "shutdown"
        why = getattr(self.repl.runner.client, "abort_reason", None)
        if why in (None, "user"):
            return "unknown"
        return str(why)

    def on_command(self, line: str) -> bool | None:
        return self._command(line)

    def _turn_busy(self) -> bool:
        """A designer turn is in flight. The page disables its showcase and
        Back buttons on the same condition it disables finalize with; this
        holds that line for any other caller of /mutate."""
        return bool(self._turn_active or (self.worker and self.worker.is_alive()))

    def on_mutate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A structured edit made in a UI rather than by the model.

        The pump has no idea what a mutation means. It knows only that a
        harness's state object may accept one, and asks; a session whose
        harness has no such notion (code, a plain REPL) says so and nothing
        breaks. The applying side is responsible for using the same validation
        path the model's tools use — see arch's mutate.py for why that matters.
        A session with no arch state falls through to its design session, if it
        has one — same duck-typed deal, with `finalize` picking the artboard.
        """
        target = getattr(self.repl.runner.ctx, "arch", None)
        apply = getattr(target, "apply_mutation", None)
        if apply is None:
            design = getattr(self.repl.runner.ctx, "design", None)
            if design is None:
                return {"ok": False, "error": "this session has no state a UI can edit"}
            try:
                op = payload.get("op")
                if op == "finalize":
                    design.finalize(payload.get("artboard"))
                    return {"ok": True}
                if op == "showcase":
                    # push, then dispatch — the answer_ask pattern. showcase()
                    # has already pushed status=showcase by the time it
                    # returns, so the page has swapped to the showcase view
                    # before the polish turn starts. A queued entry would
                    # dispatch that turn into a live one, so the same line
                    # the page's disabled button holds is held here too.
                    if self._turn_busy():
                        return {"ok": False,
                                "error": "a turn is running — wait for it before showcasing"}
                    turn = design.showcase(payload.get("artboard"))
                    if turn:
                        self.on_user_input(turn)
                    return {"ok": True}
                if op == "showcase_exit":
                    # Back is the entry guard's mirror: a mid-turn exit would
                    # pull the full-bleed view out from under a polish pass in
                    # flight, and the designer was told the canvas is gone
                    if self._turn_busy():
                        return {"ok": False,
                                "error": "a turn is running — wait for it before going back"}
                    design.showcase_exit()
                    return {"ok": True}
                if op == "delete_artboard":
                    # a board-level deletion is deliberate and irreversible —
                    # no op-undo sits behind it — so the page confirms before
                    # sending, and the pump holds the same mid-turn line the
                    # showcase entry holds: a card vanishing under a turn in
                    # flight would pull the document out from under the
                    # designer mid-edit
                    if self._turn_busy():
                        return {"ok": False,
                                "error": "a turn is running — wait for it before deleting"}
                    design.delete_artboard(payload.get("artboard"))
                    return {"ok": True}
                if op == "undo":
                    result = design.undo(payload.get("artboard"))
                else:
                    result = design.apply_edit(payload.get("artboard"), payload)
            except Exception as e:  # a refusal is an answer, not a dead HTTP thread
                return {"ok": False, "error": str(e)}
            return {"ok": True, **result}
        try:
            result = apply(payload)
        except Exception as e:  # a refusal is an answer, not a dead HTTP thread
            return {"ok": False, "error": str(e)}
        return {"ok": True, **(result or {})}

    def on_answer(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A row taken in a UI's picker.

        Same duck-typed deal as on_mutate: whichever harness state object owns
        questions settles it and hands back the turn the answer unblocks, if
        any. The pump does not know what the question was about — it only
        knows that an answer can start a turn, which is the one thing a state
        object is not allowed to do for itself.

        This is what makes the questions arrive one at a time: the answer goes
        to the harness, the harness pushes the next question with its state,
        and the page renders whatever is pending. Nothing on the page decides
        how far the conversation has got.
        """
        for attr in ("arch", "design"):
            target = getattr(self.repl.runner.ctx, attr, None)
            answer = getattr(target, "answer_ask", None)
            if answer is None:
                continue
            try:
                result = answer(payload) or {}
            except Exception as e:  # a refusal is an answer, not a dead HTTP thread
                return {"ok": False, "error": str(e)}
            text = str(result.pop("input", "") or "")
            if text:
                self.on_user_input(text)
            return {"ok": True, **result}
        return {"ok": False, "error": "this session has no question on the table"}

    def on_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The page's answer to a capture_request: {id, png|error}.

        Same duck-typed deal as on_mutate and on_answer: whichever harness
        state object owns the capture round trip settles it, and a session
        with no such object (code, arch, a plain REPL) says so. The pump never
        learns what a screenshot is — it only knows a png arrived and where
        to hand it."""
        for attr in ("design",):
            target = getattr(self.repl.runner.ctx, attr, None)
            resolve = getattr(target, "resolve_capture", None)
            if resolve is None:
                continue
            try:
                return {"ok": True, **(resolve(payload) or {})}
            except Exception as e:  # a refusal is an answer, not a dead HTTP thread
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "this session has no capture waiting"}

    def on_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """An image the page uploaded — a paste or a drop in the composer.

        The page holds the bytes and no filesystem; the session holds the run
        dir. Saving here puts the image in the same attachments/ dir the
        path-ingest flow uses, and the reference returned is the same
        repo-relative shape ingest_images rewrites named paths to — so the
        page can drop it into the message text and _ingest leaves it exactly
        as written (in-repo paths are already stable), with the model seeing
        one consistent spelling of "where the attachment is".
        """
        run_dir = getattr(self.repl.recorder, "run_dir", None)
        if run_dir is None:
            return {"ok": False, "error": "this session has no run dir to save attachments into"}
        raw = payload.get("data")
        if not isinstance(raw, str) or not raw:
            return {"ok": False, "error": "the upload carried no image data"}
        try:
            image = base64.b64decode(raw, validate=True)
        except ValueError as e:  # binascii.Error is a ValueError
            return {"ok": False, "error": f"the upload is not valid base64: {e}"}
        if not image:
            return {"ok": False, "error": "the upload is empty"}
        if len(image) > MAX_IMAGE_BYTES:
            # read_image refuses anything bigger anyway; refusing here says so
            # before the bytes are written, not after the model hits the cap
            return {"ok": False,
                    "error": f"the image is over the {MAX_IMAGE_BYTES} byte cap — send a smaller one"}
        name = str(payload.get("name") or "pasted-image.png")
        try:
            dest_dir = run_dir / ATTACHMENTS_DIRNAME
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_dest(dest_dir, _slugify(Path(name).stem),
                                Path(name).suffix.lower() or ".png")
            dest.write_bytes(image)
            # the same detector the file tools use, so an upload is judged
            # exactly as read_image would judge the saved file — a dropped
            # pdf or text file is refused before the model ever sees a path
            _, is_raster = detect_image_mime(dest)
            if not is_raster:
                dest.unlink(missing_ok=True)
                return {"ok": False,
                        "error": "the upload is not a raster image the vision model can read"}
        except OSError as e:
            return {"ok": False, "error": f"could not save the attachment: {e}"}
        try:
            rel = dest.relative_to(Path(self.repl.runner.ctx.repo_root).resolve()).as_posix()
        except ValueError:
            # a run dir outside the repo (tests, embedders): the reference is
            # still a path the model can resolve, and ingest treats it like
            # any out-of-repo image named in a message
            rel = dest.relative_to(Path(run_dir).resolve()).as_posix()
        self._emit(
            "harness_event",
            event="attachment_saved",
            data={"path": rel, "size": len(image), "original": name},
        )
        return {"ok": True, "path": rel, "size": len(image)}

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

    def _drain_pending(self) -> list[str]:
        """Take everything the user has typed since the last drain, oldest
        first. Called by the runner on the worker thread; filled by
        `on_user_input` on the transport thread."""
        with self._pending_lock:
            texts, self._pending = self._pending, []
        return texts

    def _start_turn(self, text: str) -> None:
        if self.worker and self.worker.is_alive():
            # A turn thread that has already published its last turn_end is
            # microseconds from exiting; input that arrives in that window is
            # not a concurrent turn, so wait for it rather than refusing.
            if not self._turn_active:
                self.worker.join(timeout=1.0)
            if self.worker.is_alive():
                self._emit("error", message="a turn is already running")
                return
        self.cancel.clear()
        self._interrupted_by = None
        clear_abort = getattr(self.repl.runner.client, "clear_abort", None)
        if clear_abort is not None:
            clear_abort()

        with self._pending_lock:
            self._turn_active = True

        def work() -> None:
            prompt = text
            while True:
                try:
                    result = self.repl.runner.chat(self.repl.messages, prompt)
                except (_Interrupted, WireAborted):
                    repair_interrupted(self.repl.messages)
                    reason = self._stop_reason()
                    if reason in ("user", "shutdown"):
                        self._emit(
                            "turn_end", status="interrupted", reason=reason, summary="", turns=0
                        )
                    else:
                        # Nobody in the room stopped this. Say so as a
                        # failure with the culprit named, not as the interrupt
                        # the page would otherwise credit to the user — that
                        # misattribution is how a critic's stray abort read as
                        # "you interrupted it" on the design board.
                        summary = (
                            f"the model request was aborted by '{reason}', not by you — "
                            "send your message again to continue"
                        )
                        self.repl.recorder.event(
                            "turn_error", {"summary": summary, "reason": reason}
                        )
                        self._emit(
                            "turn_end", status="error", reason=reason, summary=summary, turns=0
                        )
                    break
                except Exception as e:  # surface, don't die: the UI owns the terminal
                    summary = str(e)
                    if "401" in summary or "Unauthorized" in summary or "not reachable" in summary:
                        summary += " — /setup configures a key or picks a local model; /doctor explains"
                    self.repl.recorder.event("turn_error", {"summary": summary})
                    self._emit("turn_end", status="error", summary=summary, turns=0)
                    break
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
                # Text that arrived during the run's LAST model call missed
                # every step boundary the loop had left. Calling the turn done
                # while it sits in the buffer would strand it until the user
                # typed again — run it as its own turn instead. The flag drops
                # under the same lock that admits injections, so the window
                # where input could be parked with nobody left to drain it
                # does not exist.
                with self._pending_lock:
                    leftover, self._pending = self._pending, []
                    if not leftover:
                        self._turn_active = False
                        return
                # The UI marks a bubble as in-flight the moment it ships one and
                # retires it on user_injected. This text never reached the loop's
                # drain, so nothing else will ever emit that — say it here, or the
                # bubble stays dim forever over a message that is about to run.
                for item in leftover:
                    self._emit("harness_event", event="user_injected", data={"text": item})
                prompt = "\n\n".join(leftover)
            # interrupted or errored: whatever is still parked was never shown
            # to the model. Hand it back instead of firing it into the next
            # turn — the user just stopped something, and auto-sending into
            # the wreckage is the footgun the held queue exists to prevent.
            with self._pending_lock:
                unsent, self._pending = self._pending, []
                self._turn_active = False
            if unsent:
                self._emit("input_unsent", texts=unsent)

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

    # ------------------------------------------------------- MCP catalog

    def _mcp_connected(self) -> list[dict[str, Any]]:
        """Configured servers with live connection state: name, source,
        disabled, and either the live tool count (from the session's mounted
        client) or a not-connected marker. Local-only — works offline."""
        clients = {c.spec.name: c for c in self.repl.runner.ctx.mcp_clients}
        try:
            specs = load_mcp_servers(self.repl.runner.ctx.repo_root)
        except McpError as e:
            return [{"name": "", "error": str(e)}]
        out: list[dict[str, Any]] = []
        for spec in specs:
            client = clients.get(spec.name)
            connected = bool(client is not None and client._alive())
            out.append({
                "name": spec.name,
                "source": spec.source,
                "disabled": spec.disabled,
                "connected": connected,
                "tools": len(client.tools) if client is not None else 0,
                "command": spec.command,
                "args": list(spec.args),
                "env": sorted(spec.env),
            })
        return out

    def _mcp_catalog_payload(self, query: str) -> dict[str, Any]:
        """Everything the catalog view needs in one message: connected
        servers (local), registry entries (cached when offline), page info,
        and the degraded-state markers the view branches on."""
        payload: dict[str, Any] = {
            "query": query,
            "connected": self._mcp_connected(),
            "entries": [],
            "total": 0,
            "cache_age": None,
            "registry_error": None,
        }
        try:
            entries, info = catalog_page(query)
            payload["entries"] = [asdict(e) for e in entries]
            payload["total"] = info.get("total", len(entries))
            payload["cache_age"] = info.get("cache_age")
        except McpError as e:
            # offline: connected servers still show (they're local config);
            # the view shows the error + retry instead of the registry list
            payload["registry_error"] = str(e)
        return payload

    def on_mcp_refresh(self, query: str) -> None:
        """Re-fetch the catalog (retry after an error, or a new search).
        Runs on the worker thread — a slow registry must not block inbound."""
        if self.worker and self.worker.is_alive():
            self._emit("command_output", text="busy: wait for the current operation to finish")
            return
        # the view mounts NOW with the local facts (connected servers) and a
        # fetching marker — the prototype's slow-network state — so focus and
        # keystrokes land in the catalog rather than the chat bar while the
        # registry answers; the worker then sends the real page.
        self._emit("mcp_catalog", query=query, connected=self._mcp_connected(),
                   entries=[], total=None, cache_age=None, registry_error=None,
                   fetching=True)
        self._start_worker(lambda: self._emit("mcp_catalog", **self._mcp_catalog_payload(query)))

    def on_mcp_install(self, name: str) -> None:
        """Install a registry server. The TUI has ALREADY shown the exact
        command and env vars and obtained an explicit 'y' — this is the
        post-confirmation write + connection test, reported as mcp_result."""
        def work() -> None:
            from .mcp.management import install_from_registry, test_connection

            repo_root = self.repl.runner.ctx.repo_root
            try:
                entry, warnings = install_from_registry(name, repo_root, confirm=True)
            except McpError as e:
                self._emit("mcp_result", name=name, ok=False, why=str(e),
                           fix=[], log=[], installed=False)
                return
            # parse the fresh entry into a spec and try to connect — the
            # result view needs tool count or the failure + server log
            spec = parse_servers({"servers": {name: entry}}, Path("<entry>"), "project")[0]
            ok, tools, error, log = test_connection(spec)
            self._emit(
                "mcp_result",
                name=name,
                ok=ok,
                installed=True,
                tools=[t.get("name", "?") for t in tools],
                why=error,
                fix=[w for w in warnings],
                log=log,
            )

        self._start_worker(work)

    def on_mcp_remove(self, name: str) -> None:
        """Remove a server from mcp.json (the catalog's 'x' key)."""
        def work() -> None:
            from .mcp.management import remove_server

            try:
                remove_server(name, self.repl.runner.ctx.repo_root)
                self._emit("mcp_result", name=name, ok=True, removed=True)
            except McpError as e:
                self._emit("mcp_result", name=name, ok=False, removed=False, why=str(e))

        self._start_worker(work)

    def on_mcp_test(self, name: str) -> None:
        """Re-test a configured server (the catalog's 'i reconnect' on a
        Connected row and 'r retry' on a failed result card): read its spec
        from mcp.json, start it, list tools, report as mcp_result."""
        def work() -> None:
            from .mcp.management import test_connection

            try:
                specs = load_mcp_servers(self.repl.runner.ctx.repo_root)
            except McpError as e:
                self._emit("mcp_result", name=name, ok=False, installed=True,
                           why=str(e), fix=[], log=[])
                return
            spec = next((s for s in specs if s.name == name), None)
            if spec is None:
                self._emit("mcp_result", name=name, ok=False, installed=False,
                           why=f"no server '{name}' in mcp.json", fix=[], log=[])
                return
            ok, tools, error, log = test_connection(spec)
            self._emit("mcp_result", name=name, ok=ok, installed=True,
                       tools=[t.get("name", "?") for t in tools],
                       why=error, fix=[], log=log)

        self._start_worker(work)

    def _start_worker(self, fn) -> None:
        """Run fn on the worker thread (registry/network work must not block
        the reader loop). Mirrors _start_setup's shape."""
        if self.worker and self.worker.is_alive():
            self._emit("command_output", text="busy: wait for the current operation to finish")
            return
        self.cancel.clear()

        def run() -> None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self._emit("error", message=f"mcp catalog: {e}")

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _command(self, line: str) -> bool | None:
        if self.worker and self.worker.is_alive():
            self._emit("command_output", text="busy: wait for the current turn or catalog fetch to finish")
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
            # bare /model is the harness step of the picker walk — the UI
            # renders the list and answers with "/model <harness>"
            self._emit(
                "harness_list",
                current=self.repl._running_harness(),
                harnesses=self.repl._harness_rows(),
            )
            return None
        model_words = line.split()
        if (
            len(model_words) == 2
            and model_words[0] == "/model"
            and model_words[1] in self.repl._harness_aliases()
        ):
            # /model <harness> is the model step: the UI renders the list and
            # answers with "/model <harness> <spec> [mode]". The thinking
            # step needs no round-trip — each entry carries its stored level
            # and think_modes says what each provider accepts.
            harness = model_words[1]
            alias = self.repl._harness_aliases()[harness]
            models, notes = discover_models(self.repl.registry)
            providers = {m.spec.split(":", 1)[0] for m in models}
            self._emit(
                "model_list",
                harness=harness,
                alias=alias,
                current=self.repl._current_model_for(harness),
                default=self.repl.registry.aliases.get(alias),
                models=[
                    {
                        "spec": m.spec,
                        "source": m.source,
                        "context_window": m.context_window,
                        "think_mode": self.repl._think_label_for(m.spec),
                    }
                    for m in models
                ],
                notes=notes,
                think_modes={p: list(self.repl.think_modes_for_provider(p)) for p in sorted(providers)},
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
        if line.strip() == "/mcp":
            # bare /mcp opens the catalog view — the TUI renders it and
            # answers with mcp_install/mcp_remove/mcp_catalog_refresh.
            # /mcp search|add|remove fall through to the text path below
            # (the REPL's own fallbacks, captured as command_output).
            # Route through on_mcp_refresh so the registry fetch happens
            # on the worker thread — a slow registry must not block the
            # reader loop (the live-worker guard above makes this safe).
            self.on_mcp_refresh("")
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
