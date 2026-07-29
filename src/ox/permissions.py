"""Permission gating: the broker protocol, the tool wrapper, the brokers.

Gating used to live in `serve.py`, which meant it only existed when a Server
existed — so `ox code`, the plain REPL, and every sub-harness the lead
dispatched ran their tools raw. The gate now attaches at runner construction
(`harnesses.registry.build_runner`), and the broker rides on ToolContext, so a
sub-session forked from a gated parent inherits the gate for free.

Which tools are gated is a property of the tool (`Tool.requires_permission`),
not a name list kept in some other module — a new mutating tool is gated by
declaring itself so, next to its own implementation.

A broker is anything with `.request(payload) -> (approved, feedback)`:

  PermissionBroker  blocks the calling thread until a UI answers (serve/TUI,
                    arch's browser page). Used across threads.
  ConsoleBroker     prompts on stdin; for the plain terminal REPL. 'a' opts
                    into auto-approving edits for the rest of the session.
  AutoApproveBroker approves everything — `ox code --yes`, unattended runs.
  DenyBroker        refuses everything, with a reason the model can act on;
                    the default when nobody can be asked (non-tty, no --yes).

ToolContext.broker of None means ungated. That is the library/test default
(constructing a Runner in a unit test should not need a UI), so every
user-facing entry point in cli.py sets a broker explicitly — leaving it None
there is the bug this module exists to prevent.
"""

from __future__ import annotations

import contextlib
import difflib
import sys
import threading
from typing import Any, Callable, Protocol

from .tools import Tool, ToolContext, ToolResult

DIFF_CONTEXT_LINES = 2
MAX_DIFF_LINES = 40


class Broker(Protocol):
    """What GatedTool needs: ask, and block until answered."""

    def request(self, payload: dict[str, Any]) -> tuple[bool, str]: ...


# ------------------------------------------------------------------ payloads


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


def permission_payload(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """What the user is being asked to approve, in UI-ready form."""
    if name == "edit":
        return {
            "kind": "edit",
            "file": args.get("path") or "?",
            "lines": _diff_lines(args.get("old_text", ""), args.get("new_text", "")),
        }
    if name == "write":
        path = args.get("path") or "?"
        content = args.get("content", "")
        old = ""
        with contextlib.suppress(Exception):
            p = ctx.resolve_path(path)
            if p.is_file():
                old = p.read_text(encoding="utf-8", errors="replace")
        return {
            "kind": "write",
            "file": path,
            "new_file": not old,
            "lines": _diff_lines(old, content),
        }
    return {"kind": "bash", "cmd": args.get("command") or name}


# ------------------------------------------------------------------- wrapper


class GatedTool(Tool):
    """Wraps a tool so execution waits for approval."""

    def __init__(self, inner: Tool, broker: Broker):
        self.inner = inner
        self.broker = broker
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters
        self.requires_permission = False  # already gated; never wrap twice

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        payload = permission_payload(self.name, args, ctx)
        approved, feedback = self.broker.request(payload)
        if not approved:
            ctx.emit("permission_denied", {"tool": self.name, "args": args})
            detail = f" They said: {feedback}" if feedback else ""
            return ToolResult(
                output=(
                    f"The user DENIED permission for this {self.name}.{detail} Do not "
                    "retry the same change; ask the user or try a different approach."
                ),
                details={"denied": True, "feedback": feedback},
                is_error=True,
            )
        return self.inner.execute(args, ctx)


def gate_tools(tools: list[Tool], broker: Broker | None) -> list[Tool]:
    """Wrap every tool that declares requires_permission. No broker means
    ungated (library/test use). Idempotent: GatedTool clears the flag on
    itself, so re-gating an already-gated list is a no-op."""
    if broker is None:
        return tools
    return [GatedTool(t, broker) if getattr(t, "requires_permission", False) else t
            for t in tools]


# ------------------------------------------------------------------- brokers


class PermissionBroker:
    """Blocks a worker-thread tool call until the UI answers. The answer is
    (approved, feedback); feedback carries "Request changes" text on
    rejection of the arch gates and is empty otherwise.

    `emit` may be bound after construction: the runner has to be built (and
    gated) before the Server that owns the transport exists, so cli.py makes
    the broker first and binds the sink once the Server is up.
    """

    def __init__(self, emit: Callable[..., None] | None = None) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, list[Any]]] = {}

    def bind(self, emit: Callable[..., None]) -> None:
        self._emit = emit

    def request(self, payload: dict[str, Any]) -> tuple[bool, str]:
        if self._emit is None:
            # nothing is listening; refuse rather than silently proceed
            return False, "no UI is connected to approve this"
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


class AutoApproveBroker:
    """Approves everything. `ox code --yes` and other unattended runs."""

    def request(self, payload: dict[str, Any]) -> tuple[bool, str]:
        return True, ""


class DenyBroker:
    """Refuses everything, telling the model why so it can report back rather
    than thrash. The default when there is no one to ask."""

    def __init__(self, reason: str = "no interactive terminal to approve it") -> None:
        self.reason = reason

    def request(self, payload: dict[str, Any]) -> tuple[bool, str]:
        return False, self.reason


class ConsoleBroker:
    """Prompts on stdin for the plain REPL and headless-on-a-tty runs.

    'a' turns on auto-approve for edits (edit/write) for the rest of the
    session — the terminal equivalent of the TUI's Shift+Tab. bash keeps
    asking either way: it can write anywhere, so auto-accepting *edits* must
    not quietly auto-accept a shell that can do the same thing unobserved.
    """

    def __init__(self, out=None, ask: Callable[[str], str] | None = None) -> None:
        self.out = out if out is not None else sys.stdout
        self.ask = ask if ask is not None else input
        self.auto_edits = False

    def request(self, payload: dict[str, Any]) -> tuple[bool, str]:
        kind = payload.get("kind", "?")
        if self.auto_edits and kind in ("edit", "write"):
            print(f"  ✓ auto-approved {kind} {payload.get('file', '')}", file=self.out)
            return True, ""
        self._render(payload)
        while True:
            try:
                answer = self.ask("  approve? [y/N/a=auto-approve edits] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(file=self.out)
                return False, "the user interrupted the approval prompt"
            if answer in ("y", "yes"):
                return True, ""
            if answer in ("", "n", "no"):
                return False, ""
            if answer == "a":
                self.auto_edits = True
                return True, ""
            print("  answer y, n, or a", file=self.out)

    def _render(self, payload: dict[str, Any]) -> None:
        kind = payload.get("kind", "?")
        if kind == "bash":
            print(f"\n  ┌ bash: {payload.get('cmd', '')}", file=self.out)
            print("  └", file=self.out)
            return
        if kind == "read_outside_repo":
            print(f"\n  ┌ read (outside repo) {payload.get('path', '')}", file=self.out)
            print(f"  │ requested by {payload.get('tool', '?')}", file=self.out)
            print("  └", file=self.out)
            return
        label = "write (new file)" if payload.get("new_file") else kind
        print(f"\n  ┌ {label} {payload.get('file', '')}", file=self.out)
        for line in payload.get("lines", []):
            # text already carries its unified-diff marker (+/-/space)
            print(f"  │ {line['text']}", file=self.out)
        print("  └", file=self.out)
