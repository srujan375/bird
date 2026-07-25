"""ArchState — the single source of truth for an architecture session.

Tools mutate it, the UI renders it, phase gates read it, and the handoff
bundle is serialized from it at finalize. Implements docs/arch-state-schema.md
exactly; do not diverge. Validation raises ValueError with instructive text —
the tool layer converts those to ToolErrors verbatim.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .sketch import Sketchbook

Mode = Literal["system", "feature"]
Phase = Literal[
    "brainstorm",
    "intake", "propose", "toplevel_review", "expand", "challenge", "resolved", "finalized"
]
PHASES: tuple[str, ...] = (
    "brainstorm",
    "intake", "propose", "toplevel_review", "expand", "challenge", "resolved", "finalized"
)

KINDS: tuple[str, ...] = (
    "service", "api", "gateway", "store", "queue", "cache",
    "job", "ui", "llm", "external", "infra",
)
CONNECTION_KINDS: tuple[str, ...] = ("sync", "async", "batch")
FLOW_KINDS: tuple[str, ...] = ("happy", "failure", "background")
SCOPES: tuple[str, ...] = ("prototype", "internal", "production", "high_scale")
DECISION_CATEGORIES: tuple[str, ...] = (
    "storage", "communication", "consistency", "deployment", "integration", "llm", "other"
)
QUESTION_SOURCES: tuple[str, ...] = ("model", "harness_audit", "judge", "user")

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

# which facet variant each component kind owes when expanded
FACET_FOR_KIND: dict[str, str] = {
    "api": "api",
    "gateway": "api",
    "store": "store",
    "cache": "store",
    "queue": "queue",
    "service": "service",
    "job": "service",
    "ui": "service",
    "llm": "llm",
    "infra": "infra",
    # "external" deliberately absent: not ours to design
}


# ---------------------------------------------------------------- brief


@dataclass
class Scale:
    users: str | None = None
    reads_per_sec: str | None = None
    writes_per_sec: str | None = None
    data_volume: str | None = None
    growth: str | None = None

    def any_set(self) -> bool:
        return any(v for v in asdict(self).values())


@dataclass
class Brief:
    goal: str = ""
    actors: list[str] = field(default_factory=list)
    scope: str = ""
    scale: Scale = field(default_factory=Scale)
    latency: str | None = None
    consistency: str | None = None
    availability: str | None = None
    deploy_target: str | None = None
    constraints: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)

    def missing(self) -> list[str]:
        """Load-bearing fields still absent — the `component` unlock gate."""
        missing = []
        if not self.goal.strip():
            missing.append("goal")
        if not self.actors:
            missing.append("actors")
        if self.scope not in SCOPES:
            missing.append("scope")
        if self.scope in ("production", "high_scale"):
            if not self.scale.any_set():
                missing.append("scale")
            if self.consistency not in ("strong", "eventual", "mixed"):
                missing.append("consistency")
            if not (self.availability or "").strip():
                missing.append("availability")
        return missing


# ------------------------------------------------------------- structure


@dataclass
class Component:
    id: str
    name: str
    kind: str
    responsibility: str
    trace: list[str] = field(default_factory=list)
    existing: bool = False
    tech: str | None = None
    data_owned: str | None = None
    failure_notes: str | None = None
    facet: Any | None = None  # one of the *Facet classes; None = black box


@dataclass
class Connection:
    src: str
    dst: str
    label: str
    kind: str
    mechanism: str | None = None
    protocol: str | None = None
    data: str | None = None
    failure_mode: str | None = None


@dataclass
class FlowStep:
    src: str
    dst: str
    action: str
    note: str | None = None


@dataclass
class Flow:
    id: str
    name: str
    kind: str
    steps: list[FlowStep] = field(default_factory=list)


# ---------------------------------------------------------------- facets


@dataclass
class Endpoint:
    route: str
    method: str
    request: str
    response: str
    auth: str
    errors: list[str] = field(default_factory=list)
    idempotency: str | None = None
    pagination: str | None = None


@dataclass
class ApiFacet:
    endpoints: list[Endpoint] = field(default_factory=list)
    facet_kind: str = field(default="api", init=False)


@dataclass
class Entity:
    name: str
    keys: str
    fields: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)


@dataclass
class StoreFacet:
    entities: list[Entity] = field(default_factory=list)
    access_patterns: list[str] = field(default_factory=list)
    retention: str | None = None
    migration_risk: str | None = None
    facet_kind: str = field(default="store", init=False)


@dataclass
class QueueMessage:
    name: str
    schema: str
    ordering: str
    delivery: str
    dlq_policy: str | None = None


@dataclass
class QueueFacet:
    messages: list[QueueMessage] = field(default_factory=list)
    facet_kind: str = field(default="queue", init=False)


@dataclass
class Module:
    name: str
    purpose: str


@dataclass
class ServiceFacet:
    interface: list[str] = field(default_factory=list)
    modules: list[Module] | None = None
    facet_kind: str = field(default="service", init=False)


@dataclass
class LlmTask:
    name: str
    model_tier: str
    prompt_contract: str
    context_strategy: str
    fallback: str
    guardrails: str
    eval_hook: str | None = None
    cost_envelope: str | None = None


@dataclass
class LlmFacet:
    tasks: list[LlmTask] = field(default_factory=list)
    facet_kind: str = field(default="llm", init=False)


@dataclass
class DeployUnit:
    name: str
    components: list[str] = field(default_factory=list)
    scaling_policy: str = ""
    region: str | None = None


@dataclass
class InfraFacet:
    units: list[DeployUnit] = field(default_factory=list)
    state_locality: str = ""
    facet_kind: str = field(default="infra", init=False)


FACET_CLASSES: dict[str, type] = {
    "api": ApiFacet,
    "store": StoreFacet,
    "queue": QueueFacet,
    "service": ServiceFacet,
    "llm": LlmFacet,
    "infra": InfraFacet,
}

_FACET_ITEM_FIELDS: dict[str, tuple[str, type]] = {
    "api": ("endpoints", Endpoint),
    "queue": ("messages", QueueMessage),
    "llm": ("tasks", LlmTask),
}


# ------------------------------------- decisions / questions / audit trail


@dataclass
class Option:
    name: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)


@dataclass
class Decision:
    id: str
    topic: str
    category: str
    options: list[Option]
    choice: str
    rationale: str
    status: str = "decided"  # decided | deferred (deferred still records the default taken)


@dataclass
class OpenQuestion:
    id: str
    question: str
    blocking: bool
    source: str
    answer: str | None = None
    resolution: str | None = None  # answered | deferred | dropped

    @property
    def open(self) -> bool:
        return self.resolution is None


@dataclass
class Obligation:
    component_id: str
    facet: str
    reason: str
    status: str = "pending"  # pending | done | waived (waived only by the user)


@dataclass
class Amendment:
    turn: int
    description: str
    structural: bool


# ------------------------------------------------------------- the state


@dataclass
class ArchState:
    mode: str = "system"
    phase: str = "intake"
    brief: Brief = field(default_factory=Brief)
    sketchbook: Sketchbook = field(default_factory=Sketchbook)
    components: dict[str, Component] = field(default_factory=dict)
    connections: list[Connection] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    questions: list[OpenQuestion] = field(default_factory=list)
    obligations: list[Obligation] = field(default_factory=list)
    amendments: list[Amendment] = field(default_factory=list)

    # ---- gates ----

    def scope_is_production(self) -> bool:
        return self.brief.scope in ("production", "high_scale")

    def blocking_questions(self) -> list[OpenQuestion]:
        return [q for q in self.questions if q.blocking and q.open]

    def pending_obligations(self) -> list[Obligation]:
        return [o for o in self.obligations if o.status == "pending"]

    def happy_flows(self) -> list[Flow]:
        return [f for f in self.flows if f.kind == "happy"]

    def toplevel_missing(self) -> list[str]:
        """What the propose phase still owes before toplevel_review."""
        missing = []
        if not self.components:
            missing.append("at least one component")
        if not self.happy_flows():
            missing.append("a happy flow covering the primary goal")
        if not self.decisions:
            missing.append("at least one major decision with alternatives")
        # promoted-from-brainstorm components arrive as drafts (no trace / bare
        # responsibility); they must be tightened before the top level is approved.
        untightened = sorted(
            c.id for c in self.components.values()
            if not c.existing and (not c.trace or not c.responsibility.strip())
        )
        if untightened:
            missing.append("trace + responsibility for: " + ", ".join(untightened))
        return missing

    # ---- validation (ValueError text goes to the model verbatim) ----

    def validate_component(self, comp: Component, updating: bool) -> None:
        if not _ID_RE.match(comp.id):
            raise ValueError(
                f"component id {comp.id!r} must be kebab-case (lowercase letters, "
                "digits, hyphens; starts with a letter), e.g. 'worker-pool'."
            )
        if comp.kind not in KINDS:
            raise ValueError(f"unknown kind {comp.kind!r}; one of: {', '.join(KINDS)}.")
        if not comp.responsibility.strip():
            raise ValueError("responsibility is required: one sentence on what this component does.")
        if not comp.trace:
            raise ValueError(
                "trace is required: which brief goals/constraints does this component "
                "serve? A component that serves none is YAGNI — drop it or justify it."
            )
        if comp.kind == "store" and not (comp.data_owned or "").strip():
            raise ValueError("a store must declare data_owned: what data does it own?")
        if self.scope_is_production() and not (comp.failure_notes or "").strip():
            raise ValueError(
                f"scope is {self.brief.scope}: failure_notes is required — what happens "
                "when this component fails, and how is that contained?"
            )

    def validate_connection(self, conn: Connection) -> None:
        for ref in (conn.src, conn.dst):
            if ref not in self.components:
                raise ValueError(
                    f"connection references unknown component {ref!r}; add it with "
                    "`component` first."
                )
        if conn.kind not in CONNECTION_KINDS:
            raise ValueError(f"connection kind must be one of {', '.join(CONNECTION_KINDS)}.")
        if conn.kind == "async" and not (conn.mechanism or "").strip():
            raise ValueError(
                "an async connection must name its mechanism: which queue/bus/stream "
                "carries it?"
            )
        if self.scope_is_production() and not (conn.failure_mode or "").strip():
            raise ValueError(
                f"scope is {self.brief.scope}: failure_mode is required — what does "
                f"{conn.src} do when {conn.dst} is down?"
            )

    def validate_flow(self, flow: Flow) -> None:
        if flow.kind not in FLOW_KINDS:
            raise ValueError(f"flow kind must be one of {', '.join(FLOW_KINDS)}.")
        if not flow.steps:
            raise ValueError("a flow needs at least one step.")
        for s in flow.steps:
            for ref in (s.src, s.dst):
                if ref not in self.components:
                    raise ValueError(
                        f"flow step references unknown component {ref!r}; add it with "
                        "`component` first."
                    )

    def validate_decision(self, dec: Decision) -> None:
        if dec.category not in DECISION_CATEGORIES:
            raise ValueError(f"decision category must be one of {', '.join(DECISION_CATEGORIES)}.")
        if len(dec.options) < 2:
            raise ValueError(
                "a decision needs at least 2 options — a choice without alternatives "
                "is not a decision."
            )
        names = [o.name for o in dec.options]
        if dec.choice not in names:
            raise ValueError(f"choice {dec.choice!r} must match one option name: {names}.")

    def references_to(self, component_id: str) -> list[str]:
        """Human-readable list of things that would dangle if the id vanished."""
        refs = []
        for c in self.connections:
            if component_id in (c.src, c.dst):
                refs.append(f"connection {c.src} -> {c.dst}")
        for f in self.flows:
            if any(component_id in (s.src, s.dst) for s in f.steps):
                refs.append(f"flow {f.id!r}")
        return refs

    # ---- obligations (deterministic; the model can never write these) ----

    def compute_obligations(self) -> None:
        """Scope baseline + per-component risk signals. Called on top-level
        approval and after structural amendments; preserves done/waived
        statuses across recomputes."""
        previous = {(o.component_id, o.facet): o.status for o in self.obligations}
        critical: set[str] = set()
        for flow in self.happy_flows():
            for s in flow.steps:
                critical.update((s.src, s.dst))

        computed: list[Obligation] = []
        scope = self.brief.scope
        for comp in self.components.values():
            if comp.existing:
                continue  # brownfield background is frozen, not owed
            facet = FACET_FOR_KIND.get(comp.kind)
            if facet is None:
                continue
            reason = self._obligation_reason(comp, scope, comp.id in critical)
            if reason is None:
                continue
            status = previous.get((comp.id, facet), "pending")
            computed.append(Obligation(comp.id, facet, reason, status))
        self.obligations = computed

    @staticmethod
    def _obligation_reason(comp: Component, scope: str, on_critical_flow: bool) -> str | None:
        """Cost-of-change-later decides: schemas and public contracts always
        owe depth at production scope; stateless internals usually don't."""
        stateful = comp.kind in ("store", "cache")
        public = comp.kind in ("api", "gateway")
        backbone = comp.kind == "queue"
        if scope == "prototype":
            return None
        if scope == "internal":
            if stateful:
                return "stateful — schema changes are expensive even internally"
            if public and on_critical_flow:
                return "public surface on the critical flow"
            return None
        # production / high_scale
        if stateful:
            return f"stateful at {scope} scope — the schema is the hardest thing to change later"
        if public:
            return f"public contract at {scope} scope"
        if backbone:
            return f"async backbone at {scope} scope — message contracts and delivery semantics"
        if comp.kind == "llm":
            return f"llm task contracts at {scope} scope — prompts, fallbacks, cost envelope"
        if scope == "high_scale":
            if comp.kind == "infra":
                return "deployment topology at high scale"
            if on_critical_flow:
                return "on the critical flow at high scale"
        return None

    # ---- serialization ----

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # asdict flattens facet dataclasses fine (facet_kind rides along);
        # components stay a dict keyed by id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArchState":
        state = cls(mode=d.get("mode", "system"), phase=d.get("phase", "intake"))
        state.sketchbook = Sketchbook.from_dict(d.get("sketchbook"))
        b = d.get("brief", {})
        state.brief = Brief(
            goal=b.get("goal", ""),
            actors=list(b.get("actors", [])),
            scope=b.get("scope", ""),
            scale=Scale(**(b.get("scale") or {})),
            latency=b.get("latency"),
            consistency=b.get("consistency"),
            availability=b.get("availability"),
            deploy_target=b.get("deploy_target"),
            constraints=list(b.get("constraints", [])),
            non_goals=list(b.get("non_goals", [])),
        )
        for cid, c in (d.get("components") or {}).items():
            state.components[cid] = Component(
                id=c["id"], name=c.get("name", cid), kind=c["kind"],
                responsibility=c.get("responsibility", ""),
                trace=list(c.get("trace", [])),
                existing=bool(c.get("existing", False)),
                tech=c.get("tech"), data_owned=c.get("data_owned"),
                failure_notes=c.get("failure_notes"),
                facet=facet_from_dict(c.get("facet")),
            )
        state.connections = [Connection(**c) for c in d.get("connections", [])]
        state.flows = [
            Flow(
                id=f["id"], name=f.get("name", f["id"]), kind=f.get("kind", "happy"),
                steps=[FlowStep(**s) for s in f.get("steps", [])],
            )
            for f in d.get("flows", [])
        ]
        state.decisions = [
            Decision(
                id=x["id"], topic=x.get("topic", ""), category=x.get("category", "other"),
                options=[Option(**o) for o in x.get("options", [])],
                choice=x.get("choice", ""), rationale=x.get("rationale", ""),
                status=x.get("status", "decided"),
            )
            for x in d.get("decisions", [])
        ]
        state.questions = [OpenQuestion(**q) for q in d.get("questions", [])]
        state.obligations = [Obligation(**o) for o in d.get("obligations", [])]
        state.amendments = [Amendment(**a) for a in d.get("amendments", [])]
        return state


def facet_from_dict(d: dict[str, Any] | None) -> Any | None:
    if not d:
        return None
    kind = d.get("facet_kind")
    if kind == "api":
        return ApiFacet(endpoints=[Endpoint(**e) for e in d.get("endpoints", [])])
    if kind == "store":
        return StoreFacet(
            entities=[Entity(**e) for e in d.get("entities", [])],
            access_patterns=list(d.get("access_patterns", [])),
            retention=d.get("retention"),
            migration_risk=d.get("migration_risk"),
        )
    if kind == "queue":
        return QueueFacet(messages=[QueueMessage(**m) for m in d.get("messages", [])])
    if kind == "service":
        modules = d.get("modules")
        return ServiceFacet(
            interface=list(d.get("interface", [])),
            modules=[Module(**m) for m in modules] if modules is not None else None,
        )
    if kind == "llm":
        return LlmFacet(tasks=[LlmTask(**t) for t in d.get("tasks", [])])
    if kind == "infra":
        return InfraFacet(
            units=[DeployUnit(**u) for u in d.get("units", [])],
            state_locality=d.get("state_locality", ""),
        )
    raise ValueError(f"unknown facet_kind {kind!r}")
