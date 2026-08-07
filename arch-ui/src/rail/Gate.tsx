/**
 * The ruling, handover §9 — a 720px sheet over a dimmed canvas.
 *
 * It used to be a note pinned to the composer, and it used to say almost
 * nothing: the summary was prose the agent had just written in the transcript,
 * and every concern in it had a tab of its own a few inches to the right, so
 * restating both was how it grew tall enough to need scrolling.
 *
 * Those tabs are gone (§1). Decisions and questions no longer live anywhere
 * else in the page, and this is one of exactly two moments they change what you
 * press — so the gate now carries them in full, and it outgrew the rail doing
 * it. It is a sheet, not a modal-with-a-veil-you-cannot-dismiss: the canvas
 * behind it stays readable, and replying in the chat still counts as requesting
 * changes.
 */
import { useEffect, useMemo, useState } from "react";
import { respondToGate, useSession } from "../store/session";
import { Markdown } from "./Markdown";
import type { ArchState, Concern, PermissionEvent } from "../types";

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

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="section-label">{label}</div>
      {children}
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
  const questions = useMemo(() => {
    // unanswered above answered (§9): the ones that can still change the design
    // come first, and a blocking one is flagged rather than reordered again
    const qs = arch?.questions ?? [];
    return [...qs].sort((a, b) => Number(!!a.resolution) - Number(!!b.resolution));
  }, [arch?.questions]);
  const settled = (arch?.concerns ?? []).filter((c) => c.status !== "open");

  if (!open) {
    return (
      <button className="gate-pill" data-kind={req.kind} onClick={() => setOpen(true)}>
        <span className="severity-dot blocker" /> {title} <span className="faint">— open</span>
      </button>
    );
  }

  return (
    <>
      <div className="sheet-veil" onClick={() => setOpen(false)} />
      <div className="sheet" data-kind={req.kind} role="dialog" aria-label={title}>
        <div className="sheet-head">
          <h3>{title}</h3>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="ghost tuck" title="tuck away — the request stays open (esc)"
                  aria-label="tuck away" onClick={() => setOpen(false)}>⌄</button>
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

          {decisions.length > 0 && (
            <Section label={`decisions — ${decisions.length}`}>
              {decisions.map((d) => (
                <div key={d.id} className="srow">
                  <span className="k">{d.category}</span>
                  <span className="v">
                    <b>{d.topic} → {d.choice}</b>
                    {d.status === "deferred" && <span className="tag" style={{ marginLeft: 6 }}>deferred</span>}
                    {d.source === "user" && <span className="tag" style={{ marginLeft: 6 }}>your call</span>}
                    <div className="faint">{d.rationale}</div>
                    <div className="faint mono" style={{ fontSize: 10 }}>
                      weighed: {d.options.map((o) => o.name).join(" · ") || "—"}
                      {d.options.length < 2 ? "  (no alternative)" : ""}
                    </div>
                  </span>
                </div>
              ))}
            </Section>
          )}

          {questions.length > 0 && (
            <Section label={`questions — ${questions.filter((q) => !q.resolution).length} unanswered`}>
              {questions.map((q) => (
                <div key={q.id} className="srow">
                  <span className="k">{q.resolution ?? "open"}</span>
                  <span className="v">
                    {q.question}
                    {q.blocking && !q.resolution && (
                      <span className="tag" style={{ marginLeft: 6 }}>blocking</span>
                    )}
                    {q.answer && <div className="faint">→ {q.answer}</div>}
                  </span>
                </div>
              ))}
            </Section>
          )}

          {thin.length > 0 && (
            <Section label={`thin — ${thin.length}, none required`}>
              <ul className="weigh" style={{ margin: 0, paddingLeft: 16 }}>
                {thin.map((t) => <li key={t}>{t}</li>)}
              </ul>
            </Section>
          )}

          {settled.length > 0 && (
            <Section label={`settled objections — ${settled.length}`}>
              <ul className="weigh" style={{ margin: 0, paddingLeft: 16 }}>
                {settled.map((c) => <ConcernLine key={c.id} c={c} />)}
              </ul>
            </Section>
          )}

          {finalize && arch && (
            <Section label="what the handoff writes">
              <div className="srow">
                <span className="k">bundle</span>
                <span className="v mono" style={{ fontSize: 10.5 }}>
                  {(req.artifacts ?? []).join("  ") || "architecture.json · architecture.md"}
                </span>
              </div>
              <div className="srow">
                <span className="k">graph</span>
                <span className="v">
                  about {seedCount(arch)} nodes seeded into the knowledge graph, so the build
                  session can query this design on its first turn.
                </span>
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
          <span className="faint" style={{ fontSize: 11 }}>
            {blockers.length > 0
              ? "What you type in the composer is recorded as why you overruled."
              : "Or just reply in the chat — that counts as requesting changes."}
          </span>
        </div>
      </div>
    </>
  );
}
