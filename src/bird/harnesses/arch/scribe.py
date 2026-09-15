"""The scribe: a second, cheap model that draws the board one step behind
the conversation.

Why it exists. Under the Claude engine every drawing call sits between the
user's answer and the architect's next question. Moving the drawing to a
background model keeps the architect's turn to three sentences and one
question, and gives the page a board that fills in beside the conversation.
The user asked for this on 2026-09-15, revising the earlier "no second model"
ruling for this one job.

What it reads. Never the transcript. Each job gets the records of the turns
since the last job (the user's words, the architect's reply, the questions
it parked and the approaches it named), plus the note-sized board summary.
Jobs coalesce: three answers given while one job runs become one job with
three records, which is what keeps "one step behind" from becoming five and
keeps the token bill flat.

What it writes. Only through the board tools, over its own MCP endpoint, so
its edits go through the same validation and the same state push as the
architect's and the user's. It owns `canvas` and `decide`; the architect keeps
`question`, `approach`, `brief`, `import_repo` and `handoff`. A handoff waits
for the queue to drain, so the bundle carries the last exchange.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ...claude_code import ClaudeCodeProcess, StreamMapper, TurnResult, build_argv, write_mcp_config
from ...tools import ToolResult
from . import derive
from .state import ArchState

MCP_PATH = "/mcp/scribe"
MCP_SERVER_NAME = "board"
DEFAULT_MODEL = "haiku"
RECENT_SHOWN = 3
# a job that has not finished in this long is killed and reported; the next
# job carries its records again
JOB_TIMEOUT_SECONDS = 180.0
# how many turn records one job may carry; older ones are dropped first, and
# the note-sized summary carries what they settled
MAX_RECORDS_PER_JOB = 6
MAX_RECORD_CHARS = 2400

SYSTEM_PROMPT = """\
You are the scribe for an architecture session. An architect is designing a
system with a user on a shared board. You never talk to either of them. You
read what was just said and put it on the board with two tools:

- `mcp__board__canvas`: boxes and wires. Batch a whole shape into one call.
  Use stable kebab-case ids (the same box keeps the same id across turns), and
  send only the fields that changed. `kind` is one of: service, store, queue,
  api, ui, llm, external, infra, group. `depth` is one of: stub, sketch,
  detailed — set it when the conversation reaches a box's details. Label
  boxes that belong to one approach with `approaches: [id]`; leave shared
  boxes unlabelled.
- `mcp__board__decide`: a call that got made — topic, choice, what it was
  weighed against, and why, in the architect's or the user's own words.

Rules:
- Draw only what was said. Never invent a box, a wire, or a reason.
- If the architect described a shape in words, draw that shape. If the user
  picked a row, the decision is already recorded; draw what it implies.
- Existing boxes are background: do not redraw them, only wire to them.
- An approach is never a box. The architect names approaches with its own
  tool; you only label the boxes that belong to one with `approaches: [id]`.
- A row the user picked is already recorded as a decision. Do not `decide`
  it again. Draw what it implies, if anything.
- Keep each box to what was said: `label`, `kind`, one line of
  `responsibility`, `tech` if named. No `facts`, `items` or `detail` unless
  the conversation gave them. Short strings. Speed matters more than polish;
  the architect deepens boxes later.
- One canvas call for the whole turn, one decide per call that got made. Do
  not call tools you do not need. If nothing on the board changes, make no
  calls.
- Never ask questions. Never write prose. When you are done, reply with the
  single word: done.
"""

# what the page shows; kept small and stable
STATE_DRAWING = "drawing"
STATE_IDLE = "idle"
STATE_ERROR = "error"


class Scribe:
    """The queue, the runner thread, and the process it spawns per job."""

    def __init__(
        self,
        *,
        bin_path: str,
        repo_root: Path,
        run_dir: Path,
        mcp_url: str,
        token: str,
        state: Callable[[], ArchState],
        emit: Callable[[dict[str, Any]], None],
        record: Callable[[str, dict[str, Any]], None] | None = None,
        model: str | None = DEFAULT_MODEL,
        spawn: Callable[..., Any] | None = None,
    ) -> None:
        self.bin_path = bin_path
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.mcp_url = mcp_url
        self.token = token
        self.model = model
        self._state = state
        self._emit = emit
        self._record = record
        self._spawn = spawn
        self.mcp_config = run_dir / "claude" / "scribe-mcp.json"

        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop = False
        self._thread: threading.Thread | None = None
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_SHOWN)
        self.jobs = 0
        self.last_error: str | None = None

    # ---- what the arch server calls ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="arch-scribe")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def enqueue(self, record: str) -> None:
        """A turn's worth of conversation for the scribe to draw from."""
        with self._lock:
            self._queue.append(record[:MAX_RECORD_CHARS])
            while len(self._queue) > MAX_RECORDS_PER_JOB:
                self._queue.popleft()
            self._idle.clear()
        self._push(STATE_DRAWING)
        self._wake.set()

    def tool_ran(self, name: str, args: dict[str, Any], result: ToolResult) -> None:
        """The endpoint's hook: what the scribe just drew, for the page's
        activity strip and the session log."""
        if self._record is not None:
            self._record("scribe_call", {
                "name": name, "is_error": result.is_error,
                "summary": str(result.details.get("summary") or result.output.split("\n")[0])[:200],
                "args": json.dumps(args, ensure_ascii=False)[:600],
            })
        if result.is_error:
            return
        summary = str(result.details.get("summary") or name).replace("Board: ", "")
        ids = [str(s) for s in result.details.get("subjects") or []]
        self.recent.append({"text": summary.rstrip("."), "ids": ids})

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until every queued record has been drawn (or the timeout
        passes). What `handoff` calls before writing the bundle."""
        return self._idle.wait(timeout)

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            queued = len(self._queue)
        state = STATE_ERROR if self.last_error else (STATE_DRAWING if self.busy else STATE_IDLE)
        payload: dict[str, Any] = {
            "type": "scribe", "state": state, "queued": queued,
            "recent": list(self.recent), "model": self.model,
        }
        if self.last_error:
            payload["error"] = self.last_error
        return payload

    # ---- the runner ----

    def _push(self, state: str, error: str | None = None) -> None:
        self.last_error = error
        self._emit(self.status())

    def _run(self) -> None:
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            while not self._stop:
                with self._lock:
                    if not self._queue:
                        self._idle.set()
                        break
                    records = list(self._queue)
                    self._queue.clear()
                try:
                    self._job(records)
                except Exception as e:  # the board is never held hostage by the scribe
                    self._push(STATE_ERROR, f"couldn't draw the last change ({type(e).__name__}: {e})")
                    if self._record is not None:
                        self._record("scribe_error", {"error": str(e)})
                with self._lock:
                    left = len(self._queue)
                if left == 0:
                    self._idle.set()
                    if not self.last_error:
                        self._push(STATE_IDLE)
                    break

    def _job(self, records: list[str]) -> None:
        self.jobs += 1
        write_mcp_config(self.mcp_config, name=MCP_SERVER_NAME, url=self.mcp_url, token=self.token)
        argv = build_argv(
            bin_path=self.bin_path, mcp_config=self.mcp_config, mcp_server=MCP_SERVER_NAME,
            system_prompt=SYSTEM_PROMPT, model=self.model, max_turns=6,
            builtin_tools=(), effort="low",
        )
        done = threading.Event()
        result: list[TurnResult] = []
        exited: list[tuple[int, str]] = []

        def on_result(r: TurnResult) -> None:
            result.append(r)
            done.set()

        def on_exit(code: int, tail: str) -> None:
            exited.append((code, tail))
            done.set()

        mapper = StreamMapper(emit=lambda e, d: None, on_result=on_result,
                              mcp_prefix=f"mcp__{MCP_SERVER_NAME}__")
        # no extended thinking: a job is one small canvas call, and every
        # thinking token is a second the board stays behind
        env = {**os.environ, "MAX_THINKING_TOKENS": "0"}
        proc = ClaudeCodeProcess(argv, cwd=self.repo_root, on_line=mapper.feed,
                                 on_exit=on_exit, spawn=self._spawn, env=env)
        started = time.monotonic()
        proc.start()
        proc.send_user(self._message(records))
        # one message, then EOF: the process draws and exits on its own
        proc.close_input()
        if not done.wait(JOB_TIMEOUT_SECONDS):
            proc.terminate(grace=0)
            raise TimeoutError(f"the scribe took longer than {JOB_TIMEOUT_SECONDS:.0f}s")
        proc.wait_exit(5)
        elapsed = time.monotonic() - started
        if self._record is not None:
            self._record("scribe_job", {
                "records": len(records), "seconds": round(elapsed, 1),
                "status": result[0].status if result else "exited",
                "input_tokens": result[0].input_tokens if result else 0,
                "output_tokens": result[0].output_tokens if result else 0,
                # what it was given and what it said back, for reading a
                # session log without guessing why the board did not move
                "given": "\n".join(records)[-800:],
                "reply": (result[0].summary if result else "")[:300],
            })
        if not result:
            code, tail = exited[0] if exited else (-1, "")
            raise RuntimeError(f"scribe exited (code {code}){': ' + tail if tail else ''}")
        if result[0].is_error:
            raise RuntimeError(result[0].summary or result[0].subtype)
        self.last_error = None

    def _message(self, records: list[str]) -> str:
        """One job's input: the turns since the last job, then the board as it
        stands. The summary is the same note the architect reads, so the
        scribe and the architect agree on what is already there."""
        parts = ["What was just said, oldest first:", ""]
        for i, rec in enumerate(records, 1):
            parts.append(f"--- turn {i} ---")
            parts.append(rec)
            parts.append("")
        parts.append("The board as it stands:")
        parts.append(derive.note(self._state()))
        parts.append("")
        parts.append("Draw what changed, then reply: done.")
        return "\n".join(parts)


def turn_record(events: list[tuple[str, dict[str, Any]]]) -> str:
    """One turn's harness events, rendered for the scribe: the user's words,
    the calls the architect made (with their arguments), and what it said."""
    lines: list[str] = []
    for kind, data in events:
        if kind == "run_start":
            lines.append("user: " + str(data.get("task", "")).strip())
        elif kind == "assistant":
            for tc in data.get("tool_calls") or []:
                name = str(tc.get("name", ""))
                try:
                    args = json.loads(tc.get("arguments_json") or "{}")
                except ValueError:
                    args = {}
                if name in ("question", "approach", "brief", "decide", "canvas"):
                    lines.append(f"architect called {name}: {json.dumps(args, ensure_ascii=False)}")
            content = str(data.get("content") or "").strip()
            if content:
                lines.append("architect: " + content)
    return "\n".join(lines)
