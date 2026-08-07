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
    "concern", "offer", "ask", "answer", "amend_toplevel",
})

EXPLORE_NUDGE = (
    "[system notice] {n} turns without recording anything. If you have a shape in mind, "
    "put it on the canvas (variant / node / link, or component / flow / decide); if you "
    "have an objection, record it with `concern`; if you need the user, reply and ask. "
    "Thinking that never lands anywhere doesn't survive the session."
)


def arch_tracker(ctx: ToolContext) -> str | None:
    """The engine's tracker provider: pin the arch tracker every turn.

    Also where the critic is kicked — this runs once per turn, and
    `start_critic` returns immediately (the review happens on its own thread),
    so the turn is never delayed by it."""
    if ctx.arch is None:
        return None
    ctx.arch.start_critic()
    return render.tracker(ctx.arch.state)
