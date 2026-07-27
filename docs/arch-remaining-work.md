# Architecture Harness — Remaining Work

Handover for what is left after the 2026-07-25 open-harness overhaul and the
React Workbench rebuild. Written for whoever picks this up next, including a
future me with no memory of the session.

**Status:** backend overhaul done and tested; Workbench phases 1, 3 and 4 (the
sketch layer, the mutation endpoint, the component dialog) done and
browser-verified; every §4 loose end closed. 415 python tests + 38 vitest green;
everything uncommitted.

Current and authoritative alongside this file:
- `docs/arch-state-schema.md` — the state, the tools, the wire.
- `docs/arch-ui-features.md` — what the page does. *(Rewritten 2026-07-25; the
  Mermaid-page version is gone. `arch-backend-handoff.md` was deleted the same
  day — a completed build brief whose protocol section had become false.)*

Historical, do not build against it:
- `Arch Canvas Handover.pdf` / `Arch harness React redesign/` — the designer's
  React direction. Still the visual reference for phases 2–6, but written
  before the overhaul, so it has **no sketch layer and no concerns**. Read it
  with §2 of this document beside it.

---

## 1. Where things stand

### What changed in the harness

The arch harness was gated: eight phases with hard locks, validation that
refused thin fields, and a `done` that would not proceed until a checklist was
satisfied. All of that is gone.

- **Validation refuses only what is broken** — a malformed id, an edge to a
  component that does not exist. Thinness (`no trace`, `store with no
  data_owned`, `async with no mechanism`) comes back as *advice* on a
  successful call, via `ArchState.gaps_by_subject()`.
- **No tool is phase-locked.** The only hard stop is `finalized`. Post-approval
  structural edits apply and record an `Amendment`.
- **`Concern` is a first-class part of state** — a recorded objection with
  severity (`blocker` / `risk` / `smell`), a target (a component, a decision,
  `"brief"`, or `"user"`), what breaks, and the cheaper option. Overruled
  concerns are kept with the reason.
- **The critic runs continuously**, on a daemon thread kicked from the tracker
  each turn, and files concerns of its own.
- **`done` never refuses.** It is two human gates and nothing else; whatever is
  thin, unanswered or objected-to travels to the user with the request.

### What exists in the UI

`arch-ui/` — Vite + React 18 + TS + `@xyflow/react` v12 + Zustand. Builds into
`src/ox/harnesses/arch/static/`, so no Python knows the UI was rewritten.

Built: two-layer canvas (Sketch / Design + variant tabs), layered auto-layout
that never moves a hand-placed node, overlay persistence per run id, rail
(Chat / Concerns / Decisions / Questions / Flows), both gates as non-blocking
banners, and every connection and turn state.

Added since: the **component dialog** with a sub-diagram per facet kind
(`src/dialog/`), and **user mutations** — inline editing, settling a concern
from the rail, promoting a sketch from the canvas — over `POST /mutate`.

---

## 2. The gap the design handover does not cover

**The reference mocks predate the overhaul.** If you build them literally you
will ship a page that cannot show half of what the harness now produces.

| Harness concept | In the mocks? | What the page must do |
|---|---|---|
| Sketch layer (`state.sketchbook`) | No | Draw loose variants; the session *opens* here, so a design-only canvas is blank for the first phase |
| Rival variants | No | Switchable, and archived rivals stay readable — `rejected_reason` is the ADR gold |
| `Concern` | No | Objections on the node, in the rail, and at the finalize gate |
| Gaps (thinness) | No | Advisory marks — never an error state, never blocking |
| Obligations | Yes | Unchanged |

The mocks' `1b` direction is named "Sketchbook", but that is a *visual*
direction (hand-drawn nodes), not the sketch data layer. Do not confuse them.

---

## 3. Remaining work

Ordered by what I would do next. Phase numbers follow the design handover's
build order so the two documents line up.

### ~~3.1 Component dialog + facet sub-diagrams (handover phase 4)~~ — **done**

`arch-ui/src/dialog/`. The dialog floats over a dimmed canvas — the system graph
never reflows, because node positions are user-owned and growing a card in place
would shove hand-placed neighbours. One shell (header, tabs, port chips naming
the real inbound/outbound neighbours, footer, resize grip, `Full canvas`), and
`facet.facet_kind` picks the body:

| Facet | Sub-diagram | Reads |
|---|---|---|
| `store` | ER canvas: entity cards, keys, indexes, inferred relations | `entities[]`, `access_patterns[]`, `retention`, `migration_risk` |
| `service` | module cards, unconnected on purpose | `modules[]`, `interface[]` |
| `queue` | message contract cards + DLQ, producers/consumers as ports | `messages[]` |
| `api` | endpoint table grouped by route | `endpoints[]` |
| `infra` | deployment frames holding component ids | `units[]`, `state_locality` |
| `llm` | task chain: prompt → context → guardrails → fallback | `tasks[]` |

Two things deliberately *not* drawn, because the schema does not record them:
edges between service modules (a `Module` is a name and a purpose — nothing says
what calls what), and ER relations as fact (they are inferred from field names
and the canvas says so).

Open with `⤢`, double-click, or `E`; `⎋` closes. A component with no facet opens
the same dialog on an `Internals` tab holding the empty state and an "Expand
this component" action that POSTs the instruction to `/input`. `external`
components say instead that their internals are not ours to design.

### ~~3.2 The mutation endpoint (handover phase 3)~~ — **done**

`POST /mutate` → `Server.on_mutate` → `ctx.arch.apply_mutation` →
`harnesses/arch/mutate.py`, which applies through the *same* `_apply_component`
/ `_promote` the model's tools call, so validation and the amendment trail are
identical whether a rename came from the architect or from the person reading
it. The pump stays generic: it duck-types `apply_mutation` on the harness state
object, and a session that has none (code, a plain REPL) declines politely.

Three ops — `component` (prose fields only; `id` is immutable and `kind` is
structural), `concern` (accept / overrule / withdraw), `promote` (with
`replace` for switching shapes). Creating and deleting stay agent-only, per §6.2.

The reply is the verdict, not the state: state travels on the `arch_state` SSE
push that `touched()` already makes, and a second copy of it over a different
wire is a second thing to keep in sync.

Client-side, an edit applies optimistically and rolls back visibly with an
inline rail notice if the harness refuses. Overruling an objection **requires**
the reason, in the UI and again on the server — that sentence is what the code
harness inherits, and a placeholder in its place is worth less than nothing.

### 3.3 Canvas tooling (handover phase 2)

Left tool rail (52px), sticky notes, text annotations, freehand layer, `⌘K`
command palette. All client-only — annotations and notes never leave the
browser, and are not in the handoff bundle. Offer "send as a question" to
promote a note into an `ask`.

Open question from the design handover §11, still unanswered: do annotations
belong in the session log at all, or are they noise the harness should not
carry?

### 3.4 L2 module chains (handover phase 5) — **needs a decision first**

`Module` is `name: str; purpose: str`. That is not enough to draw a chain.

- **Author it** — add optional `steps: list[ModuleStep]` and `reads: list[str]`
  to `Module`, filled by a scoped `expand_module` tool. Deterministic and
  immediate; costs the model a few more tokens on risky modules.
- **Derive it** — leave the schema alone, build L2 from the knowledge graph
  once code exists. Free at design time, empty on greenfield — which is exactly
  when the user wants it.

**Recommendation: author it**, gated on risk the same way facet obligations
are — only modules on a critical flow, or holding rules expensive to get wrong.
Until this lands, render the module card with its purpose and *no* L2
affordance. Do not fake the chain.

### 3.5 Polish (handover phase 6)

Flow playback (step through a `Flow` on the canvas), PNG export.

---

## 4. Backend loose ends — all closed 2026-07-26

- ~~**Stale docs.**~~ Done: `arch-ui-features.md` rewritten as what the page
  actually does; `arch-backend-handoff.md` deleted (a build brief for work that
  shipped, whose protocol section had gone false).
- ~~**Vestigial phases.**~~ Removed. `intake` and `challenge` are gone from
  `PHASES`; `from_dict` migrates a pre-overhaul state file through
  `RETIRED_PHASES` (`intake` → `brainstorm`, `challenge` → `expand`). The
  default phase is now `brainstorm`, so the two bring-up paths no longer set it
  by hand, and adding the first component is what leaves the sketch layer —
  the brief gates nothing and no longer moves the session.
- ~~**KG seed never built.**~~ Built: `harnesses/arch/kg_seed.py` +
  `KG.seed()`. At finalize the design goes into the graph — a node per
  component, entity, endpoint, message, module, task and deploy unit, edged by
  the real connections and flow steps — so a greenfield `ox code` can
  `kg_query` the architecture on turn one. Labels carry the words someone would
  search for (`query()` matches labels only); `source_location` reads
  `design:<id>` so a hit is never mistaken for discovered code. A later full
  `KG.build()` drops them, which is right: by then the code exists.
- ~~**The server dies ~0.3s after finalize.**~~ `HttpTransport(linger=…)`:
  `stop_when` now starts a read window instead of pulling the plug, and the
  server exits when the last page closes (noticed within ~1s — the linger pokes
  each SSE client so a gone tab doesn't wait on the 15s keepalive) or after 30
  minutes. `ox arch` uses it; the lead's `run_arch_interactive` deliberately
  does not, because a caller is blocked on the return.
- ~~**No automated tests for the React app.**~~ 38 vitest tests over the parts
  where the logic actually lives — see §5.

---

## 5. Working on the UI

```bash
cd arch-ui
npm install
npm run build     # -> src/ox/harnesses/arch/static/  (tsc --noEmit runs first)
npm run dev       # vite dev server; set OX_URL to a live `ox arch` port
npm test          # vitest, ~0.2s
npm run check     # tsc --noEmit && vitest run — the pre-commit pair
```

### The frontend tests

Vitest, no DOM: the tests cover the three places the page keeps its logic, and
skip the rendering, which Playwright covers better.

- `src/layout.test.ts` — the one promise layout makes: **a node the user placed
  is never moved**, however the graph grows around it. Plus idempotence, cycles,
  and Tidy up discarding pins.
- `src/dialog/graphs.test.ts` — facet → sub-diagram. The load-bearing tests are
  the ones asserting a *missing* edge: no invented flow between service modules,
  no ER relation for a field nothing matches. An invented arrow reads exactly
  like a designed one.
- `src/store/session.test.ts` — the event vocabulary, and every mutation path:
  optimistic apply, rollback on refusal, rollback on an unreachable harness, and
  *not* clobbering a state push that landed while an edit was in flight.

All four of those invariants were mutation-checked: break the source line and
the suite goes red. That is the bar for adding one here — a test that passes
against broken code is worse than no test, because it reads like cover.

**The built output is committed on purpose.** `ox arch` must work for someone
who has never installed Node, so `static/index.html` + `static/assets/*` are
artifacts in the repo. `tests/test_packaging.py` fails if they are missing or
if `index.html` references filenames that are not there — rebuild and commit
both together, always.

### Verifying against a real session

Do not mock the event stream. `scripts/arch_devserver.py` runs the **real**
`Runner` / `HttpTransport` / `ArchSession` with a scripted `FakeClient` and a
fake judge, on a fixed port — genuine replay, gates, mid-turn pushes and
threading:

```bash
python3 scripts/arch_devserver.py 8766     # state goes to a temp dir, not the repo
```

The script walks two rival sketches → an objection against the user's own
request → promote → the top-level gate → depth on all six facet kinds → a
finalize gate with a blocker still open, so every sub-diagram the component
dialog can draw is reachable. `PAUSE` at the top slows the fake model down
enough to watch mid-turn behaviour.

Two traps found the hard way:

1. **Playwright's `dragTo` does not drive React Flow.** d3-drag needs
   intermediate pointer moves; a single move does nothing. Use an explicit
   `mouse.down()` → stepped `mouse.move()` loop → `mouse.up()`.
2. **Do not send `/input` before `turn_end`.** The server correctly rejects a
   second turn with "a turn is already running"; wait for the turn to close.

### The acceptance checks that matter

From the design handover §10, plus the two the overhaul adds:

- Move three nodes, then let the agent add two more — the three stay exactly
  where they were put. *(verified)*
- Refresh mid-session: transcript, state, positions, viewport all come back.
  *(verified)*
- Kill the server: veil appears, input disabled, **canvas still pans**.
  *(verified — the veil must be `pointer-events: none`)*
- Finalize: canvas read-only, bundle paths shown, no edit affordance, and no
  disconnect veil when the server exits. *(verified)*
- Sketch layer draws during brainstorm, before anything is promoted.
  *(verified)*
- Overruling a blocker at the finalize gate records **the user's typed reason**
  in `architecture.md`, not a generic fallback. *(verified)*
- Open a component dialog mid-turn: it fills as tools land and the graph behind
  it does not shift. *(verified — every node transform identical across six
  `expand` calls, dialog never closed)*
- Rename a component: the id in every connection, flow and the bundle is
  unchanged. *(verified)*
- A refused edit rolls back: blanking a name reverts the card and the header,
  and drops `✗ a component needs a name.` in the rail. *(verified)*
- Settle an objection from the rail while the finalize gate is open: the ruling
  sticks. The gate carries a *snapshot* of the open blockers, and used to
  rewrite an accepted one as `overruled` on approval —
  `tests/test_arch_concerns.py::test_a_blocker_settled_while_the_gate_is_up_is_not_overruled_anyway`.
  *(fixed + verified)*

---

## 6. Decisions still open

1. **L2: author or derive** (§3.4). Gates phase 5. Recommendation: author.
2. **Can the user draw new components/connections on the canvas in v1**, or
   stay agent-only? Current answer: agent-only — the user edits what exists,
   and `/mutate` enforces it (no create, no remove).
3. ~~**Should the component dialog be dockable** into the right rail?~~ Built
   floating, with `Full canvas` promoting it to the whole stage — the two sizes
   the handover asked for. Docking into the 380px rail is still unbuilt; if
   someone wants side-by-side reading, that is the change.
4. **Do annotations belong in the session log** (§3.3)?
