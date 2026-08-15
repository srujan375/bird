/**
 * The ruling, handover §9 — a sheet over a dimmed canvas.
 *
 * It used to be a note pinned to the composer, and it used to say almost
 * nothing: the summary was prose the agent had just written in the transcript,
 * and every concern in it had a tab of its own a few inches to the right, so
 * restating both was how it grew tall enough to need scrolling.
 *
 * Those tabs are gone (§1). Decisions and questions no longer live anywhere
 * else in the page, and this is one of exactly two moments they change what you
 * press — so the gate carries them in full, and it outgrew the rail doing it.
 *
 * Carrying them in full turned out not to be the same as showing them all at
 * once, which is what it did next: six decisions, each with its rationale and
 * its rejected options, is a page of reading standing between you and a button,
 * and that reading is reference — you want it for the two decisions you doubt,
 * not for all six. So a decision is now a line you can scan (what was decided,
 * what was chosen) that opens onto why. What is never folded away is what
 * should change your press: open blockers, and questions still unanswered.
 *
 * Hiding is rebuilt for the same reason. It was a bare chevron that dropped the
 * sheet into a small pill over the minimap — a request that vanishes to
 * somewhere you are not looking reads as a request you dismissed. The control
 * now says what it does, and what it leaves behind says what it is, why it is
 * still there, and how to get back.
 */
import { useEffect, useMemo, useState } from "react";
import { respondToGate, useSession } from "../store/session";
import { Markdown } from "./Markdown";
import type { ArchState, Concern, Decision, OpenQuestion, PermissionEvent } from "../types";

const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

function ConcernLine({ c }: { c: Concern }) {
  return (
    <li>
      <span className={`severity-dot ${c.severity}`} /> <b>{c.target}</b> —{" "}
      <Markdown text={c.claim} className="md inline" />
      {c.resolution && <span className="faint"> → {c.resolution}</span>}
    </li>
  );
}

/**
 * What the bundle will put into the knowledge graph, counted.
 *
 * Derived here rather than reported by the server, because the server only
 * knows once it has written it — and by then the button is pressed. It is the
 * same walk `kg_seed.py` does: a node per component and per thing inside a
 * component's facet.
 */
function seedCount(arch: ArchState): number {
  let n = Object.keys(arch.components).length;
  for (const c of Object.values(arch.components)) {
    const f = c.facet;
    if (!f) continue;
    switch (f.facet_kind) {
      case "api": n += f.endpoints.length; break;
      case "store": n += f.entities.length; break;
      case "queue": n += f.messages.length; break;
      case "service": n += f.modules?.length ?? 0; break;
      case "llm": n += f.tasks.length; break;
      case "infra": n += f.units.length; break;
    }
  }
  return n;
}

/**
 * A titled block of the sheet.
 *
 * `foldable` is for the sections that are record rather than ruling — what is
 * thin, what was already settled, what you already answered. They are worth
 * having in the sheet and are not worth the vertical space they were taking
 * above the button.
 */
function Section({ label, count, note, foldable = false, children }: {
  label: string;
  count?: number;
  note?: string;
  foldable?: boolean;
  children: React.ReactNode;
}) {
  const head = (
    <>
      <span className="section-name">{label}</span>
      {count !== undefined && <span className="section-count">{count}</span>}
      {note && <span className="section-note">{note}</span>}
    </>
  );
  if (!foldable) {
    return (
      <section className="gsec">
        <div className="section-label">{head}</div>
        {children}
      </section>
    );
  }
  return (
    <details className="gsec foldable">
      <summary className="section-label">
        {head}
        <span className="spacer" />
        <span className="chev" aria-hidden>⌄</span>
      </summary>
      {children}
    </details>
  );
}

/**
 * One decision, scannable closed and complete open.
 *
 * The category leads as an eyebrow rather than sitting in a left-hand column:
 * at this type size the column was spending 110px of a 780px sheet on one word
 * and pushing every title onto a second line. Closed, the row answers "what was
 * decided and which way did it go"; that is the whole question at a gate. The
 * rationale and the roads not taken are one click down.
 */
function DecisionRow({ d }: { d: Decision }) {
  // the chosen option is already the headline — what is worth reading here is
  // what it beat, and a decision that beat nothing is worth seeing as such
  const alternatives = d.options.filter((o) => o.name !== d.choice);
  return (
    <details className="drow">
      <summary>
        <span className="drow-eyebrow">
          <span className="topic">{d.category}</span>
          {d.status === "deferred" && <span className="tag">deferred</span>}
          {d.source === "user" && <span className="tag your-call">your call</span>}
          <span className="spacer" />
          <span className="chev" aria-hidden>⌄</span>
        </span>
        <span className="drow-topic">{d.topic}</span>
        <span className="drow-choice"><span className="arrow" aria-hidden>→</span> {d.choice}</span>
      </summary>
      <div className="drow-body">
        <div className="field">
          <div className="field-label">Why</div>
          <p>{d.rationale || "No rationale was recorded."}</p>
        </div>
        <div className="field">
          <div className="field-label">Weighed against</div>
          {alternatives.length > 0 ? (
            <ul className="opts">
              {alternatives.map((o) => <li key={o.name}>{o.name}</li>)}
            </ul>
          ) : (
            <p className="faint">Nothing else was put on the table.</p>
          )}
        </div>
      </div>
    </details>
  );
}

function QuestionRow({ q }: { q: OpenQuestion }) {
  const unanswered = !q.resolution;
  return (
    <div className="qrow" data-unanswered={unanswered}>
      <div className="drow-eyebrow">
        <span className="topic">{q.resolution ?? "unanswered"}</span>
        {q.blocking && unanswered && <span className="tag blocking">blocking</span>}
      </div>
      <div className="qrow-text">{q.question}</div>
      {q.answer && (
        <div className="qrow-answer"><span className="arrow" aria-hidden>→</span> {q.answer}</div>
      )}
    </div>
  );
}

export function Gate({ req, reason, onRespond }: {
  req: PermissionEvent;
  /** Whatever is in the composer travels with the ruling — an overruled
   *  blocker is only worth recording if it carries the user's actual reason. */
  reason: string;
  onRespond: () => void;
}) {
  const arch = useSession((s) => s.arch);
  const finalize = req.kind === "finalize";
  const blockers = req.blockers ?? [];
  const thin = [...(req.thin ?? []), ...(req.gaps ?? [])];
  const title = finalize ? "Finalize this architecture?" : "Approve the top level?";

  const [open, setOpen] = useState(true);
  useEffect(() => { setOpen(true); }, [req]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const decisions = arch?.decisions ?? [];
  const questions = arch?.questions ?? [];
  // §9 splits these rather than sorting them: one is a call to action and the
  // other is a record, and a sorted single list made them look like one thing.
  const unanswered = useMemo(() => questions.filter((q) => !q.resolution), [questions]);
  const answered = useMemo(() => questions.filter((q) => q.resolution), [questions]);
  const settled = (arch?.concerns ?? []).filter((c) => c.status !== "open");

  if (!open) {
    return (
      <button
        className="gate-tuck"
        data-kind={req.kind}
        onClick={() => setOpen(true)}
        title="reopen the request"
      >
        <span className="gate-tuck-text">
          <b>Waiting on you</b>
          <span>{title}</span>
        </span>
        <span className="gate-tuck-cta">Reopen</span>
      </button>
    );
  }

  return (
    <>
      <div className="sheet-veil" onClick={() => setOpen(false)} />
      <div className="sheet" data-kind={req.kind} role="dialog" aria-label={title}>
        <div className="sheet-head">
          <div className="sheet-title">
            <h3>{title}</h3>
            <p>
              {finalize
                ? "This is what the handoff bundle will carry into the build."
                : "This is the shape the build will follow."}{" "}
              Read what you doubt, then rule on it.
            </p>
          </div>
          <span className="spacer" style={{ flex: 1 }} />
          <button
            className="ghost tuck"
            title="the request stays open and waits at the bottom of the canvas (esc)"
            onClick={() => setOpen(false)}
          >
            Hide
          </button>
        </div>

        <div className="sheet-body">
          {/* Blockers first, and stated as a consequence rather than a recap:
              pressing the button is what overrules them. */}
          {blockers.length > 0 && (
            <div className="blocker-warn">
              <b>{plural(blockers.length, "open blocker")}.</b>{" "}
              {finalize ? "Finalizing" : "Approving"} records{" "}
              {blockers.length === 1 ? "it" : "them"} as overruled, with whatever is in the
              composer as the reason.
              <ul>{blockers.map((c) => <ConcernLine key={c.id} c={c} />)}</ul>
            </div>
          )}

          {unanswered.length > 0 && (
            <Section
              label="Still unanswered"
              count={unanswered.length}
              note="answering one in the chat is better than ruling around it"
            >
              {unanswered.map((q) => <QuestionRow key={q.id} q={q} />)}
            </Section>
          )}

          {decisions.length > 0 && (
            <Section label="Decisions" count={decisions.length} note="open one to read why">
              {decisions.map((d) => <DecisionRow key={d.id} d={d} />)}
            </Section>
          )}

          {answered.length > 0 && (
            <Section label="Answered" count={answered.length} foldable>
              {answered.map((q) => <QuestionRow key={q.id} q={q} />)}
            </Section>
          )}

          {thin.length > 0 && (
            <Section label="Thin" count={thin.length} note="none of it is required" foldable>
              <ul className="weigh">
                {thin.map((t) => <li key={t}>{t}</li>)}
              </ul>
            </Section>
          )}

          {settled.length > 0 && (
            <Section label="Settled objections" count={settled.length} foldable>
              <ul className="weigh">
                {settled.map((c) => <ConcernLine key={c.id} c={c} />)}
              </ul>
            </Section>
          )}

          {finalize && arch && (
            <Section label="What the handoff writes">
              <div className="handoff">
                <div className="field">
                  <div className="field-label">Bundle</div>
                  <p className="mono">
                    {(req.artifacts ?? []).join("  ") || "architecture.json · architecture.md"}
                  </p>
                </div>
                <div className="field">
                  <div className="field-label">Graph</div>
                  <p>
                    About {seedCount(arch)} nodes seeded into the knowledge graph, so the build
                    session can query this design on its first turn.
                  </p>
                </div>
              </div>
            </Section>
          )}
        </div>

        <div className="sheet-foot">
          <button className="primary" onClick={() => { respondToGate(true, reason); onRespond(); }}>
            {finalize ? "Finalize" : "Approve"}
          </button>
          <button onClick={() => { respondToGate(false, reason); onRespond(); }}>
            Request changes
          </button>
          <span className="spacer" style={{ flex: 1 }} />
          <span className="foot-note">
            {blockers.length > 0
              ? "What you type in the composer is recorded as why you overruled."
              : "Or just reply in the chat — that counts as requesting changes."}
          </span>
        </div>
      </div>
    </>
  );
}
