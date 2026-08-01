"""The KG seed — the architecture, as something the code harness can query.

`architecture.md` is the seam between arch and code: the builder reads it as a
system prompt. That works for "what am I building"; it does not work for "what
writes to the orders table", asked eleven turns later about a design that has
scrolled out of attention. `kg_query` answers that shape of question — but on
greenfield there is nothing to extract, so it answers nothing at all.

So finalize also writes the design into the graph: a node per component, per
entity, endpoint, message, module, task and deploy unit, edged by the real
connections and flows. Turn one of `bird code` can then ask the graph about a
system that does not exist yet.

Two rules the node shapes follow:

- **Labels carry the words someone would search for.** `query()` matches on
  labels alone, so a component's label is its name *and* its responsibility, and
  an entity's is its name *and* its fields. A bare id retrieves nothing.
- **Every node says where it came from.** `source_file` points at the bundle and
  `source_location` reads `design:<component-id>` — which is what `kg_query`
  prints beside a hit, so it lands as "the architecture said this", never as
  discovered code.
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
    cid_node = {cid: f"arch_{_slug(cid)}" for cid in state.components}

    for comp in state.components.values():
        nid = cid_node[comp.id]
        label = comp.name
        if comp.responsibility:
            label = f"{comp.name} — {comp.responsibility}"
        nodes.append(_node(
            nid, label, f"component:{comp.kind}", source,
            source_location=f"design:{comp.id}",
            tech=comp.tech or "",
            existing=comp.existing,
        ))
        if comp.data_owned:
            owned = f"{nid}_owns"
            nodes.append(_node(owned, f"{comp.name} owns {comp.data_owned}", "data", source,
                               source_location=f"design:{comp.id}"))
            edges.append(_edge(nid, owned, "owns_data", source))
        nodes.extend(_facet_nodes(comp, nid, source))
        edges.extend(_facet_edges(comp, nid, source))

    for conn in state.connections:
        src, dst = cid_node.get(conn.src), cid_node.get(conn.dst)
        if not src or not dst:
            continue
        relation = _slug(conn.label) or conn.kind
        edges.append(_edge(src, dst, relation, source,
                           context=f"{conn.kind}" + (f" via {conn.mechanism}" if conn.mechanism else "")))

    for flow in state.flows:
        fid = f"arch_flow_{_slug(flow.id)}"
        steps = " → ".join(f"{s.src} {s.action}" for s in flow.steps)
        nodes.append(_node(fid, f"{flow.name} ({flow.kind} flow): {steps}", "flow", source,
                           source_location=f"design:{flow.id}"))
        for step in flow.steps:
            for ref in (step.src, step.dst):
                target = cid_node.get(ref)
                if target:
                    edges.append(_edge(fid, target, "step_in_flow", source, context=step.action))
    return nodes, edges


def _facet_nodes(comp: Any, parent: str, source: str) -> list[dict[str, Any]]:
    """One node per thing a builder would look up by name."""
    facet = comp.facet
    if facet is None:
        return []
    out: list[dict[str, Any]] = []
    kind = facet.facet_kind
    if kind == "store":
        for ent in facet.entities:
            fields = ", ".join(ent.fields)
            out.append(_node(
                f"{parent}_entity_{_slug(ent.name)}",
                f"{ent.name} (entity in {comp.name}): keys {ent.keys}" + (f"; {fields}" if fields else ""),
                "entity", source, source_location=f"design:{comp.id}",
            ))
    elif kind == "api":
        for e in facet.endpoints:
            out.append(_node(
                f"{parent}_endpoint_{_slug(e.method)}_{_slug(e.route)}",
                f"{e.method} {e.route} on {comp.name}: {e.request} → {e.response} (auth: {e.auth})",
                "endpoint", source, source_location=f"design:{comp.id}",
            ))
    elif kind == "queue":
        for m in facet.messages:
            out.append(_node(
                f"{parent}_message_{_slug(m.name)}",
                f"{m.name} (message on {comp.name}): {m.schema}; {m.delivery}, ordering {m.ordering}",
                "message", source, source_location=f"design:{comp.id}",
            ))
    elif kind == "service":
        for mod in facet.modules or []:
            out.append(_node(
                f"{parent}_module_{_slug(mod.name)}",
                f"{mod.name} (module in {comp.name}): {mod.purpose}",
                "module", source, source_location=f"design:{comp.id}",
            ))
    elif kind == "llm":
        for t in facet.tasks:
            out.append(_node(
                f"{parent}_task_{_slug(t.name)}",
                f"{t.name} (llm task in {comp.name}): {t.prompt_contract}; fallback {t.fallback}",
                "llm_task", source, source_location=f"design:{comp.id}",
            ))
    elif kind == "infra":
        for u in facet.units:
            out.append(_node(
                f"{parent}_unit_{_slug(u.name)}",
                f"{u.name} (deploy unit): hosts {', '.join(u.components)}; {u.scaling_policy}",
                "deploy_unit", source, source_location=f"design:{comp.id}",
            ))
    return out


_FACET_RELATION = {
    "store": "defines_entity",
    "api": "exposes_endpoint",
    "queue": "carries_message",
    "service": "has_module",
    "llm": "runs_task",
    "infra": "deploys",
}


def _facet_edges(comp: Any, parent: str, source: str) -> list[dict[str, Any]]:
    if comp.facet is None:
        return []
    relation = _FACET_RELATION.get(comp.facet.facet_kind, "contains")
    return [
        _edge(parent, child["id"], relation, source)
        for child in _facet_nodes(comp, parent, source)
    ]


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
