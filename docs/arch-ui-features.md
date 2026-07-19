# Architecture Harness — Page Feature Spec

Functional spec for the architecture-harness web page. Layout, styling, and visual
design are entirely the designer's call — this document only fixes **what the page
contains and what it must do**.

## Context

`mha arch "<what to build>"` starts an interactive architecture session: an agent
designs a system architecture in a ReAct loop while the user watches and steers.
The process serves a local web page and opens it in the browser; the initial prompt
is already running as the first turn when the page loads. The entire session then
happens in the page: the user chats with the agent, watches the architecture
diagram assemble and change live, reviews decisions, and finally approves
("finalizes") the architecture — which writes a handoff bundle consumed later by
the code harness.

One user, localhost only, one session per page.

## What the harness does (server side, for context)

- Runs a chat loop: agent proposes/revises architecture, user replies, repeat.
- The agent does not draw the diagram directly. It mutates a structured state via
  tools: add/update/remove **components**, **connections** between them, and
  **decisions** (choice + rationale) / **open questions**. The server renders that
  state to Mermaid diagram source and pushes it to the page after every change.
- Updates arrive **mid-turn**: during one agent turn the diagram may update several
  times as components are added one by one, while the agent's narration streams in
  parallel.
- The agent may also read the repo, query its knowledge graph, and do web research
  mid-turn (surfaced to the page only as activity notices).
- When the agent believes the architecture is complete it attempts to finish; this
  is **gated on the user**: the page must ask Finalize vs. Request changes. Request
  changes returns the user's feedback to the loop; Finalize ends the session and
  writes the handoff bundle to disk.
- The user can interrupt a running turn at any time.
- The session is persisted server-side and resumable; the page doesn't manage this.

## Page contents (functional inventory)

### 1. Diagram view
- Renders the current architecture diagram from Mermaid source pushed by the server.
- Re-renders on every state update, including several times within one agent turn.
- A state update replaces the whole diagram (no client-side diffing needed), but a
  subtle cue for what just changed is welcome.
- Needs: an empty state (session just started, nothing designed yet), a
  render-failure state (bad diagram source — show the error, keep the last good
  render), and basic zoom/pan or fit-to-view for larger graphs.

### 2. Chat / transcript
- Full conversation history: user messages and agent replies.
- Agent text arrives as a **token stream** (many small deltas per turn) — must
  render progressively, not after the turn ends.
- Tool activity appears between agent text as compact notices, e.g. "added
  component `api-gateway`", "connected `worker` → `queue`", "decision recorded",
  "searched the web", "read `src/mha/runner.py`". These are glanceable status
  lines, not full chat bubbles.
- Turn boundaries are explicit events; show when a turn ends and its status
  (completed / interrupted / error).

### 3. Decisions & open questions panel
- Running list of recorded decisions: topic, the choice made, rationale.
- Separate list of open questions the agent has flagged for the user.
- Both update live as the agent records them; clicking/selecting one ideally
  highlights or relates to the diagram, but that's optional in v1.

### 4. Message input
- Text input to send the next user message (multi-line capable).
- While a turn is running, sending is unavailable — instead the user gets an
  **Interrupt** control (stop the current turn).
- After an interrupt or error, input is available again.

### 5. Finalize flow
- Triggered by the server (a blocking request event), not by a always-visible
  button: when the agent proposes completion, the page surfaces a decision moment
  with two actions:
  - **Finalize** — approve; session ends.
  - **Request changes** — with a text field; feedback is sent back and the loop
    continues.
- After finalizing: a terminal "session complete" state showing that the handoff
  bundle was written (file paths provided by the server) and the suggested next
  step (`mha code`). Page becomes read-only.

### 6. Status / session info
- Somewhere visible: model in use, repo path, session name/id, and live connection
  status (connected / reconnecting / server gone).

## Event-driven behavior (what makes the UI change)

The page holds one server-sent-events connection and POSTs user actions back.
Every UI change is driven by one of these events:

| Event | Page behavior |
|---|---|
| `ready` | Populate status info (model, repo, session id); enable UI |
| `assistant_delta` | Append streamed text to the in-progress agent message |
| tool activity events | Add a compact activity notice to the transcript |
| `arch_state` | Re-render diagram; refresh decisions/open-questions panel |
| `permission_request` (finalize) | Enter the Finalize / Request-changes decision moment |
| `turn_end` | Close out the agent message; show turn status; re-enable input |
| `error` | Show a non-fatal error notice |
| connection lost / `bye` | Show disconnected state; disable input |

User actions the page sends: send message, interrupt, finalize-approve,
finalize-reject (with feedback text).

## States to design for

1. Connecting / first load (initial turn may already be streaming).
2. Agent turn in progress: text streaming + diagram updating simultaneously.
3. Idle: waiting for user input between turns.
4. Empty diagram (before the first component exists).
5. Finalize decision pending.
6. Interrupted turn.
7. Error (turn failed) and disconnected (server exited).
8. Finalized / read-only session complete.

## Explicitly out of scope (v1)

- Editing the diagram directly on a canvas (drag boxes, rename nodes) — future.
- Multiple sessions, session switching, or resume UI.
- Auth, multi-user, remote access.
- Mobile layouts (desktop browser is the target).
