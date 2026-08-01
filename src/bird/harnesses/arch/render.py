"""Derived projections: pure functions ArchState → mermaid sources + tracker.

No model involvement, no I/O. These ARE the UI (and the bundle's diagrams):
the browser page draws its own SVG from the structured state, but the
mermaid sources ship in the arch_state payload, the handoff bundle, and the
tests that pin rendering behavior.
"""

from __future__ import annotations

import re
from typing import Any

from .state import ArchState, Component, Flow

TRACKER_PREFIX = "[arch tracker"

# most expensive-to-change first — the expand queue's ordering
RISK_ORDER = {"store": 0, "api": 1, "queue": 2, "llm": 3, "infra": 4, "service": 5}

_NODE_SHAPES = {
    # kind → (open, close) mermaid brackets
    "store": ('[("', '")]'),
    "cache": ('[("', '")]'),
    "queue": ('[["', '"]]'),
    "external": ('(("', '"))'),
}
_EDGE_ARROWS = {"sync": "-->", "async": "-.->", "batch": "==>"}


def _label(text: str) -> str:
    # str() guard: render must never hard-crash on a stray non-string that
    # slipped past tool validation (the tool layer is the real gate)
    return str(text).replace('"', "'")


def _ident(text: str) -> str:
    """A mermaid-safe identifier (ER entities, module nodes)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(text)).strip("_") or "X"


def toplevel_mermaid(state: ArchState) -> str:
    """The system flowchart: components + connections. In feature mode,
    existing (brownfield) components are styled as frozen background."""
    lines = ["flowchart TD"]
    for comp in state.components.values():
        open_b, close_b = _NODE_SHAPES.get(comp.kind, ('["', '"]'))
        lines.append(f"  {comp.id}{open_b}{_label(comp.name)}<br/><i>{comp.kind}</i>{close_b}")
    for conn in state.connections:
        arrow = _EDGE_ARROWS.get(conn.kind, "-->")
        label = conn.label
        if conn.kind == "async" and conn.mechanism:
            label = f"{label} via {conn.mechanism}"
        lines.append(f'  {conn.src} {arrow}|"{_label(label)}"| {conn.dst}')
    existing = [c.id for c in state.components.values() if c.existing]
    if existing:
        lines.append("  classDef existing fill:#f5f5f5,stroke:#949494,color:#949494")
        lines.append(f"  class {','.join(existing)} existing")
    return "\n".join(lines)


def sketch_mermaid(variant: Any) -> str:
    """A loose variant as a diagram. The sketch layer is a first-class view now,
    not a private scratchpad, so it renders wherever the strict graph does —
    including while nothing has been promoted at all."""
    lines = ["flowchart LR"]
    for node in variant.nodes.values():
        ident = _ident(node.id)
        kind = f"<br/><i>{_label(node.kind)}</i>" if node.kind and node.kind != "component" else ""
        lines.append(f'  {ident}["{_label(node.label or node.id)}{kind}"]')
    for ln in variant.links:
        arrow = _EDGE_ARROWS.get(ln.kind, "-->")
        label = f'|"{_label(ln.label)}"|' if ln.label else ""
        lines.append(f"  {_ident(ln.src)} {arrow}{label} {_ident(ln.dst)}")
    return "\n".join(lines)


def flow_mermaid(flow: Flow) -> str:
    lines = ["sequenceDiagram"]
    seen: list[str] = []
    for step in flow.steps:
        for ref in (step.src, step.dst):
            if ref not in seen:
                seen.append(ref)
                lines.append(f"  participant {ref}")
    for step in flow.steps:
        lines.append(f"  {step.src}->>{step.dst}: {_label(step.action)}")
        if step.note:
            lines.append(f"  note over {step.dst}: {_label(step.note)}")
    return "\n".join(lines)


def facet_mermaid(comp: Component) -> str | None:
    """Diagram-natural facets only: store → ER, infra → deployment view,
    service with modules → module graph. API/queue/llm depth is tabular
    (rendered from state by the page and the bundle) — no forced diagrams."""
    facet = comp.facet
    if facet is None:
        return None
    kind = facet.facet_kind
    if kind == "store" and facet.entities:
        lines = ["erDiagram"]
        for ent in facet.entities:
            lines.append(f"  {_ident(ent.name).upper()} {{")
            for f in ent.fields:
                lines.append(f"    string {_ident(f)}")
            lines.append("  }")
        return "\n".join(lines)
    if kind == "infra" and facet.units:
        lines = ["flowchart TD"]
        for unit in facet.units:
            uid = _ident(unit.name)
            lines.append(f'  subgraph {uid}["{_label(unit.name)} ({_label(unit.scaling_policy)})"]')
            for cid in unit.components:
                lines.append(f"    {uid}_{_ident(cid)}[{cid}]")
            lines.append("  end")
        return "\n".join(lines)
    if kind == "service" and facet.modules:
        lines = ["flowchart TD", f'  subgraph {comp.id}["{_label(comp.name)}"]']
        for mod in facet.modules:
            lines.append(f'    {comp.id}_{_ident(mod.name)}["{_label(mod.name)}: {_label(mod.purpose)}"]')
        lines.append("  end")
        return "\n".join(lines)
    return None


def render_all(state: ArchState) -> dict[str, Any]:
    """The `renders` block of the arch_state event."""
    facets: dict[str, Any] = {}
    for comp in state.components.values():
        if comp.facet is None:
            continue
        entry: dict[str, Any] = {"kind": comp.facet.facet_kind}
        mermaid = facet_mermaid(comp)
        if mermaid:
            entry["mermaid"] = mermaid
        facets[comp.id] = entry
    return {
        "toplevel": toplevel_mermaid(state),
        "flows": {f.id: flow_mermaid(f) for f in state.flows},
        "facets": facets,
        # every live variant, so the page has something to draw before (and
        # after) anything is promoted
        "sketches": {
            v.id: sketch_mermaid(v) for v in state.sketchbook.variants.values() if v.nodes
        },
        "active_sketch": state.sketchbook.active,
    }


def risk_ordered_pending(state: ArchState) -> list:
    """Pending obligations, most expensive-to-change first."""
    return sorted(
        state.pending_obligations(),
        key=lambda o: (RISK_ORDER.get(o.facet, 9), o.component_id),
    )


def _next_hint(state: ArchState) -> str:
    """A suggestion, not an instruction. Nothing here is enforced anywhere —
    the model is free to ignore it and talk to the user instead."""
    if state.phase == "finalized":
        return "session complete."
    if state.phase == "toplevel_review":
        return "awaiting the user's ruling on the top level."

    blockers = state.open_blockers()
    if blockers:
        b = blockers[0]
        return (
            f"{b.id} is an open blocker against {b.target} — resolve it (design change, "
            "or overruled with the reason) or put it to the user."
        )
    v = state.sketchbook.active_variant()
    if not state.components:
        if v is None or not v.nodes:
            return (
                "sketch a rough shape from what they asked for — a diagram they can "
                "react to beats a form they have to fill. Rival takes are better than one."
            )
        return (
            f"'{v.name}' has {len(v.nodes)} box(es): argue it through with the user, spin "
            "up a rival, or `promote` it once you've landed on a shape."
        )
    if state.phase in ("expand", "resolved"):
        queue = risk_ordered_pending(state)
        if queue:
            head = queue[0]
            return (
                f'expand("{head.component_id}") is the one that would hurt most to get '
                f"wrong — {head.reason}."
            )
        return "the design is covered — `done` when you want the user to finalize it."
    thin = state.toplevel_missing()
    if thin:
        return (
            "still loose: " + "; ".join(thin) + ". Tighten what matters, say why the rest "
            "doesn't, then `done` for the user's sign-off."
        )
    return "the top level holds together — `done` when you want the user's sign-off."


GAPS_SHOWN = 5
CONCERNS_SHOWN = 5


def tracker(state: ArchState) -> str:
    """The pinned tracker — re-rendered into the conversation every turn.

    Both layers are always shown, because both are always live: the sketchbook
    doesn't disappear when a shape is promoted. Gaps and concerns are reported,
    never demanded — the model decides which are worth acting on."""
    lines = [f"{TRACKER_PREFIX} — pinned; phase: {state.phase}]"]

    book = state.sketchbook
    if book.variants:
        parts = []
        for v in book.variants.values():
            tag = ""
            if v.status == "chosen":
                tag = " ✓chosen"
            elif v.status == "archived":
                tag = " (archived)"
            elif v.id == book.active:
                tag = " ◀ active"
            parts.append(f"{v.name} [{len(v.nodes)}n/{len(v.links)}e]{tag}")
        lines.append("sketch: " + " · ".join(parts))

    if state.components:
        happy = len(state.happy_flows())
        lines.append(
            f"design: {len(state.components)} components · {len(state.connections)} connections · "
            f"{len(state.flows)} flows ({happy} happy) · {len(state.decisions)} decisions"
        )
    else:
        lines.append("design: nothing promoted yet")
    lines.append(f"brief: scope={state.brief.scope or '?'} · goal={'set' if state.brief.goal else '?'}")

    concerns = state.open_concerns()
    if concerns:
        shown = concerns[:CONCERNS_SHOWN]
        lines.append("open concerns:")
        lines += [f"  - {c.id} [{c.severity}] {c.target}: {c.claim}" for c in shown]
        if len(concerns) > len(shown):
            lines.append(f"  … {len(concerns) - len(shown)} more")

    gaps = state.gaps()
    if gaps:
        lines.append(f"thin ({len(gaps)}, none required): " + "; ".join(gaps[:GAPS_SHOWN]))

    queue = risk_ordered_pending(state)
    if queue:
        lines.append("worth depth (risk order): " + ", ".join(
            f"{o.component_id}({o.facet})" for o in queue))
    blocking = state.blocking_questions()
    if blocking:
        lines.append("unanswered questions you asked: " + "; ".join(
            f"{q.id}: {q.question[:60]}" for q in blocking))

    lines.append(f"next: {_next_hint(state)}")
    return "\n".join(lines)
