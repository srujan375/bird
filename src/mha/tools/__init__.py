from .base import Tool, ToolContext, ToolError, ToolResult
from .bash import BashTool
from .done import DoneTool
from .files import EditTool, ReadTool, WriteTool
from .kg_query import KgQueryTool
from .plan import PlanState, PlanTool, PlanUpdateTool


def code_harness_tools(with_kg: bool = True) -> list[Tool]:
    """The Code harness toolset. with_kg=False is the eval control arm
    (identical harness minus kg_query — decision #12; plan tools stay, they
    just lose the KG blast-radius expansion)."""
    tools: list[Tool] = [ReadTool(), EditTool(), WriteTool(), BashTool()]
    if with_kg:
        tools.append(KgQueryTool())
    tools.extend([PlanTool(), PlanUpdateTool(), DoneTool()])
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
    "PlanState",
    "PlanTool",
    "PlanUpdateTool",
    "DoneTool",
    "code_harness_tools",
]
