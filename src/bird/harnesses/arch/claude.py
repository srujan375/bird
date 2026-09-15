"""The arch Workbench on Claude Code's loop.

`bird arch --engine claude`. Same page, same board, same seven recording
tools — but the model turn is a `claude -p` process the user already has
installed and logged into, so Claude Code picks the model, runs its own read
and search tools, manages the context window, and bills the user's
subscription exactly as a terminal session would. Bird keeps what Claude Code
has no notion of: the board, the picker, the pinned note, the handoff bundle.

How the two meet:

  page ──/input──▶ ClaudeArchServer ──stdin line──▶ claude -p
  page ◀──SSE──── ClaudeArchServer ◀──stdout lines── claude -p
                        ▲                               │
                        └────── POST /mcp (tools/call) ─┘

Claude Code reaches the board tools over MCP: the transport mounts an
McpEndpoint at /mcp, and the spawn line carries a per-run config file that
points at it with a per-run bearer token. Nothing is written into the user's
settings or the repo's own .mcp.json, and the config dies with the run.

After `handoff`, the conversation is a Claude Code session on disk — the
banner prints `claude --resume <id>` so the build continues in the terminal
on the same context the design was made in.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Sequence

from ...attachments import ATTACHMENTS_DIRNAME, _slugify, _unique_dest, ingest_images
from ...claude_code import (
    INTERRUPT_GRACE_SECONDS,
    ClaudeCodeProcess,
    ClaudeNotFound,
    StreamMapper,
    TurnResult,
    auth_status,
    build_argv,
    find_claude,
    new_session_id,
    new_token,
    write_mcp_config,
)
from ...engine.session import SessionRecorder, new_run_id
from ...http_transport import HttpTransport
from ...mcp.endpoint import McpEndpoint
from ...tools import ToolContext, ToolResult
from ...tools.files import MAX_IMAGE_BYTES, detect_image_mime
from . import derive
from . import harness as arch_def
from .run import HANDED_OFF
from .scribe import MCP_PATH as SCRIBE_MCP_PATH
from .scribe import MCP_SERVER_NAME as SCRIBE_MCP_NAME
from .scribe import Scribe, turn_record
from .session import ArchSession
from .state import LegacyStateError
from .tools import (
    ApproachTool, BriefTool, CanvasTool, DecideTool, HandoffTool, ImportRepoTool,
    QuestionTool, arch_board_tools,
)
from ...tools import Tool, ToolError

MCP_SERVER_NAME = "arch"
MCP_PATH = "/mcp"
MCP_CONFIG_RELATIVE = Path("claude") / "mcp.json"
CLAUDE_SESSION_FILENAME = "claude_session.json"
# what the page prints as the denominator; Claude Code compacts on its own
CONTEXT_WINDOW = 200_000
# the handoff ends the session, not the reading of it (mirrors `bird arch`)
LINGER_SECONDS = 30 * 60
# events the recorder keeps; deltas are display-only (the whole message follows)
LIVE_ONLY = {"assistant_delta", "tool_call_delta", "thinking_delta"}

# Appended after instructions.md. The instructions name bird's own tools;
# under Claude Code the board tools wear the MCP prefix and the fact-finding
# ones are Claude's. Said once here, where it costs nothing.
ADDENDUM = """\
## Running under Claude Code

You are the architect described above, running inside Claude Code with the
board served by bird. Four differences, and nothing else changes:

- The board tools are the `arch` MCP server's: `mcp__arch__canvas`,
  `mcp__arch__approach`, `mcp__arch__decide`, `mcp__arch__question`,
  `mcp__arch__brief`, `mcp__arch__handoff` and `mcp__arch__import_repo`.
  Wherever the instructions say `canvas`, that is the tool they mean.
- Read, Glob and Grep replace `read` and `ls`; WebSearch and WebFetch replace
  `web_search` and `web_fetch`. There is no `kg_query` — grep the repo.
- Never use AskUserQuestion. Park a question with `mcp__arch__question` and
  give it `options`; the page renders the picker, and the answer arrives as
  the next message.
- You cannot edit files here. If the user wants it built, `handoff` — the
  build continues in the terminal afterwards, on this same conversation.

The `[arch]` note arrives at the end of each user message rather than pinned
on its own. Read it before you answer.

## Research before the first question

Your first turn is homework, not a question. Before you ask anything:
`mcp__arch__import_repo` if there is a repo, then Read, Grep and Glob for
what already exists; WebSearch for how this kind of system is built
elsewhere and at what scale. Then put two or three approaches on the board
as whole shapes: `mcp__arch__approach` names each one, with `evidence` —
who runs that shape, at what scale, with sources — and `mcp__arch__canvas`
draws its boxes and wires labelled with that approach's id, with the boxes
every approach shares left unlabelled so they are drawn once. Draw them
yourself in this turn; the fork is asked against shapes the user can see.
Recommend the one that fits the brief's scale, not the one with the best
blog post. Your first question is the fork. Once it is settled, grey the
losers with the reason they lost and walk the winner.

## The question comes last

Say why you would take your row first, then park the question with
`mcp__arch__question`, then stop. Nothing after the question: the picker
sits above the composer, and words that stream after it are words the user
reads while they are trying to answer.
"""

SCRIBE_ADDENDUM = """## The scribe draws

You do not draw in the normal course of a turn. A scribe reads each turn —
your reply, the questions you park, the approaches you name — and puts it on
the board a moment later. So say the shape in your reply when it changes:
the boxes, what connects to what, in one sentence. Say what settled when
something settles. A row the user picks is recorded as a decision on its
own; you need not repeat it, and `mcp__arch__decide` is not yours here.
`mcp__arch__canvas` is still yours for the one case the scribe cannot
cover: something must be on the board *this* turn — the user asked to hand
off and the board is empty, say. Otherwise leave the drawing to the scribe.
"""


def system_prompt(with_scribe: bool = False) -> str:
    text = arch_def.INSTRUCTIONS_PATH.read_text(encoding="utf-8").rstrip() + "\n\n" + ADDENDUM
    if with_scribe:
        text += "\n" + SCRIBE_ADDENDUM
    return text


class DrainingHandoff(Tool):
    """`handoff`, but it waits for the scribe to finish drawing first, so the
    bundle carries the last exchange rather than the one before it."""

    name = HandoffTool.name
    description = HandoffTool.description
    parameters = HandoffTool.parameters

    def __init__(self, scribe: Scribe, inner: Tool | None = None, timeout: float = 90.0) -> None:
        self.scribe = scribe
        self.inner = inner or HandoffTool()
        self.timeout = timeout

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self.scribe.wait_idle(self.timeout):
            raise ToolError(
                "the scribe is still drawing the last exchange — try the handoff again in a moment."
            )
        if not ctx.arch.state.nodes:
            raise ToolError(
                "there is nothing on the board to hand off. Draw the shape you have been "
                "talking about with mcp__arch__canvas now (the scribe only draws after a "
                "turn ends), then hand off."
            )
        return self.inner.run(args, ctx)


def architect_tools(with_scribe: bool) -> list[Tool]:
    """What the architect gets over MCP. With a scribe, the drawing tools are
    the scribe's; the architect keeps the conversation's tools."""
    if not with_scribe:
        return arch_board_tools()
    # canvas stays as the fallback for "it must be on the board this turn";
    # decide is the scribe's alone, and picks are recorded without either
    return [ImportRepoTool(), CanvasTool(), ApproachTool(), QuestionTool(), BriefTool()]


class DedupDecide(Tool):
    """`decide` for the scribe: a topic already decided — by the user's pick,
    or by an earlier job — is not recorded twice. A cheap model repeats what
    it was told is settled; the guard costs nothing and the board stays
    honest."""

    name = DecideTool.name
    description = DecideTool.description
    parameters = DecideTool.parameters

    def __init__(self) -> None:
        self.inner = DecideTool()

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not args.get("id"):
            topic = _norm(str(args.get("topic") or ""))
            for d in ctx.arch.state.decisions:
                if _norm(d.topic) == topic or (
                    _norm(d.choice) == _norm(str(args.get("choice") or "")) and d.source == "user"
                ):
                    return ToolResult(
                        output=f"already recorded as {d.id}: {d.topic} -> {d.choice}. Nothing added.",
                        details={"ok": True, "summary": f"{d.id} already recorded", "duplicate": d.id},
                    )
        return self.inner.run(args, ctx)


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("-", " ").split())


def scribe_tools() -> list[Tool]:
    return [CanvasTool(), DedupDecide()]


def load_claude_session(run_dir: Path) -> str | None:
    """The Claude Code session id a previous bird run was talking to."""
    path = run_dir / CLAUDE_SESSION_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("session_id") or None
    except (OSError, ValueError):
        return None


class ClaudeArchServer:
    """The pump for this engine: the Handlers the transport delivers to, the
    turn thread, and the process it talks to. Transport-agnostic in the same
    way Server is; only the engine differs."""

    def __init__(
        self,
        *,
        arch: ArchSession,
        transport: HttpTransport,
        recorder: SessionRecorder,
        repo_root: Path,
        run_dir: Path,
        run_id: str,
        bin_path: str = "claude",
        model: str | None = None,
        effort: str | None = None,
        max_turns: int | None = None,
        session_id: str | None = None,
        spawn: Callable[..., Any] | None = None,
        scribe_model: str | None = "haiku",
        with_scribe: bool = True,
    ) -> None:
        self.arch = arch
        self.transport = transport
        self.recorder = recorder
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.run_id = run_id
        self.bin_path = bin_path
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self._spawn = spawn

        # A session handed in is one a previous bird run left on disk: resume
        # it. One minted here is fresh until its first turn completes.
        self.session_id = session_id
        self._resumable = session_id is not None
        self.model_name: str | None = None

        self.token = new_token()
        self.mcp_config = run_dir / MCP_CONFIG_RELATIVE
        self.ctx = ToolContext(repo_root=repo_root, run_dir=run_dir, arch=arch, record=recorder.event)
        # structured details per tool, oldest first, for the tool_result the
        # stream reports after the call — the wire only carries the text
        self._details: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self.with_scribe = with_scribe
        self.scribe: Scribe | None = None
        if with_scribe:
            self.scribe = Scribe(
                bin_path=bin_path, repo_root=repo_root, run_dir=run_dir,
                mcp_url=transport.url.rstrip("/") + SCRIBE_MCP_PATH, token=self.token,
                state=lambda: arch.state, emit=transport.emit, record=recorder.event,
                model=scribe_model, spawn=spawn,
            )
            scribe_endpoint = McpEndpoint(
                SCRIBE_MCP_NAME, scribe_tools(), self.ctx, self.token,
                on_result=self.scribe.tool_ran,
            )
            transport.mount(SCRIBE_MCP_PATH, scribe_endpoint.handle)
        tools = architect_tools(with_scribe)
        if self.scribe is not None:
            tools.append(DrainingHandoff(self.scribe))
        else:
            tools.append(HandoffTool())
        self.endpoint = McpEndpoint(
            MCP_SERVER_NAME, tools, self.ctx, self.token, on_result=self._tool_ran
        )
        transport.mount(MCP_PATH, self.endpoint.handle)
        # the events of the turn in flight, for the scribe's record of it
        self._turn_events: list[tuple[str, dict[str, Any]]] = []
        self._research_seen: set[str] = set()

        self.mapper = StreamMapper(
            emit=self._harness_event,
            details_for=self._pop_details,
            on_result=self._turn_result_arrived,
            on_init=self._init_arrived,
            mcp_prefix=f"mcp__{MCP_SERVER_NAME}__",
        )
        self.proc: ClaudeCodeProcess | None = None
        self.usage_in = 0
        self.usage_out = 0
        self._turns_completed = 0

        self._lock = threading.Lock()
        self._turn_active = False
        self._pending: list[str] = []
        self._turn_done = threading.Event()
        self._turn_result: TurnResult | None = None
        self._exit_info: tuple[int, str] | None = None
        self._interrupted = False
        self._shutting_down = False
        self.worker: threading.Thread | None = None

    # ---- events out ----

    def _emit(self, event_type: str, **data: Any) -> None:
        self.transport.emit({"type": event_type, **data})

    def _harness_event(self, event: str, data: dict[str, Any]) -> None:
        if event not in LIVE_ONLY:
            self.recorder.event(event, data)
            if event in ("run_start", "assistant"):
                self._turn_events.append((event, data))
        self._emit("harness_event", event=event, data=data)
        if event == "tool_result" and self._turns_completed == 0:
            self._research_step(str(data.get("name") or ""))

    # The research turn, as the rail shows it: a short list of what the
    # architect is doing, each line once, derived from the tools it reaches
    # for. Only during the first turn; after that the board is the progress.
    RESEARCH_STEPS = (
        ("repo", ("import_repo", "Read", "Glob", "Grep"), "Reading the repo"),
        ("web", ("WebSearch", "WebFetch"), "Looking at how others build this"),
        ("approaches", ("approach",), "Naming the approaches"),
    )

    def _research_step(self, tool_name: str) -> None:
        for step, tools, text in self.RESEARCH_STEPS:
            if tool_name in tools and step not in self._research_seen:
                self._research_seen.add(step)
                self._emit("harness_event", event="research",
                           data={"step": step, "text": text, "done": False})
                return

    def ready_payload(self) -> dict[str, Any]:
        return {
            "model": self.model_name or self.model or "claude code",
            "context_window": CONTEXT_WINDOW,
            "kg": False,
            "kg_ready": False,
            "run_id": self.run_id,
            "repo": str(self.repo_root),
            "skills": [],
            "engine": "claude",
            "scribe": (self.scribe.model or "claude") if self.scribe is not None else None,
            **({"input_tokens": self.usage_in, "output_tokens": self.usage_out}
               if self.usage_in or self.usage_out else {}),
        }

    def run(self) -> int:
        self._emit("ready", **self.ready_payload())
        if self.scribe is not None:
            self.scribe.start()
            self.transport.emit(self.scribe.status())
        self.transport.run(self)
        self._shutting_down = True
        self._interrupted = True
        if self.scribe is not None:
            self.scribe.stop()
        proc = self.proc
        if proc is not None:
            proc.terminate()
        if self.worker:
            self.worker.join(timeout=5)
        self._emit("bye")
        return 0

    # ---- the MCP side ----

    def _tool_ran(self, name: str, args: dict[str, Any], result: ToolResult) -> None:
        self._details[name].append(dict(result.details))

    def _pop_details(self, name: str) -> dict[str, Any] | None:
        q = self._details.get(name)
        return q.popleft() if q else None

    # ---- the process side ----

    def _init_arrived(self, event: dict[str, Any]) -> None:
        self.model_name = str(event.get("model") or "") or self.model_name
        self.session_id = str(event.get("session_id") or "") or self.session_id
        self._save_session()
        # the page shows the model next to the run id; the first ready went
        # out before the process said which one it is
        self._emit("ready", **self.ready_payload())
        for server in event.get("mcp_servers") or []:
            if server.get("name") == MCP_SERVER_NAME and server.get("status") != "connected":
                self._emit(
                    "error",
                    message=f"Claude Code could not reach the board tools "
                            f"(mcp server {MCP_SERVER_NAME!r}: {server.get('status')})",
                )

    def _turn_result_arrived(self, result: TurnResult) -> None:
        self._turn_result = result
        self._turn_done.set()

    def _process_exited(self, code: int, stderr_tail: str) -> None:
        self._exit_info = (code, stderr_tail)
        self._turn_done.set()

    def _ensure_process(self) -> None:
        if self.proc is not None and self.proc.alive:
            return
        write_mcp_config(
            self.mcp_config,
            name=MCP_SERVER_NAME,
            url=self.transport.url.rstrip("/") + MCP_PATH,
            token=self.token,
        )
        resume = self.session_id if (self._resumable or self._turns_completed > 0) else None
        if resume is None:
            self.session_id = new_session_id()
        argv = build_argv(
            bin_path=self.bin_path,
            mcp_config=self.mcp_config,
            mcp_server=MCP_SERVER_NAME,
            system_prompt=system_prompt(with_scribe=self.scribe is not None),
            session_id=None if resume else self.session_id,
            resume=resume,
            model=self.model,
            effort=self.effort,
            max_turns=self.max_turns,
        )
        self._exit_info = None
        self.proc = ClaudeCodeProcess(
            argv, cwd=self.repo_root, on_line=self.mapper.feed,
            on_exit=self._process_exited, spawn=self._spawn,
        )
        self.proc.start()
        self.recorder.event("engine_spawn", {"engine": "claude", "resume": bool(resume)})
        self._save_session()

    def _save_session(self) -> None:
        if not self.session_id:
            return
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / CLAUDE_SESSION_FILENAME).write_text(
                json.dumps({"session_id": self.session_id, "model": self.model_name}),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ---- inbound (the Handlers protocol) ----

    def on_user_input(self, text: str, subjects: Sequence[str] = ()) -> None:
        drawn = self.arch.compose_activity_prompt()
        pointed = self.arch.describe_subjects(subjects) if subjects else None
        typed = self._ingest(text)
        parts = [p for p in (drawn, pointed, typed) if p]
        joined = "\n\n".join(parts)
        if not joined:
            return
        with self._lock:
            parked = self._turn_active
            if parked:
                self._pending.append(joined)
        if parked:
            self._emit("input_pending", text=joined)
            return
        self._start_turn(joined)

    def on_board_submit(self) -> None:
        if self.worker and self.worker.is_alive():
            self._emit("error", message="a turn is already running")
            return
        prompt = self.arch.compose_activity_prompt()
        if prompt:
            self._start_turn(prompt)

    def on_permission(self, req_id: int, approved: bool, feedback: str) -> None:
        pass  # Claude Code answers its own gates (none: dontAsk)

    def on_prompt(self, req_id: int, value: str | None) -> None:
        pass

    def on_command(self, line: str) -> bool | None:
        return None

    def on_interrupt(self, source: str = "unknown") -> None:
        self.recorder.event("interrupt", {"source": source})
        self._interrupted = True
        proc = self.proc
        if proc is None or not proc.alive or not self._turn_active:
            return
        try:
            proc.interrupt()
        except Exception:
            proc.terminate()
            return
        threading.Thread(target=self._enforce_interrupt, daemon=True).start()

    def _enforce_interrupt(self) -> None:
        # the control request is a request; a process that keeps going past
        # the grace is torn down, and the next turn respawns with --resume
        if not self._turn_done.wait(INTERRUPT_GRACE_SECONDS):
            proc = self.proc
            if proc is not None:
                proc.terminate(grace=0)

    def on_mutate(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.arch.apply_mutation(payload)
        except Exception as e:  # a refusal is an answer, not a dead HTTP thread
            return {"ok": False, "error": str(e)}
        return {"ok": True, **(result or {})}

    def on_answer(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.arch.answer_ask(payload) or {}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        text = str(result.pop("input", "") or "")
        if text:
            self.on_user_input(text)
        return {"ok": True, **result}

    def on_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "this session has no capture waiting"}

    def on_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("data")
        if not isinstance(raw, str) or not raw:
            return {"ok": False, "error": "the upload carried no image data"}
        try:
            image = base64.b64decode(raw, validate=True)
        except ValueError as e:
            return {"ok": False, "error": f"the upload is not valid base64: {e}"}
        if not image:
            return {"ok": False, "error": "the upload is empty"}
        if len(image) > MAX_IMAGE_BYTES:
            return {"ok": False,
                    "error": f"the image is over the {MAX_IMAGE_BYTES} byte cap — send a smaller one"}
        name = str(payload.get("name") or "pasted-image.png")
        try:
            dest_dir = self.run_dir / ATTACHMENTS_DIRNAME
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_dest(dest_dir, _slugify(Path(name).stem),
                                Path(name).suffix.lower() or ".png")
            dest.write_bytes(image)
            _, is_raster = detect_image_mime(dest)
            if not is_raster:
                dest.unlink(missing_ok=True)
                return {"ok": False,
                        "error": "the upload is not a raster image the vision model can read"}
        except OSError as e:
            return {"ok": False, "error": f"could not save the attachment: {e}"}
        try:
            rel = dest.relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            rel = dest.relative_to(self.run_dir.resolve()).as_posix()
        self._harness_event("attachment_saved", {"path": rel, "size": len(image), "original": name})
        return {"ok": True, "path": rel, "size": len(image)}

    def _ingest(self, text: str) -> str:
        try:
            rewritten, found = ingest_images(text, self.run_dir, self.repo_root)
        except Exception as e:
            self._harness_event("attachment_failed", {"error": str(e)})
            return text
        for a in found:
            self._harness_event(
                "attachment_saved", {"path": a.path, "size": a.size, "original": a.original}
            )
        return rewritten

    # ---- turns ----

    def _start_turn(self, text: str) -> None:
        if self.worker and self.worker.is_alive():
            if not self._turn_active:
                self.worker.join(timeout=1.0)
            if self.worker.is_alive():
                self._emit("error", message="a turn is already running")
                return
        self._interrupted = False
        with self._lock:
            self._turn_active = True
        self.worker = threading.Thread(target=self._work, args=(text,), daemon=True)
        self.worker.start()

    def _work(self, text: str) -> None:
        prompt = text
        while True:
            ok = self._turn(prompt)
            with self._lock:
                leftover, self._pending = self._pending, []
                if not ok or not leftover:
                    self._turn_active = False
                    break
            for item in leftover:
                self._harness_event("user_injected", {"text": item})
            prompt = "\n\n".join(leftover)
        if not ok and leftover:
            self._emit("input_unsent", texts=leftover)

    def _turn(self, prompt: str) -> bool:
        """One exchange. True when it ended normally (leftover input may follow)."""
        self._turn_events = []
        self._harness_event("run_start", {"task": prompt})
        # The note the bird engine pins: what is settled, what is waiting,
        # which branches are askable, and what the user drew that this turn
        # did not already carry. Claude Code has no pinned slot, so it rides
        # at the end of the message.
        note = derive.note(self.arch.state, self.arch.take_user_edits())
        message = f"{prompt}\n\n{note}"

        self._turn_done.clear()
        self._turn_result = None
        try:
            self._ensure_process()
            self.proc.send_user(message)  # type: ignore[union-attr]
        except (ClaudeNotFound, OSError, RuntimeError) as e:
            self._end_turn_error(str(e))
            return False

        while not self._turn_done.wait(0.25):
            if self._shutting_down:
                break
        result = self._turn_result
        if result is None:
            if self._interrupted:
                self._emit("turn_end", status="interrupted",
                           reason="shutdown" if self._shutting_down else "user",
                           summary="", turns=0)
                return False
            code, tail = self._exit_info or (-1, "")
            self._end_turn_error(
                f"claude exited (code {code}) before finishing the turn"
                + (f":\n{tail}" if tail else "")
            )
            return False

        if self._turns_completed == 0 and self._research_seen:
            for step, _tools, text in self.RESEARCH_STEPS:
                if step in self._research_seen:
                    self._emit("harness_event", event="research",
                               data={"step": step, "text": text, "done": True})
        self._turns_completed += 1
        self.usage_in += result.input_tokens
        self.usage_out += result.output_tokens
        if self.scribe is not None and not self._interrupted:
            record = turn_record(self._turn_events)
            if record:
                self.scribe.enqueue(record)
        if self._interrupted:
            self._emit("turn_end", status="interrupted", reason="user", summary="", turns=result.turns,
                       input_tokens=self.usage_in, output_tokens=self.usage_out)
            return False
        if result.is_error and result.status == "error":
            self.recorder.event("turn_error", {"summary": result.summary, "reason": result.subtype})
        self.recorder.event("turn_end", {"status": result.status, "turns": result.turns})
        self._emit(
            "turn_end",
            status=result.status,
            summary=result.summary,
            turns=result.turns,
            input_tokens=self.usage_in,
            output_tokens=self.usage_out,
        )
        return not result.is_error

    def _end_turn_error(self, summary: str) -> None:
        self.recorder.event("turn_error", {"summary": summary})
        self._emit("turn_end", status="error", summary=summary, turns=0,
                   input_tokens=self.usage_in, output_tokens=self.usage_out)


# ------------------------------------------------------------------ the CLI


def _find_session(sessions_dir: Path, run_id: str) -> Path | None:
    if not sessions_dir.is_dir():
        return None
    for entry in sorted(sessions_dir.iterdir(), key=lambda p: p.stat().st_mtime):
        if entry.name == run_id or entry.name.startswith(run_id + "-"):
            return entry
    return None


def main(args: Any) -> int:
    """`bird arch --engine claude`: the Workbench with Claude Code behind it."""
    import webbrowser

    from .bundle import bundle_paths

    repo_root = Path(args.repo).resolve()
    if not repo_root.is_dir():
        print(f"not a directory: {repo_root}", file=sys.stderr)
        return 2
    try:
        bin_path = find_claude(getattr(args, "claude_bin", None))
    except ClaudeNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    status = auth_status(bin_path)
    if status is not None and status.get("loggedIn") is False:
        print("error: claude is not logged in — run `claude auth login` first", file=sys.stderr)
        return 2

    # bird's aliases mean nothing to Claude Code; only an explicit model
    # name is forwarded, and it picks its own default otherwise
    model = None if args.model in (None, "", "default", "architect") else args.model

    run_id = new_run_id()
    sessions_dir = repo_root / ".bird" / "sessions"
    run_dir = sessions_dir / run_id
    resumed = _find_session(sessions_dir, args.resume) if getattr(args, "resume", None) else None
    if getattr(args, "resume", None) and resumed is None:
        print(f"no session matches {args.resume!r} under {sessions_dir}", file=sys.stderr)
        return 2

    with SessionRecorder(run_dir, model="claude-code") as recorder:
        if resumed is not None:
            try:
                arch = ArchSession.load(resumed)
            except LegacyStateError as e:
                print(f"cannot resume: {e}", file=sys.stderr)
                return 2
            arch.run_dir = run_dir
            claude_session = load_claude_session(resumed)
        else:
            arch = ArchSession(run_dir=run_dir)
            claude_session = None

        transport = HttpTransport(
            static_dir=arch_def.STATIC_DIR, stop_when=HANDED_OFF,
            linger=getattr(args, "linger", LINGER_SECONDS),
            close_when_empty=getattr(args, "close_when_empty", 0.0),
        )
        server = ClaudeArchServer(
            arch=arch, transport=transport, recorder=recorder,
            repo_root=repo_root, run_dir=run_dir, run_id=run_id,
            bin_path=bin_path, model=model, max_turns=getattr(args, "max_turns", None),
            session_id=claude_session,
            with_scribe=not getattr(args, "no_scribe", False),
            scribe_model=getattr(args, "scribe_model", None) or "haiku",
        )

        def on_state(payload: dict[str, Any]) -> None:
            recorder.event(
                "arch_state", {"status": payload["status"], "changed": payload.get("changed")}
            )
            transport.emit(payload)

        arch.on_state = on_state

        scribe_note = "" if getattr(args, "no_scribe", False) else f" | scribe={server.scribe.model}"
        print(f"bird arch | engine=claude code{scribe_note} | session={run_id}"
              + (f" | resumes claude session {claude_session}" if claude_session else ""))
        print(f"page: {transport.url}")
        if not args.no_open:
            webbrowser.open(transport.url)
        if args.task:
            server.on_user_input(args.task)
        elif resumed is None:
            print("note: no task given — send the first message from the page", file=sys.stderr)
        try:
            rc = server.run()
        except KeyboardInterrupt:
            transport.shutdown()
            rc = 0
        time.sleep(0.3)  # let SSE clients drain the closing events
        # The last lines are a contract: a terminal agent that dispatched this
        # run reads them to find the bundle, or to learn there is none.
        if arch.state.handed_off:
            json_path, md_path = bundle_paths(run_dir)
            print("architecture handed off. bundle:")
            print(f"  {json_path}")
            print(f"  {md_path}")
            print(f"handoff: {md_path}")
            if server.session_id:
                print(f"next: claude --resume {server.session_id}   (in {repo_root})")
        else:
            print(f"no handoff: the page closed before the design was handed off. "
                  f"resume it with: bird arch --engine claude --resume {run_id}")
        return rc
