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

        return ToolResult(output=args["summary"], details=details)
