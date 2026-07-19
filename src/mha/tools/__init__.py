from .base import Tool, ToolContext, ToolError, ToolResult
from .bash import BashTool
from .done import DoneTool
from .files import EditTool, ReadTool, WriteTool
from .kg_query import KgQueryTool
from .plan import PlanState, PlanTool, PlanUpdateTool
from .skill import SkillTool
from .web import WebFetchTool, WebSearchTool


def code_harness_tools(with_kg: bool = True, with_web: bool = True) -> list[Tool]:
    """The Code harness toolset.

    - with_kg=False  → eval control arm: no kg_query (decision #12). Plan tools
                       stay; they just lose the KG blast-radius expansion.
    - with_web=False → also strip web tools, used by offline eval runs where
                       network egress must be a hard NO.
    """
    tools: list[Tool] = [ReadTool(), EditTool(), WriteTool(), BashTool()]
    if with_kg:
        tools.append(KgQueryTool())
    if with_web:
        tools.extend([WebSearchTool(), WebFetchTool()])
    tools.extend([PlanTool(), PlanUpdateTool(), SkillTool(), DoneTool()])
    return tools


__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ReadTool",
    "EditTool",
    "WriteTool",
    "BashTool",
    "KgQueryTool",
    "WebSearchTool",
    "WebFetchTool",
    "PlanState",
    "PlanTool",
    "PlanUpdateTool",
    "SkillTool",
    "DoneTool",
    "code_harness_tools",
]
