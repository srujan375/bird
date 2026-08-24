"""The KG seed — the architecture, as something the code harness can query.

`architecture.md` is the seam between arch and code: the builder reads it as a
system prompt. That works for "what am I building"; it does not work for "what
writes to the orders table", asked eleven turns later about a design that has
scrolled out of attention. `kg_query` answers that shape of question — but on
greenfield there is nothing to extract, so it answers nothing at all.

So handoff also writes the design into the graph: a node per box, plus a node
per decision and per approach that lost, edged by the real connections. Turn one
of `bird code` can then ask the graph about a system that does not exist yet —
including the two questions a builder asks most, "why is it like this" and "why
not the other way".

Two rules the node shapes follow:

- **Labels carry the words someone would search for.** `query()` matches on
  labels alone, so a box's label is its name *and* its responsibility, and a
  decision's is the topic, the choice and the reason. A bare id retrieves
  nothing.
- **Every node says where it came from.** `source_file` points at the bundle and
  `source_location` reads `design:<id>` — which is what `kg_query` prints beside
  a hit, so it lands as "the architecture said this", never as discovered code.
"""

from __future__ import annotations

import re
from typing import Any

from .state import ArchState

ORIGIN = "arch"  # `_origin` on every node, so seeded nodes stay identifiable


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_") or "x"


def _node(nid: str, label: str, ntype: str, source: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": nid,
        "label": label,
        "type": ntype,
        "file_type": "design",
        "source_file": source,
        "_origin": ORIGIN,
        **extra,
    }


def _edge(src: str, dst: str, relation: str, source: str, context: str = "") -> dict[str, Any]:
    return {
        "source": src,
        "target": dst,
        "relation": relation,
        "context": context,
        "confidence": "DESIGN",
        "source_file": source,
        "weight": 1.0,
    }


def build_seed(state: ArchState, source: str = "bundle/architecture.md") -> tuple[list, list]:
    """(nodes, edges) for `KG.seed`. Pure — no I/O, no graph library."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_id = {nid: f"arch_{_slug(nid)}" for nid in state.nodes}

    for box in state.nodes.values():
        # a box that lost with its approach is still seeded, marked as such:
        # "we considered this and dropped it" is a real answer to a query
        greyed = state.is_greyed(box)
        label = box.label
        if box.responsibility:
            label = f"{box.label} — {box.responsibility}"
        if box.tech:
            label += f" (on {box.tech})"
        if greyed:
            label = f"[not taken] {label}"
        nodes.append(_node(
            node_id[box.id], label, f"component:{box.kind}", source,
            source_location=f"design:{box.id}",
            tech=box.tech,
            existing=box.existing,
            greyed=greyed,
        ))
        if box.detail:
            did = f"{node_id[box.id]}_detail"
            nodes.append(_node(
                did, f"Inside {box.label}: {box.detail}", "detail", source,
                source_location=f"design:{box.id}",
            ))
            edges.append(_edge(node_id[box.id], did, "detailed_as", source))

    for edge in state.edges:
        src, dst = node_id.get(edge.src), node_id.get(edge.dst)
        if not src or not dst:
            continue
        relation = _slug(edge.label) or edge.kind
        context = edge.kind + (f" — {edge.notes}" if edge.notes else "")
        edges.append(_edge(src, dst, relation, source, context=context))

    for dec in state.decisions:
        label = f"Decision: {dec.topic} → {dec.choice}"
        if dec.rationale:
            label += f" — {dec.rationale}"
        if dec.pragmatism_note:
            label += f" (deliberately good enough: {dec.pragmatism_note})"
        rivals = [o.name for o in dec.options if o.name != dec.choice]
        if rivals:
            label += f" [not: {', '.join(rivals)}]"
        nodes.append(_node(
            f"arch_decision_{_slug(dec.id)}", label, "decision", source,
            source_location=f"design:{dec.id}", by=dec.source,
        ))

    for app in state.greyed_approaches():
        label = f"Approach not taken: {app.name}"
        if app.summary:
            label += f" — {app.summary}"
        label += f". Why not: {app.rejected_reason}"
        aid = f"arch_approach_{_slug(app.id)}"
        nodes.append(_node(aid, label, "approach", source, source_location=f"design:{app.id}"))
        for box in state.nodes_in(app.id):
            target = node_id.get(box.id)
            if target:
                edges.append(_edge(aid, target, "would_have_used", source))

    return nodes, edges


def seed_kg(kg: Any, state: ArchState, source: str = "bundle/architecture.md") -> str:
    """Seed `kg` from `state`, returning a one-line report.

    Never raises: a knowledge graph that will not take the seed is a worse
    `bird code` session, not a failed architecture. The design is already on
    disk by the time this runs.
    """
    if kg is None:
        return ""
    nodes, edges = build_seed(state, source)
    if not nodes:
        return ""
    try:
        stats = kg.seed(nodes, edges)
    except Exception as e:
        return f"knowledge graph not seeded ({type(e).__name__}: {e}); the bundle is unaffected"
    return f"seeded the knowledge graph with {len(nodes)} design nodes (graph now {stats.nodes})"
