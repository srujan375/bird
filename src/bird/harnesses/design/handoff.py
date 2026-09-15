"""The design handoff bundle — what survives the session.

Modelled on arch/bundle.py: one dict behind one function, plus the writer
that lands it on disk. The lead's seam (ArchitectTool's `ctx.last_bundle`)
stashes the markdown; the builder seeds from it — so the shape mirrors the
arch bundle's: a structured dict whose `markdown` key is the readable doc.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import intake, themes
from .dom import unwrap_editor
from .session import DesignSession
from .state import DesignState

BUNDLE_DIRNAME = "bundle"
BUNDLE_MD = "DESIGN.md"
BUNDLE_TOKENS = "tokens.css"
# how much of a rejected artboard's last critique the builder is shown
OTHER_CRITIQUE_CHARS = 600
# the history lists stop here; the json carries every version
HISTORY_MAX = 40


def bundle_paths(run_dir: Path) -> list[Path]:
    out = run_dir / BUNDLE_DIRNAME
    return [out / "design.json", out / BUNDLE_MD]


def bundle_md_path(run_dir: Path) -> Path:
    """What the lead's seam reads — the same contract as arch's bundle_md_path."""
    return run_dir / BUNDLE_DIRNAME / BUNDLE_MD


def write_bundle(session: DesignSession, run_dir: Path) -> list[Path]:
    bundle = make_handoff_bundle(session, session.state)
    json_path, md_path = bundle_paths(run_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    md_path.write_text(bundle["markdown"], encoding="utf-8")
    written = [json_path, md_path]
    if bundle.get("tokens_css"):
        # the theme's tokens as a file the builder can drop into the project,
        # instead of digging them out of the html's first <style>
        tokens_path = json_path.parent / BUNDLE_TOKENS
        tokens_path.write_text(bundle["tokens_css"], encoding="utf-8")
        written.append(tokens_path)
    return written


def make_handoff_bundle(session: DesignSession, state: DesignState) -> dict[str, Any]:
    """Snapshot the finalized artboard: the edited HTML (the current Version),
    the DESIGN.md content, the generation prompt, the version history (the op
    log, with actors), and a short structured summary. Shaped like the arch
    bundle so the lead's handoff seam can stash it the same way."""
    aid = state.finalized_artboard or state.selected
    artboard = state.artboards.get(aid)
    if artboard is None:
        known = ", ".join(state.artboards) or "none"
        raise ValueError(f"nothing to hand off — no artboard {aid!r} (known: {known})")
    cv = artboard.current_version()
    # the final word on the chosen artboard: the critique of the version being
    # handed off when there is one, else the most recent — the code harness
    # inherits the intent, not just the pixels
    critique = state.latest_critique(aid)
    bundle: dict[str, Any] = {
        "harness": "design",
        "finalized": state.finalized,
        "summary": {
            "artboard": artboard.title,
            "chosen": aid,
            "finalized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # the active design system, or absent when none was ever set
            **({"theme": session.theme} if session.theme else {}),
        },
        "prompt": state.prompt,
        # what the user answered before anything was drawn — the fidelity and
        # the design system are decisions the builder should not re-make
        "intake": _intake(state),
        # the plan the designer submitted before building (design_plan), the
        # critic's verdict on it, and the last vision critique of the chosen
        # artboard — all absent when the loop never ran (no vision model, no page)
        "plan": state.plan,
        "plan_critique": state.plan_critique,
        "critique": critique,
        # the artboards that were NOT chosen, with the last thing the critic
        # said about each: the directions that were tried and set aside
        "others": _others(state, aid),
        "current_version": cv.id if cv else "",
        # hands html OUT of the harness: strip the editor bridge the stored
        # versions carry — the bundle is the clean spec the code harness reads
        "html": unwrap_editor(session.read_html(aid)),
        "versions": [vars(v) for v in artboard.versions],
        "op_log": [e for e in state.op_log if e.get("artboard") == aid],
        "tokens_css": _tokens_css(session),
    }
    bundle["markdown"] = _markdown(bundle)
    return bundle


def _intake(state: DesignState) -> dict[str, str]:
    queue = state.intake
    if not queue.asks:
        return {}
    out = {"direction": intake.direction(queue),
           "design_system": intake.design_system(queue) or "designer's choice"}
    for ask in queue.answered():
        out[ask.id] = ask.answer_label() or ask.answer
    return out


def _others(state: DesignState, chosen: str) -> list[dict[str, Any]]:
    out = []
    for aid, a in state.artboards.items():
        if aid == chosen:
            continue
        found = state.latest_critique_version(aid)
        out.append({
            "artboard": aid, "title": a.title, "current": a.current,
            "critique": (found[1].get("critique") or "") if found else "",
        })
    return out


def _tokens_css(session: DesignSession) -> str:
    if not session.theme:
        return ""
    try:
        return themes.load_theme(session.theme, getattr(session, "repo_root", None))["css"]
    except FileNotFoundError:
        return ""


def _markdown(bundle: dict[str, Any]) -> str:
    s = bundle["summary"]
    lines = [f"# Design — {s['artboard']}", ""]
    lines.append(f"- **chosen artboard:** `{s['chosen']}` (at {bundle['current_version']})")
    lines.append(f"- **finalized:** {s['finalized_at']}")
    if s.get("theme"):
        lines.append(f"- **design system:** `{s['theme']}`")
    polished = [v for v in bundle["versions"] if v.get("kind") == "polished"]
    if polished:
        # the builder inherits transitions, not static pixels: the html block
        # below is the animated current version, motion nodes included
        lines.append(
            f"- **polish:** the showcase pass animated this artboard "
            f"({len(polished)} polish version{'s' if len(polished) != 1 else ''})")
    if bundle["prompt"]:
        lines += ["", "## The prompt", "", bundle["prompt"], ""]
    intake_block = bundle.get("intake") or {}
    if intake_block:
        lines += ["", "## Intake", ""]
        lines.append(f"- **direction:** {intake_block.get('direction', '')}")
        lines.append(f"- **design system:** {intake_block.get('design_system', '')}")
        lines.append("")
    if bundle.get("tokens_css"):
        lines.append(f"- **tokens:** `{BUNDLE_TOKENS}` beside this file holds the theme's "
                     "CSS custom properties; the html below already includes them")
    if bundle.get("plan"):
        lines += ["", "## The plan", "", str(bundle["plan"]).rstrip(), ""]
        if bundle.get("plan_critique"):
            lines += ["", "### The critic on the plan", "",
                      str(bundle["plan_critique"]).rstrip(), ""]
    critique = bundle.get("critique")
    if critique and critique.get("critique"):
        # the last eyes on the artboard before it was handed off: what a
        # second model flagged, so the builder knows what was already judged
        meta = ", ".join(x for x in (critique.get("model"), critique.get("at")) if x)
        lines += ["", f"## The last critique{f' ({meta})' if meta else ''}", "",
                  str(critique["critique"]).rstrip(), ""]
    lines += _others_section(bundle)
    lines += _history_section(bundle)
    lines += ["", "## The artboard", "", "```html", bundle["html"].rstrip(), "```", ""]
    return "\n".join(lines)


def _others_section(bundle: dict[str, Any]) -> list[str]:
    others = bundle.get("others") or []
    if not others:
        return []
    lines = ["", "## The other directions", "",
             "Artboards that were tried and not chosen, with the critic's last word on each.", ""]
    for o in others:
        lines.append(f"- **{o['title']}** (`{o['artboard']}` at {o.get('current') or '?'})")
        note = " ".join(str(o.get("critique") or "").split())
        if note:
            if len(note) > OTHER_CRITIQUE_CHARS:
                note = note[:OTHER_CRITIQUE_CHARS].rstrip() + "…"
            lines.append(f"  {note}")
    lines.append("")
    return lines


def _history_section(bundle: dict[str, Any]) -> list[str]:
    """The version notes, grouped by who made them — what the designer did
    and what the user changed by hand. Notes, not op names with numeric
    paths: `set_style 0>1>3` means nothing once the document has moved on,
    while "user: set_text" on v4 still says a person touched the copy."""
    versions = bundle.get("versions") or []
    if len(versions) < 2:
        return []
    designer: list[str] = []
    user: list[str] = []
    for v in versions:
        note = str(v.get("note") or v.get("kind") or "")
        if note.startswith("user:"):
            user.append(f"- `{v.get('id', '?')}` {note[5:].strip()}")
        else:
            designer.append(f"- `{v.get('id', '?')}` {note[3:].strip() if note.startswith('ai:') else note}")
    lines = ["", "## History", ""]
    for title, rows in (("The designer", designer), ("The user, by hand", user)):
        if not rows:
            continue
        lines += [f"### {title}", ""]
        lines += rows[:HISTORY_MAX]
        if len(rows) > HISTORY_MAX:
            lines.append(f"- … {len(rows) - HISTORY_MAX} more in design.json")
        lines.append("")
    return lines