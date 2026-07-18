"""bash — category-allowlisted shell execution (decision #10).

Allowed categories: read-only search, test runners, linters, git reads.
Everything else is rejected LOUDLY, naming what IS allowed, and every
rejection is logged as a session event — rejection frequency is data about
what the harness is missing.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import Any

from .base import Tool, ToolContext, ToolError, ToolResult

TIMEOUT_SECONDS = 120

SEARCH_COMMANDS = {"rg", "grep", "find", "ls", "cat", "head", "tail", "wc", "file", "tree", "sort", "uniq", "cut", "awk", "sed", "xargs", "dirname", "basename", "echo", "which", "cd", "pwd"}
LINT_COMMANDS = {"ruff", "flake8", "mypy", "pylint", "eslint", "tsc", "black", "isort"}
TEST_COMMANDS = {"pytest", "tox"}
GIT_READ_SUBCOMMANDS = {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "blame", "grep", "remote", "tag", "describe", "shortlog"}
PYTHON_MODULE_ALLOW = {"pytest", "unittest", "json.tool", "py_compile"}
NPM_LIKE = {"npm", "pnpm", "yarn"}
MAKE_TARGETS = {"test", "tests", "lint", "check"}
# sed/awk/xargs can write or execute; only their read-only usage is intended.
WRITE_FLAGS = {"sed": {"-i"}, "xargs": set()}

CATEGORY_HELP = (
    "Allowed command categories: read-only search (rg, grep, find, ls, cat, head, tail, "
    "wc, tree, cd, pwd), test runners (pytest, python -m pytest, npm test, go test, "
    "cargo test, make test), linters (ruff, mypy, flake8, eslint), and git reads "
    "(status, log, diff, show, branch, blame). Use the edit/write tools to change files."
)


def _split_unquoted(command: str) -> tuple[list[str], str]:
    """Split into pipeline/sequence segments on UNQUOTED |, ||, &&, ;, and
    newlines, and also return the command with quoted spans blanked out (for
    operator checks). A naive re.split broke quoting: grep "a\\|b" and
    multi-line python -c '...' were split mid-string and rejected as
    unparseable."""
    segments: list[str] = []
    buf: list[str] = []
    bare: list[str] = []
    quote: str | None = None
    escaped = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        i += 1
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        bare.append(ch)
        if ch in "|;\n":
            # || yields an empty in-between segment, skipped by the caller
            segments.append("".join(buf))
            buf = []
            continue
        if ch == "&" and i < n and command[i] == "&":
            bare.append("&")
            i += 1
            segments.append("".join(buf))
            buf = []
            continue
        # a lone & (2>&1, trailing background) is not a separator
        buf.append(ch)
    segments.append("".join(buf))
    return segments, "".join(bare)


def check_command(command: str, categories: tuple[str, ...]) -> str | None:
    """Return None if allowed, else a rejection reason."""
    segments, bare = _split_unquoted(command)
    if re.search(r"(?<!\d)>{1,2}|<\(", bare):
        return "output redirection is not allowed; use the write tool to create files"
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError as e:
            return f"cannot parse command segment {seg!r}: {e}"
        if not tokens:
            continue
        # skip leading env assignments (FOO=bar cmd ...)
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        head = tokens[0].rsplit("/", 1)[-1]
        reason = _check_segment(head, tokens, categories)
        if reason:
            return reason
    return None


def _check_segment(head: str, tokens: list[str], categories: tuple[str, ...]) -> str | None:
    if "search" in categories and head in SEARCH_COMMANDS:
        if head == "sed" and any(t == "-i" or t.startswith("-i") and len(t) <= 4 for t in tokens[1:]):
            return "sed -i edits files; use the edit tool instead"
        return None
    if "lint" in categories and head in LINT_COMMANDS:
        return None
    if "test" in categories:
        if head in TEST_COMMANDS:
            return None
        if head in {"python", "python3"} and len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in PYTHON_MODULE_ALLOW:
            return None
        if head in NPM_LIKE and len(tokens) >= 2 and (
            tokens[1] == "test" or (tokens[1] == "run" and len(tokens) >= 3 and tokens[2].startswith(("test", "lint")))
        ):
            return None
        if head == "go" and len(tokens) >= 2 and tokens[1] in {"test", "vet", "build"}:
            return None
        if head == "cargo" and len(tokens) >= 2 and tokens[1] in {"test", "check", "clippy"}:
            return None
        if head == "make" and len(tokens) >= 2 and tokens[1] in MAKE_TARGETS:
            return None
    if "git_read" in categories and head == "git":
        sub = next((t for t in tokens[1:] if not t.startswith("-")), "")
        if sub in GIT_READ_SUBCOMMANDS:
            return None
        return f"git '{sub}' is not a read-only subcommand ({CATEGORY_HELP})"
    return f"'{head}' is not in the allowed categories. {CATEGORY_HELP}"


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a read-only shell command: search (rg/grep/find/ls/cat), tests (pytest, "
        "npm test), linters, git reads. File changes must use edit/write."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]
        reason = check_command(command, ctx.bash_categories)
        if reason:
            ctx.emit("bash_rejected", {"command": command, "reason": reason})
            raise ToolError(f"command rejected: {reason}")
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=ctx.repo_root,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"command timed out after {TIMEOUT_SECONDS}s") from None
        out = proc.stdout
        if proc.stderr:
            out += ("\n" if out else "") + proc.stderr
        if proc.returncode != 0:
            out += f"\n[exit code {proc.returncode}]"
        return ToolResult(
            output=out or "(no output)",
            details={"command": command, "exit_code": proc.returncode},
            is_error=proc.returncode != 0,
        )
