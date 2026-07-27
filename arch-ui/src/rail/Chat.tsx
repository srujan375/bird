/**
 * Transcript + composer + the gate banner.
 *
 * The gate is a banner, not a modal: the canvas stays visible and the composer
 * stays live, because replying during a gate is "request changes" — the same
 * conversational behaviour the vanilla page settled on.
 */
import { useEffect, useRef, useState } from "react";
import { interrupt, respondToGate, sendInput, useSession } from "../store/session";
import type { Concern, PermissionEvent } from "../types";

function ConcernLine({ c }: { c: Concern }) {
  return (
    <li>
      <span className={`severity-dot ${c.severity}`} /> <b>{c.target}</b> — {c.claim}
      {c.alternative ? <> · <span className="faint">instead: {c.alternative}</span></> : null}
    </li>
  );
}

function Gate({ req, reason, onRespond }: {
  req: PermissionEvent;
  /** Whatever is in the composer travels with the ruling — an overruled
   *  blocker is only worth recording if it carries the user's actual reason. */
  reason: string;
  onRespond: () => void;
}) {
  const finalize = req.kind === "finalize";
  const blockers = req.blockers ?? [];
  const concerns = req.concerns ?? [];
  const thin = req.thin ?? [];
  const gaps = req.gaps ?? [];
  const questions = req.questions ?? [];
  const obligations = req.obligations ?? [];

  return (
    <div className="gate" data-kind={req.kind}>
      <h4>{finalize ? "Finalize this architecture?" : "Approve the top level?"}</h4>
      <p>{req.summary}</p>

      {blockers.length > 0 && (
        <div className="blocker-warn">
          <b>{blockers.length} open blocker{blockers.length === 1 ? "" : "s"}.</b> Finalizing
          records {blockers.length === 1 ? "it" : "them"} as overruled, with anything you type
          below as the reason.
          <ul>{blockers.map((c) => <ConcernLine key={c.id} c={c} />)}</ul>
        </div>
      )}

      {(concerns.length > 0 || thin.length > 0 || gaps.length > 0 ||
        questions.length > 0 || obligations.length > 0) && (
        <div className="weigh">
          <div className="section-label">worth weighing</div>
          <ul>
            {concerns.map((c) => <ConcernLine key={c.id} c={c} />)}
            {thin.map((t) => <li key={t} className="faint">{t}</li>)}
            {questions.map((q) => <li key={q}>unanswered: {q}</li>)}
            {obligations.map((o) => <li key={o} className="faint">no depth yet: {o}</li>)}
            {gaps.slice(0, 6).map((g) => <li key={g} className="faint">{g}</li>)}
            {gaps.length > 6 && <li className="faint">… {gaps.length - 6} more thin spots</li>}
          </ul>
        </div>
      )}

      {finalize && (req.artifacts?.length ?? 0) > 0 && (
        <p className="mono faint">writes: {req.artifacts!.join(", ")}</p>
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
          ? "What you type below is recorded as why you overruled."
          : "Or just reply below — that counts as requesting changes."}
      </p>
    </div>
  );
}

export function Chat() {
  const transcript = useSession((s) => s.transcript);
  const stream = useSession((s) => s.stream);
  const running = useSession((s) => s.running);
  const permission = useSession((s) => s.permission);
  const conn = useSession((s) => s.conn);
  const finalized = useSession((s) => s.finalized);
  const artifacts = useSession((s) => s.artifacts);

  const [draft, setDraft] = useState("");
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [transcript, stream, permission]);

  const submit = () => {
    if (!draft.trim()) return;
    sendInput(draft);
    setDraft("");
  };

  return (
    <div className="rail-body chat">
      <div className="log" ref={logRef}>
        {transcript.length === 0 && !stream && (
          <div className="empty-note">
            The first turn is already running — the agent's opening take will appear here.
          </div>
        )}
        {transcript.map((item, i) => {
          if (item.t === "user") return <div key={i} className="msg user">{item.text}</div>;
          if (item.t === "agent") return <div key={i} className="msg agent">{item.text}</div>;
          if (item.t === "notice")
            return <div key={i} className="msg notice" data-err={item.err}>{item.text}</div>;
          return (
            <div key={i} className="msg turn">
              turn {item.status}{item.message ? ` — ${item.message}` : ""}
            </div>
          );
        })}
        {stream !== null && stream !== "" && (
          <div className="msg agent">{stream}<span className="cursor" /></div>
        )}
        {running && stream === "" && <div className="msg notice">thinking…</div>}
      </div>

      {permission && (
        <Gate req={permission} reason={draft.trim()} onRespond={() => setDraft("")} />
      )}

      {finalized ? (
        <div className="composer">
          <div className="faint" style={{ fontSize: 11.5, lineHeight: 1.55 }}>
            <b>Architecture finalized.</b> The handoff bundle is written; the session is over
            but the design stays here to read.
            {artifacts.length > 0 && (
              <div className="mono" style={{ marginTop: 4 }}>{artifacts.join("\n")}</div>
            )}
            <div style={{ marginTop: 6 }}>Next step: <code className="mono">mha code</code></div>
            <div style={{ marginTop: 6 }}>Close the tab when you are done — the server is
              only still up so you can read this.</div>
          </div>
        </div>
      ) : (
        <div className="composer">
          <textarea
            value={draft}
            placeholder={
              permission ? "Reply to request changes…" :
              conn === "disconnected" ? "Disconnected." :
              "Talk to the architect… (⏎ to send, ⇧⏎ for a newline)"
            }
            disabled={conn === "disconnected"}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="actions">
            {running && <button onClick={interrupt}>Interrupt</button>}
            <span className="spacer" />
            <button className="primary" onClick={submit} disabled={!draft.trim() || conn === "disconnected"}>
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
