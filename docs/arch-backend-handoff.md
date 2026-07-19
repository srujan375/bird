# Architecture Harness — Backend Implementation Handoff

Implementation handoff for the `mha arch` backend. **The frontend design is still
in progress and will be attached separately when ready** — build the backend
against the protocol contract in this document, not against any particular page.
Any FE that speaks this protocol must work; a bare test page or curl is enough to
verify the backend until the designed page lands.

Authoritative companion docs (read both before starting):

- `docs/arch-state-schema.md` — ArchState, session phases, tools, gates,
  validations, derived projections. **This is the contract; do not diverge.**
- `docs/arch-ui-features.md` — what the page functionally does (context for the
  protocol; the visual design attached later must not change the protocol).

## Context: what exists and gets reused

- `src/mha/harness/runner.py` — the ReAct loop (streaming, retries, validation,
  compaction, stuck guards). **Unchanged.** The arch harness is a new
  instructions file + tool set on the same Runner, run in chat mode.
- `src/mha/serve.py` — `Server` (event pump, worker-thread turns, interrupts)
  and `PermissionBroker` (blocks a tool call until the UI answers). Reused via
  the transport refactor below. The TUI path must keep working unchanged.
- `src/mha/tools/plan.py` — the pattern to copy for state-mutating tools:
  harness-owned state, model mutates via validated calls, tracker re-rendered
  and pinned every turn, gates as tool errors.
- Session machinery (`harness/session.py`) — arch sessions record and resume
  exactly like code sessions (`.mha/sessions/<run-id>/`).
- `models.json` aliases — `judge` (challenge-phase critique). Add an
  `architect` alias so the arch harness can run a stronger model than coding;
  default it to the current default model.

## Work items

### 1. Transport refactor (serve.py)

Extract the transport from `Server` so the pump is transport-agnostic:

- `Transport` interface: `emit(event: dict)` outbound; inbound delivery of
  `user_input` / `permission_response` / `interrupt` messages to the pump.
- `StdioTransport` — current behavior, byte-for-byte protocol compatible.
  The TUI (`tui/src/bridge.ts`) must not need changes. Existing serve tests
  must pass.
- `HttpTransport` — stdlib only (`http.server.ThreadingHTTPServer`):
  - Binds `127.0.0.1` on a random free port.
  - `GET /` serves the FE (static files from a directory; the designed page
    drops in later — ship a minimal placeholder page now).
  - `GET /events` — SSE stream of the same JSON events `emit` produces
    (fan-out to all connected clients via per-client queues; late joiners get
    a `ready` replay and the latest `arch_state`).
  - `POST /input` `{text}` · `POST /permission` `{id, approved, feedback?}` ·
    `POST /interrupt` — map onto the same handlers stdio uses.

### 2. Protocol additions (the FE contract — freeze this)

Outbound events, in addition to the existing serve vocabulary
(`ready`, `harness_event`/`assistant_delta`, `turn_end`, `error`, `bye`):

- `arch_state` — emitted after every state mutation (mid-turn). Payload:

  ```json
  {
    "type": "arch_state",
    "phase": "propose",
    "state": { /* full ArchState serialized per schema doc */ },
    "renders": {
      "toplevel": "<mermaid flowchart source>",
      "flows": {"<flow-id>": "<mermaid sequence source>"},
      "facets": {"<component-id>": {"kind": "store", "mermaid": "<er source>"}}
    },
    "tracker": "<plain-text tracker: phase, obligations queue, blocking questions>"
  }
  ```

  Full-state replacement every time; the FE never diffs. State is small.

- `permission_request` gains two kinds beyond edit/write/bash:
  - `{"kind": "toplevel_approval", "id": n, "summary": "..."}` — the
    approve-high-level gate at the end of `propose`.
  - `{"kind": "finalize", "id": n, "summary": "...", "artifacts": [paths]}` —
    the Finalize gate on `done`.

  `permission_response` gains an optional `feedback` string: on rejection,
  the harness injects it as the next user turn (this is "Request changes").

- Tool activity for the transcript's compact notices rides the existing
  `harness_event` stream (`tool_call` / `tool_result` events already carry
  name + args; no new event type needed).

### 3. ArchState module

`src/mha/arch/state.py` (new package `src/mha/arch/`): the dataclasses,
validation rules, phase machine, and obligation computation exactly as in
`docs/arch-state-schema.md`. Obligation computation is deterministic code
(scope baseline + risk signals); the model can never write obligations.
Serialize/deserialize to JSON for the `arch_state` event and for session
persistence (state is part of the session dir, restored on `--resume`).

### 4. Arch tools

`src/mha/arch/tools.py`: `brief`, `component`, `connect`, `flow`, `expand`,
`decide`, `ask`, `answer`, `amend_toplevel`, `done` — signatures, validations,
and phase gates per the schema doc's tool table. Follow `plan.py` conventions:
gates return instructive `ToolError`s (tell the model what to do instead, e.g.
"components are locked until the brief has goal/actors/scope — call brief or
ask the user"). `done` and the propose→toplevel_review transition are gated
through `PermissionBroker` with the new request kinds. No `edit`/`write`/`bash`
in this harness; keep `read`, `kg_query`, `WebSearch`, `WebFetch`, `skill`.

### 5. Renderers

`src/mha/arch/render.py`: pure functions ArchState → mermaid flowchart
(existing vs new components styled distinctly in feature mode), sequence
diagram per flow, ER diagram per store facet, and the tracker text. No model
involvement, no I/O. Unit-test these heavily — they are the UI.

### 6. Harness wiring + CLI

- `src/mha/arch/instructions.md` — arch system prompt: phase discipline,
  ask-don't-assume for missing brief fields, one component at a time in
  expand, decisions require alternatives. Keep it as tight as the code
  harness's 51 lines; the tracker and schema gates do the enforcement.
- `mha arch "<prompt>" [--repo PATH] [--model SPEC] [--resume RUN_ID]` in
  `cli.py`: build the arch tool set + instructions on the Runner (architect
  alias by default), start `HttpTransport`, `webbrowser.open()` the page, feed
  the prompt as the first turn. `--no-open` for tests/headless.
- Challenge phase: on entering `challenge`, the harness dumps ArchState to the
  `judge` model with a critique prompt (breakage under stated scale +
  simplification pass); findings are appended as `OpenQuestion`s with
  `source: "judge"`. Offline/judge-unavailable degrades to the deterministic
  coverage audit alone (never blocks the session).

### 7. Finalize (stubbed bundle)

The handoff bundle schema is **deliberately not final** (parked; will be
specified separately). Implement finalize behind one function —
`write_bundle(state, run_dir) -> list[Path]` — that for now writes:
`architecture.json` (full ArchState) and `architecture.md` (generated:
top-level doc + per-component contract sheets + decision log). Mark the
function as the swap point. Do not build the KG seed yet.

## Constraints

- Stdlib only for HTTP/SSE — no new runtime dependencies.
- Tool schemas stay small-model-friendly: flat fields, short enums, one
  concept per tool call; instructive error messages on every gate.
- Don't break: existing serve protocol (TUI), code-harness behavior, tests.
- Tests in the existing style (`tests/test_arch_state.py`, `test_arch_tools.py`,
  `test_arch_render.py`, `test_http_transport.py` with a fake client;
  extend `test_serve.py` for the transport split). Mock the wire as the
  existing tests do.

## Acceptance

1. `mha arch "build a url shortener" --no-open` starts, serves the placeholder
   page, streams `arch_state` events over SSE as a scripted/mocked model fills
   the brief and adds components.
2. Phase gates enforce: no `component` before brief; no `expand` before
   top-level approval (via `POST /permission`); no finalize with a pending
   obligation or blocking question.
3. Rejecting approval with `feedback` continues the loop with that feedback as
   the next user turn.
4. Interrupt mid-turn works over HTTP; session resumes with `--resume`
   including restored ArchState.
5. TUI + `mha serve` + full existing test suite: unchanged and green.

## Out of scope

- FE visual implementation (design attached separately; only the protocol
  above is binding).
- Final handoff bundle schema + KG seed (swap point stubbed in §7).
- Excalidraw canvas / bidirectional diagram editing.
- The routing "main agent"; harness is invoked explicitly via `mha arch`.
