"""done — explicit termination (standing decision: no stopped-calling-tools heuristics)."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolError, ToolResult


class DoneTool(Tool):
    name = "done"
    description = (
        "Call this exactly once, when the task is fully complete, with a short summary "
        "of what was done. This ends the session. Blocked while plan steps are open."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What was accomplished"},
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
        return ToolResult(output=args["summary"], details={"done": True, "summary": args["summary"]})
