"""The handoff bundle — SWAP POINT.

The real bundle schema is deliberately not final (parked; will be specified
separately). Until then finalize writes two artifacts behind this one
function: architecture.json (the full ArchState) and architecture.md (a
generated top-level doc + per-component contract sheets + decision log).
Replace write_bundle when the schema lands; nothing else may know the
bundle's shape.

The bundle's other half is the KG seed (`kg_seed.py`), written at the same
moment: the markdown is what the next session *reads*, the graph is what it
can *ask*.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import render
from .state import ArchState, Component

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
    lines = [
        f"# Architecture — {b.goal or 'untitled'}",
        "",
        f"- **scope:** {b.scope}  ·  **actors:** {', '.join(b.actors)}",
    ]
    if b.constraints:
        lines.append(f"- **constraints:** {'; '.join(b.constraints)}")
    if b.non_goals:
        lines.append(f"- **non-goals:** {'; '.join(b.non_goals)}")
    lines += [
        "",
        "## System diagram",
        "",
        "```mermaid",
        render.toplevel_mermaid(state),
        "```",
        "",
        "## Components",
        "",
    ]
    for comp in state.components.values():
        lines.extend(_contract_sheet(comp))
    if state.flows:
        lines.append("## Flows")
        lines.append("")
        for flow in state.flows:
            lines += [f"### {flow.name} ({flow.kind})", "", "```mermaid",
                      render.flow_mermaid(flow), "```", ""]
    if state.decisions:
        lines.append("## Decision log")
        lines.append("")
        for d in state.decisions:
            lines.append(f"- **{d.topic}** → {d.choice} ({d.status}): {d.rationale}")
        lines.append("")
    open_qs = [q for q in state.questions if q.open]
    if open_qs:
        lines.append("## Open questions (unresolved at finalize)")
        lines.append("")
        for q in open_qs:
            lines.append(f"- {q.id}: {q.question}")
        lines.append("")

    lines += _concerns_section(state)
    lines += _rivals_section(state)

    gaps = state.gaps()
    if gaps:
        lines += [
            "## Known gaps",
            "",
            "Deliberately unspecified at finalize — decide these while building, and "
            "say so if a choice contradicts the design above.",
            "",
        ]
        lines += [f"- {g}" for g in gaps]
        lines.append("")
    return "\n".join(lines)


def _concerns_section(state: ArchState) -> list[str]:
    """The objections raised against this design and what happened to them.

    Overruled ones matter most: they tell the builder "this was seen, and
    chosen anyway, for this reason" — which is exactly what stops someone
    re-litigating it, or quietly re-introducing the problem."""
    if not state.concerns:
        return []
    lines = ["## Concerns raised", ""]
    order = {"blocker": 0, "risk": 1, "smell": 2}
    for c in sorted(state.concerns, key=lambda x: (order.get(x.severity, 9), x.id)):
        status = c.status if c.status != "open" else "**still open**"
        lines.append(f"- **[{c.severity}] {c.target}** — {c.claim} ({status})")
        if c.alternative:
            lines.append(f"  - alternative: {c.alternative}")
        if c.resolution:
            lines.append(f"  - resolution: {c.resolution}")
    lines.append("")
    return lines


def _rivals_section(state: ArchState) -> list[str]:
    """The shapes that were considered and dropped. Cheap to record, and the
    single most common question a builder asks later: why not X?"""
    rivals = [
        v for v in state.sketchbook.variants.values()
        if v.status != "chosen" and v.nodes
    ]
    if not rivals:
        return []
    lines = ["## Alternatives considered", ""]
    for v in rivals:
        head = f"- **{v.name}**"
        if v.summary:
            head += f" — {v.summary}"
        lines.append(head)
        if v.rejected_reason:
            lines.append(f"  - not taken: {v.rejected_reason}")
        lines.append(f"  - shape: {', '.join(n.label or n.id for n in v.nodes.values())}")
    lines.append("")
    return lines


def _contract_sheet(comp: Component) -> list[str]:
    lines = [f"### {comp.name} (`{comp.id}`, {comp.kind})", "", comp.responsibility, ""]
    if comp.tech:
        lines.append(f"- tech: {comp.tech}")
    if comp.data_owned:
        lines.append(f"- owns: {comp.data_owned}")
    if comp.failure_notes:
        lines.append(f"- on failure: {comp.failure_notes}")
    facet = comp.facet
    if facet is not None:
        kind = facet.facet_kind
        if kind == "api":
            for e in facet.endpoints:
                lines.append(f"- `{e.method} {e.route}` — req {e.request} → {e.response} "
                             f"(auth: {e.auth}; errors: {', '.join(e.errors) or '—'})")
        elif kind == "store":
            for ent in facet.entities:
                lines.append(f"- entity **{ent.name}** (keys: {ent.keys}) — "
                             f"{', '.join(ent.fields)}")
            for p in facet.access_patterns:
                lines.append(f"- access: {p}")
            if facet.retention:
                lines.append(f"- retention: {facet.retention}")
        elif kind == "queue":
            for m in facet.messages:
                lines.append(f"- message **{m.name}** ({m.delivery}, {m.ordering}) — {m.schema}")
        elif kind == "service":
            for i in facet.interface:
                lines.append(f"- exposes: {i}")
        elif kind == "llm":
            for t in facet.tasks:
                lines.append(f"- task **{t.name}** ({t.model_tier}) — {t.prompt_contract}; "
                             f"fallback: {t.fallback}")
        elif kind == "infra":
            for u in facet.units:
                lines.append(f"- unit **{u.name}** [{', '.join(u.components)}] — {u.scaling_policy}")
    lines.append("")
    return lines
