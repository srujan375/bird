"""skill — load a reusable skill's full instructions on demand.

The system prompt carries a cheap index of skill names + one-line
descriptions. When the model decides a skill is relevant to the current
task, it calls this tool to pull the full body into context. This is
progressive disclosure: descriptions are always in context, full
instructions load on-demand (mirrors pi / Claude Code skills).
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolError, ToolResult


class SkillTool(Tool):
    name = "skill"
    description = (
        "Load a skill's full instructions by name. The system prompt lists "
        "available skills with one-line descriptions; call this with a name "
        "to retrieve the complete procedure when a task matches one."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (from the [skills] index in the system prompt)",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = args["name"]
        skills = ctx.skills
        if not skills:
            return ToolResult(
                output="No skills are available in this repository.",
                details={"available": []},
            )
        by_name = {s.name: s for s in skills}
        if name in by_name:
            sk = by_name[name]
            ctx.emit("skill_loaded", {"name": name, "source": sk.source})
            return ToolResult(
                output=sk.body,
                details={"name": name, "source": sk.source, "path": str(sk.path)},
            )
        # helpful miss: list what's available (mirrors validate_tool_call's
        # nearest-term pattern — a miss should never leave the model guessing)
        available = ", ".join(sorted(by_name))
        raise ToolError(
            f"No skill named {name!r}. Available skills: {available}"
        )