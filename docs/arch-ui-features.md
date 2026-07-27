# The Architecture Workbench — what the page does

The page `mha arch` serves. Companion to `docs/arch-state-schema.md`, which owns
the state and the wire; this document owns behaviour — what the user sees, what
they can change, and which of it never leaves the browser.

Source: `arch-ui/` (Vite + React 18 + TS + `@xyflow/react` + Zustand), built into
`src/mha/harnesses/arch/static/`. **The build output is committed on purpose** —
`mha arch` has to work for someone who has never installed Node. Rebuild and
commit `index.html` and `assets/*` together; `tests/test_packaging.py` fails if
they drift apart.

> Rewritten 2026-07-25. The version before this described a Mermaid-rendering
> page with a phase-gated `done`; both are gone. Mermaid is still generated
> server-side, but only for the handoff bundle and the tests — the page draws
> from structured state, which is what makes a node clickable and editable.

## The one principle

`ArchState` is the single source of truth. The canvas is a **projection** of it
plus one client-owned overlay. Anything the user changes that belongs to the
architecture is sent back as the tool-equivalent mutation; anything that is only
about *looking at* the architecture never leaves the browser.

| Owned by the server (`store/session.ts`) | Owned by the browser (`store/canvas.ts`) |
|---|---|
| components, connections, flows, decisions, questions, concerns, obligations, the sketchbook | node positions and pins, viewport, which layer/variant is showing, rail tab, dialog size + which component is open |
| replaced wholesale by every `arch_state` event | persisted to `localStorage["mha_arch_canvas:<run_id>"]`, never touched by an event |

That split is why a mid-turn state push can add five components without moving a
card the user dragged, and why a refresh comes back to the same viewport.

## Shell

Top bar (44px): goal · Sketch/Design switcher · phase · Tidy up · model · repo ·
connection dot. Centre: the canvas. Right rail (380px): Chat · Concerns ·
Decisions · Questions · Flows. No page scroll — every region owns its own.

## The two layers

The harness keeps a loose **sketch** layer and a strict **design** layer live at
the same time, so the page draws both:

- **Sketch** — the active variant's nodes and links, drawn provisionally
  (dashed, dimmer). Rival variants are tabs; an archived one stays readable
  because its `rejected_reason` is the ADR gold. A `use this shape` button
  promotes the variant on the canvas without a model turn.
- **Design** — components and connections. `existing: true` renders dashed and
  dimmed (brownfield background). A pending obligation puts a dot on the card;
  thinness puts a `thin · N` pill on it; an open concern puts a severity dot.

The switcher follows the session (design once anything is promoted) until the
user picks a side, after which their choice sticks. Both layers keep their own
positions, because ids can collide across them.

## Layout, and why nothing moves

`layout.ts` is a small layered layout (longest-path layering + two barycentre
sweeps) — no ELK, no dependency. It places **only ids it has never seen**,
treating everything already positioned as an immovable obstacle. Dropping a node
pins it. `Tidy up` is the only thing that ever re-flows the graph, and it is a
button, never automatic.

## The component dialog

Opening a component (`⤢`, double-click, or `E`) mounts a dialog over a dimmed
canvas. **The system graph never reflows** — positions are user-owned, so growing
a card in place would shove hand-placed neighbours, and internals need more room
than a card's footprint anyway.

One shell — header, facet-dependent tabs, dashed port chips naming the real
inbound/outbound neighbours (click one to walk there), footer, resize grip,
`Full canvas` — and `facet.facet_kind` picks the body:

| Facet | Body |
|---|---|
| `store` | ER canvas: entity cards with keys, fields, indexes |
| `service` | module cards |
| `queue` | message contract cards, each with its DLQ |
| `api` | endpoint table grouped by route |
| `infra` | deployment frames holding component ids |
| `llm` | task chain: prompt → context → guardrails → fallback |

Every body renders straight from `arch_state`, so a dialog left open while the
architect runs `expand` fills in underneath the user without closing.

Two things are deliberately **not** drawn, because the schema does not record
them: edges between service modules (a `Module` is a name and a purpose —
nothing says what calls what), and ER relations as fact (they are inferred from
`*_id` field names, and the canvas says so). Drawing them would be inventing
architecture, which is the one thing this page must never do.

A component with no facet opens the same dialog on an `Internals` tab: its
responsibility, why it owes depth if it does, and an **Expand this component**
action that posts the instruction to `/input`. An `external` component says
instead that its internals are not ours to design.

## What the user can change

Edits apply optimistically and then POST to `/mutate`. If the harness refuses,
the canvas rolls back visibly and the reason lands in the rail — the page must
never show something the harness does not believe.

| Action | Sent as | Notes |
|---|---|---|
| Move a node | nothing | overlay only; pins it |
| Rename · edit responsibility, tech, data owned, trace, failure notes | `component` | ids are immutable; a rename changes `name` only |
| Accept / overrule a concern | `concern` | **overruling requires the reason** — that sentence is what the code harness inherits |
| Promote a sketch variant | `promote` | replacing an already-seeded shape asks twice |
| Expand a component | `/input` | it is a request to the architect, not a mutation |

Creating and deleting components stay agent-only in v1: the user edits what
exists and asks for the rest.

## Events

One SSE connection to `/events`; POSTs to `/input`, `/permission`, `/interrupt`,
`/mutate`. Late joiners are replayed by the server, so a refresh rebuilds
everything from scratch.

| Event | Page behaviour |
|---|---|
| `ready` | model, repo, run id; restore this run's overlay |
| `assistant_delta` | append to the in-flight message, progressively |
| `harness_event` tool calls | one compact activity line per call (`+ component`, `→ connect`, `◆ decide`, `▸ expand`, `⚑ concern`, `⌕ kg_query`) |
| `arch_state` | replace server truth wholesale; ring what `changed` for ~2s; never re-fit, never move a placed node; open dialogs refresh in place |
| `permission_request` | a **banner**, not a modal — the canvas stays visible and the composer stays live, because replying *is* "request changes" |
| `turn_end` | close the message, show status, re-enable input |
| `error` · `bye`/drop | inline notice; disconnect veil that explains itself and **never captures the pointer**, so the canvas still pans |

## Gates

Both human gates are banners above the composer. The finalize gate lists open
blockers and says plainly that finalizing records them as overruled with
whatever the user types — and what they type is what gets recorded, verbatim.

Approving is one button; replying instead is requesting changes. Neither gate
blocks reading the design.

## States to keep working

Connecting (first turn already streaming) · turn in progress (text streaming
while nodes land) · idle · empty sketch · empty design · dialog open, filled ·
dialog open, empty facet · dialog filling live mid-turn · pending mutation ·
rejected mutation rolled back · top-level gate · finalize gate · interrupted ·
turn error · disconnected · finalized read-only.

**Finalized** means: the canvas still pans, zooms and opens dialogs; every edit
affordance and the composer are gone; the rail shows the bundle paths and
`mha code` as the next step. A finalized session is never shown as disconnected,
even after the server exits.

## Not built

Left tool rail, sticky notes, annotations, freehand, `⌘K` palette (handover
phase 2) · L2 module chains (phase 5, needs a schema delta) · flow playback and
PNG export (phase 6). See `docs/arch-remaining-work.md`.
