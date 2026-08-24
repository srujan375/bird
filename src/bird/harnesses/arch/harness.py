"""The Arch harness definition: instructions, engine tuning, static page dir.

The toolset factory lives in tools.py; this module is what the CLI wires into
the Runner so arch sessions get arch behavior from the shared engine.

The tracker hook is still here but it does a different job now. It used to
re-render a wall of status into the conversation every turn *and* kick a
background critic. It now returns a short internal note — what is settled, what
is waiting on the user, which branches are askable — and nothing else. The user
never sees it; the detail belongs on the canvas.
"""

from __future__ import annotations

from pathlib import Path

from ...tools import ToolContext
from . import derive

INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.md")
STATIC_DIR = Path(__file__).with_name("static")

TRACKER_PREFIX = "[arch]"

EXPLORE_NUDGE = (
    "[system notice] {n} turns without recording anything. If you have a shape in "
    "mind, put it on the board with `canvas`; if a call got made, `decide` it; if "
    "you need the user, ask them in your reply. Thinking that never lands anywhere "
    "doesn't survive the session."
)


def arch_tracker(ctx: ToolContext) -> str | None:
    """The engine's tracker provider: the architect's own working memory,
    pinned once per turn and refreshed in place.

    Draining the user's edits here is what puts them in front of the model — a
    page edit changes the shared state, but the state is a snapshot and cannot
    say a human just did something."""
    if ctx.arch is None:
        return None
    return derive.note(ctx.arch.state, ctx.arch.take_user_edits())
