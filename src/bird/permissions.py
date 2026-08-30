"""Permission gating: the broker protocol, the tool wrapper, the brokers.

Gating used to live in `serve.py`, which meant it only existed when a Server
existed — so `bird code`, the plain REPL, and every sub-harness the lead
dispatched ran their tools raw. The gate now attaches at runner construction
(`harnesses.registry.build_runner`), and the broker rides on ToolContext, so a
sub-session forked from a gated parent inherits the gate for free.

Which tools are gated is a property of the tool (`Tool.requires_permission`),
not a name list kept in some other module — a new mutating tool is gated by
declaring itself so, next to its own implementation.

A gated tool may still waive an individual call via `Tool.needs_permission`,
which `bash` uses to run search-only commands unprompted (they have already
been proven read-only by the category allowlist, so the prompt asked a
question with one answer). Waived calls are logged as `auto_approved`.

A broker is anything with `.request(payload) -> (approved, feedback)`:

  PermissionBroker  blocks the calling thread until a UI answers (serve/TUI,
                    arch's browser page). Used across threads.
  ConsoleBroker     prompts on stdin; for the plain terminal REPL. 'a' opts
                    into auto-approving edits for the rest of the session.
  AutoApproveBroker approves everything — `bird code --yes`, unattended runs.
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
import json
import sys
import threading
from typing import Any, Callable, Literal, Protocol

from .tools import Tool, ToolContext, ToolResult

DIFF_CONTEXT_LINES = 2
MAX_DIFF_LINES = 40

# ------------------------------------------------------------------- modes

# The three-state approval mode. The default ("normal") asks for every
# gated call; "auto_edits" auto-approves edit/write (and reads outside the
# repo) but still asks for bash; "full_auto" additionally auto-approves bash.
# The console broker and the TUI implement identical semantics against this
# one reading of the truth — the TUI mirrors it in tui/src/components.ts.
PermissionMode = Literal["normal", "auto_edits", "full_auto"]

# Shift+Tab / 'A' cycle order.
NEXT_MODE: dict[PermissionMode, PermissionMode] = {
    "normal": "auto_edits",
    "auto_edits": "full_auto",
    "full_auto": "normal",
}

# The payload kinds each mode auto-approves. "offer" is NEVER covered: an
# offer's answer IS the feedback string, so an auto-approved offer with no
# feedback is a corrupted answer — offers stay manual in every mode.
# "mcp" is full_auto-only, next to bash: an MCP tool is arbitrary remote code
# behind a friendly name, not a repo-local edit, so auto_edits never covers it.
AUTO_MODES: dict[PermissionMode, frozenset[str]] = {
    "normal": frozenset(),
    "auto_edits": frozenset({"edit", "write", "read_outside_repo"}),
    "full_auto": frozenset({"edit", "write", "read_outside_repo", "bash", "mcp"}),
}


def auto_approves(mode: PermissionMode, payload: dict[str, Any]) -> bool:
    """Does this mode auto-approve this payload without asking?"""
    return payload.get("kind", "?") in AUTO_MODES[mode]


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
    if name.startswith("mcp__"):
        # mcp__<server>__<tool>: split back apart so the card can show WHICH
        # server is being asked to run WHAT — the flattened name alone reads
        # as one opaque token. Args as compact JSON, truncated — enough to
        # judge the call without dumping a payload the size of a file into
        # the prompt.
        parts = name[5:].split("__", 1)
        server = parts[0] if parts else "?"
        tool = parts[1] if len(parts) > 1 else "?"
        compact = json.dumps(args, separators=(",", ":"), default=str)
        if len(compact) > 500:
            compact = compact[:500] + "…"
        return {"kind": "mcp", "server": server, "tool": tool, "args": compact}
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
        # The tool gets to say this particular call is provably safe (bash
        # running a search-only command). Logged either way: an unprompted
        # run must still be auditable in the session record.
        if not self.inner.needs_permission(args, ctx):
            ctx.emit("auto_approved", {"tool": self.name, "args": args})
            return self.inner.execute(args, ctx)
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
    """Approves everything. `bird code --yes` and other unattended runs."""

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

    The approval mode is a three-state cycle (normal | auto_edits | full_auto),
    the terminal equivalent of the TUI's Shift+Tab. 'a' turns on auto_edits
    (edit/write/read_outside_repo auto-approved); 'A' escalates to full_auto,
    which additionally auto-approves bash. The mode lives on this object, so a
    sub-harness that forks the ToolContext and re-gates on the same broker
    instance inherits it for free.

    The default ("normal") never auto-accepts bash — bash can write anywhere,
    so auto-accepting *edits* must not quietly auto-accept a shell that can do
    the same thing unobserved. Full auto is explicit opt-in via 'A'.
    """

    def __init__(self, out=None, ask: Callable[[str], str] | None = None) -> None:
        self.out = out if out is not None else sys.stdout
        self.ask = ask if ask is not None else input
        self.mode: PermissionMode = "normal"

    # Back-compat view for existing tests and any reader of the attribute:
    # the old boolean toggle is "auto_edits mode is on".
    @property
    def auto_edits(self) -> bool:
        return self.mode == "auto_edits"

    @auto_edits.setter
    def auto_edits(self, value: bool) -> None:
        self.mode = "auto_edits" if value else "normal"

    def request(self, payload: dict[str, Any]) -> tuple[bool, str]:
        kind = payload.get("kind", "?")
        # Check the mode's covered kinds FIRST — same position as the old
        # auto_edits check, before the offer branch.
        if self.mode != "normal" and auto_approves(self.mode, payload):
            self._audit_auto(payload)
            return True, ""
        if kind == "offer":
            return self._offer(payload)
        self._render(payload)
        while True:
            try:
                answer = self.ask(
                    "  approve? [y/N/a=auto-edits/A=FULL AUTO] "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print(file=self.out)
                return False, "the user interrupted the approval prompt"
            # 'a' (auto-edits) and 'A' (full-auto) are case-sensitive — the
            # distinction is the whole point — so pull them out before
            # lowercasing the rest. Everything else (y/yes/n/no and their
            # mixed-case variants) is matched case-insensitively.
            if answer == "A":
                self._enter_full_auto()
                return True, ""
            if answer == "a":
                self.mode = "auto_edits"
                return True, ""
            lowered = answer.lower()
            if lowered in ("y", "yes"):
                return True, ""
            if lowered in ("", "n", "no"):
                return False, ""
            print("  answer y, n, a, or A", file=self.out)

    def _enter_full_auto(self) -> None:
        """Escalate to full auto: approve THIS request and flip the session.
        Prints one loud warning line at escalation time naming what unlocks."""
        self.mode = "full_auto"
        print(
            "  ⚠ FULL AUTO: bash now runs WITHOUT asking. "
            "Shift+Tab (TUI) or restart to reset.",
            file=self.out,
        )

    def _audit_auto(self, payload: dict[str, Any]) -> None:
        """Every full-auto approval prints an auditable line so
        execute-without-review leaves a visible trace."""
        if self.mode != "full_auto":
            print(
                f"  ✓ auto-approved {payload.get('kind', '?')} "
                f"{payload.get('file', payload.get('cmd', payload.get('path', '')))}",
                file=self.out,
            )
            return
        kind = payload.get("kind", "?")
        if kind == "bash":
            print(f"  ✓ ⚠ FULL AUTO ran bash: {payload.get('cmd', '')}", file=self.out)
        elif kind == "mcp":
            print(
                f"  ✓ ⚠ FULL AUTO ran mcp: "
                f"{payload.get('server', '?')}.{payload.get('tool', '')}",
                file=self.out,
            )
        else:
            print(
                f"  ✓ ⚠ FULL AUTO ran {kind} "
                f"{payload.get('file', payload.get('path', ''))}",
                file=self.out,
            )

    def _offer(self, payload: dict[str, Any]) -> tuple[bool, str]:
        """A multiple-choice question, not an approval. The answer travels back
        as the feedback string — the broker protocol is (approved, feedback) and
        an offer's feedback IS the chosen option."""
        options = [str(o) for o in payload.get("options", [])]
        print(f"\n  ┌ {payload.get('question', '?')}", file=self.out)
        for i, opt in enumerate(options, 1):
            print(f"  │ {i}. {opt}", file=self.out)
        print("  └", file=self.out)
        while True:
            try:
                answer = self.ask(f"  choose [1-{len(options)}, or d=don't know] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(file=self.out)
                return False, ""
            if answer in ("d", "", "n"):
                return False, ""
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return True, options[int(answer) - 1]
            print(f"  answer 1-{len(options)}, or d", file=self.out)

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
        if kind == "mcp":
            print(f"\n  ┌ mcp call: {payload.get('server', '?')}.{payload.get('tool', '?')}", file=self.out)
            print(f"  │ args: {payload.get('args', '{}')}", file=self.out)
            print("  └", file=self.out)
            return
        label = "write (new file)" if payload.get("new_file") else kind
        print(f"\n  ┌ {label} {payload.get('file', '')}", file=self.out)
        for line in payload.get("lines", []):
            # text already carries its unified-diff marker (+/-/space)
            print(f"  │ {line['text']}", file=self.out)
        print("  └", file=self.out)
