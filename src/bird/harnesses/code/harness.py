"""The Code harness definition: its instructions and toolset selection.

Moved verbatim from bird.tools (which stays the shared toolbox — concrete tool
implementations live there; which of them a harness mounts is decided here).
"""

from __future__ import annotations

from pathlib import Path

from ...tools import (
    BashTool,
    DoneTool,
    EditTool,
    KgQueryTool,
    LsTool,
    PlanTool,
    PlanUpdateTool,
    ReadImageTool,
    ReadTool,
    SkillTool,
    Tool,
    WebFetchTool,
    WebSearchTool,
    WriteTool,
)

INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.md")


def code_harness_tools(with_kg: bool = True, with_web: bool = True) -> list[Tool]:
    """The Code harness toolset.

    - with_kg=False  → eval control arm: no kg_query (decision #12). Plan tools
                       stay; they just lose the KG blast-radius expansion.
    - with_web=False → also strip web tools, used by offline eval runs where
                       network egress must be a hard NO.
    """
    tools: list[Tool] = [ReadTool(), ReadImageTool(), LsTool(), EditTool(), WriteTool(), BashTool()]
    if with_kg:
        tools.append(KgQueryTool())
    if with_web:
        tools.extend([WebSearchTool(), WebFetchTool()])
    tools.extend([PlanTool(), PlanUpdateTool(), SkillTool(), DoneTool()])
    return tools
