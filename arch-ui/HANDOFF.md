# Architecture board — handoff

Covers the arch board as it actually stands in this repo. Every claim below was
checked against the code on 2026-08-23; where something is inherited from the
earlier design-pass handoff and *not* verified here, it says so.

---

## 0. The one structural rule

**There is one target: `arch-ui/`.**

The earlier handoff opened by saying there were two — `arch-board.html` and the
React port — kept in sync by hand. That is not true of this repo. There is no
`arch-board.html` on disk and none in any branch (`git log --all -- '*arch-board.html'`
is empty). The single-file prototype lives in Open Design (project `e916fc6f-…`),
not in git.

So there is nothing to keep in sync, and no grep-the-other-file discipline to
observe. If the prototype is ever brought back into the repo, reinstate that rule
— until then, do not go looking for a second file to edit.

`arch-ui/` builds into `src/bird/harnesses/arch/static/`, which is what the
harness serves.

---

## 1. What is here

The earlier handoff described six workstreams. Three landed in this tree intact,
one landed in a different shape, and two did not land at all.

### 1.1 The chat is a collapsible sidebar — **landed**

- Collapse/expand from the appbar toggle, `⌘\` / `Ctrl+\` (`App.tsx:48`), or the
  seam. Width and open/closed persist in `localStorage` (`arch.rail`,
  `arch.chat` — `hooks/useRail.ts`).
- The seam is a drag handle (`.grip`, `role="separator"`, arrow keys resize —
  `components/Chat.tsx:59-75`). Range 320–620px.
- The board holds its place when the rail closes (`nudgeX`, 430ms).
- Below 940px it collapses as a bottom sheet; there is a 560px tier too
  (`styles/board.css:553`, `:569`).

Docked right deliberately: the lanes read left→right, so a left-docked rail would
push the board's origin off the reading start.

### 1.2 Files and images in the chat — **landed**

- Paperclip, `⌘V` paste, or drag anywhere over the composer (`drop to attach`).
- Staged attachments in `.tray`; images as thumbs, everything else a name+size
  chip with a remove ×.
- Caps: **6 files per message, 10 MB each** (`Composer.tsx:9-10`). Over either and
  you get an inline `role="alert"` naming the file, self-clearing after 7s.
- Sent `.shot` thumbnails open a `.lightbox`; `.doc` chips for everything else.

### 1.3 The `saved` indicator is gone — **landed**

Nothing in markup or CSS.

### 1.4 Accessibility and state-coverage — **landed**

- Boxes are focusable `role="group"` with `aria-roledescription="board box"` and a
  live `aria-label` (`NodeCard.tsx:97-105`). Focus selects.
- Arrow keys move a selection (8px, Shift 40px), routed through the same state as
  a drag.
- `<main id="content">` (`App.tsx:100`) and an `<h1>` for the session goal
  (`AppBar.tsx:16`). The page had neither.
- Empty-board state (`Board.tsx:499`, `#board-empty`).
- `.board-line .go` revealed on hover *and* `:focus-visible`
  (`board.css:375`).
- `--danger` is the palette's only semantic colour, used once, on rejected files
  (`board.css:48`, `:452`).

### 1.5 Add-a-box ghost, and the Edit form — **NOT here**

There is no `.ghost`, no `data-target` lane lift, and no inline edit form.
Placement is still blind: lane membership is decided on click by whatever the
cursor was over.

`deepen` is still the live model, not `Edit`:

- `NodeAct` includes `"deepen"` (`NodeCard.tsx:7`)
- the toolbar's second slot is the Deepen button (`NodeCard.tsx:136`)
- `Board.tsx` has `runDeepen`, and `E` calls it (`Board.tsx:409`)
- the dock hint reads "`E` deepens" (`Board.tsx:496`)

So `depthFor()` does not exist, depth is not derived from content, and the
"I have nothing recorded for X yet" dead end the earlier handoff described is
still reachable — `runDeepen` bails with exactly that message when a box has no
`resp`/`tech`/`rows`, and there is still no way for a user to record any.

### 1.6 The board as a draft — **landed, but harness-side and in a different shape**

This is the section the earlier handoff gets most wrong, and the correction
matters because the two designs look similar and are not.

**What the earlier handoff described** (client-side): a `state.pending` array of
`Change` records, per-thing coalescing, a `.pending` strip reading
`3 CHANGES · Search index, Embedder, Queue · show me`, and a per-box ink dot.
None of that is here — no `Change` type, no `notePending`/`clearPending`/
`pendingIds`/`summarise`, no `.pending` strip, no `data-pending` attribute.

**What is actually here:** the commit model is real, but the harness owns it.

- Every board gesture calls `/mutate` **immediately** (`wire/session.ts:252`).
  The graph changes at once, persists, and pushes new state. Nothing waits.
- The harness keeps `_user_edits`, each with a renders-seen counter, and
  `pending_edit_count()` returns those the architect has not seen yet
  (`src/bird/harnesses/arch/session.py:127`). That count rides the wire as
  `pending_edits` (`session.py:72` → `wire/session.ts:133`).
- The composer turns the count into an **editable draft message**:
  `I've made 3 changes on the board, please check.` (`Composer.tsx:52`). It is a
  normal draft — reword it, add to it, delete it.
- **It never overwrites what you have started typing.** `ours` tracks whether the
  text is still the composer's to rewrite; the moment you touch it, it is yours
  (`Composer.tsx:64-81`).
- **One commit: Send.** Enabled by `pendingEdits > 0` alone, so a batch goes with
  no message (`Composer.tsx:180`); that path posts `/board` (`session.ts:235`).
- Edits made *during* a turn reach the architect through the pinned note and never
  fire a second turn (`session.py:132-145`).

So "nothing reaches the model until you hit Send" holds. "Nothing on the board
commits itself" does **not** — it commits to the graph instantly, and only the
*conversation* waits.

The dedup rule is different too: the harness dedups by *has the architect seen
it*, not by the earlier handoff's per-thing coalescing (added→edited stays added,
added→deleted drops out). Do not port that coalescing without deciding which of
the two rules wins.

---

## 2. Data model

`src/board/types.ts` holds the board view — `Lane`, `BoardNode`, `Wire`, `Anno`,
`Selection`, `Attachment`, `Depth`, `Tool`. It is a *view* of the harness graph,
derived by `board/adapter.ts`, not a second copy.

There is **no `src/board/store.ts`**, and none of `depthFor`, `notePending`,
`clearPending`, `pendingIds`, or `summarise` exists. The earlier handoff's §2 is
a description of code that is not in this repo.

What does exist alongside the view:

| Module | Holds |
|---|---|
| `board/ui.ts` | selection, tool, editing, `flash`, and `drafts`/`noteDrafts` — optimistic drag positions, so a box does not snap back while `/mutate` is in flight |
| `wire/session.ts` | the harness connection: `arch`, `pendingEdits`, `mutate`, `sendBoard`, `sendInput`, `refusal` |

Note `drafts` in `ui.ts` is about *drag positions*, not pending changes. The name
collides with §1.6's sense of "draft" and means something unrelated.

### 2.1 The arrangement — `src/board/layout.ts`

Added 2026-08-24, replacing the one-box-per-200px-row stack that lived inside
`adapter.ts`. Read this before touching either file; the old model still reads
plausibly and produced boards that were unusable.

**What the old one did.** Each column stacked its boxes at a fixed `ROW_H = 200`,
in `Object.values(arch.nodes)` order, with a constant `BOX_H = 120` standing in
for every height. Two consequences, both visible on any real board:

- a stub renders 39px and a detailed box 171px, so every stub sat in the middle
  of 160px of nothing — a 13-box board became a 2,600px ribbon that `Fit` had to
  show at 36%, and every box in it was too small to read;
- dictionary order is not reading order, so a five-box chain was dealt out as
  rows 13, 10, 12, 6, 7 and its wires crossed the whole board to find each other.

**What replaces it.** The two axes now carry different things:

| Axis | Comes from |
|---|---|
| down the page | layers over the wires, across the **whole board** — not per column |
| across the page | which approach the box belongs to |

Layering is global on purpose. The box a fork is *about* is the one that has an
approach, so a chain normally steps out into a column and back; layering inside
each column separately leaves that chain jumping up and down the page.

The pieces, in order:

1. `forwardEdges` drops one edge per cycle — for placement only; it is still
   drawn.
2. `layerOf` is longest-path-from-a-source, **except** for boxes nothing feeds,
   which drop to sit just above the highest thing they feed. Without that step
   every source lands in layer 0, and a board reverse-engineered from a codebase
   opens with twenty boxes across the top and a diagonal underneath. Nothing
   points *into* a source, so moving one cannot turn another box's wire upwards.
3. `order` is the usual barycentre sweep, with column as the outer key — a box
   never leaves its approach's territory to be near what it is wired to.
4. `align` pulls each box towards the mean of its same-column neighbours, then
   pushes the row apart again. Only same-column wires vote: `x` is measured from
   the column's own axis, and a box one column over is not on that ruler.
5. Whatever the alignment left off-centre is re-centred, so the shelf (which has
   no wires to be pulled by) and the design agree on where the column's middle is.
6. Boxes wired to nothing at all go on a **shelf** under the design, packed
   three-across rather than one per row. They are the pieces the design touches,
   not steps in it.

**Heights are measured, not guessed.** `Board.tsx` passes `heights` — the
`ResizeObserver`'s numbers — into `toBoard`. That looks circular and is not: a
box's height depends on its content and its width, never on where it sits, so
the first paint uses `estimateHeight` and the second settles on the truth. It
does not oscillate. `estimateHeight`'s constants track `.node` in `board.css`
and were checked against a rendered board; they only have to be close.

**Two rules worth not undoing:**

- *A hand-placed box keeps its slot.* The layout says where every box would go
  and a moved one overrides its own answer. Skipping it would close the gap it
  left, and then dragging one thing moves two others. Where a box sits is the
  one thing on this board a person chose directly.
- *A lane hugs the ground its boxes stand on, not every box that belongs to it.*
  A box dragged a little way still stretches the rectangle; one carried across
  the board does not, or the lane becomes the size of the board with two boxes
  in it and everything else underneath. Such a box still wears its column's
  colour on its own border, which is what says whose it is. A column whose boxes
  have *all* been placed by hand has no other ground to describe, so there it
  follows them.

`Tidy` (beside `Fit`) is the way out: `{"op":"tidy"}` clears every `x`/`y` and
hands the board back. It exists because the layout deliberately cannot arrange
around a chosen position — the point of one is that nothing else moves it — so
without a way to give them back, a board arranged against an older layout can
never benefit from a newer one. Like `move`, it is not reported to the architect.

---

## 3. Interaction contracts

**Keyboard.** The global handler bails inside `INPUT`/`TEXTAREA`/`contenteditable`
and on any meta/ctrl/alt chord (`Board.tsx:372-376`).

| Key | Effect |
|---|---|
| `V` `N` `T` | select / box / note tool |
| `F` | fit the board |
| `E` | **deepen** the selected box (not edit) |
| `↑↓←→` | move the selection 8px (Shift 40px) |
| `⌫` `Del` | delete the selection |
| `Esc` | clear the selection |
| `⌘\` | toggle the chat rail (separate handler, `App.tsx:48`) |

`Esc` also closes the lightbox, but via the lightbox's own listener
(`Lightbox.tsx:19`), not this table's handler.

**Commit model** — corrected per §1.6:

| Surface | Reaches the graph | Reaches the architect |
|---|---|---|
| Rename (inline) | on Enter / blur, immediately | on Send |
| Drag, arrow-move | on release, immediately | never — position is not what the design says |
| `Tidy` | immediately, clearing every `x`/`y` | never, for the same reason |
| Add, delete, connect | immediately | on Send |
| Note text | on blur, immediately | on Send |
| Composer **Send** | — | now |

A refusal from the harness is rolled back onto the page and said in the
conversation (`session.ts:260-264`) — the harness disagreeing belongs in the same
channel everything else disagrees on.

**What a message carries.** A turn's prompt is context blocks the harness puts in
front of the words, assembled in `serve.py` `on_user_input`:

| Block | Marker | From |
|---|---|---|
| what was drawn | `[the user changed the board]` | `compose_activity_prompt()` |
| what was selected | `[the user is pointing at]` | `describe_subjects(ids)` |
| what was typed | — | the composer |

Selection is the page's — the harness has no idea what is highlighted until a
message says so. The composer reads `getUi().selected` **at submit**, not from
held state: the selection can change while you are still typing, and the one that
counts is the one you were looking at when you sent it. Only boxes travel; a note
is not a component. Ids the graph cannot resolve are dropped silently, because a
stale selection is the page being a moment behind, not something to explain.

`wire/task.ts` `splitTask()` takes the blocks back apart before the turn is shown.
**This is load-bearing:** without it the user's own message renders with the
prompt's scaffolding inside it, and the transcript then claims they typed
something they never typed. `wire/task.test.ts` covers it, including a case parsed
from real harness output.

---

## 4. Rules that will get broken by accident

Decisions, not defaults. Each has a reason not obvious from the code.

1. **The architect never claims to know what is inside an attachment.** It names
   the file and its kind and asks a question. Inventing content here is the
   fastest way to make the whole surface untrustworthy.
2. **Territory is paper tone, not colour.** Lane tints are ~2% chroma. They
   separate columns; they do not signal status. Never let a lane become a wash of
   accent.
3. **Accent budget is two per screen.** Currently the focused field and Send.
4. **`rows` are the model's.** They come out of a schema it read. A free-text list
   in a user form would invite invention.
5. **Depth means "how far the conversation got here."** Today it is set by
   `deepen`, which is the rung §1.5 was meant to remove — but do not add a control
   that lets a user set depth *directly*.
6. **Uppercase tracking ≥ 0.06em.**
7. **Lane colour is positional, never chosen.** See §6 — the model picks how many
   approaches and their order; the design system picks the values. If it ever
   needs a say, let it pick a *name* from the ramp, never a raw colour.
8. **Moves are never reported to the architect.** Where a box sits is not what the
   design says. `Tidy` is a move too, and is just as silent.
9. **Nothing in the arrangement may depend on where a box ended up.** Heights go
   into the layout because a box's height is a function of its content and its
   width; feeding a *position* back the same way would not settle. See §2.1.

---

## 5. Verification

```bash
cd arch-ui
npm run check     # tsc --noEmit && vitest run
npm run build     # tsc --noEmit && vite build   → src/bird/harnesses/arch/static/
npm run dev       # http://localhost:5173
```

Both pass as handed over: typecheck clean, 44/44 tests.

`src/styles/board.css` is 612 lines; `src/board/layout.ts` is 534.

`npm run dev` can also render a saved board with no harness behind it: drop any
harness `arch_state.json` into `arch-ui/dev-fixtures/` (gitignored) and open
`?fixture=<name>`. `src/dev/fixture.ts` is reached only under `import.meta.env.DEV`
and is not in the production bundle.

---

## 6. Known gaps

**~~1. Lane colours are hardcoded to exactly two approaches.~~ Fixed 2026-08-23.**

Kept here because the fix is a rule worth not undoing. The five `--lane-a/b/s`
tokens and their five literal selectors are gone. In their place:

- a five-step ramp in `:root` at locked L (97.5% tint / 69% line) and C (0.018 /
  0.075), hue the only variable — 168, 302, 35, 235, 95, ordered so two or three
  approaches land furthest apart (`board.css:22-46`)
- two generic rules — `.lane` and `.node[data-lane]` — reading `--lane-tint` /
  `--lane-edge` / `--lane-line`, set inline by `laneVars(slot)`
  (`board/geometry.ts:16`), which only ever emits `var(--ramp-N-*)`. Every colour
  value stays in the stylesheet.
- slots assigned by **position in the approach list**
  (`adapter.ts:88`), not by live/lost order.

That last choice fixed a second bug the original gap did not mention: approaches
were being **recoloured by a rival's rejection**. `live` and `lost` were slotted
in separate passes, so greying one approach re-indexed the survivors and a
surviving column changed tint mid-session for a reason that had nothing to do
with it. Losing is a status change, not a repaint of everything that stayed.

The original write-up also mispredicted the symptom: a third approach did not
render untinted grey. The old code wrapped at `% 2`, so approach 3 was tinted
*identically to approach 1*, and `?? "a"` hid any miss. Two rivals in one tint
read as one approach — the exact thing lane colour exists to prevent.

Three tests cover it (`board/adapter.test.ts`): a third approach gets a distinct
tint, a rejected approach never wears a live one's, and an approach keeps its tint
when a rival is rejected. All three fail against the old logic.

**Remaining limit:** past five approaches the hues repeat. `RAMP = 5` is the
stylesheet's ramp, and two columns sharing a tint is the honest failure rather
than a sixth colour invented in the adapter — but it is an uncaptured limit if
boards that wide are expected.

**2. The lightbox is not focus-trapped.** `role="dialog"` with no `aria-modal`, no
focus move on open, no focus return on close (`components/Lightbox.tsx`). Esc and
click-out work. Fine for a prototype, not for ship.

**3. Attachments are session-only.** Object URLs are created at
`Composer.tsx:119` and revoked only when you remove a staged file (`:137`) —
never for *sent* attachments, which need to outlive the turn. A real
implementation uploads and references by path.

**4. §1.5 never landed**, so blind placement and the `deepen` dead end are both
still live. See §1.5 for the specific sites.

**5. `Discard` deliberately not built.** The changes are already on the graph; a
button labelled Discard would imply a revert, and there is no undo stack.
Building one means building undo first. *(Inherited from the earlier handoff;
still consistent with §1.6's real commit model, where edits land immediately.)*

**6. A very dense board is still dense.** The arrangement fixed the boards this
harness draws — a design with a dozen or two boxes now fits the screen and reads
top to bottom. The reverse-seeded kind does not: 40 boxes and 114 `imports_from`
edges lay out without a single overlap, but at that density the *wire labels* are
the clutter, and no arrangement fixes a label repeated sixty times. If those
boards are meant to be read rather than scanned, the answer is on the drawing
side — hiding a label the whole board shares, or thinning wires below some zoom —
not in `layout.ts`.

**7. An annotation pinned to a box can still land on another one.**
`toBoard` puts an unplaced anchored note at `host.cx + w/2 + 26`, which with the
tighter columns is more likely to be somewhere a box already is. Nothing checks.
Only affects notes that have never been dragged.

---

## 7. What this document corrected

For anyone comparing against the earlier design-pass handoff:

| Earlier claim | Reality |
|---|---|
| Two targets kept in sync by hand | One. `arch-board.html` is not in this repo or its history. |
| §1.5 Edit form, derived depth, `deepen()` deleted | Not here. `deepen` is live in four places. |
| §1.6 client-side `pending` array + `.pending` strip | Landed harness-side as `pending_edits` + an editable composer draft. Different dedup rule. |
| §2 `store.ts` with five new exports | No such file. |
| Gap 1: 11 selectors, third lane renders untinted grey | 5 selectors; third approach collided with the first. Now fixed. |

And within this document, for anyone holding a copy from before 2026-08-24:

| Then | Now |
|---|---|
| The arrangement was a fixed 200px stack in `adapter.ts`, in dictionary order | `layout.ts` — layered on the wires, rows sized to the measured boxes. §2.1 |
| `toBoard(arch)` | `toBoard(arch, heights)`; `bounds(nodes, annos, lanes, heights, ids?)` gained `lanes` |
| Nothing could give a hand-placed box back | `Tidy`, beside `Fit` — `{"op":"tidy"}` |
