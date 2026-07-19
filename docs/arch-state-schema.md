# ArchState — Architecture Harness State Schema

The single source of truth for an architecture session. Tools mutate it, the UI
renders it, phase gates read it, and the handoff bundle is serialized from it at
finalize. Nothing design-relevant lives outside this state: prose stays in the
chat transcript; if it isn't in a field, it doesn't survive the session.

Companion docs: `arch-ui-features.md` (page behavior). Handoff bundle schema: TBD
(it is a projection of this state plus the KG seed).

## Design principles

- **Structured mutation, not free-form authoring.** The model never writes
  documents or diagram source. It fills small typed schemas via tools; the
  harness renders Mermaid, the tracker, and the handoff deterministically.
- **The checklist lives in the schema.** Required and conditionally-required
  fields (async ⇒ mechanism, store ⇒ data_owned, production ⇒ failure_mode)
  encode architectural discipline as validation errors the model must fix —
  not as prompt exhortation.
- **Depth is risk-driven and stops at contracts.** Every component's contract
  is pinned; internals (facets) are owed only where scope + risk demand it,
  tracked as harness-computed obligations.
- **Breadth before depth.** Top level (components, connections, flows,
  key decisions) is settled and user-approved before any `expand`.
- **Flat, one level of depth.** Components form a flat dict; depth is a facet
  on a component, not a recursive tree. One zoom level (system → component
  internals) covers the useful C4 range.
- **Ids are immutable.** Model-chosen, kebab-case, unique. Renames change
  `name`, never `id`, so connections, flows, and the KG seed never dangle.

## Session phases

```
intake → propose → toplevel_review → expand → challenge → resolved → finalized
```

1. **intake** — `brief` filled; blocking questions asked. `component` is locked
   until required brief fields exist.
2. **propose** — full top-level: components, typed connections, key flows,
   major decisions. `expand` is locked.
3. **toplevel_review** — user approves the high level (same broker mechanism as
   finalize, lighter weight). On approval the harness computes obligations.
4. **expand** — facets filled one component at a time, in harness-chosen risk
   order (most expensive-to-change first). Each expansion is a scoped mini-loop
   with its own pattern card and mini-challenge. Top-level edits now route
   through `amend_toplevel` only.
5. **challenge** — harness coverage audit + judge-model critique (breakage under
   stated scale, and a simplification pass: what can be merged/deleted).
   Findings land as open questions.
6. **resolved** — no pending obligations, no unresolved blocking questions.
7. **finalized** — user pressed Finalize; handoff bundle written; session ends.

## Schema

### Top level

```python
@dataclass
class ArchState:
    mode: Literal["system", "feature"]          # greenfield vs brownfield delta
    phase: Literal["intake", "propose", "toplevel_review",
                   "expand", "challenge", "resolved", "finalized"]
    brief: Brief
    components: dict[str, Component]            # flat, keyed by id
    connections: list[Connection]
    flows: list[Flow]
    decisions: list[Decision]
    questions: list[OpenQuestion]
    obligations: list[Obligation]               # computed by harness; read-only to model
    amendments: list[Amendment]                 # post-approval top-level changes
```

### Brief — the intake contract

```python
@dataclass
class Scale:
    users: str | None                           # "10k MAU", "internal team"
    reads_per_sec: str | None
    writes_per_sec: str | None
    data_volume: str | None                     # "~50GB, +1GB/mo"
    growth: str | None

@dataclass
class Brief:
    goal: str
    actors: list[str]                           # who/what uses the system
    scope: Literal["prototype", "internal", "production", "high_scale"]
    scale: Scale
    latency: str | None                         # "p95 < 200ms on read path"
    consistency: Literal["strong", "eventual", "mixed"] | None
    availability: str | None                    # "99.9"
    deploy_target: str | None                   # cloud / on-prem / serverless / single box
    constraints: list[str]                      # tech mandates, budget, team, compliance
    non_goals: list[str]
```

Gate rule: `goal` + `actors` + `scope` always required before `component`
unlocks. `scale`, `consistency`, `availability` additionally required when
`scope` is `production` or `high_scale`. Missing load-bearing fields must be
asked of the user (via `ask`), not assumed.

### Components and connections — structure

```python
Kind = Literal["service", "api", "gateway", "store", "queue", "cache",
               "job", "ui", "llm", "external", "infra"]

@dataclass
class Component:
    id: str                                     # kebab-case, unique, stable
    name: str
    kind: Kind
    responsibility: str                         # one sentence; always required
    trace: list[str]                            # brief goals/constraints it serves — the YAGNI field
    existing: bool = False                      # brownfield import; frozen background in feature mode
    tech: str | None = None
    data_owned: str | None = None               # REQUIRED when kind == "store"
    failure_notes: str | None = None            # required at production scope
    facet: Facet | None = None                  # None = black box; filled by expand()

@dataclass
class Connection:
    src: str                                    # validated component id
    dst: str                                    # validated component id
    label: str
    kind: Literal["sync", "async", "batch"]
    mechanism: str | None                       # REQUIRED when async: which queue/bus carries it
    protocol: str | None                        # http / grpc / sql / amqp …
    data: str | None                            # what crosses the edge
    failure_mode: str | None                    # required at production scope: behavior when dst is down
```

### Flows — behavior

```python
@dataclass
class FlowStep:
    src: str                                    # component id, validated
    dst: str                                    # component id, validated
    action: str                                 # "POST /orders", "publish OrderPlaced"
    note: str | None

@dataclass
class Flow:
    id: str
    name: str                                   # "place order", "token refresh"
    kind: Literal["happy", "failure", "background"]
    steps: list[FlowStep]
```

Gates: at least one `happy` flow covering the primary goal before
`toplevel_review`; at production scope each happy flow wants a failure twin
(nudged, judge-checked).

### Facets — depth, typed by component kind

Tagged union; `expand(component_id)` fills the variant matching the component's
kind. Locked until the top level is approved.

```python
@dataclass
class Endpoint:
    route: str; method: str
    request: str; response: str                 # shape descriptions / example payloads
    auth: str; errors: list[str]
    idempotency: str | None; pagination: str | None

ApiFacet:     endpoints: list[Endpoint]

@dataclass
class Entity:
    name: str; keys: str; fields: list[str]; indexes: list[str]

StoreFacet:   entities: list[Entity]
              access_patterns: list[str]        # "lookup orders by user, newest first"
              retention: str | None
              migration_risk: str | None

@dataclass
class Message:
    name: str; schema: str; ordering: str
    delivery: str                               # at-least-once / at-most-once / exactly-once claim
    dlq_policy: str | None

QueueFacet:   messages: list[Message]

ServiceFacet: interface: list[str]              # what it exposes to siblings
              modules: list[Module] | None      # internal layout — optional; usually code harness's job

@dataclass
class LlmTask:
    name: str; model_tier: str
    prompt_contract: str                        # inputs → expected output shape
    context_strategy: str                       # what goes in the window and why
    fallback: str; guardrails: str
    eval_hook: str | None; cost_envelope: str | None

LlmFacet:     tasks: list[LlmTask]

@dataclass
class DeployUnit:
    name: str; components: list[str]            # component ids hosted
    scaling_policy: str; region: str | None

InfraFacet:   units: list[DeployUnit]
              state_locality: str
```

Renderings: `StoreFacet` → Mermaid ER diagram; `ApiFacet`/`QueueFacet` →
tables; `ServiceFacet`/`InfraFacet` → nested flowchart / deployment view.

### Decisions, questions, obligations, amendments

```python
@dataclass
class Option:
    name: str; pros: list[str]; cons: list[str]

@dataclass
class Decision:
    id: str
    topic: str
    category: Literal["storage", "communication", "consistency",
                      "deployment", "integration", "llm", "other"]
    options: list[Option]                       # >= 2
    choice: str                                 # must match an option name
    rationale: str
    status: Literal["decided", "deferred"]      # deferred still records the default taken

@dataclass
class OpenQuestion:
    id: str
    question: str
    blocking: bool                              # blocking + unresolved ⇒ no finalize
    source: Literal["model", "harness_audit", "judge", "user"]
    answer: str | None
    resolution: Literal["answered", "deferred", "dropped"] | None

@dataclass
class Obligation:                               # computed from scope + risk; model cannot write
    component_id: str
    facet: str                                  # which facet is owed
    reason: str                                 # "stateful + on critical flow", "public surface"
    status: Literal["pending", "done", "waived"]  # waived only by the user

@dataclass
class Amendment:                                # audit trail of post-approval top-level edits
    turn: int
    description: str
    structural: bool                            # structural ⇒ UI flags for re-approval
```

Obligation computation inputs: `brief.scope` baseline + per-component risk
signals — on a critical flow, stateful, externally exposed, novel tech, and
above all cost-of-change-later (DB schemas and public API contracts always owe
facets at production scope; stateless service internals usually don't).

## Tool → state mapping

| Tool | Writes | Key validations / gates |
|---|---|---|
| `brief` | `brief` | unlocks `component` when required fields present |
| `component` | `components` | unique immutable id; `trace` non-empty; kind-conditional fields |
| `connect` | `connections` | both ids exist; async ⇒ `mechanism` |
| `flow` | `flows` | all step refs exist |
| `expand` | `components[id].facet` | locked until top-level approved; facet type matches kind |
| `decide` | `decisions` | ≥ 2 options; `choice` ∈ options |
| `ask` / `answer` | `questions` | blocking + unresolved gates finalize |
| `amend_toplevel` | components/connections + `amendments` | sole route to top-level edits post-approval; structural ⇒ re-approval flag |
| `done` | `phase → finalized` | gated on user Finalize; no pending obligations; no blocking questions |

## Derived projections (pure functions of state, no model involvement)

- **Mermaid sources** — flowchart (components + connections, existing vs new
  styled distinctly in feature mode), sequence diagrams (flows), ER diagrams
  (store facets) → pushed to the UI as `arch_state` events.
- **Pinned tracker** — phase, obligation queue, unresolved blocking questions,
  checklist coverage; re-rendered into context every turn (same pattern as the
  plan tracker).
- **KG seed** — components/connections/facets serialized into the knowledge
  graph so the code harness's `kg_query` works from turn one on greenfield.
- **Handoff bundle** — top-level architecture doc + per-component contract
  sheets + decision log + KG seed, written at finalize. Schema TBD.
```
