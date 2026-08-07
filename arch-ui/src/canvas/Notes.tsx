/**
 * Concerns as sticky notes, handover §5.
 *
 * An objection the agent raised and nobody can see is the same as no objection
 * at all — which is what a Concerns *tab* amounted to, since a tab is invisible
 * four fifths of the time. Here they are paper in the canvas's right margin, in
 * the same field of view as the thing they object to.
 *
 * They are not leader-lined to their target. A dashed line per note across the
 * diagram cost more than it bought: it crossed the edges it was drawn over, it
 * had to be re-measured on every pan and zoom, and the note already names its
 * target in its own header. The margin reads as margin now.
 *
 * Two rules the design is specific about, and both are about the record rather
 * than the aesthetics:
 *
 *   - A settled note **discolours; it does not disappear.** "We knew, we chose
 *     anyway, here is why" is the most valuable thing the code harness
 *     inherits, and a note that vanishes on Accept takes it with it.
 *   - Open notes **hold their slot,** in filing order, so settling one does not
 *     make the rest jump across the margin under your cursor.
 *
 * Settled notes leave the column and join a pile at the top of the margin —
 * kept, but stacked, so the twentieth ruling costs the same vertical space as
 * the second and the objections still live keep the room.
 */
import { useState } from "react";
import { resolveConcern, useSession } from "../store/session";
import type { Concern } from "../types";

/** Must match `--note-col` in theme.css: the margin the canvas reserves for
 *  paper. Exported because the layout has to keep the diagram out of it. */
export const NOTE_COL = 206;

/** How many papers of the pile are drawn. The rest are in it — the count says
 *  so, and expanding lists them — they are just not worth a third edge peeking
 *  out from under the second. */
const PILE_PEEK = 3;

/** A stable identity for "no concerns".
 *
 *  `arch?.concerns ?? []` mints a fresh array on every render, which as an
 *  effect dependency re-runs the measure, which sets state, which renders —
 *  a loop React kills with "maximum update depth exceeded". Both tsc and the
 *  unit tests are blind to it; only the browser says anything. */
const NO_CONCERNS: Concern[] = [];

/**
 * Settling from the note itself.
 *
 * Overruling asks for the reason and will not proceed without it — that
 * sentence is what the builder inherits, and a placeholder in its place is
 * worth less than nothing. Accepting is the design having changed, so its
 * reason is optional.
 */
function NoteActions({ c }: { c: Concern }) {
  const [mode, setMode] = useState<"accepted" | "overruled" | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async () => {
    setBusy(true);
    const ok = await resolveConcern(c.id, mode!, reason.trim());
    setBusy(false);
    if (ok) { setMode(null); setReason(""); }
  };

  if (mode === null) {
    return (
      <div className="sticky-actions">
        <button onClick={() => setMode("accepted")}>Accept</button>
        <button onClick={() => setMode("overruled")}>Overrule</button>
      </div>
    );
  }
  const needsReason = mode === "overruled";
  return (
    <div className="sticky-actions" style={{ flexDirection: "column", alignItems: "stretch" }}>
      <textarea
        autoFocus
        rows={2}
        value={reason}
        placeholder={needsReason ? "why you're going ahead anyway — this is the record"
                                 : "what changed (optional)"}
        onChange={(e) => setReason(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") { setMode(null); setReason(""); }
          if (e.key === "Enter" && !e.shiftKey && (!needsReason || reason.trim())) {
            e.preventDefault();
            void send();
          }
        }}
      />
      <div className="sticky-actions">
        <button disabled={busy || (needsReason && !reason.trim())} onClick={send}>
          {needsReason ? "Overrule it" : "Mark accepted"}
        </button>
        <button disabled={busy} onClick={() => { setMode(null); setReason(""); }}>Cancel</button>
      </div>
    </div>
  );
}

function Note({ c, editable, depth }: {
  c: Concern;
  editable: boolean;
  /** Position in the settled pile, front paper first. Absent for open notes. */
  depth?: number;
}) {
  const settled = c.status !== "open";
  // Two tilts alternating down the stack. One angle repeated reads as a skewed
  // column; two reads as paper.
  const tilt = Number(c.id.replace(/\D/g, "")) % 2 === 0 ? "b" : "a";
  return (
    <div
      className="sticky"
      data-in="true"
      data-tilt={tilt}
      data-sev={c.severity}
      data-settled={settled}
      data-depth={depth}
    >
      <div className="sticky-head">
        <span className="sticky-sev">{c.severity}</span>
        <span className="sticky-target">{c.target}</span>
        <span className="sticky-src">{settled ? c.status : c.source}</span>
      </div>
      <div className="sticky-claim">{c.claim}</div>
      {c.alternative && <div className="sticky-alt">instead: {c.alternative}</div>}
      {c.resolution && <div className="sticky-res">→ {c.resolution}</div>}
      {editable && !settled && <NoteActions c={c} />}
    </div>
  );
}

/**
 * The settled pile.
 *
 * Collapsed it is a fixed footprint whatever the count — three papers deep and
 * a tally — so a long session's rulings never crowd out the objection still
 * waiting on an answer. Opened it is the whole record, newest ruling first,
 * scrolling inside a bounded box rather than growing the margin.
 */
function Pile({ settled }: { settled: Concern[] }) {
  const [open, setOpen] = useState(false);
  // Newest first: nothing timestamps a resolution, but filing order is the
  // best proxy there is, and the ruling you just made should be the paper on
  // top rather than buried under the first one of the session.
  const newestFirst = [...settled].reverse();
  const shown = open ? newestFirst : newestFirst.slice(0, PILE_PEEK);
  return (
    <div className="note-pile" data-open={open}>
      <div className="note-pile-stack">
        {shown.map((c, i) => <Note key={c.id} c={c} editable={false} depth={i} />)}
      </div>
      <button
        className="note-pile-toggle"
        onClick={() => setOpen((v) => !v)}
        title={open ? "collapse the settled pile" : "every settled concern, still on the record"}
      >
        {settled.length} settled{open ? " — collapse" : ""}
      </button>
    </div>
  );
}

export function Notes() {
  const arch = useSession((s) => s.arch);
  const finalized = useSession((s) => s.finalized);
  const conn = useSession((s) => s.conn);

  const concerns = arch?.concerns ?? NO_CONCERNS;
  const editable = !finalized && conn !== "disconnected";

  if (concerns.length === 0) return null;

  // Filing order within each group. Sorting by severity would reshuffle the
  // margin every time the critic files something, and a note that moves while
  // you are reading it is worse than a note in the wrong place.
  const settled = concerns.filter((c) => c.status !== "open");
  const live = concerns.filter((c) => c.status === "open");

  return (
    <div className="note-layer">
      {settled.length > 0 && <Pile settled={settled} />}
      <div className="note-open">
        {live.map((c) => <Note key={c.id} c={c} editable={editable} />)}
      </div>
    </div>
  );
}
