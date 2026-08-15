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
LINT_COMMANDS = {"ruff", "flake8", "mypy", "pylint", "eslint", "tsc", "black", "isort", "pyright", "biome"}
TEST_COMMANDS = {"pytest", "tox", "vitest", "jest"}
GIT_READ_SUBCOMMANDS = {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "blame", "grep", "remote", "tag", "describe", "shortlog"}
PYTHON_MODULE_ALLOW = {"pytest", "unittest", "json.tool", "py_compile", "compileall", "mypy", "ruff", "flake8", "pylint", "pyflakes"}
NPM_LIKE = {"npm", "pnpm", "yarn"}
MAKE_TARGETS = {"test", "tests", "lint", "check", "typecheck", "build", "ci", "verify"}

# `uv run pytest`, `npx tsc`, `poetry run mypy` — the prefix is not the command.
# These are peeled off and what they actually run is checked instead, so
# allowing them widens nothing: `uv run python evil.py` still lands on `python`
# with no -m and is rejected exactly as `python evil.py` is.
DELEGATING_PREFIXES = {
    ("uv", "run"), ("uvx",), ("poetry", "run"), ("pipenv", "run"),
    ("npx",), ("pnpm", "exec"), ("pnpm", "dlx"), ("yarn", "dlx"),
}
# Script runners: what follows is a package.json/pyproject script name, not a
# command, so there is nothing to delegate to — the name is the user's own
# package.json script, not arbitrary code from the model, so any name is
# allowed (npm run dev, npm run start, npm run deploy, ...). The runner pair
# is still matched so `npm run` is gated but `npm <anything-else>` is not.
SCRIPT_RUNNERS = {
    ("npm", "run"), ("pnpm", "run"), ("yarn", "run"),
    ("hatch", "run"), ("pdm", "run"), ("rye", "run"),
}
# Package-manager install subcommands: mutate node_modules/lockfile but are
# contained to the repo and needed to set up before running tests.
INSTALL_SUBCOMMANDS = {"install", "ci", "add", "i"}
# pip install mutates the environment but is needed to set up deps to run
# tests. `pip install -r requirements.txt`, `pip install requests`, ...
PIP_INSTALL_HEADS = {"pip", "pip3"}
# `source .venv/bin/activate` — the virtualenv a project's tests need, activated
# for the one command line that runs them. `source` executes whatever is in the
# file it is handed, so what is allowed here is a path *shape*, not the builtin:
# the last two components must read <anything>/bin/activate. `source setup.sh`
# and `. ~/.bashrc` stay rejected, and this is never part of the `search`
# category — see _is_venv_activate.
SOURCE_COMMANDS = {"source", "."}
ACTIVATE_DIRS = {"bin", "Scripts"}  # Scripts/ is the Windows venv layout
ACTIVATE_NAMES = {"activate", "activate.fish", "activate.csh", "activate.ps1"}
# Script names that count as verification for `done`. Any package.json script
# is allowed to RUN (SCRIPT_RUNNERS), but only these prefixes prove the work —
# `npm run dev`/`npm run start` run but are not a check.
SCRIPT_VERIFY_PREFIXES = ("test", "lint", "check", "typecheck", "types", "build", "compile", "e2e", "unit", "integration", "ci", "verify")

# Linters that *check* (vs. black/isort, which rewrite): only these count as
# verification for `done`. A formatter run is not evidence the change works.
CHECK_LINTERS = {"ruff", "flake8", "mypy", "pylint", "eslint", "tsc", "pyright", "biome"}
FORMAT_SUBCOMMANDS = {"format", "fmt"}  # `ruff format` reformats; `ruff check` checks

# Commands in the search set that can still write or execute. Only their
# read-only use is intended — bash is not a way around edit/write, which is
# where the permission broker sees a change before it lands.
FIND_WRITE_FLAGS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprintf"}
XARGS_VALUE_FLAGS = {"-n", "-I", "-i", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args", "--replace", "--max-procs", "--arg-file", "--delimiter"}

CATEGORY_HELP = (
    "Allowed command categories: read-only search (rg, grep, find, ls, cat, head, tail, "
    "wc, tree, cd, pwd), test runners (pytest, python -m pytest, npm test/npm run test, "
    "go test, cargo test, make test, prefixed with uv run / poetry run / npx if needed), "
    "linters and type checks (ruff, mypy, flake8, eslint, tsc), and git reads (status, "
    "log, diff, show, branch, blame). Also allowed: package-manager installs (npm/pnpm/yarn "
    "install, npm ci) and any package.json script (npm/pnpm/yarn run <script>), bare python "
    "on a script file (python script.py, python3 manage.py migrate), pip install, and "
    "activating a virtualenv (source .venv/bin/activate && pytest). "
    "Rejected: python -c (inline code), python -m outside the module allowlist. "
    "Use the edit/write tools to change files."
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


def _strip_runner_prefix(tokens: list[str]) -> list[str]:
    """Peel a delegating runner prefix (`uv run`, `npx`, `poetry run`, ...) off
    the front so the segment is judged by what it actually runs. Longest prefix
    first, and any flags belonging to the runner are skipped (`npx -y tsc`)."""
    for depth in (2, 1):
        if len(tokens) <= depth:
            continue
        if tuple(t.rsplit("/", 1)[-1] for t in tokens[:depth]) in DELEGATING_PREFIXES:
            rest = tokens[depth:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            return rest
    return tokens


def _segment_tokens(command: str) -> tuple[list[list[str]], str | None]:
    """Split a command line into normalized per-segment token lists: env
    assignments and runner prefixes stripped, ready to judge. Returns
    (segments, parse_error)."""
    out: list[list[str]] = []
    for seg in _split_unquoted(command)[0]:
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError as e:
            return out, f"cannot parse command segment {seg!r}: {e}"
        # skip leading env assignments (FOO=bar cmd ...)
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens = tokens[1:]
        tokens = _strip_runner_prefix(tokens)
        if tokens:
            out.append(tokens)
    return out, None


def check_command(command: str, categories: tuple[str, ...]) -> str | None:
    """Return None if allowed, else a rejection reason."""
    if re.search(r"(?<!\d)>{1,2}|<\(", _split_unquoted(command)[1]):
        return "output redirection is not allowed; use the write tool to create files"
    segments, parse_error = _segment_tokens(command)
    if parse_error:
        return parse_error
    for tokens in segments:
        reason = _check_segment(tokens[0].rsplit("/", 1)[-1], tokens, categories)
        if reason:
            return reason
    return None


def _is_check_linter(head: str, tokens: list[str]) -> bool:
    """A linter/type checker in checking mode — `ruff format` rewrites files and
    proves nothing, so it does not count."""
    return head in CHECK_LINTERS and not (len(tokens) >= 2 and tokens[1] in FORMAT_SUBCOMMANDS)


def _is_bare_python_script(tokens: list[str]) -> bool:
    """`python script.py` / `python3 manage.py migrate` — bare python on a
    file path. Arbitrary code execution, but the harness already has
    edit/write tools that can write arbitrary code, so this removes friction
    without adding a new attack surface. `python -c "..."` (inline code with
    no file to audit) is rejected; `python -m <module>` is handled separately
    and stays restricted to PYTHON_MODULE_ALLOW."""
    if len(tokens) < 2:
        return False
    arg = tokens[1]
    if arg.startswith("-"):
        return False  # -c, -m, -I, ... are not a bare script path
    return arg.endswith(".py") or "/" in arg


def _is_venv_activate(tokens: list[str]) -> bool:
    """`source .venv/bin/activate` / `. /abs/path/venv/bin/activate`.

    Matched by path shape rather than a fixed path, so every layout works
    (.venv, venv, env, an absolute path outside the repo). Sourcing anything
    else is arbitrary code execution wearing a builtin's name, so exactly one
    argument is accepted and it has to end <dir>/bin/activate.

    Deliberately reachable only through the `test` category, never `search`:
    search-only commands run without a permission prompt, and a file the model
    could have written is not something to execute unasked.
    """
    if len(tokens) != 2:
        return False
    parts = tokens[1].split("/")
    return len(parts) >= 2 and parts[-1] in ACTIVATE_NAMES and parts[-2] in ACTIVATE_DIRS


def _is_check_segment(head: str, tokens: list[str]) -> bool:
    """True when this segment runs the project's tests (the "test" category).

    Shared by the allowlist and by `done`'s verification gate, so the two can
    never disagree: a command the model is told to verify with is always a
    command it is allowed to run.
    """
    if head in TEST_COMMANDS:
        return True
    if head in {"python", "python3"} and len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in PYTHON_MODULE_ALLOW:
        return True
    if head in NPM_LIKE and len(tokens) >= 2 and tokens[1] == "test":
        return True
    # any package.json script name counts as a test/check — the script is the
    # user's own, not arbitrary code from the model. Only the prefix-matched
    # test/lint/build/etc. scripts count as verification, though (see below).
    if len(tokens) >= 3 and (head, tokens[1]) in SCRIPT_RUNNERS and tokens[2].startswith(SCRIPT_VERIFY_PREFIXES):
        return True
    if head == "go" and len(tokens) >= 2 and tokens[1] in {"test", "vet", "build"}:
        return True
    if head == "cargo" and len(tokens) >= 2 and tokens[1] in {"test", "check", "clippy"}:
        return True
    if head == "make" and len(tokens) >= 2 and tokens[1] in MAKE_TARGETS:
        return True
    return False


def is_pure_search(command: str) -> bool:
    """True when every segment is a read-only search command.

    This is the same allowlist check the tool already runs, narrowed to the
    `search` category alone — so it inherits, for free, every escape hatch
    that check already closes: output redirection, `sed -i`, `find -exec`,
    and xargs delegating into a non-search command. A command that passes
    cannot write, so approving it is a question with one answer.

    Deliberately narrower than "allowed": tests, linters and git reads stay
    gated. A test run rewrites fixtures and a git read can be widened into a
    fetch, and neither is worth losing a prompt over.
    """
    return check_command(command, ("search",)) is None


def is_verification_command(command: str) -> bool:
    """True when the command line checks the work — a test run, type check or
    linter in any of its segments. `done` uses this to tell a session that
    verified its change from one that only asserted it did."""
    segments, parse_error = _segment_tokens(command)
    if parse_error:
        return False
    for tokens in segments:
        head = tokens[0].rsplit("/", 1)[-1]
        if _is_check_segment(head, tokens) or _is_check_linter(head, tokens):
            return True
    return False


def _search_write_reason(head: str, tokens: list[str]) -> str | None:
    """Read-only search commands that were handed write or exec flags."""
    if head == "sed" and any(t == "-i" or t.startswith("-i") and len(t) <= 4 for t in tokens[1:]):
        return "sed -i edits files; use the edit tool instead"
    if head == "find" and any(t in FIND_WRITE_FLAGS for t in tokens[1:]):
        return (
            "find -delete/-exec can delete files or run any command; search with "
            "-name/-path and use the edit/write tools to change files"
        )
    return None


def _strip_xargs_flags(tokens: list[str]) -> list[str]:
    """Drop xargs' own flags so what it will execute is left at the front."""
    i = 0
    while i < len(tokens) and tokens[i].startswith("-"):
        flag = tokens[i].split("=", 1)[0]
        i += 1
        # -n 5 / -I {} take a separate value; -n5 / -I{} carry it inline
        if flag in XARGS_VALUE_FLAGS and len(flag) <= 2 and i < len(tokens):
            i += 1
    return tokens[i:]


def _check_segment(head: str, tokens: list[str], categories: tuple[str, ...]) -> str | None:
    if "search" in categories and head in SEARCH_COMMANDS:
        # xargs executes whatever it is given — judge that, not xargs
        if head == "xargs":
            inner = _strip_runner_prefix(_strip_xargs_flags(tokens[1:]))
            if not inner:
                return None  # bare xargs runs echo
            inner_head = inner[0].rsplit("/", 1)[-1]
            if inner_head == "xargs":
                return "nested xargs is not allowed"
            return _check_segment(inner_head, inner, categories)
        return _search_write_reason(head, tokens)
    if "lint" in categories and head in LINT_COMMANDS:
        return None
    if "test" in categories and _is_check_segment(head, tokens):
        return None
    if "test" in categories and head in NPM_LIKE and len(tokens) >= 2 and tokens[1] in INSTALL_SUBCOMMANDS:
        return None
    if "test" in categories and head in PIP_INSTALL_HEADS and len(tokens) >= 2 and tokens[1] == "install":
        return None
    if "test" in categories and head in {"python", "python3"} and _is_bare_python_script(tokens):
        return None
    if "test" in categories and len(tokens) >= 3 and (head, tokens[1]) in SCRIPT_RUNNERS:
        # any package.json script name is allowed — the script is the user's
        # own, not arbitrary code from the model
        return None
    if head in SOURCE_COMMANDS:
        if "test" in categories and _is_venv_activate(tokens):
            return None
        return (
            "source runs whatever is in the file it is given; only a virtualenv "
            f"activate script is allowed (source .venv/bin/activate). {CATEGORY_HELP}"
        )
    if "git_read" in categories and head == "git":
        sub = next((t for t in tokens[1:] if not t.startswith("-")), "")
        if sub in GIT_READ_SUBCOMMANDS:
            return None
        return f"git '{sub}' is not a read-only subcommand ({CATEGORY_HELP})"
    return f"'{head}' is not in the allowed categories. {CATEGORY_HELP}"


class BashTool(Tool):
    name = "bash"
    # the category allowlist below constrains *what* may run; it is not consent.
    # An allowed category still writes (a test can rewrite fixtures, a git read
    # can be widened), and a gate on edit/write that a heredoc walks around is
    # not a gate.
    requires_permission = True
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

    def needs_permission(self, args: dict[str, Any], ctx: ToolContext) -> bool:
        """Search-only commands run unasked; everything else still prompts.

        Sessions were spending ~50 prompts apiece on `grep`/`cat`/`find` that
        the allowlist had already proven read-only, and the fatigue showed:
        users started denying them. `search` must be an enabled category for
        this to apply — a harness that turned search off has said no already.
        """
        if "search" not in ctx.bash_categories:
            return True
        return not is_pure_search(args.get("command", ""))

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
