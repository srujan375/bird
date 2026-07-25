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
    }


def risk_ordered_pending(state: ArchState) -> list:
    """Pending obligations, most expensive-to-change first."""
    return sorted(
        state.pending_obligations(),
        key=lambda o: (RISK_ORDER.get(o.facet, 9), o.component_id),
    )


def _next_hint(state: ArchState) -> str:
    if state.phase == "brainstorm":
        v = state.sketchbook.active_variant()
        if v is None or not v.nodes:
            return (
                "sketch a rough shape from the requirements — `variant` to name an idea, "
                "then `node`/`link`. Offer the user a couple of rival shapes to react to."
            )
        missing = state.brief.missing()
        tail = f" (brief still needs: {', '.join(missing)})" if missing else ""
        return (
            f"brainstorming '{v.name}' ({len(v.nodes)} node(s)): deepen/collapse/splice "
            f"freely, spin up rival variants, talk it through. `promote` when you and the "
            f"user land on one{tail}."
        )
    if state.phase == "intake":
        missing = state.brief.missing()
        if missing:
            return (
                f"brief still needs: {', '.join(missing)} — call `brief`; ask the user "
                "for load-bearing facts you don't have (do not assume)."
            )
        return "brief complete — start proposing components."
    if state.phase == "propose":
        missing = state.toplevel_missing()
        if missing:
            return "top level still owes: " + "; ".join(missing) + "."
        return "top level looks complete — call `done` to request the user's approval."
    if state.phase == "toplevel_review":
        return "awaiting user approval of the top level."
    if state.phase == "expand":
        queue = risk_ordered_pending(state)
        if queue:
            head = queue[0]
            return f'expand("{head.component_id}") next — {head.reason}.'
        return "all obligations closed — call `done` to run the challenge pass."
    if state.phase == "challenge":
        open_qs = [q for q in state.questions if q.open]
        if open_qs:
            return "address the challenge findings (answer / ask the user / amend), then call `done`."
        return "challenge clean — call `done` to request Finalize."
    if state.phase == "resolved":
        return "call `done` to request Finalize."
    return "session complete."


def tracker(state: ArchState) -> str:
    """The pinned tracker — re-rendered into the conversation every turn."""
    if state.phase == "brainstorm":
        return _brainstorm_tracker(state)
    happy = len(state.happy_flows())
    lines = [
        f"{TRACKER_PREFIX} — pinned; phase: {state.phase}]",
        (
            f"brief: scope={state.brief.scope or '?'} · "
            f"components: {len(state.components)} · connections: {len(state.connections)} · "
            f"flows: {len(state.flows)} ({happy} happy) · decisions: {len(state.decisions)}"
        ),
    ]
    queue = risk_ordered_pending(state)
    if queue:
        items = ", ".join(f"{o.component_id}({o.facet})" for o in queue)
        lines.append(f"obligations pending (risk order): {items}")
    blocking = state.blocking_questions()
    if blocking:
        items = "; ".join(f"{q.id}: {q.question[:60]}" for q in blocking)
        lines.append(f"BLOCKING questions open: {items}")
    lines.append(f"next: {_next_hint(state)}")
    return "\n".join(lines)


def _brainstorm_tracker(state: ArchState) -> str:
    book = state.sketchbook
    lines = [f"{TRACKER_PREFIX} — pinned; phase: brainstorm]"]
    if book.variants:
        parts = []
        for v in book.variants.values():
            tag = " ◀ active" if v.id == book.active else ""
            if v.status == "archived":
                tag = " (archived)"
            parts.append(f"{v.name} [{len(v.nodes)}n/{len(v.links)}e]{tag}")
        lines.append("variants: " + " · ".join(parts))
    else:
        lines.append("variants: none yet")
    lines.append(f"brief: scope={state.brief.scope or '?'} · goal={'set' if state.brief.goal else '?'}")
    lines.append(f"next: {_next_hint(state)}")
    return "\n".join(lines)
