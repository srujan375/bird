"""done — explicit termination (standing decision: no stopped-calling-tools heuristics).

`done` is where a run makes its claim, so it is where the claim gets checked.
Two gates, both engine-enforced rather than asked for in the prompt: every plan
step is closed, and every file changed since the last passing check has been
re-checked. The second one exists because the instructions already said "verify
your change" and the session logs showed that being skipped in ~38% of
completed runs.
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolError, ToolResult

MAX_LISTED_PATHS = 6


def _unverified_detail(ctx: ToolContext) -> str:
    last = ctx.last_verify
    if last is None:
        return "You have not run any test, type check or linter this session."
    if last["exit_code"] != 0:
        return f"Your last check (`{last['command']}`) failed with exit {last['exit_code']}."
    return f"Your last passing check (`{last['command']}`) ran BEFORE these edits."


def _record_findings(raw: Any, ctx: ToolContext) -> list[dict[str, str]]:
    """Put the run's findings in the shared store. Best-effort: a malformed
    entry is skipped rather than failing a `done` that is otherwise valid —
    losing a note is a smaller harm than refusing to end a finished run."""
    if ctx.store is None or not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path, note = item.get("path"), item.get("note")
        if not isinstance(path, str) or not isinstance(note, str) or not note.strip():
            continue
        ctx.store.note(ctx.repo_root, path, ctx.harness, note)
        out.append({"path": path, "note": note})
    return out


class DoneTool(Tool):
    name = "done"
    # Schema text is per-turn context for every turn (decision #6), so the
    # rules live in instructions.md and in the rejection message; this stays
    # short enough to be worth its place in the window.
    description = (
        "Call this once, when the task is complete. Ends the session. Blocked while "
        "plan steps are open, or while an edited file has no passing check."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What was accomplished"},
            "unverified_reason": {
                "type": "string",
                "description": "Why, if no check covers the change",
            },
            "findings": {
                "type": "array",
                "description": (
                    "What the NEXT session would otherwise have to rediscover: "
                    "per file, the one fact about it that cost you a read to learn."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["path", "note"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.plan is not None:
            open_steps = ctx.plan.open_steps()
            if open_steps:
                titles = "; ".join(
                    f"{i + 1}. {ctx.plan.steps[i].title}" for i in open_steps
                )
                raise ToolError(
                    f"plan steps still open: {titles}. Finish them (or mark them "
                    'skipped with plan_update {"step": N, "status": "skipped"}), '
                    "then call done."
                )

        details: dict[str, Any] = {"done": True, "summary": args["summary"]}
        if ctx.require_verification and ctx.unverified_paths:
            paths = ctx.unverified_paths
            shown = ", ".join(paths[:MAX_LISTED_PATHS])
            if len(paths) > MAX_LISTED_PATHS:
                shown += f" (+{len(paths) - MAX_LISTED_PATHS} more)"
            reason = args.get("unverified_reason")
            # The escape hatch only opens after the model has been told once —
            # otherwise the first `done` can carry a reason and the gate is
            # decorative. One extra turn is the price of an honest skip.
            if not reason or not ctx.done_blocked_once:
                ctx.done_blocked_once = True
                ctx.emit(
                    "done_blocked_unverified",
                    {"paths": list(paths), "last_verify": ctx.last_verify},
                )
                raise ToolError(
                    f"{_unverified_detail(ctx)} Changed and unchecked: {shown}. Run this "
                    "project's check with bash (pytest / npm test / npm run build / ruff "
                    "check / mypy / tsc --noEmit — `uv run` and `npx` prefixes are "
                    "allowed) and call done once it passes. If this repo genuinely has "
                    "no check covering the change, call done again with "
                    "unverified_reason saying why."
                )
            ctx.emit(
                "done_unverified",
                {"paths": list(paths), "reason": reason, "last_verify": ctx.last_verify},
            )
            details["unverified"] = {"paths": list(paths), "reason": reason}

        # The run is already stopping to summarize, so harvesting findings here
        # costs no extra turn — which is the whole reason they hang off `done`
        # rather than a tool of their own that a model has to remember to call.
        recorded = _record_findings(args.get("findings"), ctx)
        if recorded:
            details["findings"] = recorded
        return ToolResult(output=args["summary"], details=details)
