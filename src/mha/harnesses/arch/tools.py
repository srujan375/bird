"""The arch toolset — a design conversation with a memory.

The tools exist so that thinking survives the session; they are not a form to
be filled. Three rules follow from that, and they are what separate this from
the original phase-gated toolset:

1. A tool refuses only what is *broken* (an edge to a component that doesn't
   exist, a session already finalized). Thinness comes back as advice on a
   successful call, so the model can argue about whether it matters.
2. No tool is locked by phase. Sketch after promoting, add a component before
   the brief is complete, expand before approval — all allowed. Post-approval
   structural edits still record an amendment; the audit trail was the part
   worth keeping, not the lock.
3. Disagreement is first-class: `concern` records an objection against the
   design, a decision, or the user's own instruction, and open blockers are
   shown to the user at the finalize gate rather than blocking the work.

`done` is the two human gates and nothing else: top-level approval, then
finalize. It never refuses for an incomplete design — it reports what is thin
and lets the user decide.
"""

from __future__ import annotations

import re
from typing import Any

from ...tools import Tool, ToolContext, ToolError, ToolResult
from ...tools.kg_query import KgQueryTool
from ...tools.files import ReadTool
from ...tools.skill import SkillTool
from ...tools.web import WebFetchTool, WebSearchTool
from . import render
from .reverse_seed import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
    SeedResult,
    Subgraph,
    reverse_seed,
    scope_subgraph,
)
from .session import ArchSession
from .sketch import DEPTHS, SketchLink, SketchNode, Variant
from .state import (
    CONCERN_SEVERITIES,
    KINDS,
    CONNECTION_KINDS,
    DECISION_CATEGORIES,
    FACET_FOR_KIND,
    FLOW_KINDS,
    SCOPES,
    ApiFacet,
    Amendment,
    Component,
    Connection,
    Decision,
    DeployUnit,
    Endpoint,
    Entity,
    Flow,
    FlowStep,
    InfraFacet,
    LlmFacet,
    LlmTask,
    Module,
    OpenQuestion,
    Option,
    QueueFacet,
    QueueMessage,
    ServiceFacet,
    StoreFacet,
)

POST_APPROVAL_PHASES = ("expand", "resolved")


def _check(validate, *args) -> None:
    """State-layer ValueErrors become model-visible ToolErrors verbatim."""
    try:
        validate(*args)
    except ValueError as e:
        raise ToolError(str(e)) from e


def _session(ctx: ToolContext) -> ArchSession:
    if ctx.arch is None:
        raise ToolError("not an architecture session — arch tools are unavailable.")
    return ctx.arch


def _guard_not_finalized(session: ArchSession) -> None:
    """The one hard lock left in the harness. Everything the old phase gates
    refused — sketching after promote, components before a complete brief,
    expanding before approval — is now allowed and merely noted."""
    if session.state.phase == "finalized":
        raise ToolError("the session is finalized — no further changes are possible.")


def _toplevel_locked(session: ArchSession) -> bool:
    """After the user approves the top level, structural edits still go
    through — they just leave an amendment behind."""
    return session.state.phase in POST_APPROVAL_PHASES


def _active_variant(session: ArchSession) -> Variant:
    """The sketch surface is always available: if nothing is open, open one.
    Going back to the napkin mid-design is a legitimate move, not an error."""
    book = session.state.sketchbook
    v = book.active_variant()
    if v is not None:
        return v
    live = next((x for x in book.variants.values() if x.status != "archived"), None)
    if live is not None:
        book.active = live.id
        return live
    vid = f"v{len(book.variants) + 1}"
    v = Variant(id=vid, name="first take", summary="")
    book.variants[vid] = v
    book.active = vid
    return v


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return s or "node"


# loose sketch `kind` hint -> strict KIND at promotion; anything else -> service
_KIND_MAP = {
    "db": "store", "database": "store", "datastore": "store", "storage": "store",
    "mq": "queue", "broker": "queue", "bus": "queue", "stream": "queue", "topic": "queue",
    "frontend": "ui", "web": "ui", "client": "ui", "spa": "ui",
    "model": "llm", "ai": "llm", "agent": "llm",
    "worker": "job", "cron": "job", "batch": "job",
    "endpoint": "api", "rest": "api", "http": "api",
    "3rd-party": "external", "third-party": "external", "vendor": "external",
    "component": "service", "module": "service", "idea": "service",
}


def _strict_kind(loose: str) -> str:
    k = (loose or "").strip().lower()
    if k in KINDS:
        return k
    return _KIND_MAP.get(k, "service")


def _drop_components(state: Any, ids: set[str]) -> None:
    """Remove components and everything that would dangle without them."""
    for cid in ids:
        state.components.pop(cid, None)
    state.connections = [c for c in state.connections if c.src not in ids and c.dst not in ids]
    kept = []
    for f in state.flows:
        f.steps = [s for s in f.steps if s.src not in ids and s.dst not in ids]
        if f.steps:
            kept.append(f)
    state.flows = kept
    state.obligations = [o for o in state.obligations if o.component_id not in ids]


def _promote(session: ArchSession, variant: Variant, replace: bool = False) -> tuple[int, int, int]:
    """Seed the strict layer from a sketch: nodes -> draft Components, links ->
    Connections. Deliberately skips the thinness checks — these are drafts.

    Re-runnable and non-destructive. Rivals stay live (archiving is a separate,
    deliberate move), a node already seeded from this variant is left alone
    rather than duplicated, and `replace` clears what an *earlier* choice
    seeded so switching horses doesn't leave two architectures on the canvas.
    """
    state = session.state
    book = state.sketchbook
    for x in book.variants.values():
        if x.id == variant.id:
            x.status = "chosen"
        elif x.status == "chosen":
            x.status = "draft"  # the previous choice steps down but stays live
    book.active = variant.id

    prefix = f"sketch:{variant.id}:"
    if replace:
        stale = {
            c.id for c in state.components.values()
            if c.origin.startswith("sketch:") and not c.origin.startswith(prefix)
        }
        _drop_components(state, stale)

    seeded = {c.origin: c.id for c in state.components.values() if c.origin.startswith(prefix)}
    idmap: dict[str, str] = {}
    added_c = kept = 0
    for nid, node in variant.nodes.items():
        origin = prefix + nid
        if origin in seeded:
            idmap[nid] = seeded[origin]
            kept += 1
            continue
        base = _slug(nid)
        cid, i = base, 2
        while cid in state.components:
            cid, i = f"{base}-{i}", i + 1
        state.components[cid] = Component(
            id=cid,
            name=node.label or cid,
            kind=_strict_kind(node.kind),
            responsibility=(node.note or node.detail or "").strip(),
            trace=[],
            existing=False,
            origin=origin,
        )
        idmap[nid] = cid
        added_c += 1

    have = {(c.src, c.dst, c.label) for c in state.connections}
    added_conn = 0
    for ln in variant.links:
        s, d = idmap.get(ln.src), idmap.get(ln.dst)
        if not s or not d:
            continue
        label = ln.label or "calls"
        if (s, d, label) in have:
            continue
        have.add((s, d, label))
        state.connections.append(Connection(
            src=s, dst=d,
            label=label,
            kind=ln.kind if ln.kind in CONNECTION_KINDS else "sync",
        ))
        added_conn += 1
    if state.phase == "brainstorm":
        state.phase = "propose"  # never yanks a later phase backwards
    return added_c, added_conn, kept


def _post_approval_amendment(session: ArchSession, description: str, *, structural: bool) -> str:
    """After the user approves the top level, an edit is not refused — it is
    recorded. The audit trail was the thing worth protecting; the lock wasn't."""
    if not _toplevel_locked(session):
        return ""
    state = session.state
    state.amendments.append(Amendment(
        turn=len(state.amendments) + 1, description=description, structural=structural,
    ))
    if structural:
        state.compute_obligations()
        return (
            "this changes the top level the user already approved — recorded as a "
            "structural amendment and obligations recomputed. Tell them what moved and why."
        )
    return "recorded as an amendment against the approved top level."


def _confirm(
    action: str,
    session: ArchSession,
    *,
    gaps: list[str] | None = None,
    note: str = "",
) -> ToolResult:
    """Tool receipt: what happened, what is thin about it, what's worth doing
    next. `gaps` is advice — the call already succeeded."""
    parts = [action]
    if note:
        parts.append(note)
    if gaps:
        parts.append(
            "thin: " + "; ".join(gaps)
            + "\n(fill these in when you know them, or say why they don't matter here — "
            "they are not required)"
        )
    parts.append(f"next: {render._next_hint(session.state)}")
    return ToolResult(output="\n".join(parts))


# ============================ the loose sketch layer ============================
# Brainstorming primitives. No validation — you're sketching on a napkin. Several
# variants of the same feature can coexist; `promote` commits one into the strict
# ArchState. See sketch.py for the model these mutate.


class VariantTool(Tool):
    name = "variant"
    description = (
        "Create, select, or archive a candidate architecture (a variant). Several "
        "coexist for the same feature; the active one is what node/link/splice edit. "
        "Offer the user rival shapes early. Archive a loser with a rejected_reason — "
        "that reasoning is the ADR gold that survives into the handoff doc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Optional; auto-assigned v1, v2, ..."},
            "name": {"type": "string", "description": "e.g. 'synchronous', 'event-driven'"},
            "summary": {"type": "string", "description": "the idea/tradeoff this take explores, one line"},
            "select": {"type": "boolean", "description": "make it the active variant (default true)"},
            "archive": {"type": "boolean", "description": "retire it as a rejected alternative"},
            "rejected_reason": {"type": "string", "description": "why it lost — recorded on archive"},
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_not_finalized(session)
        book = session.state.sketchbook
        if args.get("archive"):
            vid = (args.get("id") or "").strip()
            v = book.variants.get(vid)
            if v is None:
                known = ", ".join(book.variants) or "none"
                raise ToolError(f"no variant {vid!r} to archive (known: {known}).")
            v.status = "archived"
            if args.get("rejected_reason"):
                v.rejected_reason = args["rejected_reason"]
            if book.active == vid:
                book.active = next(
                    (k for k, x in book.variants.items() if x.status != "archived"), None
                )
            session.touched("variant", vid)
            return _confirm(f"Archived variant {vid} ({v.name}).", session)
        vid = (args.get("id") or f"v{len(book.variants) + 1}").strip()
        v = book.variants.get(vid)
        if v is None:
            v = Variant(id=vid, name=args.get("name", vid), summary=args.get("summary", ""))
            book.variants[vid] = v
            action = f"Started variant {vid}: {v.name}."
        else:
            if "name" in args:
                v.name = args["name"]
            if "summary" in args:
                v.summary = args["summary"]
            v.status = "draft"
            action = f"Updated variant {vid}."
        if args.get("select", True) and v.status != "archived":
            book.active = vid
        session.touched("variant", vid)
        return _confirm(action, session)


class NodeTool(Tool):
    name = "node"
    description = (
        "Add, update, or remove a box in the active variant. Loose: `kind` is a free "
        "hint (service/store/queue/ui/llm/idea/...), nothing is validated — you're "
        "sketching. remove:true deletes it (and any links touching it)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "short, stable handle for the box"},
            "label": {"type": "string", "description": "display name"},
            "kind": {"type": "string", "description": "free hint, e.g. 'store', 'queue', 'idea'"},
            "note": {"type": "string", "description": "what it is / why it's here"},
            "remove": {"type": "boolean"},
        },
        "required": ["id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_not_finalized(session)
        v = _active_variant(session)
        nid = args["id"].strip()
        if args.get("remove"):
            if nid not in v.nodes:
                raise ToolError(f"node {nid!r} is not in variant {v.id}.")
            v.nodes.pop(nid)
            v.links = [ln for ln in v.links if nid not in (ln.src, ln.dst)]
            action = f"Removed node {nid}."
        else:
            node = v.nodes.get(nid)
            if node is None:
                node = SketchNode(
                    id=nid, label=args.get("label", nid),
                    kind=args.get("kind", "component"), note=args.get("note", ""),
                )
                v.nodes[nid] = node
                action = f"Added node {nid} ({node.kind})."
            else:
                for k in ("label", "kind", "note"):
                    if k in args:
                        setattr(node, k, args[k])
                action = f"Updated node {nid}."
        session.touched("node", nid)
        return _confirm(action, session)


class LinkTool(Tool):
    name = "link"
    description = (
        "Add, update, or remove an edge in the active variant. Missing endpoints are "
        "auto-created as stub nodes (fast sketching). `kind` is a free hint "
        "(sync/async/batch). remove:true drops the edge."
    )
    parameters = {
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "dst": {"type": "string"},
            "label": {"type": "string", "description": "what the edge means, e.g. 'writes', 'emits'"},
            "kind": {"type": "string", "description": "sync / async / batch (free hint)"},
            "note": {"type": "string"},
            "remove": {"type": "boolean"},
        },
        "required": ["src", "dst"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_not_finalized(session)
        v = _active_variant(session)
        src, dst = args["src"].strip(), args["dst"].strip()
        label = args.get("label")
        idx = v.link_index(src, dst, label)
        if args.get("remove"):
            if idx < 0:
                raise ToolError(f"no edge {src} -> {dst}" + (f" labeled {label!r}" if label else "") + ".")
            v.links.pop(idx)
            action = f"Removed edge {src} -> {dst}."
        elif idx >= 0:
            ln = v.links[idx]
            for k in ("label", "kind", "note"):
                if k in args:
                    setattr(ln, k, args[k])
            action = f"Updated edge {src} -> {dst}."
        else:
            created = []
            for ref in (src, dst):
                if ref not in v.nodes:
                    v.nodes[ref] = SketchNode(id=ref, label=ref)
                    created.append(ref)
            v.links.append(SketchLink(
                src=src, dst=dst, label=args.get("label", ""),
                kind=args.get("kind", "sync"), note=args.get("note", ""),
            ))
            action = f"Linked {src} -> {dst}."
            if created:
                action += f" (created stub node(s): {', '.join(created)})"
        session.touched("link", f"{src}->{dst}")
        return _confirm(action, session)


class SpliceTool(Tool):
    name = "splice"
    description = (
        "Insert a new node between two existing ones: creates the node and rewires "
        "src -> new -> dst, dropping the direct src -> dst edge (its kind/label carry "
        "onto src -> new). The 'add an intermediate step' move — a cache, queue, or "
        "gateway between two boxes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "src": {"type": "string", "description": "existing node the edge leaves"},
            "dst": {"type": "string", "description": "existing node the edge enters"},
            "id": {"type": "string", "description": "id for the new node in between"},
            "label": {"type": "string"},
            "kind": {"type": "string", "description": "free hint for the new node"},
            "note": {"type": "string"},
        },
        "required": ["src", "dst", "id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_not_finalized(session)
        v = _active_variant(session)
        src, dst, nid = args["src"].strip(), args["dst"].strip(), args["id"].strip()
        for ref in (src, dst):
            if ref not in v.nodes:
                raise ToolError(f"node {ref!r} is not in variant {v.id} — splice needs both ends to exist.")
        if nid in v.nodes:
            raise ToolError(f"node {nid!r} already exists; pick a new id for the inserted node.")
        v.nodes[nid] = SketchNode(
            id=nid, label=args.get("label", nid),
            kind=args.get("kind", "component"), note=args.get("note", ""),
        )
        idx = v.link_index(src, dst)
        old = v.links.pop(idx) if idx >= 0 else None
        carry_kind = old.kind if old else "sync"
        v.links.append(SketchLink(src=src, dst=nid, label=(old.label if old else ""), kind=carry_kind))
        v.links.append(SketchLink(src=nid, dst=dst, label="", kind=carry_kind))
        session.touched("node", nid)
        return _confirm(f"Spliced {nid} between {src} and {dst}.", session)


class DepthTool(Tool):
    name = "depth"
    description = (
        "Set a node's depth — the fidelity slider. RAISE it (stub -> sketch -> "
        "detailed) to flesh out internals in `detail`, or LOWER it to collapse a node "
        "back toward a bare box. Reducing depth is a first-class simplification move, "
        "not an undo — collapsing to stub clears the internal detail."
    )
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string"},
            "level": {"type": "string", "enum": list(DEPTHS)},
            "detail": {"type": "string", "description": "the internal sketch at sketch/detailed depth"},
        },
        "required": ["node_id", "level"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_not_finalized(session)
        v = _active_variant(session)
        nid = args["node_id"].strip()
        node = v.nodes.get(nid)
        if node is None:
            raise ToolError(f"node {nid!r} is not in variant {v.id}.")
        level = args["level"]
        if level not in DEPTHS:
            raise ToolError(f"level must be one of {', '.join(DEPTHS)}.")
        node.depth = level
        if level == "stub":
            node.detail = ""  # collapsing clears the internal sketch
        elif "detail" in args:
            node.detail = args["detail"]
        session.touched("node", nid)
        return _confirm(f"{nid} depth -> {level}.", session)


class PromoteTool(Tool):
    name = "promote"
    description = (
        "Take a sketch variant forward: mark it chosen and seed the design "
        "(components + connections) from it. Rivals stay live — you can keep "
        "sketching, and promoting a different variant later is allowed (pass "
        "replace:true to clear what the earlier choice seeded). Re-running it on the "
        "same variant picks up nodes you have added since."
    )
    parameters = {
        "type": "object",
        "properties": {
            "variant_id": {"type": "string", "description": "which variant (default: the active one)"},
            "replace": {"type": "boolean",
                        "description": "drop what a previous variant seeded instead of merging"},
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        book = state.sketchbook
        vid = (args.get("variant_id") or book.active or "").strip()
        v = book.variants.get(vid) if vid else None
        if v is None:
            known = ", ".join(book.variants) or "none"
            raise ToolError(
                f"no variant {vid!r} to promote (known: {known}) — name one with "
                "`variant` and sketch it first."
            )
        if not v.nodes:
            raise ToolError(f"variant {v.id} ({v.name}) has no nodes yet — sketch it before promoting.")
        added_c, added_conn, kept = _promote(session, v, replace=bool(args.get("replace")))
        session.touched()
        bits = [f"seeded {added_c} component(s) and {added_conn} connection(s)"]
        if kept:
            bits.append(f"{kept} already seeded, left alone")
        note = ""
        missing = state.brief.missing()
        if missing:
            note = (
                f"the brief has no {', '.join(missing)} yet — not a blocker, but these are "
                "load-bearing for the build. Ask the user rather than assuming."
            )
        return _confirm(
            f"Chose '{v.name}': " + "; ".join(bits) + ".",
            session,
            note=note,
            gaps=state.gaps()[:6],
        )


class BriefTool(Tool):
    name = "brief"
    description = (
        "Record or update the design brief (intake contract). Merge semantics: only "
        "the fields you pass change. goal + actors + scope unlock `component`; "
        "production/high_scale scope additionally requires scale, consistency, "
        "availability. Ask the user for load-bearing facts instead of assuming."
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "What the system must achieve, one sentence"},
            "actors": {"type": "array", "items": {"type": "string"},
                       "description": "Who/what uses the system"},
            "scope": {"type": "string", "enum": list(SCOPES)},
            "users": {"type": "string", "description": "Scale: e.g. '10k MAU', 'internal team'"},
            "reads_per_sec": {"type": "string"},
            "writes_per_sec": {"type": "string"},
            "data_volume": {"type": "string", "description": "e.g. '~50GB, +1GB/mo'"},
            "growth": {"type": "string"},
            "latency": {"type": "string", "description": "e.g. 'p95 < 200ms on read path'"},
            "consistency": {"type": "string", "enum": ["strong", "eventual", "mixed"]},
            "availability": {"type": "string", "description": "e.g. '99.9'"},
            "deploy_target": {"type": "string", "description": "cloud / on-prem / serverless / single box"},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "non_goals": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        b = state.brief
        before_scope = b.scope
        for key in ("goal", "scope", "latency", "consistency", "availability", "deploy_target"):
            if key in args:
                setattr(b, key, args[key])
        for key in ("actors", "constraints", "non_goals"):
            if key in args:
                setattr(b, key, list(args[key]))
        for key in ("users", "reads_per_sec", "writes_per_sec", "data_volume", "growth"):
            if key in args:
                setattr(b.scale, key, args[key])
        missing = b.missing()
        note = ""
        if _toplevel_locked(session) and b.scope != before_scope:
            state.amendments.append(Amendment(
                turn=len(state.amendments) + 1,
                description=f"brief scope {before_scope or '?'} -> {b.scope}", structural=True,
            ))
            note = "scope changed after approval — recorded as an amendment; obligations recomputed."
            state.compute_obligations()
        session.touched()
        if missing:
            return _confirm(
                f"Brief updated. Still unknown: {', '.join(missing)} — ask the user for "
                "these rather than assuming them.", session, note=note,
            )
        return _confirm("Brief updated.", session, note=note)


class ComponentTool(Tool):
    name = "component"
    description = (
        "Add, update, or remove a top-level component. Upserts by id (ids are "
        "immutable and kebab-case; rename via `name`). `remove: true` deletes it — "
        "connections and flows referencing it must be removed first. Only `id` is "
        "required: record what you know now, fill the rest in as it settles. After "
        "top-level approval this still works and records an amendment."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "kebab-case, unique, stable"},
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "responsibility": {"type": "string", "description": "One sentence; required"},
            "trace": {"type": "array", "items": {"type": "string"},
                      "description": "Which brief goals/constraints this serves (YAGNI check)"},
            "existing": {"type": "boolean", "description": "Brownfield import (feature mode)"},
            "tech": {"type": "string"},
            "data_owned": {"type": "string", "description": "Required when kind is store"},
            "failure_notes": {"type": "string", "description": "Required at production scope"},
            "remove": {"type": "boolean"},
        },
        "required": ["id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        cid = args["id"].strip()
        result = _apply_component(session, args, cid)
        note = _post_approval_amendment(session, result, structural=(
            bool(args.get("remove")) or "kind" in args
        ))
        comp = state.components.get(cid)
        session.touched("component", cid)
        return _confirm(
            result, session, note=note,
            gaps=state.component_gaps(comp) if comp is not None else None,
        )


def _apply_component(session: ArchSession, args: dict[str, Any], cid: str) -> str:
    """Shared by ComponentTool and amend_toplevel."""
    state = session.state
    if args.get("remove"):
        if cid not in state.components:
            raise ToolError(f"component {cid!r} does not exist.")
        refs = state.references_to(cid)
        if refs:
            raise ToolError(
                f"cannot remove {cid!r}: still referenced by {', '.join(refs)}. "
                "Remove or rewire those first."
            )
        del state.components[cid]
        return f"Removed component {cid}."
    current = state.components.get(cid)
    if current is None:
        comp = Component(
            id=cid,
            name=args.get("name", cid),
            kind=args.get("kind") or "service",
            responsibility=args.get("responsibility", ""),
            trace=list(args.get("trace", [])),
            existing=bool(args.get("existing", False)),
            tech=args.get("tech"),
            data_owned=args.get("data_owned"),
            failure_notes=args.get("failure_notes"),
        )
        _check(state.validate_component, comp, False)
        state.components[cid] = comp
        # the design layer has something in it now, so the session is no longer
        # only sketching. `promote` does the same thing for the other route in.
        if state.phase == "brainstorm":
            state.phase = "propose"
        return f"Added component {cid} ({comp.kind})."
    for key in ("name", "kind", "responsibility", "tech", "data_owned", "failure_notes"):
        if key in args:
            setattr(current, key, args[key])
    if "trace" in args:
        current.trace = list(args["trace"])
    if "existing" in args:
        current.existing = bool(args["existing"])
    _check(state.validate_component, current, True)
    return f"Updated component {cid}."


class ConnectTool(Tool):
    name = "connect"
    description = (
        "Add, update, or remove a connection between two components. Upserts by "
        "(src, dst, label). async connections must name their mechanism (which "
        "queue/bus carries it). Locked after top-level approval (use amend_toplevel)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "dst": {"type": "string"},
            "label": {"type": "string", "description": "e.g. 'consume', 'write', 'emit'"},
            "kind": {"type": "string", "enum": list(CONNECTION_KINDS)},
            "mechanism": {"type": "string", "description": "Required when async"},
            "protocol": {"type": "string", "description": "http / grpc / sql / amqp ..."},
            "data": {"type": "string", "description": "What crosses the edge"},
            "failure_mode": {"type": "string",
                             "description": "Required at production scope: behavior when dst is down"},
            "remove": {"type": "boolean"},
        },
        "required": ["src", "dst"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        result = _apply_connection(session, args)
        note = _post_approval_amendment(session, result, structural=bool(args.get("remove")))
        src, dst = args["src"].strip(), args["dst"].strip()
        conn = next((c for c in state.connections if c.src == src and c.dst == dst), None)
        session.touched("connection", f"{src}->{dst}")
        return _confirm(
            result, session, note=note,
            gaps=state.connection_gaps(conn) if conn is not None else None,
        )


def _apply_connection(session: ArchSession, args: dict[str, Any]) -> str:
    state = session.state
    src, dst = args["src"].strip(), args["dst"].strip()
    label = args.get("label")
    matches = [
        c for c in state.connections
        if c.src == src and c.dst == dst and (label is None or c.label == label)
    ]
    if args.get("remove"):
        if not matches:
            raise ToolError(f"no connection {src} -> {dst}" + (f" labeled {label!r}" if label else "") + ".")
        if len(matches) > 1:
            labels = ", ".join(repr(c.label) for c in matches)
            raise ToolError(f"multiple connections {src} -> {dst} ({labels}); pass `label` to pick one.")
        state.connections.remove(matches[0])
        return f"Removed connection {src} -> {dst}."
    if matches and label is not None:
        conn = matches[0]
        for key in ("kind", "mechanism", "protocol", "data", "failure_mode", "label"):
            if key in args:
                setattr(conn, key, args[key])
        _check(state.validate_connection, conn)
        return f"Updated connection {src} -> {dst} ({conn.label})."
    conn = Connection(
        src=src, dst=dst,
        label=label or "",
        kind=args.get("kind", "sync"),
        mechanism=args.get("mechanism"),
        protocol=args.get("protocol"),
        data=args.get("data"),
        failure_mode=args.get("failure_mode"),
    )
    if not conn.label:
        raise ToolError("a new connection needs a label (what the edge means, e.g. 'consume').")
    _check(state.validate_connection, conn)
    state.connections.append(conn)
    return f"Connected {src} -> {dst} ({conn.label}, {conn.kind})."


class FlowTool(Tool):
    name = "flow"
    description = (
        "Record or update a key flow (sequence of steps across components). Upserts "
        "by id. At least one happy flow covering the primary goal is required before "
        "top-level review; at production scope every happy flow wants a failure twin."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "kebab-case, e.g. 'place-order'"},
            "name": {"type": "string", "description": "e.g. 'place order'"},
            "kind": {"type": "string", "enum": list(FLOW_KINDS)},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "src": {"type": "string"},
                        "dst": {"type": "string"},
                        "action": {"type": "string", "description": "'POST /orders', 'publish OrderPlaced'"},
                        "note": {"type": "string"},
                    },
                    "required": ["src", "dst", "action"],
                    "additionalProperties": False,
                },
            },
            "remove": {"type": "boolean"},
        },
        "required": ["id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        fid = args["id"].strip()
        existing = next((f for f in state.flows if f.id == fid), None)
        if args.get("remove"):
            if existing is None:
                raise ToolError(f"flow {fid!r} does not exist.")
            state.flows.remove(existing)
            action = f"Removed flow {fid}."
        else:
            if not args.get("steps"):
                raise ToolError("a flow needs steps: [{src, dst, action}, ...].")
            flow = Flow(
                id=fid,
                name=args.get("name", existing.name if existing else fid),
                kind=args.get("kind", existing.kind if existing else "happy"),
                steps=[FlowStep(src=s["src"], dst=s["dst"], action=s["action"],
                                note=s.get("note")) for s in args["steps"]],
            )
            _check(state.validate_flow, flow)
            if existing is not None:
                state.flows[state.flows.index(existing)] = flow
                action = f"Updated flow {fid}."
            else:
                state.flows.append(flow)
                action = f"Recorded flow {fid} ({flow.kind}, {len(flow.steps)} steps)."
        note = _post_approval_amendment(session, action, structural=False)
        session.touched("flow", fid)
        return _confirm(action, session, note=note)


def _as_str(owner: str, key: str, val: Any) -> str:
    if not isinstance(val, str):
        raise ToolError(
            f"{owner} field {key!r} must be a plain string, not {type(val).__name__}. "
            f'Describe it as text (e.g. "contactId"), not an object or array.'
        )
    return val


def _as_str_opt(owner: str, key: str, val: Any) -> str | None:
    return None if val is None else _as_str(owner, key, val)


def _as_str_list(owner: str, key: str, val: Any) -> list[str]:
    if not isinstance(val, list) or any(not isinstance(x, str) for x in val):
        found = (
            next((type(x).__name__ for x in val if not isinstance(x, str)), "ok")
            if isinstance(val, list) else type(val).__name__
        )
        raise ToolError(
            f"{owner} field {key!r} must be a list of plain strings "
            f'(e.g. ["firstName", "email"]) — found a {found}. Flatten each item to '
            f"one string; do not pass objects."
        )
    return list(val)


_FACET_BUILDERS = {
    "api": ("endpoints", lambda a: ApiFacet(
        endpoints=[_build(Endpoint, e, ("route", "method", "request", "response", "auth"))
                   for e in a.get("endpoints", [])])),
    "store": ("entities + access_patterns", lambda a: StoreFacet(
        entities=[_build(Entity, e, ("name", "keys")) for e in a.get("entities", [])],
        access_patterns=_as_str_list("store facet", "access_patterns", a.get("access_patterns", [])),
        retention=_as_str_opt("store facet", "retention", a.get("retention")),
        migration_risk=_as_str_opt("store facet", "migration_risk", a.get("migration_risk")))),
    "queue": ("messages", lambda a: QueueFacet(
        messages=[_build(QueueMessage, m, ("name", "schema", "ordering", "delivery"))
                  for m in a.get("messages", [])])),
    "service": ("interface", lambda a: ServiceFacet(
        interface=_as_str_list("service facet", "interface", a.get("interface", [])),
        modules=[_build(Module, m, ("name", "purpose")) for m in a["modules"]]
        if a.get("modules") else None)),
    "llm": ("tasks", lambda a: LlmFacet(
        tasks=[_build(LlmTask, t, ("name", "model_tier", "prompt_contract",
                                   "context_strategy", "fallback", "guardrails"))
               for t in a.get("tasks", [])])),
    "infra": ("units + state_locality", lambda a: InfraFacet(
        units=[_build(DeployUnit, u, ("name", "components", "scaling_policy"))
               for u in a.get("units", [])],
        state_locality=_as_str_opt("infra facet", "state_locality", a.get("state_locality")) or "")),
}

# dataclass annotations are strings here (from __future__ import annotations),
# so we match on the annotation text to type-check tool input
_STR_ANNS = {"str", "str|None"}
_STRLIST_ANNS = {"list[str]"}


def _build(cls: type, d: dict[str, Any], required: tuple[str, ...]) -> Any:
    for req in required:
        if req not in d or d[req] in ("", [], None):
            raise ToolError(f"{cls.__name__} needs {req!r} (got: {sorted(d)}).")
    dfields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    kw: dict[str, Any] = {}
    for k, v in d.items():
        if k not in dfields:
            continue
        ann = str(dfields[k].type).replace(" ", "")
        if v is not None and ann in _STR_ANNS:
            v = _as_str(cls.__name__, k, v)
        elif ann in _STRLIST_ANNS:
            v = _as_str_list(cls.__name__, k, v)
        kw[k] = v
    return cls(**kw)


# Fully-specified item schemas. An underspecified {"type": "object"} was the main
# cause of repeated expand failures — the model had to guess every field name and
# only learned the shape from error messages. Each item now states its fields,
# which are required, and a concrete example.
_STR_ARR = {"type": "array", "items": {"type": "string"}}

_ENDPOINT_ITEM = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "description": "path, e.g. /contacts/search"},
        "method": {"type": "string", "description": "HTTP verb: GET/POST/PUT/DELETE"},
        "request": {"type": "string", "description": "request shape, one line, e.g. '{q, page, agentId?}'"},
        "response": {"type": "string", "description": "response shape, one line, e.g. '{results[], nextCursor}'"},
        "auth": {"type": "string", "description": "authorization rule, e.g. 'org admin/owner'"},
        "errors": {**_STR_ARR, "description": "error cases, e.g. ['403 not admin', '400 bad query']"},
        "idempotency": {"type": "string"},
        "pagination": {"type": "string", "description": "e.g. 'cursor (searchAfter)'"},
    },
    "required": ["route", "method", "request", "response", "auth"],
}
_ENTITY_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "entity/collection name, e.g. 'CrmContact'"},
        "keys": {"type": "string", "description": "primary/partition key(s) as text, e.g. 'organisationId + _id'"},
        "fields": {**_STR_ARR, "description": "field NAMES as plain strings, e.g. ['firstName', 'primaryEmail'] — not objects"},
        "indexes": {**_STR_ARR, "description": "indexes as text, e.g. ['atlas-search: name,email', 'org+chatbot']"},
    },
    "required": ["name", "keys"],
}
_MESSAGE_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "schema": {"type": "string", "description": "payload shape, one line"},
        "ordering": {"type": "string", "description": "e.g. 'per-key FIFO' or 'none'"},
        "delivery": {"type": "string", "description": "e.g. 'at-least-once'"},
        "dlq_policy": {"type": "string"},
    },
    "required": ["name", "schema", "ordering", "delivery"],
}
_MODULE_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "purpose": {"type": "string", "description": "what this module does, one line"},
    },
    "required": ["name", "purpose"],
}
_TASK_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "model_tier": {"type": "string", "description": "e.g. 'frontier', 'local-8b'"},
        "prompt_contract": {"type": "string", "description": "inputs -> outputs, one line"},
        "context_strategy": {"type": "string"},
        "fallback": {"type": "string", "description": "what happens on failure/timeout"},
        "guardrails": {"type": "string"},
        "eval_hook": {"type": "string"},
        "cost_envelope": {"type": "string"},
    },
    "required": ["name", "model_tier", "prompt_contract", "context_strategy", "fallback", "guardrails"],
}
_UNIT_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "deploy unit, e.g. 'search-api pod'"},
        "components": {**_STR_ARR, "description": "component ids this unit runs"},
        "scaling_policy": {"type": "string", "description": "e.g. 'HPA on CPU 70%, 2-10 replicas'"},
        "region": {"type": "string"},
    },
    "required": ["name", "components", "scaling_policy"],
}


class ExpandTool(Tool):
    name = "expand"
    description = (
        "Fill one component's facet (its internal contract). Available at any point — "
        "expand something early if that is what the conversation is about; the tracker "
        "still suggests a risk order. Pass the field group matching the component's kind: endpoints "
        "(api/gateway) · entities/access_patterns/retention (store/cache) · messages "
        "(queue) · interface/modules (service/job/ui) · tasks (llm) · units/"
        "state_locality (infra)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "component_id": {"type": "string", "description": "the component to expand (take the next one from the tracker's risk order)"},
            "endpoints": {"type": "array", "items": _ENDPOINT_ITEM,
                          "description": "api/gateway: the HTTP contract (one item per endpoint)"},
            "entities": {"type": "array", "items": _ENTITY_ITEM,
                         "description": "store/cache: the data entities (one item per entity/collection)"},
            "access_patterns": {**_STR_ARR,
                                "description": "store/cache: how the data is queried, e.g. ['search by name prefix within org']"},
            "retention": {"type": "string", "description": "store/cache: how long the data lives"},
            "migration_risk": {"type": "string", "description": "store/cache: risk of changing this schema later"},
            "messages": {"type": "array", "items": _MESSAGE_ITEM,
                         "description": "queue: the messages carried (one item per message type)"},
            "interface": {**_STR_ARR,
                          "description": "service/job/ui: exposed operations, e.g. ['search(q, scope) -> results']"},
            "modules": {"type": "array", "items": _MODULE_ITEM,
                        "description": "service/job/ui: internal modules (optional)"},
            "tasks": {"type": "array", "items": _TASK_ITEM,
                      "description": "llm: the model tasks (one item per task)"},
            "units": {"type": "array", "items": _UNIT_ITEM,
                      "description": "infra: deploy units (one item per unit)"},
            "state_locality": {"type": "string", "description": "infra: where state lives (stateless / which store)"},
        },
        "required": ["component_id"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        cid = args["component_id"].strip()
        comp = state.components.get(cid)
        if comp is None:
            raise ToolError(f"component {cid!r} does not exist.")
        facet_kind = FACET_FOR_KIND.get(comp.kind)
        if facet_kind is None:
            raise ToolError(f"{cid} is kind {comp.kind!r} — not ours to design; no facet applies.")
        hint, builder = _FACET_BUILDERS[facet_kind]
        facet = builder(args)
        primary = getattr(facet, ("endpoints", "entities", "messages", "interface",
                                  "tasks", "units")[
            ("api", "store", "queue", "service", "llm", "infra").index(facet_kind)])
        if not primary:
            raise ToolError(f"a {facet_kind} facet needs {hint}.")
        comp.facet = facet
        queue = render.risk_ordered_pending(state)
        owed = next((o for o in queue if o.component_id == cid), None)
        if owed is not None:
            owed.status = "done"
        note = ""
        if queue and owed is not queue[0]:
            head = queue[0]
            note = (
                f"note: {head.component_id} is the riskier one still open ({head.reason}) "
                "— worth doing next unless you have a reason to leave it."
            )
        session.touched("component", cid)
        return _confirm(f"Expanded {cid} ({facet_kind} facet).", session, note=note)


class DecideTool(Tool):
    name = "decide"
    description = (
        "Record an architectural decision: the options you weighed with pros/cons, the "
        "choice (must match one of the option names), and the rationale. Upserts by id. "
        "Recording a one-option decision is allowed and noted — but if there was never "
        "an alternative, that is usually worth saying out loud rather than dressing up "
        "as a decision."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Optional; auto-assigned d1, d2, ..."},
            "topic": {"type": "string", "description": "e.g. 'Message queue'"},
            "category": {"type": "string", "enum": list(DECISION_CATEGORIES)},
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "pros": {"type": "array", "items": {"type": "string"}},
                        "cons": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "choice": {"type": "string"},
            "rationale": {"type": "string"},
            "status": {"type": "string", "enum": ["decided", "deferred"],
                       "description": "deferred still records the default taken"},
        },
        "required": ["topic", "category", "choice", "rationale"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        did = args.get("id") or f"d{len(state.decisions) + 1}"
        dec = Decision(
            id=did,
            topic=args["topic"],
            category=args["category"],
            options=[Option(name=o["name"], pros=list(o.get("pros", [])),
                            cons=list(o.get("cons", []))) for o in args.get("options", [])],
            choice=args["choice"],
            rationale=args["rationale"],
            status=args.get("status", "decided"),
        )
        _check(state.validate_decision, dec)
        existing = next((d for d in state.decisions if d.id == did), None)
        if existing is not None:
            state.decisions[state.decisions.index(existing)] = dec
            action = f"Updated decision {did}: {dec.topic} -> {dec.choice}."
        else:
            state.decisions.append(dec)
            action = f"Recorded decision {did}: {dec.topic} -> {dec.choice}."
        session.touched("decision", did)
        return _confirm(action, session, gaps=state.decision_gaps(dec))


class AskTool(Tool):
    name = "ask"
    description = (
        "Flag an open question for the user. blocking=true prevents finalize until "
        "it is resolved. Then actually ask it in your reply text — this tool only "
        "records it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "blocking": {"type": "boolean", "description": "Blocks finalize until resolved"},
        },
        "required": ["question", "blocking"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_not_finalized(session)
        qid = f"q{len(session.state.questions) + 1}"
        session.state.questions.append(
            OpenQuestion(id=qid, question=args["question"], blocking=bool(args["blocking"]),
                         source="model")
        )
        session.touched("question", qid)
        return _confirm(f"Recorded open question {qid}. Ask the user in your reply.", session)


class AnswerTool(Tool):
    name = "answer"
    description = (
        "Resolve an open question with the answer (usually the user's), or mark it "
        "deferred/dropped with the reason."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "answer": {"type": "string"},
            "resolution": {"type": "string", "enum": ["answered", "deferred", "dropped"]},
        },
        "required": ["id", "answer"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_not_finalized(session)
        qid = args["id"].strip()
        q = next((q for q in session.state.questions if q.id == qid), None)
        if q is None:
            known = ", ".join(x.id for x in session.state.questions) or "none"
            raise ToolError(f"no question {qid!r} (known: {known}).")
        q.answer = args["answer"]
        q.resolution = args.get("resolution", "answered")
        session.touched("question", qid)
        return _confirm(f"Question {qid} {q.resolution}.", session)


class ConcernTool(Tool):
    name = "concern"
    description = (
        "Put an objection on the record — the design is wrong, a decision will hurt, "
        "or something is more than it needs to be. Use it against the design, against "
        "a decision, against your OWN earlier proposal, or against what the user just "
        "asked for. Say what breaks concretely and name the cheaper option.\n"
        "severity: blocker (it will not work) · risk (it works but will hurt) · smell "
        "(more than it needs to be). Open blockers are shown to the user at the "
        "finalize gate — they never stop you working.\n"
        "Resolve one with resolve:<id> and a status: `accepted` (the design changed), "
        "`overruled` (the user or you decided to live with it — the reason is the "
        "record), or `withdrawn` (you were wrong). Overruled concerns are kept: the "
        "code harness inherits them.\n"
        "State it in your reply too — this tool only records it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": list(CONCERN_SEVERITIES)},
            "target": {"type": "string",
                       "description": "component/decision id, 'brief', 'user', or what it is about"},
            "claim": {"type": "string", "description": "what breaks, concretely"},
            "alternative": {"type": "string", "description": "the cheaper or safer option"},
            "resolve": {"type": "string", "description": "id of an existing concern to close"},
            "status": {"type": "string", "enum": ["accepted", "overruled", "withdrawn"]},
            "resolution": {"type": "string", "description": "why — kept in the handoff"},
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        rid = (args.get("resolve") or "").strip()
        if rid:
            c = next((x for x in state.concerns if x.id == rid), None)
            if c is None:
                known = ", ".join(x.id for x in state.concerns) or "none"
                raise ToolError(f"no concern {rid!r} (known: {known}).")
            c.status = args.get("status", "accepted")
            c.resolution = args.get("resolution", "")
            session.touched("concern", rid)
            tail = f" — {c.resolution}" if c.resolution else ""
            return _confirm(f"Concern {rid} {c.status}{tail}.", session)
        claim = (args.get("claim") or "").strip()
        if not claim:
            raise ToolError("a concern needs a `claim`: what breaks, concretely.")
        severity = args.get("severity", "risk")
        if severity not in CONCERN_SEVERITIES:
            raise ToolError(f"severity must be one of {', '.join(CONCERN_SEVERITIES)}.")
        filed = session.file_concerns([{
            "severity": severity,
            "target": args.get("target", "design"),
            "claim": claim,
            "alternative": args.get("alternative", ""),
        }], source="model")
        if not filed:
            return _confirm("Already on the record — not filed twice.", session)
        c = filed[0]
        session.touched("concern", c.id)
        return _confirm(
            f"Recorded {c.id} [{c.severity}] against {c.target}. Say it in your reply — "
            "and if the user disagrees, resolve it as overruled with their reason.",
            session,
        )


class AmendTool(Tool):
    name = "amend_toplevel"
    description = (
        "Apply a component or connection change (same fields as those tools, incl. "
        "remove) together with a written reason. `component`/`connect` also work after "
        "approval and record an amendment automatically — reach for this one when the "
        "*why* matters and should be in the audit trail in your words."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "What is changing and why"},
            "component": {"type": "object", "description": "component-tool payload"},
            "connection": {"type": "object", "description": "connect-tool payload"},
        },
        "required": ["description"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        _guard_not_finalized(session)
        comp_args = args.get("component")
        conn_args = args.get("connection")
        if bool(comp_args) == bool(conn_args):
            raise ToolError("pass exactly one of `component` or `connection`.")
        if comp_args:
            cid = str(comp_args.get("id", "")).strip()
            if not cid:
                raise ToolError("component payload needs an id.")
            structural = bool(comp_args.get("remove")) or cid not in state.components or (
                "kind" in comp_args and comp_args["kind"] != state.components[cid].kind
            )
            action = _apply_component(session, comp_args, cid)
            changed = ("component", cid)
        else:
            structural = bool(conn_args.get("remove")) or not any(
                c.src == conn_args.get("src") and c.dst == conn_args.get("dst")
                for c in state.connections
            )
            action = _apply_connection(session, conn_args)
            changed = ("connection", f"{conn_args.get('src')}->{conn_args.get('dst')}")
        state.amendments.append(
            Amendment(turn=len(state.amendments) + 1,
                      description=args["description"], structural=structural)
        )
        if structural:
            state.compute_obligations()
        session.touched(*changed)
        note = " (structural — approval re-flagged; obligations recomputed)" if structural else ""
        return _confirm(f"Amendment recorded: {action}{note}", session)


def _concern_payload(concerns: list[Any]) -> list[dict[str, str]]:
    return [
        {"id": c.id, "severity": c.severity, "target": c.target,
         "claim": c.claim, "alternative": c.alternative, "source": c.source}
        for c in concerns
    ]


class ArchDoneTool(Tool):
    name = "done"
    description = (
        "Take the design to the user. Before their sign-off this requests top-level "
        "approval; after it, Finalize (writes the handoff bundle and ends the "
        "session).\n"
        "It never refuses because the design is unfinished. Whatever is still thin, "
        "still unanswered, or still objected to travels to the user with the request, "
        "and they decide. Don't call it to end a turn — a plain reply does that; call "
        "it when you genuinely want the user's ruling."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Short summary of where the design stands"},
        },
        "required": ["summary"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        state = session.state
        summary = args["summary"]
        if state.phase == "finalized":
            raise ToolError("the session is already finalized.")
        if not state.components:
            return _confirm(
                "Nothing is committed to the design yet, so there is nothing to approve. "
                "`promote` the sketch you and the user have landed on first — or just keep "
                "talking; a plain reply ends your turn.",
                session,
            )
        if _toplevel_locked(session):
            return self._finalize_gate(session, summary, ctx.kg)
        return self._toplevel_gate(session, summary)

    def _toplevel_gate(self, session: ArchSession, summary: str) -> ToolResult:
        state = session.state
        state.phase = "toplevel_review"
        session.touched()
        approved, feedback = session.request_gate({
            "kind": "toplevel_approval",
            "summary": summary,
            "thin": state.toplevel_missing(),
            "gaps": state.gaps(),
            "concerns": _concern_payload(state.open_concerns()),
            "questions": [q.question for q in state.blocking_questions()],
        })
        if not approved:
            state.phase = "propose"
            session.touched()
            return _confirm(
                f"The user wants changes before approving: {feedback or '(no details given)'}",
                session,
            )
        state.phase = "expand"
        state.compute_obligations()
        session.touched()
        queue = render.risk_ordered_pending(state)
        if queue:
            items = "; ".join(f"{o.component_id} ({o.reason})" for o in queue)
            return _confirm(
                f"Top level approved. Worth depth, riskiest first: {items}.", session,
            )
        return _confirm("Top level approved.", session)

    def _finalize_gate(self, session: ArchSession, summary: str, kg: Any = None) -> ToolResult:
        from .bundle import bundle_paths, write_bundle
        from .kg_seed import seed_kg

        state = session.state
        session.run_audit()  # deterministic pass; the model critic runs on its own
        blockers = state.open_blockers()
        state.phase = "resolved"
        session.touched()
        artifacts = [str(p) for p in bundle_paths(session.run_dir)] if session.run_dir else []
        approved, feedback = session.request_gate({
            "kind": "finalize",
            "summary": summary,
            "artifacts": artifacts,
            # everything the user should weigh before signing it off
            "blockers": _concern_payload(blockers),
            "concerns": _concern_payload([c for c in state.open_concerns() if c not in blockers]),
            "gaps": state.gaps(),
            "questions": [q.question for q in state.blocking_questions()],
            "obligations": [f"{o.component_id} ({o.facet})" for o in render.risk_ordered_pending(state)],
        })
        if not approved:
            state.phase = "expand"
            session.touched()
            return _confirm(
                f"The user wants changes instead of finalizing: {feedback or '(no details given)'}",
                session,
            )
        # approving with objections open is a decision, and it is recorded as one.
        # Re-check `open`: the gate is a snapshot, and the user can settle an
        # objection from the rail while it is up — that ruling is the real one.
        overruled = [c for c in blockers if c.open]
        for c in overruled:
            c.status = "overruled"
            c.resolution = feedback.strip() or "overruled by the user at the finalize gate"
        written = write_bundle(state, session.run_dir) if session.run_dir else []
        # the other half of the handoff: the markdown is what the builder reads,
        # the graph is what it can ask questions of eleven turns later
        seeded = seed_kg(kg, state, str(written[1]) if len(written) > 1 else "architecture.md")
        state.phase = "finalized"
        session.touched()
        paths = ", ".join(str(p) for p in written) or "(no run dir — nothing written)"
        tail = f" {len(overruled)} open blocker(s) overruled and recorded." if overruled else ""
        if seeded:
            tail += f" {seeded}."
        return ToolResult(
            output=f"Architecture finalized. Handoff bundle: {paths}.{tail} Next step: mha code.",
            details={"done": True, "artifacts": [str(p) for p in written]},
        )


class ImportStateTool(Tool):
    name = "import_state"
    description = (
        "Read the existing code knowledge graph and populate the design with the "
        "as-is architecture (components, connections, facets) for a feature or "
        "subsystem, so the render layer can display it and you can propose "
        "modifications against the loaded state. One-shot at session start: it "
        "refuses if the design already has components. Returns a transparency "
        "report of what was inferred and what is uncertain — review the low-"
        "confidence inferences and correct them with `component`/`connect`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "A feature name, file path, or symbol to scope the "
                               "import to (not always the whole codebase).",
            },
            "max_nodes": {
                "type": "integer",
                "description": f"Cap on subgraph size (default {DEFAULT_MAX_NODES}). "
                               "A truncation is reported, not an error.",
            },
            "max_depth": {
                "type": "integer",
                "description": f"BFS depth cap (default {DEFAULT_MAX_DEPTH}).",
            },
        },
        "required": ["scope"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_not_finalized(session)
        state = session.state
        scope = (args.get("scope") or "").strip()
        if not scope:
            raise ToolError("scope is required — a feature name, file path, or symbol.")

        # one-shot: refuse to clobber a design the model already started
        if state.components:
            raise ToolError(
                f"state already has {len(state.components)} component(s); import_state "
                "is a one-shot at session start. Start a new session to re-scope."
            )

        kg = ctx.kg
        if kg is None or not kg.is_ready():
            raise ToolError(
                "the knowledge graph is not ready; run `mha kg build` first, or "
                "describe the architecture from scratch."
            )

        max_nodes = int(args.get("max_nodes", DEFAULT_MAX_NODES))
        max_depth = int(args.get("max_depth", DEFAULT_MAX_DEPTH))
        subgraph = scope_subgraph(kg, scope, max_nodes=max_nodes, max_depth=max_depth)
        if not subgraph.nodes:
            raise ToolError(
                "scope query matched no KG nodes; try a file path, symbol name, "
                "or broader term."
            )

        result = reverse_seed(subgraph, scope)
        if not result.components:
            raise ToolError(
                "scope query matched no KG nodes; try a file path, symbol name, "
                "or broader term."
            )

        # import_state is the sole writer to arch-state for this step (the
        # reverse-seed→arch-state connection was collapsed: reverse_seed is pure).
        for comp in result.components:
            _check(state.validate_component, comp, False)
            state.components[comp.id] = comp
        for conn in result.connections:
            _check(state.validate_connection, conn)
            state.connections.append(conn)
        # a loaded design is no longer a blank sketch — move to propose so the
        # model can refine and the user can approve the top level.
        if state.phase == "brainstorm":
            state.phase = "propose"
        session.touched("import_state", scope)

        return _import_report(session, result, scope)


def _import_report(session: ArchSession, result: SeedResult, scope: str) -> ToolResult:
    """The transparency report: what loaded, what was inferred, what's thin."""
    state = session.state
    inferences = [
        {
            "component_id": i.component_id,
            "field": i.field,
            "value": i.value,
            "confidence": i.confidence,
            "evidence": i.evidence,
        }
        for i in result.inference_log
    ]
    gaps = state.gaps()
    low = [i for i in result.inference_log if i.confidence == "low"]
    parts = [
        f"Imported {len(result.components)} component(s) and {len(result.connections)} "
        f"connection(s) from the knowledge graph (scope: {scope!r}).",
        f"components: {', '.join(c.id for c in result.components)}",
    ]
    if low:
        parts.append(
            f"{len(low)} low-confidence inference(s) — review and correct with "
            "`component`/`connect`:"
        )
        for i in low[:12]:
            parts.append(f"  - {i.component_id}.{i.field} = {i.value} ({i.evidence})")
        if len(low) > 12:
            parts.append(f"  ... and {len(low) - 12} more (see details).")
    if gaps:
        parts.append(
            "thin: " + "; ".join(gaps[:12])
            + (f" ... and {len(gaps) - 12} more" if len(gaps) > 12 else "")
        )
    parts.append(
        "The loaded state is editable: set responsibilities, fix kinds, tighten "
        "connections, then `done` for top-level approval."
    )
    parts.append(f"next: {render._next_hint(state)}")
    return ToolResult(
        output="\n".join(parts),
        details={
            "loaded": len(result.components),
            "components": [c.id for c in result.components],
            "connections": len(result.connections),
            "inferences": inferences,
            "gaps": gaps,
            "concerns": [],
        },
    )


def arch_harness_tools(with_kg: bool = True, with_web: bool = True) -> list[Tool]:
    """The arch toolset: state mutation + research. No edit/write/bash —
    the model designs; it does not touch the repo."""
    tools: list[Tool] = [ReadTool()]
    if with_kg:
        tools.append(KgQueryTool())
    if with_web:
        tools.extend([WebSearchTool(), WebFetchTool()])
    tools.extend([
        ImportStateTool(),
        VariantTool(), NodeTool(), LinkTool(), SpliceTool(), DepthTool(), PromoteTool(),
        BriefTool(), ComponentTool(), ConnectTool(), FlowTool(), ExpandTool(),
        DecideTool(), ConcernTool(), AskTool(), AnswerTool(), AmendTool(), SkillTool(),
        ArchDoneTool(),
    ])
    return tools
