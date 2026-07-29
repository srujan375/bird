"""Tool plumbing: dual results, execution context, base class.

Every tool returns a ToolResult with `output` (the string the model sees)
and `details` (structured data for the session log / future UI) — pi's dual
output. Schemas are hand-written JSON Schema dicts on each tool class; they
are exactly what the model sees, so keep them lean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..llm.types import ToolSpec

MAX_OUTPUT_CHARS = 30_000

# A backslash escaping a non-word, non-separator character — what a shell does
# to a dropped filename ("Screenshot\ 2026.png"). The `[^\w/\\]` class leaves
# Windows-style separators (C:\Users) and escaped backslashes alone.
_SHELL_ESCAPE = re.compile(r"\\([^\w/\\])")

_QUOTES = "'\"`"


def normalize_path_arg(path: str) -> str:
    """Undo shell quoting a model copied into a path argument.

    Models lift paths verbatim out of the user's message, and a terminal that
    just received a drag-and-dropped file writes them shell-quoted — macOS
    screenshots land as ``'/var/folders/.../Screenshot 2026-07-28 at 10.39.37
    PM.png'``. Passed through unchanged, the leading quote makes an absolute
    path *relative*, so it resolves to a nonexistent path under the repo root
    and the user is told "file not found" about a file that is plainly there.

    Strips surrounding quotes (nested included: ``"'/a/b'"``) and expands
    ``~``. Backslash unescaping is not done here — it needs a filesystem check
    to stay safe, so `ToolContext.resolve_path` handles it.
    """
    s = path.strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in _QUOTES:
        s = s[1:-1].strip()
    if s.startswith("~"):
        s = str(Path(s).expanduser())
    return s


def resolve_under(root: Path, path: str) -> Path:
    """Normalize a user/model-supplied path and resolve it against `root`.

    The resolution half of `ToolContext.resolve_path`, split out so the
    attachment ingester can agree with the file tools on what a given path
    string means — two spellings of "which file did they mean" is how the
    permission card and the tool end up talking about different files.
    """
    cleaned = normalize_path_arg(path)
    p = (root / cleaned).resolve()
    # Backslash-escaped spaces are the other half of terminal drag-and-drop
    # quoting. A backslash is a legal filename character, so only prefer the
    # unescaped reading when it is the one that actually exists — that way a
    # real "weird\name" file is never silently redirected.
    if "\\" in cleaned and not p.exists():
        alt = (root / _SHELL_ESCAPE.sub(r"\1", cleaned)).resolve()
        if alt.exists():
            return alt
    return p


@dataclass
class ToolResult:
    output: str
    details: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False

    def clipped(self) -> "ToolResult":
        if len(self.output) <= MAX_OUTPUT_CHARS:
            return self
        return ToolResult(
            output=self.output[:MAX_OUTPUT_CHARS]
            + f"\n... [truncated {len(self.output) - MAX_OUTPUT_CHARS} chars]",
            details={**self.details, "truncated_from": len(self.output)},
            is_error=self.is_error,
        )


@dataclass
class ToolContext:
    repo_root: Path
    kg: Any | None = None  # context.kg.KG once built; duck-typed to avoid import cycle
    plan: Any | None = None  # tools.plan.PlanState once the model calls plan
    record: Callable[[str, dict], None] | None = None  # session event sink
    bash_categories: tuple[str, ...] = ("search", "test", "lint", "git_read")
    client: Any | None = None  # llm.wire.openai_compat.OpenAICompatClient — used by web_fetch to ask the model
    skills: list[Any] | None = None  # skills.Skill list; None = no skills loaded
    arch: Any | None = None  # harnesses.arch.session.ArchSession in arch sessions
    # lead-harness wiring: the lead's dispatch tools spin up sub-harnesses, so
    # they need the registry to resolve models and a dir to nest sub-sessions
    # under; last_bundle is the arch->code seam (the finalized design, stashed
    # by `architect` and seeded into the `code` sub-session)
    registry: Any | None = None  # llm.registry.Registry
    run_dir: Path | None = None  # this session's dir; sub-sessions nest beneath it
    last_bundle: str | None = None  # seed_context handed from architect to code
    # permissions.Broker — duck: .request(payload) -> (approved, feedback).
    # build_runner wraps every requires_permission tool with it, so a sub-harness
    # forked from this ctx inherits the gate. None = ungated (library/test use);
    # every cli.py entry point sets one.
    broker: Any | None = None
    # --- verification ledger ---
    # "Verify your change" was an instruction, and instructions are the thing
    # the engine is supposed to replace: across 67 logged sessions, 8 of 21
    # runs called `done` having never run a check, or having edited files after
    # the last one. The runner stamps this ledger as results land and `done`
    # reads it, so finishing takes evidence instead of an assertion.
    # Harnesses that never touch the repo (lead, arch) leave the flag off and
    # the gate never fires.
    require_verification: bool = False
    unverified_paths: list[str] = field(default_factory=list)  # edited since the last passing check
    last_verify: dict[str, Any] | None = None  # {"command", "exit_code"} of the last check run
    done_blocked_once: bool = False  # the model has been told; `unverified_reason` now unlocks

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self.record:
            self.record(event_type, data)

    def note_tool_result(self, name: str, result: ToolResult) -> None:
        """Update the verification ledger from a tool result.

        Two facts decide whether a change was checked: which files it touched,
        and whether a check has passed *since* they were touched. A passing
        check clears the ledger; a later edit refills it, so "ran the tests,
        then kept editing" can never read as "tested".
        """
        if name == "bash":
            from .bash import is_verification_command  # deferred: bash imports this module

            command = result.details.get("command", "")
            exit_code = result.details.get("exit_code")  # absent when denied/rejected
            if exit_code is None or not is_verification_command(command):
                return
            self.last_verify = {"command": command, "exit_code": exit_code}
            if exit_code == 0:
                self.unverified_paths.clear()
        elif name in ("edit", "write") and not result.is_error:
            path = result.details.get("path")
            if path and path not in self.unverified_paths:
                self.unverified_paths.append(path)

    def resolve_path(self, path: str) -> Path:
        """Resolve a model-supplied path to an absolute path.

        The argument is normalized first (see `normalize_path_arg`) so a
        shell-quoted path pasted through the model still lands on the file the
        user meant.

        Paths inside the repo root are returned as-is. Paths that resolve
        *outside* the repo root are also returned — but the caller must check
        `is_outside_repo` and route the access through the permission broker
        before reading. Mutating tools (edit/write) must not call this for
        out-of-repo paths; they keep their own repo-scoped confinement.
        """
        return resolve_under(self.repo_root, path)

    def is_outside_repo(self, p: Path) -> bool:
        """True if a resolved path is not within the repo root."""
        root = self.repo_root.resolve()
        return p != root and root not in p.parents

    def resolve_repo_path(self, path: str) -> Path:
        """Resolve a path and confine it to the repo root.

        For mutating tools (edit/write): an out-of-repo path is a hard error,
        never a prompt — writes stay repo-scoped.
        """
        p = self.resolve_path(path)
        if self.is_outside_repo(p):
            raise ToolError(f"path '{path}' escapes the repository root")
        return p


class ToolError(Exception):
    """Raised by tools for model-visible failures; runner turns it into an error result."""


def gate_outside_repo_read(
    ctx: "ToolContext", path_str: str, resolved: Path, tool_name: str
) -> None:
    """Permission-gate a read of a path outside the repo root.

    Reads inside the repo are always allowed. A read outside the repo root is
    allowed only after the user approves it through the session's permission
    broker — the same gate mutating tools use. No broker (library/test use)
    means the read is refused with a clear error rather than silently
    proceeding, so the default stays safe.

    Raises ToolError on denial or when no broker is configured.
    """
    if not ctx.is_outside_repo(resolved):
        return  # in-repo read: ungated
    if ctx.broker is None:
        raise ToolError(
            f"reading {path_str} requires permission, but no approval broker is "
            f"configured"
        )
    payload = {
        "kind": "read_outside_repo",
        "tool": tool_name,
        "path": str(resolved),
    }
    approved, feedback = ctx.broker.request(payload)
    if not approved:
        ctx.emit("permission_denied", {"tool": tool_name, "args": {"path": path_str}})
        detail = f" They said: {feedback}" if feedback else ""
        raise ToolError(
            f"the user DENIED permission to read {path_str} (outside the "
            f"repository root).{detail}"
        )


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    # True on anything that can change the user's repo. build_runner wraps
    # these with the session's permission broker. Declared here rather than in
    # a name list elsewhere so a new mutating tool is gated by default, at the
    # point someone writes it.
    requires_permission: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """run() with ToolError converted to a model-visible error result."""
        try:
            return self.run(args, ctx).clipped()
        except ToolError as e:
            return ToolResult(output=f"Error: {e}", details={"error": str(e)}, is_error=True)
