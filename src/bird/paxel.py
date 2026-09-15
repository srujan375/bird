"""`bird paxel` — stage this repo's bird sessions for Paxel's uploader.

Paxel (YC's coding-agent report) ingests the "Claude Code layout": one
directory per project holding a `sessions-index.json` plus one JSONL
transcript per session, each line carrying a top-level `type` and a nested
`message` object with a `role`. This module converts bird's own session store
into that layout and stops there — it never uploads. The staging dir is a
local file the user can read before anything leaves the machine, which is the
whole reason v1 can skip redaction.

The export source is `messages.jsonl` (the final conversation), not
`events.jsonl`. events.jsonl is the append-only trace and is deliberately out
of scope: it is a different schema, and it is also the more sensitive of the
two — it carries every bash command and tool argument verbatim, including
anything the user pasted into a prompt. Reading messages.jsonl keeps the
export to what a resumed session would see.

`originalPath` in the index is the repo's git toplevel, not bird's
`.bird/sessions/` path. That field is how Paxel buckets sessions into a
project and attaches git metadata, so declaring the same toplevel Claude Code
declares is what merges bird's sessions into the same project bucket instead
of splitting them into a second one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .engine.session import load_messages

STAGE_DIR_ENV = "PAXEL_STAGE_DIR"
DEFAULT_STAGE_DIR = Path.home() / ".paxel" / "staged-transcripts"
INDEX_FILE = "sessions-index.json"
SESSIONS_SUBDIR = Path(".bird") / "sessions"
UPLOAD_URL = "https://paxel.ycombinator.com/upload.sh"

# How much of the first prompt the per-session audit line shows. The line is
# the only thing between a session the user would rather not upload and
# Paxel's uploader, so it has to be recognizable at a glance — but a full
# prompt would wrap the terminal and bury the run-id it belongs to.
PROMPT_PREVIEW = 60


def resolve_stage_dir(cli_value: str | None) -> Path:
    """--stage-dir beats PAXEL_STAGE_DIR beats ~/.paxel/staged-transcripts.

    The default lives under $HOME because Paxel's uploader refuses a
    TRANSCRIPT_DIR outside it; an explicit override is the user's call.
    """
    if cli_value:
        return Path(cli_value).expanduser()
    env = os.environ.get(STAGE_DIR_ENV)
    if env:
        return Path(env).expanduser()
    return DEFAULT_STAGE_DIR


def encode_dir(path: Path) -> str:
    """The community bridge's project-dir encoding: `/` and `.` become `-`.

    Taken verbatim from stage-transcripts.py so bird's project dir lands
    beside the Cursor/Hermes ones rather than in a shape of our own.
    """
    return re.sub(r"[/.]", "-", str(path))


def git_toplevel(repo_root: Path) -> tuple[Path, bool]:
    """The repo's git toplevel, or (repo_root, False) when there isn't one.

    Returns the flag rather than raising: a non-git directory is a normal
    thing to run bird in, and the caller says so in its output instead of
    failing. A missing git binary, a non-zero exit and an empty answer all
    mean the same thing here — no toplevel to declare.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return repo_root, False
    if proc.returncode != 0:
        return repo_root, False
    out = proc.stdout.strip()
    if not out:
        return repo_root, False
    top = Path(out)
    if not top.is_dir():
        return repo_root, False
    return top, True


def _text_of(content: Any) -> str:
    """Flatten a message's content to text.

    String content is the normal case. List content is the multimodal
    (content-parts) shape, which per llm/types.py never reaches the
    transcript — but a session written by a future version might carry it, and
    dropping the turn would be worse than flattening it.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _tool_input(tool_call: dict[str, Any]) -> dict[str, Any]:
    """The tool_use `input` block.

    `arguments` is None when the model emitted invalid JSON; the raw string is
    still on the call, so try it before giving up on an empty object — the
    malformed call is itself part of how the session went.
    """
    args = tool_call.get("arguments")
    if isinstance(args, dict):
        return args
    raw = tool_call.get("arguments_json")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def map_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One bird Message.to_dict() per line → Paxel JSONL lines, Claude-shaped.

    `type` is set here, which is what makes the community bridge's
    fix-format.py pass a no-op over bird's output — that is the contract this
    mapping is checked against.

    Consecutive role="tool" rows coalesce into ONE user turn with several
    tool_result blocks: that is how Claude Code writes a multi-tool turn, and
    bird writes one line per result. bird's tool_call_id is preserved verbatim
    as tool_use_id so the pairs resolve.
    """
    lines: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            lines.append({
                "type": "user",
                "message": {"role": "user", "content": list(pending_results)},
            })
            pending_results.clear()

    for row in rows:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        if role == "system":
            # the system prompt is bird's own instructions plus the repo map —
            # never a turn in the conversation, and not the user's words
            continue
        if role == "tool":
            pending_results.append({
                "type": "tool_result",
                "tool_use_id": row.get("tool_call_id"),
                "content": _text_of(row.get("content")),
            })
            continue
        flush_results()
        if role == "user":
            lines.append({
                "type": "user",
                "message": {"role": "user", "content": _text_of(row.get("content"))},
            })
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _text_of(row.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tool_call in row.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                blocks.append({
                    "type": "tool_use",
                    "id": tool_call.get("id"),
                    "name": tool_call.get("name"),
                    "input": _tool_input(tool_call),
                })
            if not blocks:
                # an assistant turn with neither text nor a tool call says
                # nothing; `thinking` is display-only and never exported, so
                # there is no content left to write
                continue
            # content is the block list alone when there is no text — never
            # null, which is the shape Paxel's reader expects
            lines.append({
                "type": "assistant",
                "message": {"role": "assistant", "content": blocks},
            })
    flush_results()
    return lines


def _first_prompt(rows: list[dict[str, Any]]) -> str:
    """The first user message, flattened to one line for the audit print."""
    for row in rows:
        if isinstance(row, dict) and row.get("role") == "user":
            text = " ".join(_text_of(row.get("content")).split())
            if text:
                return text[:PROMPT_PREVIEW]
    return ""


def _session_dirs(sessions_dir: Path, wanted: list[str] | None) -> list[Path]:
    """Session dirs to export, oldest first (run-ids are timestamp-prefixed,
    so a name sort is a chronological one).

    `wanted` matches a run-id exactly or by prefix, the same way /continue
    resolves one — a user reading a run-id off the terminal rarely has the
    whole thing.
    """
    if not sessions_dir.is_dir():
        return []
    dirs = sorted((p for p in sessions_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not wanted:
        return dirs
    out = []
    for p in dirs:
        if any(p.name == w or p.name.startswith(w + "-") for w in wanted):
            out.append(p)
    return out


def stage_sessions(
    repo_root: Path,
    stage_dir: Path,
    sessions: list[str] | None = None,
) -> int:
    """Write the staged layout and print what was staged. Returns an exit code.

    Replaces bird's own project dir wholesale: the index has to match the
    files beside it, and a stale entry pointing at a transcript that is gone
    is worse than re-running the command. The staging ROOT is never wiped —
    whatever the Cursor/Hermes exporters staged there is not bird's to touch.
    """
    original_path, is_git = git_toplevel(repo_root)
    project_dir = stage_dir / encode_dir(original_path)

    # encode_dir of an absolute path can't collapse to the root itself, but a
    # guard is cheaper than the one bug that would delete every other agent's
    # staged transcripts
    if project_dir == stage_dir:
        print(f"error: refusing to stage into the staging root itself ({stage_dir})", file=sys.stderr)
        return 1

    sessions_dir = repo_root / SESSIONS_SUBDIR
    candidates = _session_dirs(sessions_dir, sessions)
    if not candidates:
        if sessions:
            print(f"no session matched {', '.join(sessions)} under {sessions_dir}", file=sys.stderr)
        else:
            print(f"no sessions found under {sessions_dir}", file=sys.stderr)
        return 1

    # map everything before touching the disk: a session that turns out to
    # have nothing exportable must not leave a half-written project dir, and
    # the manifest has to describe exactly the files that get written
    staged: list[tuple[str, list[dict[str, Any]], str]] = []
    for run_dir in candidates:
        # load_messages() is the same reader the resume path uses, so a legacy
        # session exports as a degraded transcript instead of vanishing
        rows = load_messages(run_dir)
        if not rows:
            continue
        lines = map_messages(rows)
        if not lines:
            continue
        staged.append((run_dir.name, lines, _first_prompt(rows)))

    if not staged:
        print(f"nothing to stage: {len(candidates)} session(s) had no exportable transcript",
              file=sys.stderr)
        return 1

    entries = [
        {"sessionId": run_id, "originalPath": str(original_path)}
        for run_id, _lines, _prompt in staged
    ]

    try:
        if project_dir.exists():
            shutil.rmtree(project_dir)
        project_dir.mkdir(parents=True)
        for run_id, lines, _prompt in staged:
            body = "".join(
                json.dumps(line, ensure_ascii=False, default=str) + "\n" for line in lines
            )
            (project_dir / f"{run_id}.jsonl").write_text(body, encoding="utf-8")
        (project_dir / INDEX_FILE).write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        # a half-written project dir is regenerable, so this fails loudly with
        # the path and the errno rather than doing a temp-dir dance
        print(f"error: cannot write staging dir {project_dir}: {e.strerror or e} "
              f"(errno {e.errno})", file=sys.stderr)
        return 1

    for run_id, lines, prompt in staged:
        suffix = f'  "{prompt}"' if prompt else ""
        print(f"  {run_id}  {len(lines)} turns{suffix}")
    print(f"\nstaged {len(staged)} session(s) → {project_dir}")
    if is_git:
        print(f"project: {original_path} (git toplevel)")
    else:
        print(f"project: {original_path} (not a git repo — falling back to the project root, "
              f"so Paxel will not attach git metadata)")
    if not str(stage_dir).startswith(str(Path.home())):
        print(f"warning: {stage_dir} is outside $HOME — Paxel's uploader refuses a "
              f"TRANSCRIPT_DIR there", file=sys.stderr)

    # The export is the last moment the user is looking at the transcript
    # before it becomes someone else's input, and the staging dir is the only
    # place they can still change their mind. Two sentences here are the whole
    # reason v1 can skip redaction.
    print("\nnothing has left this machine yet — read the files above before uploading.")
    print("Paxel sends transcript excerpts (prompts, responses, tool-call snippets) to "
          "Claude/GPT via its proxy; the report it uploads to YC carries scores, narratives, "
          "file paths and git metadata.")
    print("\nnext:")
    print(f"  export TRANSCRIPT_DIR={stage_dir}")
    print(f"  curl -fsSL {UPLOAD_URL} | bash -s -- --all")
    print("  (with the community bridge, --all-agents stages bird and Claude Code into ONE "
          "report; running Paxel from the parent folder does the same)")
    return 0


def paxel_main(args, repo_root: Path) -> int:
    """Dispatch for `bird paxel`."""
    stage_dir = resolve_stage_dir(getattr(args, "stage_dir", None))
    return stage_sessions(repo_root, stage_dir, sessions=getattr(args, "session", None))
