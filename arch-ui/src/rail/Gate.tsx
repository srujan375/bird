/**
 * The approval gate.
 *
 * Not a modal: the canvas stays live and the composer stays typeable, because
 * replying during a gate *is* "request changes" — the conversational behaviour
 * the vanilla page settled on. Not a banner either: it used to sit in the
 * rail's own flow, between the transcript and the composer, where a long
 * summary ate half the column and pushed the conversation it was asking you to
 * weigh out of sight. It hangs off the chat instead, and tucks away to a pill.
 *
 * It also says almost nothing. The summary the server sends is the same prose
 * the agent has just written in the transcript, and every concern in it has a
 * tab of its own a few inches to the right — restating both inside the note is
 * how it grew tall enough to need scrolling. What is left is what only this
 * moment can tell you: what the two buttons will do, and what is still
 * unsettled when you press one.
 */
import { useEffect, useState } from "react";
import { respondToGate } from "../store/session";
import { useCanvas } from "../store/canvas";
import { Markdown } from "./Markdown";
import type { Concern, PermissionEvent } from "../types";

function ConcernLine({ c }: { c: Concern }) {
  return (
    <li>
      <span className={`severity-dot ${c.severity}`} /> <b>{c.target}</b> —{" "}
      <Markdown text={c.claim} className="md inline" />
    </li>
  );
}

const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

export function Gate({ req, reason, onRespond }: {
  req: PermissionEvent;
  /** Whatever is in the composer travels with the ruling — an overruled
   *  blocker is only worth recording if it carries the user's actual reason. */
  reason: string;
  onRespond: () => void;
}) {
  const setTab = useCanvas((s) => s.setRailTab);
  const finalize = req.kind === "finalize";
  const blockers = req.blockers ?? [];
  const concerns = req.concerns ?? [];
  const thin = [...(req.thin ?? []), ...(req.gaps ?? [])];
  const questions = req.questions ?? [];
  const obligations = req.obligations ?? [];
  const title = finalize ? "Finalize this architecture?" : "Approve the top level?";

  // a fresh request always arrives open, however the last one was left
  const [open, setOpen] = useState(true);
  useEffect(() => { setOpen(true); }, [req]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  /** Counts, not contents — each one is a tab away, and the tab is the better
   *  place to read it because that is where you can settle it. */
  const unsettled: { key: string; label: string; tab: string }[] = [];
  if (concerns.length) unsettled.push({ key: "c", label: plural(concerns.length, "open concern"), tab: "concerns" });
  if (questions.length) unsettled.push({ key: "q", label: plural(questions.length, "unanswered question"), tab: "questions" });
  if (obligations.length) unsettled.push({ key: "o", label: `${obligations.length} with no depth yet`, tab: "concerns" });
  if (thin.length) unsettled.push({ key: "t", label: plural(thin.length, "thin spot"), tab: "concerns" });

  if (!open) {
    return (
      <button className="gate-pill" data-kind={req.kind} onClick={() => setOpen(true)}>
        <span className="severity-dot blocker" /> {title} <span className="faint">— open</span>
      </button>
    );
  }

  return (
    <div className="gate" data-kind={req.kind}>
      <div className="gate-head">
        <h4>{title}</h4>
        <span className="spacer" />
        <button
          className="ghost tuck"
          title="tuck away — the request stays open (esc)"
          aria-label="tuck away"
          onClick={() => setOpen(false)}
        >
          ⌄
        </button>
      </div>

      {/* Blockers are the exception to saying nothing: pressing the button
          overrules them, and a consequence is not a recap. */}
      {blockers.length > 0 && (
        <div className="gate-body">
          <div className="blocker-warn">
            <b>{plural(blockers.length, "open blocker")}.</b>{" "}
            {finalize ? "Finalizing" : "Approving"} records {blockers.length === 1 ? "it" : "them"} as
            overruled, with whatever is in the composer as the reason.
            <ul>{blockers.map((c) => <ConcernLine key={c.id} c={c} />)}</ul>
          </div>
        </div>
      )}

      {unsettled.length > 0 && (
        <p className="unsettled">
          still open:{" "}
          {unsettled.map((u, i) => (
            <span key={u.key}>
              {i > 0 ? " · " : null}
              <button className="linkish" onClick={() => setTab(u.tab)}>{u.label}</button>
            </span>
          ))}
        </p>
      )}

      {finalize && (req.artifacts?.length ?? 0) > 0 && (
        <p className="mono faint tiny">writes: {req.artifacts!.join(", ")}</p>
      )}

      <div className="buttons">
        <button
          className="primary"
          onClick={() => { respondToGate(true, reason); onRespond(); }}
        >
          {finalize ? "Finalize" : "Approve"}
        </button>
        <button onClick={() => { respondToGate(false, reason); onRespond(); }}>
          Request changes
        </button>
      </div>
      <p className="faint" style={{ marginTop: 6, marginBottom: 0 }}>
        {blockers.length > 0
          ? "What you type in the composer is recorded as why you overruled."
          : "Or just reply in the chat — that counts as requesting changes."}
      </p>
    </div>
  );
}
