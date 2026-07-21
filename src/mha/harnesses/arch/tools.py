"""The arch toolset — structured mutation of ArchState, gates as errors.

Follows plan.py's conventions: harness-owned state, the model mutates via
validated calls, gates return instructive ToolErrors that say what to do
instead. `done` is the universal phase gate (design ruling): in propose it
fires the toplevel_approval broker request; in expand (obligations closed)
it runs the challenge pass; in challenge/resolved it fires the finalize
request. Gate rejections return as same-turn tool errors carrying the
user's feedback.
"""

from __future__ import annotations

from typing import Any

from ...tools import Tool, ToolContext, ToolError, ToolResult
from ...tools.kg_query import KgQueryTool
from ...tools.files import ReadTool
from ...tools.skill import SkillTool
from ...tools.web import WebFetchTool, WebSearchTool
from . import render
from .session import ArchSession
from .state import (
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

POST_APPROVAL_PHASES = ("expand", "challenge", "resolved")


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
    if session.state.phase == "finalized":
        raise ToolError("the session is finalized — no further changes are possible.")


def _guard_toplevel_unlocked(session: ArchSession, what: str) -> None:
    """component/connect are propose-phase tools."""
    state = session.state
    _guard_not_finalized(session)
    if state.phase == "intake":
        missing = state.brief.missing()
        raise ToolError(
            f"{what} is locked until the brief has {', '.join(missing) or 'its required fields'} "
            "— call `brief` with what you know, and ask the user for load-bearing facts "
            "you don't have."
        )
    if state.phase in POST_APPROVAL_PHASES:
        raise ToolError(
            f"the top level is user-approved; {what} is locked. Route the change through "
            "`amend_toplevel` (it records an amendment and re-flags approval when structural)."
        )


def _confirm(action: str, session: ArchSession) -> ToolResult:
    return ToolResult(output=f"{action}\nnext: {render._next_hint(session.state)}")


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
        if state.phase in POST_APPROVAL_PHASES:
            raise ToolError(
                "the brief is settled (top level approved). Record a scope change as an "
                "amendment (`amend_toplevel`) or an open question (`ask`)."
            )
        b = state.brief
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
        if state.phase == "intake" and not missing:
            state.phase = "propose"
        session.touched()
        if missing:
            return _confirm(f"Brief updated; still missing: {', '.join(missing)}.", session)
        return _confirm("Brief complete — components unlocked.", session)


class ComponentTool(Tool):
    name = "component"
    description = (
        "Add, update, or remove a top-level component. Upserts by id (ids are "
        "immutable and kebab-case; rename via `name`). `remove: true` deletes it — "
        "connections and flows referencing it must be removed first. Locked after "
        "top-level approval (use amend_toplevel)."
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
        _guard_toplevel_unlocked(session, "`component`")
        cid = args["id"].strip()
        result = _apply_component(session, args, cid)
        session.touched("component", cid)
        return _confirm(result, session)


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
        for req in ("kind", "responsibility"):
            if not args.get(req):
                raise ToolError(f"a new component needs {req!r}.")
        comp = Component(
            id=cid,
            name=args.get("name", cid),
            kind=args["kind"],
            responsibility=args.get("responsibility", ""),
            trace=list(args.get("trace", [])),
            existing=bool(args.get("existing", False)),
            tech=args.get("tech"),
            data_owned=args.get("data_owned"),
            failure_notes=args.get("failure_notes"),
        )
        _check(state.validate_component, comp, False)
        state.components[cid] = comp
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
        _guard_toplevel_unlocked(session, "`connect`")
        result = _apply_connection(session, args)
        session.touched("connection", f"{args['src']}->{args['dst']}")
        return _confirm(result, session)


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
        if state.phase == "intake":
            raise ToolError("flows are locked until the brief is complete — call `brief` first.")
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
        # post-approval flow changes are additive behavior documentation —
        # allowed directly, but they leave an audit trail
        if state.phase in POST_APPROVAL_PHASES:
            state.amendments.append(
                Amendment(turn=len(state.amendments) + 1, description=action, structural=False)
            )
        session.touched("flow", fid)
        return _confirm(action, session)


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
        "Fill one component's facet (its internal contract) — locked until the top "
        "level is user-approved, then done ONE component at a time in the tracker's "
        "risk order. Pass the field group matching the component's kind: endpoints "
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
        if state.phase in ("intake", "propose", "toplevel_review"):
            raise ToolError(
                "expand is locked until the top level is approved — finish the top "
                "level and call `done` to request the user's approval."
            )
        cid = args["component_id"].strip()
        comp = state.components.get(cid)
        if comp is None:
            raise ToolError(f"component {cid!r} does not exist.")
        facet_kind = FACET_FOR_KIND.get(comp.kind)
        if facet_kind is None:
            raise ToolError(f"{cid} is kind {comp.kind!r} — not ours to design; no facet applies.")
        queue = render.risk_ordered_pending(state)
        owed = next((o for o in queue if o.component_id == cid), None)
        if queue and owed is not queue[0] and owed is not None:
            head = queue[0]
            raise ToolError(
                f"one component at a time, in risk order: expand {head.component_id!r} "
                f"first ({head.reason})."
            )
        hint, builder = _FACET_BUILDERS[facet_kind]
        facet = builder(args)
        primary = getattr(facet, ("endpoints", "entities", "messages", "interface",
                                  "tasks", "units")[
            ("api", "store", "queue", "service", "llm", "infra").index(facet_kind)])
        if not primary:
            raise ToolError(f"a {facet_kind} facet needs {hint}.")
        comp.facet = facet
        if owed is not None:
            owed.status = "done"
        session.touched("component", cid)
        return _confirm(f"Expanded {cid} ({facet_kind} facet).", session)


class DecideTool(Tool):
    name = "decide"
    description = (
        "Record an architectural decision: at least 2 real options with pros/cons, "
        "the choice (must match an option name), and the rationale. Upserts by id."
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Optional; auto-assigned d1, d2, ..."},
            "topic": {"type": "string", "description": "e.g. 'Message queue'"},
            "category": {"type": "string", "enum": list(DECISION_CATEGORIES)},
            "options": {
                "type": "array",
                "minItems": 2,
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
        "required": ["topic", "category", "options", "choice", "rationale"],
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
                            cons=list(o.get("cons", []))) for o in args["options"]],
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
        return _confirm(action, session)


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


class AmendTool(Tool):
    name = "amend_toplevel"
    description = (
        "The ONLY route to top-level edits after user approval: apply a component or "
        "connection change (same fields as those tools, incl. remove) with a "
        "description. Structural changes (add/remove/kind) re-flag the approval."
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
        if state.phase not in POST_APPROVAL_PHASES:
            raise ToolError(
                "the top level is not approved yet — edit it directly with "
                "`component`/`connect`; amend_toplevel is for post-approval changes."
            )
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


class ArchDoneTool(Tool):
    name = "done"
    description = (
        "Signal the current phase is complete. In propose this requests the user's "
        "top-level approval; after expand it runs the challenge pass; when all "
        "blocking questions are resolved it requests Finalize (which writes the "
        "handoff bundle and ends the session). Gates tell you what is still owed."
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
        if state.phase == "intake":
            raise ToolError(
                f"the brief still needs: {', '.join(state.brief.missing())}. Call `brief` "
                "(and ask the user) before anything else."
            )
        if state.phase == "propose":
            return self._toplevel_gate(session, summary)
        if state.phase == "expand":
            return self._challenge_or_finalize(session, summary)
        # challenge / resolved
        return self._finalize_gate(session, summary)

    def _toplevel_gate(self, session: ArchSession, summary: str) -> ToolResult:
        state = session.state
        missing = state.toplevel_missing()
        if missing:
            raise ToolError("the top level still owes: " + "; ".join(missing) + ".")
        state.phase = "toplevel_review"
        session.touched()
        approved, feedback = session.request_gate(
            {"kind": "toplevel_approval", "summary": summary}
        )
        if not approved:
            state.phase = "propose"
            session.touched()
            raise ToolError(
                f"The user requested changes to the top level: {feedback or '(no details)'} "
                "— address the feedback, then call done again."
            )
        state.phase = "expand"
        state.compute_obligations()
        session.touched()
        queue = render.risk_ordered_pending(state)
        if queue:
            items = "; ".join(f"{o.component_id} ({o.reason})" for o in queue)
            return ToolResult(
                output=f"Top level approved. Obligation queue (risk order): {items}. "
                       f"Start with expand(\"{queue[0].component_id}\")."
            )
        return self._challenge_or_finalize(session, summary)

    def _challenge_or_finalize(self, session: ArchSession, summary: str) -> ToolResult:
        state = session.state
        pending = render.risk_ordered_pending(state)
        if pending:
            items = "; ".join(f"{o.component_id} ({o.facet})" for o in pending)
            raise ToolError(
                f"obligations still pending: {items}. Expand them (or have the user "
                "waive them) before finishing."
            )
        findings = session.run_challenge()
        state.phase = "challenge"
        session.touched()
        if findings:
            items = "; ".join(f"{q.id}: {q.question}" for q in findings)
            raise ToolError(
                f"challenge pass found {len(findings)} finding(s): {items}. Address "
                "them (answer / ask the user / amend), then call done again."
            )
        return self._finalize_gate(session, summary)

    def _finalize_gate(self, session: ArchSession, summary: str) -> ToolResult:
        from .bundle import bundle_paths, write_bundle

        state = session.state
        blocking = state.blocking_questions()
        if blocking:
            items = "; ".join(f"{q.id}: {q.question}" for q in blocking)
            raise ToolError(
                f"blocking questions unresolved: {items}. Resolve them with `answer` "
                "(ask the user in your reply if you need them)."
            )
        state.phase = "resolved"
        session.touched()
        artifacts = [str(p) for p in bundle_paths(session.run_dir)] if session.run_dir else []
        approved, feedback = session.request_gate(
            {"kind": "finalize", "summary": summary, "artifacts": artifacts}
        )
        if not approved:
            raise ToolError(
                f"The user requested changes instead of finalizing: {feedback or '(no details)'} "
                "— address the feedback, then call done again."
            )
        written = write_bundle(state, session.run_dir) if session.run_dir else []
        state.phase = "finalized"
        session.touched()
        paths = ", ".join(str(p) for p in written) or "(no run dir — nothing written)"
        return ToolResult(
            output=f"Architecture finalized. Handoff bundle: {paths}. Next step: mha code.",
            details={"done": True, "artifacts": [str(p) for p in written]},
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
        BriefTool(), ComponentTool(), ConnectTool(), FlowTool(), ExpandTool(),
        DecideTool(), AskTool(), AnswerTool(), AmendTool(), SkillTool(),
        ArchDoneTool(),
    ])
    return tools
