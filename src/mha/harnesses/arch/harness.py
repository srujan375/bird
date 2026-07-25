"""The Arch harness definition: instructions, engine tuning, static page dir.

The toolset factory lives in tools.py (arch_harness_tools); this module is
what the CLI wires into the Runner so arch sessions get arch behavior from
the shared engine.
"""

from __future__ import annotations

from pathlib import Path

from ...tools import ToolContext
from . import render

INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.md")
STATIC_DIR = Path(__file__).with_name("static")

# structured-state mutations count as progress for the engine's explore nudge
MUTATING_TOOLS = frozenset({
    "variant", "node", "link", "splice", "depth", "promote",
    "brief", "component", "connect", "flow", "expand", "decide",
    "ask", "answer", "amend_toplevel",
})

EXPLORE_NUDGE = (
    "[system notice] {n} turns without recording anything. Sketch or record structure "
    "NOW (variant / node / link while brainstorming; brief / component / flow / decide "
    "once promoted), or give the user your question as a reply."
)


def arch_tracker(ctx: ToolContext) -> str | None:
    """The engine's tracker provider: pin the arch tracker every turn."""
    if ctx.arch is None:
        return None
    return render.tracker(ctx.arch.state)
