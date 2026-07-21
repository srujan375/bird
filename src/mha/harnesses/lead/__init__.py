"""The Lead harness: the conversational front door.

It talks to the user directly — answering questions, exploring the repo,
researching the web — and *dispatches* to sub-harnesses when real work is
needed: `architect` then `code` for a new feature, `code` for a localized
change. It can look and talk (read / kg / web / skills), but it cannot edit
the repo itself: every code change goes through a dispatched code session.

Its instructions are the whole routing policy. The registry
(harnesses.registry) wires it into the engine.
"""

from __future__ import annotations

from pathlib import Path

from ...tools import (
    DoneTool,
    KgQueryTool,
    ReadTool,
    SkillTool,
    Tool,
    WebFetchTool,
    WebSearchTool,
)
from .tools import ArchitectTool, CodeTool

INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.md")

# what counts as "progress" for the engine's explore nudge: the lead makes
# progress by dispatching work, not by editing (it has no edit/write)
MUTATING_TOOLS = frozenset({"architect", "code"})

EXPLORE_NUDGE = (
    "[system notice] {n} turns of looking without acting. Either answer the "
    "user now (reply in plain text), dispatch the work (architect / code), or "
    "call done. Do not keep reading."
)


def lead_harness_tools(with_kg: bool = True, with_web: bool = True) -> list[Tool]:
    """The lead's toolset: read-only exploration + research + the two dispatch
    tools + done. No edit/write/bash — code changes are dispatched, never made
    by the lead itself."""
    tools: list[Tool] = [ReadTool()]
    if with_kg:
        tools.append(KgQueryTool())
    if with_web:
        tools.extend([WebSearchTool(), WebFetchTool()])
    tools.extend([SkillTool(), ArchitectTool(), CodeTool(), DoneTool()])
    return tools
