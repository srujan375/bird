# Design workbench — handoff

The design harness's browser page, as a React app. Replaces the hand-written
`static/index.html` + `app.js` + `style.css` that used to live in
`src/bird/harnesses/design/static/`. Checked against the code on 2026-09-02.

---

## 0. The shape

```
design-ui/                      the source (this directory)
  src/wire/                     the harness connection: events → stores
  src/board/                    what the page owns: focus, selection, edits
  src/components/               AppBar · Rail · Stage · Frame · DraftFrame · SelMenu · Loader
  src/dev/fixture.ts            a scripted session, no harness needed
src/bird/harnesses/design/static/   the BUILD OUTPUT — `npm run build` empties and refills it
src/bird/harnesses/design/editor-bridge.js   the script inside every artboard (moved out of static/)
```

`npm run build` writes into the harness's `STATIC_DIR`, so `bird design` serves
the page with no Python change and no Node on the machine that runs it. The
built `index.html` and `assets/*` are committed, like arch's.

**The chat rail is arch-ui's, not a copy.** `@arch/*` aliases `../arch-ui/src`
(`vite.config.ts`, `tsconfig.json`), and `Chat`, `Composer`, `Thread`,
`Picker`, `Lightbox`, the chat store and the picker model are imported from
there as they are. `resolve.dedupe` keeps one React between the two trees and
the tsconfig `paths` keep one `@types/react`. What differs between the two
boards — where a message goes, what was selected when it was sent, what "show
me" means — comes in through `arch-ui/src/chat/host.tsx` (`ChatHost`), which
`App.tsx` provides. Changing the rail means changing arch-ui; both apps pick it
up.

## 1. The wire

`src/wire/session.ts` mirrors arch's: one SSE connection at a time,
`design_state` is a full replacement, harness events become turns in the
shared chat store. The contract is `src/bird/harnesses/design/session.py`
`state_event` and is typed in `src/wire/types.ts`. What the harness carries
for this page:

- `prompt`, `theme`, `theme_css` ride every push (the brief is the app bar's
  title, the tokens so a draft is drawn in the palette the finished artboard
  will wear).
- `critiques` (artboard → the critic's latest note: version, kind, text),
  `plan_critique`, and `pending_user_edits` (hand edits the designer has not
  been shown; the server puts them ahead of the next message on its own, the
  composer only shows the count and lets Send go with no words).
- `describe_subjects` — what the composer had selected travels as
  `artboard#selector` (or a bare artboard id for the frame itself) and comes
  back to the designer as the `[the user is pointing at]` block. `serve.py`'s
  `_board_focus` asks the arch session first, then the design one.
- `turn_end` carries `reason` when its status is `interrupted`: `user`,
  `shutdown`, or something else. The page says "You stopped the turn" only
  for `user`; a stopped turn used to be blamed on the person whatever tore
  it down.

**Reconnect.** A dropped SSE connection is retried with backoff (1s, 2s, 4s…
capped at 15s) under `conn: "reconnecting"`; the composer is held read-only
meanwhile. The harness replays a joiner in full (ready, the transcript
buffer, the latest state, any pending capture), so on reopen the page clears
the thread and the drafts first (`resetForReplay`) and nothing lands twice.
Only a `bye` is final. The same lives in arch-ui's `session.ts`.

**One line about now.** `session.progress` is the single transient status
the app bar shows — "planning the direction", "capturing *Title* for the
critic", "the critic is reading *Title*", "critic: 3 notes on *Title*" (for
six seconds), "the critic could not see *Title*" — set from the events and
the capture round trip (`watchCapture`), cleared at `turn_end`. `session.working`
is what the designer's hands are on: the artboard the streaming or running
tool call names, with a verb ("editing", "reading", "reviewing") that the
frame wears as its chip. The board's own hint shows only while the board is
empty; the composer's tip is only about what a message will be taken to be
about.

The intake questions (`ask` / `intake` / `intake_locked`) render through the
shared `Picker`. Answered ones replay as answered rows; **Change** is offered
until `intake_locked` — that is the one `ChatHost.askChange` hook.

## 2. Live build

The page draws an artboard while the designer is still writing it.

| Layer | What changed |
|---|---|
| `llm/wire/openai_compat.py` | `on_tool_delta(index, name, fragment)` — each piece of a streamed tool call's arguments, as it arrives. Delivering one counts as delivery for the retry policy: a drop after it is a hard error, never a silent restart that would draw the artboard twice. |
| `engine/runner.py` | carries the callback to `client.complete` |
| `serve.py` | emits `harness_event` `tool_call_delta` `{index, name, text}` — display-only, never recorded |
| `http_transport.py` | `LIVE_ONLY_EVENTS`: `assistant_delta` and `tool_call_delta` are not replayed to late joiners |
| `src/wire/partial.ts` | decodes the `html` JSON string value as far as it has been written — escape by escape, incrementally, never re-decoding |
| `src/wire/drafts.ts` | one draft per `design_create` call in flight; pieces go straight to whoever is drawing it, React hears only start / name / closed / gone |
| `components/DraftFrame.tsx` | `document.write` piece by piece into a same-origin frame (`sandbox="allow-same-origin"`, so no scripts), the theme's tokens as an adopted stylesheet — the browser's own parser renders partial HTML the way it renders a slow page, with no reloads |

A draft retires when its artboard arrives: a new one by the id its title slugs
to (`slug()` mirrors `_slug` in `state.py`; a suffixed collision falls back to
the oldest unclaimed draft), a rebuild when its target moves to a new version,
and in any case on that call's `tool_result` and at `turn_end`. A rebuild's
draft is drawn **in the artboard's own slot**; a new one goes after the last
frame. Tool call indices restart at 0 on every assistant message, so drafts
are keyed `${message}:${index}`.

**The handover.** A retired draft stays as a *poster* (`drafts.posters`)
under the frame that replaced it — same React key, so the streamed document
is kept, no chrome of its own — until that frame's first `size` reply, and
the new frame's slot is seeded with the draft's measured size. The frame's
iframe is see-through while its document loads (`.handing-over`), so the
swap is the same picture twice rather than a white card that jumps size.

The TUI ignores the new event (its `harness_event` chain has no default).

## 3. The board

`Stage.tsx` lays the frames out in a row in harness order, sizes from the
bridge's `size` reply (drafts measure themselves, same origin), and fits when
something new lands — never when a draft merely grows. Pan and zoom live in
`hooks/useStage.ts`, imperative like arch's `useView` but clamped to 5%–400%:
four 1280px artboards need far more room than the arch board does. Everything
inside the world marked `.stay` is counter-scaled after every commit
(`useLayoutEffect`), so frame names, busy chips and the selection menu keep
their size on screen.

**Gestures over an artboard reach the board.** The pointer over a frame puts
the wheel and the trackpad pinch in the frame's document, and a pinch nobody
handles zooms the browser page. The bridge forwards both (`wheel`, `pinch`
messages) and `Stage` maps the point back through the frame's own transform
(`frames.toScreen`). Drafts have no bridge and nothing to click, so a
transparent `.shield` keeps their events on the board.

**So do keys.** A click into an artboard takes keyboard focus into that
iframe. The bridge forwards an allowlist (Escape, F, digits, Enter, ⌘/ctrl+Z
with and without shift) as `key` messages — never from the artboard's own
fields, Escape aside — and `board/keys.ts` runs them through the same handler
as the window's keydown, so Escape clears a selection made by clicking. A
cleared selection posts `deselect` back, which drops the frame's outline.

**The frame bus.** `board/frameBus.ts` listens for frame messages for the life
of the page and routes captures and keys itself, handing the rest to whatever
Stage has registered. Stage's own listener used to be the only one, and it is
unmounted in the showcase phase — so every polish critique's capture reply
landed on a page with nobody listening and timed out.

**Frame readiness.** `board/frames.ts` tracks whether a frame's document has
loaded (`markFrameLoading` before `srcdoc`, `markFrameReady` on the load
event). `wire/capture.ts` posts a capture through `whenFrameReady`: the harness
asks for it on the same push that hands the page the document, and a message
posted into a loading frame is lost — which skipped half of every session's
automatic critiques at exactly the page deadline.

Editing is optimistic, as before: the op is applied in the frame, the frame
answers `edited`, then it is posted to `/mutate`; a refusal reloads the frame
from the harness's copy (`ui.reload`) and lands in the conversation. A version
this page produced is adopted from the push without reloading (`selfApplied`).

## 4. The rail

Only there while something is selected (or the design is finalized): the grid
slides it in (`.work[data-rail]`). Layers, Inspector, History — or, finalized,
the handoff card, the edit history and what happens next.

**Layers are named by what the person can see.** The bridge's tree now carries
each element's `text` (its words, an image's alt, a field's placeholder), and a
row is labelled by that — a container by its kind (`Header`, `Main`, `Group`,
`List` …) and a summary of what it holds (`2 links`, `3 groups`), folded past
the top two levels unless the selection is inside. `html` and `body` are not
rows. The tag rides at the right in small mono for anyone who wants it; the
selector path is in the tooltip and the inspector's.

## 5. Verification

```bash
cd design-ui
npm run check     # tsc --noEmit && vitest run      (the decoder, drafts and posters, the capture handshake, the session's lines and reconnect, the composer)
npm run build     # → src/bird/harnesses/design/static/
npm run dev       # http://localhost:5173/?fixture=demo      a scripted session, no harness
                  #   …&pace=70 slows the designer's typing so the live build can be watched
                  #   ?fixture=demo-still stops after the first turn
cd ../arch-ui && npm run check   # the shared rail lives here
cd .. && .venv/bin/python -m pytest tests/test_wire.py tests/test_serve.py tests/test_design_wire.py tests/test_design_mutate.py
```

`npm run dev` proxies the harness routes to `$BIRD_URL` (default
`http://127.0.0.1:8000`) — run `bird design`, set the URL it prints, and the
dev server sits in front of a live session.

## 5b. What the person sees now

- The app bar's title is the brief's first clause (full brief in its tooltip).
- Finalize is armed then confirmed, names the artboard it records, and is
  disabled while anything is being written.
- Each frame wears the critic's latest note as a `critique · vN` button that
  opens the fix-list over the artboard; the plan critique is a folded card in
  the thread (`card` turns are the shared rail's).
- Tool lines name the act — "planned the direction", "asked the critic about
  *Title*", "made 5 edits to *Title* → v9" — and a refusal carries the first
  sentence of its reason.

## 6. Known gaps

1. **A rebuild is placed only once its `artboard` argument has been read.** A
   model that writes `html` before `artboard` streams the draft as a new frame
   at the end of the row until the document closes, then it jumps to the slot.
2. **The theme reaches a draft as an adopted stylesheet**, which cascades after
   the document's own sheets; an artboard that redefines a token on `:root`
   renders differently as a draft than it does finished. Rare, and only until
   the call runs.
3. **A poster only covers a draft that was measured.** A rebuild whose
   draft never reported a size (a document too short to grow the frame)
   hands over at the default size and the frame's own measurement lands a
   moment later.
4. **Tidy is hidden, not removed.** The shared `Zoomer` renders it; frames sit
   where the harness orders them and there is nothing to tidy.
5. **The dock is gone.** The Open Design prototypes show a three-tool dock; the
   old page drew it inert. Nothing here has a second tool yet.
