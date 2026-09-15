"""Claude Code as an engine: spawn the user's `claude` binary and speak its
stream-json wire.

This is the "Claude Code inside bird" path. Bird owns the page and the
board; Claude Code owns the model, the context window, its own read and
search tools, and the bill (the user's login, exactly as if they had typed
the prompt in a terminal). The two meet on stdin/stdout — one JSON object
per line each way — plus an MCP endpoint bird serves for the board tools.

Shape of the wire, as observed on Claude Code 2.1.x:

  in  → {"type":"user","message":{"role":"user","content":[{"type":"text","text":…}]}}
        {"type":"control_request","request_id":…,"request":{"subtype":"interrupt"}}
  out ← {"type":"system","subtype":"init","session_id":…,"model":…,"mcp_servers":[…]}
        {"type":"stream_event","event":{…raw API stream event…}}   (partial messages)
        {"type":"assistant","message":{"id":…,"content":[ONE block]}}  per block
        {"type":"user","message":{"content":[{"type":"tool_result",…}]}}
        {"type":"result","subtype":"success"|…,"session_id":…,"usage":{…},"result":…}

`StreamMapper` turns that into the harness_event vocabulary the page already
renders (assistant_delta / assistant / tool_result), so the arch page needs
no idea which engine is behind it. `ClaudeCodeProcess` is the subprocess: it
stays alive for the session, one user line per turn, and is respawned with
`--resume` if it dies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

STDERR_TAIL = 30
INTERRUPT_GRACE_SECONDS = 3.0

# Claude Code's own tools the architect keeps. Read and search, no writes:
# the architect designs and argues, it does not touch the repo — the same
# line the bird engine's arch toolset holds.
BUILTIN_TOOLS = ("Read", "Glob", "Grep", "WebSearch", "WebFetch")
# Its picker is the terminal's; ours is the page's `question` tool.
DISALLOWED_TOOLS = ("AskUserQuestion",)


class ClaudeNotFound(Exception):
    pass


def find_claude(explicit: str | None = None) -> str:
    """The `claude` binary to run, or ClaudeNotFound with what to do."""
    candidate = explicit or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not candidate:
        raise ClaudeNotFound(
            "the `claude` command is not on PATH — install Claude Code "
            "(https://claude.com/claude-code) and log in with `claude auth login`"
        )
    return candidate


def auth_status(bin_path: str) -> dict[str, Any] | None:
    """`claude auth status` parsed, or None if the probe itself fails."""
    try:
        out = subprocess.run(
            [bin_path, "auth", "status"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        return None


def build_argv(
    *,
    bin_path: str,
    mcp_config: Path,
    mcp_server: str,
    system_prompt: str,
    session_id: str | None = None,
    resume: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_turns: int | None = None,
    builtin_tools: tuple[str, ...] = BUILTIN_TOOLS,
    replace_system_prompt: bool = False,
) -> list[str]:
    """The spawn line. Exactly one of session_id/resume is used: a fresh
    session is minted by us (so we know its id before the first event), a
    respawn continues the one the previous process was running.

    `builtin_tools` is Claude Code's own set the process may use; empty means
    only the MCP server's tools (the scribe's case). `replace_system_prompt`
    swaps Claude Code's own (long, coding-flavoured) system prompt for ours
    instead of appending to it — right for a process with one small job."""
    builtin = ",".join(builtin_tools)
    allowed = f"{builtin},mcp__{mcp_server}" if builtin else f"mcp__{mcp_server}"
    argv = [
        bin_path, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        # deny anything that would have prompted: there is no terminal to
        # answer, and the allowlist below is the whole permission story
        "--permission-mode", "dontAsk",
        "--tools", builtin,
        "--allowedTools", allowed,
        "--disallowedTools", ",".join(DISALLOWED_TOOLS),
        "--mcp-config", str(mcp_config),
        "--strict-mcp-config",
        "--system-prompt" if replace_system_prompt else "--append-system-prompt", system_prompt,
    ]
    if resume:
        argv += ["--resume", resume]
    elif session_id:
        argv += ["--session-id", session_id]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    return argv


def write_mcp_config(path: Path, *, name: str, url: str, token: str) -> Path:
    """The file `--mcp-config` reads: one streamable-HTTP server, bearer auth.
    Written per run into the run dir, never into the repo's own .mcp.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "mcpServers": {
            name: {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}
        }
    }
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def new_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def new_session_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------- the mapper


@dataclass
class TurnResult:
    status: str  # done | error | max_turns
    summary: str
    session_id: str | None
    input_tokens: int
    output_tokens: int
    turns: int
    is_error: bool
    subtype: str


@dataclass
class _Block:
    kind: str
    name: str = ""
    id: str = ""
    text: str = ""
    partial_json: str = ""


@dataclass
class StreamMapper:
    """Claude Code's stream → bird harness events, one turn at a time.

    `emit(event, data)` is the harness_event sink. `details_for(tool_name)`
    lets the embedder attach structured details to a tool_result whose call
    it served itself (the MCP endpoint); the stream only carries the text.
    `on_result` fires once per turn with the parsed `result` line.
    """

    emit: Callable[[str, dict[str, Any]], None]
    details_for: Callable[[str], dict[str, Any] | None] | None = None
    on_result: Callable[[TurnResult], None] | None = None
    on_init: Callable[[dict[str, Any]], None] | None = None
    mcp_prefix: str = "mcp__arch__"

    turn: int = 0
    session_id: str | None = None
    model: str | None = None
    _blocks: dict[int, _Block] = field(default_factory=dict)
    _message_id: str | None = None
    _content: list[str] = field(default_factory=list)
    _calls: list[dict[str, str]] = field(default_factory=list)
    _tool_names: dict[str, str] = field(default_factory=dict)
    _usage: dict[str, int] = field(default_factory=dict)
    _pending: bool = False

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            return  # not ours (a stray print from a hook, say)
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "stream_event":
            self._stream(event.get("event") or {})
        elif kind == "assistant":
            self._assistant(event.get("message") or {})
        elif kind == "user":
            self._flush()
            self._user(event)
        elif kind == "result":
            self._flush()
            self._result(event)
        elif kind == "system":
            self._system(event)

    # -- raw API stream (partial messages) --

    def _stream(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "message_start":
            self._flush()
            msg = ev.get("message") or {}
            self._message_id = msg.get("id")
            self._blocks = {}
            self._pending = True
        elif t == "content_block_start":
            block = ev.get("content_block") or {}
            self._blocks[int(ev.get("index", 0))] = _Block(
                kind=str(block.get("type", "")),
                name=str(block.get("name", "")),
                id=str(block.get("id", "")),
            )
        elif t == "content_block_delta":
            delta = ev.get("delta") or {}
            dt = delta.get("type")
            index = int(ev.get("index", 0))
            block = self._blocks.setdefault(index, _Block(kind="text"))
            if dt == "text_delta":
                chunk = str(delta.get("text", ""))
                if chunk:
                    block.text += chunk
                    self.emit("assistant_delta", {"text": chunk})
            elif dt == "input_json_delta":
                chunk = str(delta.get("partial_json", ""))
                if chunk:
                    block.partial_json += chunk
                    self.emit(
                        "tool_call_delta",
                        {"index": index, "name": self._short(block.name), "text": chunk},
                    )
            elif dt == "thinking_delta":
                chunk = str(delta.get("thinking", ""))
                if chunk:
                    self.emit("thinking_delta", {"text": chunk})
        elif t == "message_delta":
            usage = ev.get("usage") or {}
            self._usage = {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            }
        elif t == "message_stop":
            self._flush()

    # -- whole blocks --

    def _assistant(self, message: dict[str, Any]) -> None:
        mid = message.get("id")
        if self._message_id is not None and mid != self._message_id:
            self._flush()
        self._message_id = mid
        self._pending = True
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                self._content.append(str(block.get("text", "")))
            elif bt == "tool_use":
                name = str(block.get("name", ""))
                self._tool_names[str(block.get("id", ""))] = name
                self._calls.append({
                    "name": self._short(name),
                    "arguments_json": json.dumps(block.get("input") or {}, ensure_ascii=False),
                })
        usage = message.get("usage") or {}
        if usage:
            self._usage = {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            }

    def _flush(self) -> None:
        """One bird `assistant` event per Claude message, once it is whole."""
        if not self._pending:
            return
        if self._content or self._calls:
            self.turn += 1
            self.emit(
                "assistant",
                {
                    "turn": self.turn,
                    "content": "".join(self._content),
                    "tool_calls": list(self._calls),
                    "stop_reason": "tool_calls" if self._calls else "stop",
                    "input_tokens": self._usage.get("input_tokens", 0),
                    "output_tokens": self._usage.get("output_tokens", 0),
                },
            )
        self._content = []
        self._calls = []
        self._blocks = {}
        self._message_id = None
        self._pending = False

    def _user(self, event: dict[str, Any]) -> None:
        message = event.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            full = self._tool_names.get(str(block.get("tool_use_id", "")), "tool")
            name = self._short(full)
            details: dict[str, Any] | None = None
            if full.startswith(self.mcp_prefix) and self.details_for is not None:
                details = self.details_for(name)
            self.emit(
                "tool_result",
                {
                    "turn": self.turn,
                    "name": name,
                    "is_error": bool(block.get("is_error", False)),
                    "details": details,
                },
            )

    def _result(self, event: dict[str, Any]) -> None:
        subtype = str(event.get("subtype", ""))
        usage = event.get("usage") or {}
        is_error = bool(event.get("is_error", False)) or subtype.startswith("error")
        if subtype == "success":
            status = "done"
        elif subtype == "error_max_turns":
            status = "max_turns"
        else:
            status = "error"
        self.session_id = event.get("session_id") or self.session_id
        result = TurnResult(
            status=status,
            summary=str(event.get("result", "") or ""),
            session_id=self.session_id,
            input_tokens=int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            turns=int(event.get("num_turns", self.turn) or 0),
            is_error=is_error,
            subtype=subtype,
        )
        if self.on_result is not None:
            self.on_result(result)

    def _system(self, event: dict[str, Any]) -> None:
        if event.get("subtype") != "init":
            return
        self.session_id = event.get("session_id") or self.session_id
        self.model = event.get("model") or self.model
        if self.on_init is not None:
            self.on_init(event)

    def _short(self, name: str) -> str:
        return name[len(self.mcp_prefix):] if name.startswith(self.mcp_prefix) else name


# --------------------------------------------------------------- the process


class ClaudeCodeProcess:
    """One long-lived `claude -p` speaking stream-json both ways.

    `on_line` gets every stdout line; `on_exit` fires once when the process
    ends, with its return code and the stderr tail. Both run on reader
    threads. `spawn` is the Popen-shaped factory tests replace."""

    def __init__(
        self,
        argv: list[str],
        cwd: Path,
        on_line: Callable[[str], None],
        on_exit: Callable[[int, str], None] | None = None,
        env: dict[str, str] | None = None,
        spawn: Callable[..., Any] | None = None,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.on_line = on_line
        self.on_exit = on_exit
        self.env = env
        self._spawn = spawn or subprocess.Popen
        self._proc: Any = None
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL)
        self._write_lock = threading.Lock()
        self._exited = threading.Event()

    def start(self) -> None:
        self._proc = self._spawn(
            self.argv,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self.env,
        )
        threading.Thread(target=self._read_stdout, daemon=True, name="claude-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="claude-stderr").start()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def send_user(self, text: str) -> None:
        self._write({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        })

    def close_input(self) -> None:
        """EOF on stdin: no more turns are coming. `claude -p` finishes the
        turn in flight and exits on its own."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        with self._write_lock:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def interrupt(self) -> None:
        """Ask the running turn to stop. The reply is a `result` like any
        other turn end; if none comes within the grace period the caller
        terminates and respawns on the next turn."""
        self._write({
            "type": "control_request",
            "request_id": uuid.uuid4().hex,
            "request": {"subtype": "interrupt"},
        })

    def terminate(self, grace: float = 2.0) -> None:
        """Close stdin — EOF is how `claude -p` is told the session is over,
        and it exits on its own once the current turn is done — then, past
        `grace`, SIGTERM and finally SIGKILL. Zero grace is the hard stop an
        unanswered interrupt earns."""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None and grace > 0:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass

    def wait_exit(self, timeout: float | None = None) -> bool:
        return self._exited.wait(timeout)

    def _write(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("claude is not running")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._write_lock:
            proc.stdin.write(line)
            proc.stdin.flush()

    def _read_stdout(self) -> None:
        proc = self._proc
        try:
            for line in proc.stdout:
                try:
                    self.on_line(line)
                except Exception:  # a mapper bug must not stop the reader
                    pass
        except (OSError, ValueError):
            pass
        code = proc.wait()
        self._exited.set()
        if self.on_exit is not None:
            try:
                self.on_exit(code, self.stderr_tail)
            except Exception:
                pass

    def _read_stderr(self) -> None:
        proc = self._proc
        try:
            for line in proc.stderr:
                self._stderr.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
