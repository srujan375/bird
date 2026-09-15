"""The critic — a second model's eyes on the designer's work.

The diagnosis that motivated this loop: a slop-inclined model will not
volunteer criticism of its own output, so the judgment comes from outside —
the vision model behind models.json's 'vision' alias, fed the rendered
artboard (or the plan, before pixels exist) plus the rubric. It returns a
concrete fix-list citing selectors, never a score: scoring stays the
design-review gate's job at finalize.

The rubric is the design-craft skill — the same file the designer loads via
the skill tool. One list, two readers: a slop refresh lands in the generation
rules and the critique rubric together, or the loop judges new slop with old
rules.
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path
from typing import Any

from ...llm.types import ContentPart, Message
from ...tools.files import detect_image_mime
from . import dom, themes

# The one place that knows where builtin skills live — the never-list is
# design-craft.md, the same file the designer loads.
from ...skills import _BUILTIN_DIR

CRAFT_SKILL = "design-craft.md"

# A critic call is capped: a hung provider must cost the create one skip
# note, not the turn. The wire client has no per-call timeout, so this is a
# watchdog that aborts the in-flight request — the same abort the interrupt
# path uses.
#
# The critic shares the DESIGNER's wire client, and that client's abort flag
# is sticky by design (an interrupt landing between requests must kill the
# next one). So a fired critic watchdog must clean up after itself: a plan
# critique that ran 30.3s against this 30s cap returned its note in full,
# and the flag it left behind killed the designer's very next request as
# "aborted (user)" — which serve.py reports as an interrupt the user never
# made. The live session died with "you interrupted it" on the board.
CRITIC_TIMEOUT = 30.0
CRITIC_ABORT_REASON = "critic"

# The element outline rides with the screenshot so fixes can cite selectors
# the designer can design_edit directly. Capped like design_read's outline.
MAX_OUTLINE_CHARS = 8000


def never_list() -> str:
    """The design-craft skill's two "Never" sections — the list itself, not
    the whole skill: the critic used to be handed the announce line, the
    Instead prescriptions and the wireframe exception too, and judged a
    procedure rather than a list.

    Read from disk on every critique rather than cached: the file is the one
    source of truth, and a stale cache is exactly the two-lists drift this
    module exists to prevent. Empty on a missing file — the rubric degrades
    to the theme brief and the plan, and a test asserts the file is there."""
    try:
        body = (_BUILTIN_DIR / CRAFT_SKILL).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _never_sections(body) or body


def _never_sections(body: str) -> str:
    """From the `## Never` heading up to the next `## ` heading — which keeps
    the `### The second generation` subsection under it. "" when the file
    has no such heading, so the caller falls back to the whole body."""
    lines = body.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("## ") and ln[3:].strip().lower().startswith("never")), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


# A full outline of a landing page runs 160-400 lines; the first N lines
# under the char cap were the nav and the hero, never the pricing table the
# fix-list needed. Past this many lines the critic gets the structural view
# instead: everything down to this depth, plus the elements a critique cites
# — headings, links, buttons, forms — at any depth.
COMPACT_ABOVE_LINES = 120
COMPACT_DEPTH = 4
CITED_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "a", "button", "input",
              "form", "nav", "header", "footer", "main", "section", "article",
              "aside", "table", "img", "figure"}


def _outline_lines(session: Any, artboard_id: str) -> str:
    """The design_read outline for the artboard — the selector vocabulary the
    fix-list must speak. Reuses tools.py's builder so the critic's selectors
    are the designer's, by construction; a long document gets the compact
    form (see COMPACT_ABOVE_LINES) rather than its first N lines."""
    from .tools import MAX_OUTLINE_LINES, _outline  # lazy: tools imports session

    html = dom.unwrap_editor(session.read_html(artboard_id))
    if not html:
        return ""
    root = dom.parse(html)
    lines: list[str] = []
    for i, node in enumerate(root.child_elements()):
        _outline(node, str(i), 0, lines)
    if len(lines) > COMPACT_ABOVE_LINES:
        lines = []
        for i, node in enumerate(root.child_elements()):
            _compact_outline(node, str(i), 0, lines)
    body = "\n".join(lines[:MAX_OUTLINE_LINES])
    return body[:MAX_OUTLINE_CHARS]


def _compact_outline(node: dom.Node, selector: str, depth: int, out: list[str]) -> None:
    """tools._outline's shape and selector arithmetic, keeping only the
    elements above COMPACT_DEPTH or in CITED_TAGS. Indices still count every
    element child, skipped ones included, so the paths stay resolvable."""
    keep = depth <= COMPACT_DEPTH or node.tag in CITED_TAGS
    if keep:
        label = node.tag
        if node.attrs.get("id"):
            label += "#" + node.attrs["id"]
        if node.attrs.get("class"):
            label += "." + ".".join(node.attrs["class"].split()[:3])
        text = " ".join(node.text_content().split())
        if text and (not node.child_elements() or node.tag in CITED_TAGS):
            label += f'  "{text[:60]}"'
        out.append(f"{selector}{'  ' * (depth + 1)}{label}")
    for i, kid in enumerate(node.child_elements()):
        if kid.tag in dom.UNSELECTABLE and kid.tag != "html":
            continue
        _compact_outline(kid, f"{selector}>{i}" if selector else str(i), depth + 1, out)


def _theme_brief(session: Any) -> str:
    """The active theme's digest — tokens, Do/Don't, type scale — or '' before
    set_theme. The digest rather than the whole DESIGN.md: a 9k-token brief
    is more than a small vision model holds next to a screenshot, and it is
    the same text the designer was handed, so the two judge by one list."""
    if not session.theme:
        return ""
    try:
        theme = themes.load_theme(session.theme, getattr(session, "repo_root", None))
    except (FileNotFoundError, KeyError):
        return ""
    return theme.get("digest") or theme.get("md", "")


def _motion_layer(session: Any, artboard_id: str) -> str:
    """The polish pass's motion layer as text — the choreography the critic
    reads the way it reads the element outline. The critic cannot watch
    motion: the capture finishes animations before rastering, so the
    screenshot is the end state and this source is the choreography. Empty
    when the artboard was never polished."""
    html = session.read_html(artboard_id)
    if not html:
        return ""
    return dom.motion_layer(dom.unwrap_editor(html))


class Critic:
    """The vision model as an external critic. Built at session start from the
    'vision' alias; a session with no resolvable alias gets critic=None and
    every critique degrades to a skip note."""

    def __init__(self, client: Any, spec: Any, run_dir: Path | None = None) -> None:
        self.client = client
        self.spec = spec
        self.model = getattr(spec, "spec", str(spec))
        self.run_dir = run_dir

    # ---- the two critiques ----

    def critique_render(self, session: Any, artboard_id: str, png: str,
                        question: str | None = None) -> str:
        """Judge a rendered artboard: screenshot + element outline + rubric in,
        fix-list prose out. Raises on a failed call — the caller (the session's
        auto-critique hook) owns the skip-with-a-note fallback. `question` is
        the user's "look at this" ask, answered first."""
        artboard = session.state.artboards[artboard_id]
        current = artboard.current_version()
        rubric = _render_rubric(
            never=never_list(),
            theme_brief=_theme_brief(session),
            plan=session.state.plan,
            outline=_outline_lines(session, artboard_id),
            motion=_motion_layer(session, artboard_id),
            artboard=f"{artboard.title!r} ({artboard_id} at {artboard.current})",
            question=question or "",
            polished=bool(current is not None and current.kind == "polished"),
        )
        critique = self._complete([
            Message(role="user", content=[
                ContentPart.text_part(rubric),
                ContentPart.image(png),
            ]),
        ])
        self._persist(session, artboard_id, png, critique)
        return critique

    def critique_plan(self, session: Any, plan: str) -> str:
        """Text-only: the plan judged against the never-list before any pixels
        exist. Works headless, where the render critique cannot run at all."""
        critique = self._complete([Message(role="user", content=_plan_rubric(
            never=never_list(),
            theme_brief=_theme_brief(session),
            plan=plan,
        ))])
        self._persist_text(session, critique, kind="plan")
        return critique

    def describe_image(self, path: Path, question: str = "") -> str:
        """One vision call on an image file — the designer's eyes on an
        attachment the user pasted. The screenshots this critic judges are its
        own captures; this one is the user's, so the ask is a faithful
        description to act on, not a critique. Raises on a failed call — the
        tool surfaces that as a ToolError, the same deal design_critique has."""
        mime, _ = detect_image_mime(path)
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        ask = question.strip() or (
            "Describe this image for a designer who cannot see it: what it "
            "shows, its layout, its text, and anything that looks like a "
            "problem worth fixing. Concrete, not generic.")
        return self._complete([
            Message(role="user", content=[
                ContentPart.text_part(ask),
                ContentPart.image(f"data:{mime or 'image/png'};base64,{data}"),
            ]),
        ])

    # ---- plumbing ----

    def _complete(self, messages: list[Message]) -> str:
        """One vision call under the watchdog. No tools: the critic is a
        judge, not an agent — same shape as the read_image sidecar."""
        timer: threading.Timer | None = None
        abort = getattr(self.client, "abort", None)
        fired = threading.Event()

        def expire() -> None:
            # A user interrupt that already landed owns the flag: leave its
            # reason in place so nothing downstream mistakes it for ours.
            if getattr(self.client, "abort_reason", None) == "user":
                return
            fired.set()
            try:
                abort(reason=CRITIC_ABORT_REASON)
            except TypeError:  # a client whose abort() takes no reason
                abort()

        try:
            if abort is not None:
                timer = threading.Timer(CRITIC_TIMEOUT, expire)
                timer.daemon = True
                timer.start()
            resp = self.client.complete(self.spec, messages, tools=None)
        finally:
            if timer is not None:
                timer.cancel()
                self._disarm(fired.is_set())
        return (resp.message.content or "").strip()

    def _disarm(self, fired: bool) -> None:
        """Clear the abort flag our watchdog set, and only ours. Same rule
        as the runner's _TurnWatchdog.disarm: a reason of "user" means a
        real interrupt arrived during the critique, and that one must stay
        stuck so the designer's turn stops as asked."""
        if not fired:
            return
        clear = getattr(self.client, "clear_abort", None)
        if clear is None:
            return
        if getattr(self.client, "abort_reason", CRITIC_ABORT_REASON) == CRITIC_ABORT_REASON:
            clear()

    def _persist(self, session: Any, artboard_id: str, png: str, critique: str) -> None:
        """The screenshot and its critique, filed in the run dir. A convenience
        for the human scrolling the session later — never load-bearing, so a
        decode or disk failure is swallowed."""
        run_dir = session.run_dir
        if run_dir is None or not critique:
            return
        try:
            version = session.state.artboards[artboard_id].current
            d = run_dir / "critiques"
            d.mkdir(parents=True, exist_ok=True)
            stem = f"{artboard_id}-{version}"
            (d / f"{stem}.png").write_bytes(
                base64.b64decode(png.split(",", 1)[-1]))
            (d / f"{stem}.md").write_text(critique, encoding="utf-8")
        except (OSError, ValueError, KeyError):
            pass

    def _persist_text(self, session: Any, critique: str, kind: str) -> None:
        """A text-only critique (the plan), filed like the render ones."""
        run_dir = session.run_dir
        if run_dir is None or not critique:
            return
        try:
            d = run_dir / "critiques"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{kind}.md").write_text(critique, encoding="utf-8")
        except OSError:
            pass


# The theme's tokens supply a font and an accent; the never-list bans some
# of both (Inter, indigo). The theme is the decision the user made, so it is
# judged first and its own tokens are never a fix item — before this the
# critic flagged linear-app's Inter as slop on every Linear render.
THEME_WINS = (
    "The active design system wins: an item on the never-list that the "
    "system's own tokens supply — its font, its accent, its radius — is not a "
    "fix item. The never-list binds where the designer chose for itself."
)

# What a polish pass may spend. Numbers, because "only where it earns its
# place" is not something a critic can check against a screenshot.
MOTION_BUDGET = (
    "Motion budget for a polished artboard — each overrun is a fix item:",
    "1. One page-load choreography, 600 ms or less end to end.",
    "2. At most 6 staggered elements in it.",
    "3. Hover and state transitions between 150 and 250 ms.",
    "4. A prefers-reduced-motion guard that disables the choreography.",
    "5. No scroll-reveal on every section; scroll-driven motion only where "
    "the brief or the theme's motion recipe asks for it.",
)


def _render_rubric(*, never: str, theme_brief: str, plan: str, outline: str,
                   motion: str = "", artboard: str, question: str = "",
                   polished: bool = False) -> str:
    """What the vision model judges a render against. Sections the critic can
    actually check: the theme's tokens and Do-nots first, then the never-list,
    then the plan's promises, the outline that gives every fix a selector,
    and — for a polished artboard — the motion layer as text plus the motion
    budget, since the screenshot can only show the end state."""
    parts = [
        "You are a design critic. You did not make this; judge it. "
        f"The artboard under review is {artboard}.",
        "",
    ]
    if question.strip():
        parts += [
            "The user asked the designer to look at this render and the "
            "designer cannot see it — you can. FIRST answer their question in "
            f"two or three plain sentences: {question.strip()}",
            "Then the fix-list.",
            "",
        ]
    parts += [
        "Reply with a concrete fix-list: one item per problem, each naming the "
        "element by its selector path from the outline and saying what to "
        "change and why. Prose, not scores. If the artboard is genuinely "
        "strong, reply with one line saying so and nothing else.",
        "",
        "Judge against, in order:",
        "1. The active design system — its tokens and its Don'ts, if given. "
        + THEME_WINS,
        "2. The never-list — each of these you can see in the render, and the "
        "system did not supply, is a fix item.",
        "3. The plan the designer submitted, if given — is the build what the "
        "plan promised? Is the Signature element actually there?",
        "",
    ]
    if polished:
        parts += list(MOTION_BUDGET) + [""]
    if theme_brief:
        parts += ["<the-active-design-system>", theme_brief.strip(),
                  "</the-active-design-system>", ""]
    if never:
        parts += ["<the-never-list>", never.strip(), "</the-never-list>", ""]
    if plan:
        parts += ["<the-plan-the-designer-submitted>", plan.strip(),
                  "</the-plan-the-designer-submitted>", ""]
    if motion:
        parts += ["<the-motion-layer>",
                  "The polish pass's motion source, as text. The screenshot "
                  "shows the end state — animations are finished before the "
                  "capture — so judge the choreography here (the never-list's "
                  "motion rules apply to it) and the end state in the image.",
                  motion.strip(), "</the-motion-layer>", ""]
    if outline:
        parts += ["<element-outline-selectors>", outline.strip(),
                  "</element-outline-selectors>", ""]
    return "\n".join(parts)


def _plan_rubric(*, never: str, theme_brief: str, plan: str) -> str:
    """What the vision model judges a plan against: intent, before pixels.

    A plan is judged against the generic defaults — the never-list — because
    at plan time there may be no theme yet to inherit Do-nots from."""
    parts = [
        "You are a design critic. A designer submitted the plan below for a UI "
        "artboard. Judge the plan, before any pixels exist.",
        "",
        "Reply with a short critique: where the plan defaults toward generic "
        "AI output (see the never-list), what is missing — a palette use in "
        "the system's tokens, a type scale, a per-section composition, one "
        "named Signature element — and one or two concrete changes. Not a "
        "score. Judge the active design system first, if given: " + THEME_WINS,
        "",
        "<the-plan>", plan.strip(), "</the-plan>", "",
    ]
    if theme_brief:
        parts += ["<the-active-design-system>", theme_brief.strip(),
                  "</the-active-design-system>", ""]
    if never:
        parts += ["<the-never-list>", never.strip(), "</the-never-list>", ""]
    return "\n".join(parts)