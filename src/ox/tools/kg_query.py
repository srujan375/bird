"""kg_query — the thesis tool. Query the repo knowledge graph.

While the KG is still building (background subprocess, decision #9) this
returns a "still building" hint so the model falls back to bash search; the
runner injects a notice when the graph becomes available. Bash-fallback
frequency is itself a KG quality metric, so both states are logged.
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolResult

STILL_BUILDING = (
    "The knowledge graph is still building. Use bash (rg/grep/find) to search "
    "the repository for now; a notice will arrive when the graph is ready — "
    "switch back to kg_query as your primary search tool then."
)


class KgQueryTool(Tool):
    name = "kg_query"
    description = (
        "Ask the repository knowledge graph a question about code structure: where "
        "something is defined, what calls/imports it, how modules relate. Faster and "
        "more precise than searching with bash."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Natural-language question about the codebase"},
            "budget": {"type": "integer", "description": "Max answer tokens (default 2000)"},
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        question = args["question"]
        budget = args.get("budget", 2000)
        if ctx.kg is None or not ctx.kg.is_ready():
            ctx.emit("kg_query_unavailable", {"question": question})
            return ToolResult(output=STILL_BUILDING, details={"ready": False})
        result = ctx.kg.query(question, budget=budget)
        ctx.emit(
            "kg_query",
            {"question": question, "hits": result.hit_count, "expanded": result.expanded_tokens},
        )
        return ToolResult(
            output=result.text,
            details={
                "ready": True,
                "hits": result.hit_count,
                "expanded_tokens": result.expanded_tokens,
                "mode": result.mode,
            },
        )
