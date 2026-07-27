# ArchState — Architecture Harness State Schema

The single source of truth for an architecture session. Tools mutate it, the UI
renders it, phase gates read it, and the handoff bundle is serialized from it at
finalize. Nothing design-relevant lives outside this state: prose stays in the
chat transcript; if it isn't in a field, it doesn't survive the session.

Companion docs: `arch-ui-features.md` (what the page does),
`arch-remaining-work.md` (what is left). Handoff bundle schema: TBD (it is a
projection of this state plus the KG seed).

## Design principles

- **Structured mutation, not free-form authoring.** The model never writes
  documents or diagram source. It fills small typed schemas via tools; the
  harness renders Mermaid, the tracker, and the handoff deterministically.
- **The schema records; it does not gate.** Validation refuses only what would
  corrupt the graph (malformed id, edge to a component that does not exist).
  Conditionally-expected fields (async ⇒ mechanism, store ⇒ data_owned,
  production ⇒ failure_mode) are **gaps**: reported on the successful call, in
  the tracker, on the page and in the bundle, never raised. A design can be
  incomplete on purpose, and the model can argue that a gap does not matter.
- **Disagreement is state.** `Concern` records an objection — against the
  design, a decision, or the user's own instruction — with what breaks and the
  cheaper option. Overruled concerns are kept with the reason; that record is
  the most useful thing the code harness inherits.
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
brainstorm → propose → toplevel_review → expand → resolved → finalized
```

**Phases are a label for where the session is, not a lock.** No tool is refused
because of the phase; the only hard stop is `finalized`. They exist so the UI and
the tracker can say what is going on, and so post-approval edits know to record
an amendment.

1. **brainstorm** — the sketch layer: loose variants, free-form node kinds, no
   validation. Where fresh sessions open. The sketch layer stays available for
   the rest of the session; this is just the phase where nothing is promoted yet.
2. **propose** — a shape has been promoted into components/connections. Tighten
   it, add flows and decisions.
3. **toplevel_review** — the user is ruling on the top level. On approval the
   harness computes obligations.
4. **expand** — facets filled, in a suggested risk order (most expensive-to-change
   first). Structural edits still work and record an `Amendment`.
5. **resolved** — the finalize gate is open with the user.
6. **finalized** — user pressed Finalize; handoff bundle written; session ends.
   The one phase that locks every tool.

`intake` and `challenge` are gone (removed 2026-07-25). Nothing advanced into
them any more: the brief accretes through the whole session instead of gating
it, and critique is continuous rather than a phase. A state file written before
the overhaul still loads — `from_dict` maps the retired names through
`RETIRED_PHASES` (`intake` → `brainstorm`, `challenge` → `expand`) and the
migrated value is what gets saved back.

## Schema

### Top level

```python
@dataclass
class ArchState:
    mode: Literal["system", "feature"]          # greenfield vs brownfield delta
    phase: Literal["brainstorm", "propose", "toplevel_review",
                   "expand", "resolved", "finalized"]
    brief: Brief
    sketchbook: Sketchbook                      # the loose layer; see sketch.py
    components: dict[str, Component]            # flat, keyed by id
    connections: list[Connection]
    flows: list[Flow]
    decisions: list[Decision]
    questions: list[OpenQuestion]
    concerns: list[Concern]                     # objections + how they were settled
    obligations: list[Obligation]               # computed by harness; read-only to model
    amendments: list[Amendment]                 # post-approval top-level changes
```

`Component` carries `origin`: `"sketch:<variant_id>:<node_id>"` when it was
seeded by `promote`, `""` when hand-written. That is what makes promotion
re-runnable — a node already seeded is updated rather than duplicated — and what
`promote(replace=True)` uses to clear a superseded variant's shape.

```python
@dataclass
class Concern:
    id: str                                     # c1, c2, ...
    severity: Literal["blocker", "risk", "smell"]
    target: str                                 # component/decision id, "brief", "user", free text
    claim: str                                  # what breaks, concretely
    alternative: str = ""                       # the cheaper or safer option
    status: Literal["open", "accepted", "overruled", "withdrawn"] = "open"
    resolution: str = ""                        # why — survives into the bundle
    source: Literal["model", "judge", "harness_audit"] = "model"
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

Hard errors are marked **error**; everything else is reported as a gap on a
successful call.

| Tool | Writes | Refuses (error) / reports (gap) |
|---|---|---|
| `variant` / `node` / `link` / `splice` / `depth` | `sketchbook` | nothing — the loose layer is unvalidated. Available in every phase |
| `promote` | components + connections, `sketchbook` statuses | **error** if the variant is unknown or empty. Re-runnable; rivals stay live |
| `brief` | `brief` | gap: fields still unknown. Post-approval scope change ⇒ amendment |
| `component` | `components` | **error** on malformed id / unknown kind / removing something still referenced. gap: no trace, no responsibility, store without `data_owned`, production without `failure_notes` |
| `connect` | `connections` | **error** if either id is unknown. gap: async without `mechanism`, production without `failure_mode` |
| `flow` | `flows` | **error** if a step ref is unknown or there are no steps |
| `expand` | `components[id].facet` | **error** if the component is unknown or the facet body is empty. Suggests the riskier component, never enforces order |
| `decide` | `decisions` | **error** if `choice` ∉ options. gap: fewer than 2 options |
| `concern` | `concerns` | **error** without a `claim`, or resolving an unknown id. Deduped by claim across every status |
| `ask` / `answer` | `questions` | unresolved questions travel to both gates; they gate nothing |
| `amend_toplevel` | components/connections + `amendments` | available any time; structural ⇒ obligations recomputed |
| `done` | `phase` | **error** only if already finalized. Requests top-level approval, then Finalize, carrying thin/gaps/concerns/questions with it |

## User mutations (`POST /mutate`)

The page can change the architecture too, and when it does it goes through the
same code the tools do — `mutate.py` calls `_apply_component` / `_promote`, so
validation, gaps and the amendment trail are identical whichever end the edit
came from. An amendment made this way says `user edit — …`, because *who*
changed it is worth knowing later.

| op | Applies | Refuses |
|---|---|---|
| `component` | name, responsibility, tech, data_owned, failure_notes, trace | an unknown id, a blank name, a non-list trace, anything structural (`id`, `kind`, create, remove) |
| `concern` | status ∈ accepted/overruled/withdrawn + resolution | an unknown id, an open status, **overruled with no reason** |
| `promote` | the same seeding `promote` does, incl. `replace` | an unknown or empty variant |

A finalized session refuses all three. The response is `{"ok": …}` or a 400
with the message; the resulting state arrives on the `arch_state` stream like
every other change.

## Derived projections (pure functions of state, no model involvement)

- **Mermaid sources** — flowchart (components + connections, existing vs new
  styled distinctly in feature mode), sequence diagrams (flows), ER diagrams
  (store facets) → pushed to the UI as `arch_state` events.
- **Sketch diagrams** — every live variant as its own flowchart, plus which one
  is active, so the canvas has something to draw before anything is promoted.
- **Pinned tracker** — both layers at once (sketch variants + the design),
  open concerns, gaps, the obligation queue and unanswered questions;
  re-rendered into context every turn (same pattern as the plan tracker).
- **KG seed** — components/connections/facets serialized into the knowledge
  graph so the code harness's `kg_query` works from turn one on greenfield.
- **Handoff bundle** — top-level architecture doc + per-component contract
  sheets + decision log + **concerns raised** (including overruled ones and
  why) + **alternatives considered** (rejected variants and why) + **known
  gaps**, written at finalize. The code harness seeds from the markdown.
```
