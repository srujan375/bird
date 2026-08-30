"""The handoff bundle — what survives the session.

Two artifacts behind one function: architecture.json (the whole board, for a
tool) and architecture.md (for a person, and for the code harness's seed
context).

The markdown's centre of gravity moved with the rebuild. It used to lead with
contract sheets — the exhaustive output of a form-filling session. It now leads
with **why**: the decisions and their rationale, then the approaches that lost
and the reason they lost. That is what someone actually needs six months later,
and it is the half a design doc usually throws away.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import derive, render
from .state import ArchState, Node

BUNDLE_DIRNAME = "bundle"


def bundle_paths(run_dir: Path) -> list[Path]:
    out = run_dir / BUNDLE_DIRNAME
    return [out / "architecture.json", out / "architecture.md"]


def write_bundle(state: ArchState, run_dir: Path) -> list[Path]:
    json_path, md_path = bundle_paths(run_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    md_path.write_text(_markdown(state), encoding="utf-8")
    return [json_path, md_path]


def _markdown(state: ArchState) -> str:
    b = state.brief
    lines = [f"# Architecture — {b.goal or 'untitled'}", ""]
    if b.actors:
        lines.append(f"- **for:** {', '.join(b.actors)}")
    if b.scale:
        lines.append(f"- **scale:** {b.scale}")
    if b.constraints:
        lines.append(f"- **constraints:** {'; '.join(b.constraints)}")
    if b.non_goals:
        lines.append(f"- **non-goals:** {'; '.join(b.non_goals)}")
    lines += ["", "## The board", "", "```mermaid", render.board_mermaid(state), "```", ""]

    lines += _decisions_section(state)
    lines += _not_taken_section(state)
    lines += _components_section(state)
    lines += _notes_section(state)
    lines += _open_section(state)
    lines += _noticing_section(state)
    return "\n".join(lines)


def _decisions_section(state: ArchState) -> list[str]:
    """Why the design is the shape it is. First, because it is what gets read."""
    if not state.decisions:
        return []
    lines = ["## Decisions", ""]
    for d in state.decisions:
        who = " *(the user's call)*" if d.source == "user" else ""
        lines.append(f"### {d.topic} → {d.choice}{who}")
        lines.append("")
        if d.rationale:
            lines.append(d.rationale)
            lines.append("")
        rivals = [o for o in d.options if o.name != d.choice]
        if rivals:
            lines.append("Weighed against:")
            for o in rivals:
                detail = ""
                if o.pros:
                    detail += f" pros: {'; '.join(o.pros)}."
                if o.cons:
                    detail += f" cons: {'; '.join(o.cons)}."
                lines.append(f"- **{o.name}**{detail}")
            lines.append("")
        if d.pragmatism_note:
            # not a caveat on the decision — part of it. Someone reading this
            # later needs to know the tradeoff was seen and taken deliberately,
            # or they will "fix" it.
            lines.append(f"> **Deliberately good enough.** {d.pragmatism_note}")
            lines.append("")
    return lines


def _not_taken_section(state: ArchState) -> list[str]:
    """The shapes that lost, and why. The single most common question a builder
    asks later is "why not X" — this is the answer, and it costs nothing to
    have kept."""
    greyed = state.greyed_approaches()
    if not greyed:
        return []
    lines = ["## Approaches not taken", ""]
    for a in greyed:
        lines.append(f"### {a.name}")
        lines.append("")
        if a.summary:
            lines.append(a.summary)
            lines.append("")
        lines.append(f"**Not taken:** {a.rejected_reason}")
        boxes = [n.label or n.id for n in state.nodes_in(a.id)]
        if boxes:
            lines.append("")
            lines.append(f"*Shape:* {', '.join(boxes)}")
        lines.append("")
    return lines


def _components_section(state: ArchState) -> list[str]:
    live = [
        n for n in state.nodes.values()
        if not state.is_greyed(n) and not n.existing
    ]
    background = [n for n in state.nodes.values() if n.existing]
    if not live and not background:
        return []
    lines = ["## Components", ""]
    for node in sorted(live, key=lambda n: n.id):
        lines += _sheet(state, node)
    if background:
        lines += ["### Existing (background, not designed here)", ""]
        for node in sorted(background, key=lambda n: n.id):
            lines.append(f"- **{node.label}** (`{node.id}`, {node.kind})")
        lines.append("")
    return lines


def _sheet(state: ArchState, node: Node) -> list[str]:
    head = f"### {node.label} (`{node.id}`, {node.kind})"
    if node.approaches:
        head += f" — {', '.join(node.approaches)}"
    lines = [head, ""]
    if node.responsibility:
        lines += [node.responsibility, ""]
    elif not node.tech and not node.detail and not node.notes:
        lines.append("*Not elaborated — a box on the board, nothing said about what is inside.*")
        lines.append("")
    if node.tech:
        lines.append(f"- **built on:** {node.tech}")
    for k, v in node.facts.items():
        lines.append(f"- **{k}:** {v}")
    if node.items:
        from .state import KIND_LIST
        lines.append(f"- **{KIND_LIST.get(node.kind, ('items', ''))[0]}:**")
        for it in node.items:
            head = f"`{it.k}` {it.v}" if it.k else it.v
            lines.append(f"  - {head}" + (f" — {it.d}" if it.d else ""))
    if node.notes:
        lines.append(f"- **notes:** {node.notes}")
    out = [e for e in state.edges if e.src == node.id]
    for e in out:
        via = f" ({e.kind})" if e.kind != "sync" else ""
        tail = f" — {e.notes}" if e.notes else ""
        lines.append(f"- **→ {e.dst}**{via}: {e.label or 'talks to'}{tail}")
    for note in state.notes_on(node.id):
        lines.append(f"- **note:** {note.text}")
    if node.detail:
        lines += ["", node.detail]
    lines.append("")
    return lines


def _notes_section(state: ArchState) -> list[str]:
    """Notes left on the canvas itself rather than on a box. Ones pinned to a
    box already travel with it, in its own sheet."""
    loose = [a for a in state.annotations if not a.anchor]
    if not loose:
        return []
    return ["## Notes on the board", "", *[f"- {a.text}" for a in loose], ""]


def _open_section(state: ArchState) -> list[str]:
    """What is still undecided, with the recommendation it was parked with —
    so the builder inherits a starting position, not just a hole."""
    still_open = [q for q in state.questions if q.status != "answered"]
    if not still_open:
        return []
    lines = ["## Still open", ""]
    for q in still_open:
        lines.append(f"- **{q.question}**")
        if q.recommendation:
            lines.append(f"  - suggested: {q.recommendation}")
        if q.status == "deferred":
            lines.append("  - deferred deliberately")
    lines.append("")
    return lines


def _noticing_section(state: ArchState) -> list[str]:
    """What the design does not say, computed at handoff. Not failures — the
    things that were left to the build, listed so they are decided on purpose
    rather than by accident."""
    thin = derive.coverage(state)
    if not thin:
        return []
    return [
        "## Left to the build",
        "",
        "Unspecified at handoff. Decide these while building, and say so if a "
        "choice contradicts the design above.",
        "",
        *[f"- {t}" for t in thin],
        "",
    ]
