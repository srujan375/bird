"""The intake: what the designer has to be told before it draws anything.

Two questions, asked one at a time in the workbench, through the same picker
the model's own questions use (`harnesses/picker.py`):

  1. Wireframe or high-fidelity — the one call that changes every artboard.
  2. Which design system — only in the high-fidelity branch; a wireframe has
     no design system to pick, so the question never appears.

The point is the order of operations. Until both are settled the designer is
not running: the brief waits, `design_create` is refused, and the page shows
the question instead of a skeleton loader pretending work is happening. Three
artboards in the wrong fidelity are three artboards nobody asked for.
"""

from __future__ import annotations

from pathlib import Path

from ..picker import Ask, Option
from . import themes

FIDELITY = "fidelity"
DESIGN_SYSTEM = "design-system"

WIREFRAME = "wireframe"
HIGH_FIDELITY = "high-fidelity"

# what the composed opening message calls each direction — the wording the
# design instructions tell the model to read
DIRECTION_LABEL = {WIREFRAME: "Wireframe", HIGH_FIDELITY: "High-fidelity"}

# the designer picks, and offers the full list in chat if they want it
DESIGNER_CHOICE = "designer"

# The order the shortlist is drawn from. Three named systems and a spread —
# product, document, and consumer — because four cards is the ceiling and a
# list of sixteen is a menu, not a question. Anything not here is still one
# sentence away in chat; this is the opener, not the catalogue.
SHORTLIST = (
    "linear-app",
    "notion",
    "editorial",
    "apple",
    "swiss-minimal",
    "claude",
    "github",
    "figma",
    "framer",
    "openai",
)
SHORTLIST_MAX = 3


def fidelity_ask() -> Ask:
    """Question one. It is first because everything downstream reads it."""
    return Ask(
        id=FIDELITY,
        prompt="Should I design this as a wireframe or high-fidelity?",
        summary_label="Direction",
        confirm_template="Design in {v}",
        confirm_empty="Pick a direction",
        options=[
            Option(
                value=WIREFRAME,
                label="Wireframe",
                description="Greyscale structure. Fast to iterate.",
            ),
            Option(
                value=HIGH_FIDELITY,
                label="High-fidelity",
                description="Real theme, colour and type.",
                rec=True,
            ),
        ],
    )


def design_system_ask(repo_root: Path | None = None) -> Ask | None:
    """Question two, in the high-fidelity branch only.

    Built from whatever themes are installed: the shortlist in preference
    order, plus the row that hands the choice back. None when there are no
    themes at all — then there is nothing to ask and the designer styles it
    itself.
    """
    available = [t for t in themes.list_themes(repo_root) if t != WIREFRAME]
    if not available:
        return None
    ranked = [t for t in SHORTLIST if t in available][:SHORTLIST_MAX]
    if not ranked:
        ranked = available[:SHORTLIST_MAX]
    options = [
        Option(value=name, label=_title(name), description=themes.describe(name, repo_root))
        for name in ranked
    ]
    options.append(
        Option(
            value=DESIGNER_CHOICE,
            label="You choose",
            description="I'll match the brief, and show you the rest.",
            rec=True,
        )
    )
    return Ask(
        id=DESIGN_SYSTEM,
        prompt="Which design system should I work in?",
        summary_label="Design system",
        confirm_template="Design in {v}",
        confirm_empty="Pick a design system",
        options=options,
    )


def _title(name: str) -> str:
    """`linear-app` → `Linear`, `swiss-minimal` → `Swiss Minimal`. The theme
    dir name is a slug; the row is read by a person."""
    words = [w for w in str(name).replace("_", "-").split("-") if w]
    if len(words) > 1 and words[-1] in ("app", "ui", "web"):
        words = words[:-1]
    return " ".join(w if w.isupper() else w.capitalize() for w in words)


def sync(queue, repo_root: Path | None = None) -> None:
    """Make the queue match the answers it already has.

    The design-system question exists only in the high-fidelity branch — and
    stops existing if they press Change and switch to a wireframe. A queue
    that keeps a question its own answers have retired would ask something
    nobody needs to answer.
    """
    fidelity = queue.by_id(FIDELITY)
    if fidelity is None or not fidelity.answered:
        return
    wants_system = fidelity.answer == HIGH_FIDELITY
    parked = queue.by_id(DESIGN_SYSTEM)
    if wants_system and parked is None:
        ask = design_system_ask(repo_root)
        if ask is not None:
            queue.add(ask)
    elif not wants_system and parked is not None:
        queue.drop(DESIGN_SYSTEM)


def direction(queue) -> str:
    """The chosen direction, in the words the opening message uses."""
    return DIRECTION_LABEL.get(queue.value(FIDELITY), DIRECTION_LABEL[HIGH_FIDELITY])


def design_system(queue) -> str:
    """The chosen theme's name, or "" when it is the designer's call. A
    wireframe direction answers this on its own: the wireframe theme *is* the
    greyscale, and asking which design system to sketch in is a question with
    no consequence."""
    if queue.value(FIDELITY) == WIREFRAME:
        return WIREFRAME
    picked = queue.value(DESIGN_SYSTEM)
    return "" if picked in ("", DESIGNER_CHOICE) else picked


def opening_message(queue, brief: str = "") -> str:
    """The first thing the designer is told, in the shape its instructions
    already know how to read: a direction, a design system, and the brief that
    has been waiting behind the questions."""
    system = design_system(queue) or "unspecified"
    text = f"Direction: {direction(queue)}. Design system: {system}."
    brief = str(brief or "").strip()
    return f"{text} Brief: {brief}" if brief else text
