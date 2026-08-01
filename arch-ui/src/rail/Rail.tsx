/**
 * The right rail. Concerns get a tab of their own — an objection the agent
 * raised and nobody can see is the same as no objection at all.
 */
import { useState } from "react";
import { useCanvas } from "../store/canvas";
import { resolveConcern, useSession } from "../store/session";
import { Chat } from "./Chat";
import { Gate } from "./Gate";
import { Markdown } from "./Markdown";
import { RailGrip } from "./RailGrip";
import type { ArchState, Concern } from "../types";

const SEVERITY_RANK: Record<string, number> = { blocker: 0, risk: 1, smell: 2 };

/**
 * Settling an objection from the rail, rather than only at the finalize gate.
 *
 * Overruling asks for the reason and will not proceed without it — that
 * sentence is what the code harness inherits as "we knew, we chose anyway",
 * and a placeholder in its place is worth less than nothing. Accepting is the
 * design having changed, so the reason there is optional.
 */
function ConcernActions({ c }: { c: Concern }) {
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
      <div className="card-actions">
        <button onClick={() => setMode("accepted")}>Accept</button>
        <button onClick={() => setMode("overruled")}>Overrule</button>
      </div>
    );
  }
  const needsReason = mode === "overruled";
  return (
    <div className="card-actions column">
      <textarea
        autoFocus
        rows={2}
        value={reason}
        placeholder={needsReason
          ? "why you're going ahead anyway — this is the record"
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
      <div className="card-actions">
        <button
          className="primary"
          disabled={busy || (needsReason && !reason.trim())}
          onClick={send}
        >
          {mode === "overruled" ? "Overrule it" : "Mark accepted"}
        </button>
        <button disabled={busy} onClick={() => { setMode(null); setReason(""); }}>Cancel</button>
      </div>
    </div>
  );
}

function ConcernsPanel({ arch, gaps }: { arch: ArchState; gaps: Record<string, string[]> }) {
  const finalized = useSession((s) => s.finalized);
  const conn = useSession((s) => s.conn);
  const editable = !finalized && conn !== "disconnected";
  const concerns = [...arch.concerns].sort((a, b) => {
    if (a.status === "open" !== (b.status === "open")) return a.status === "open" ? -1 : 1;
    return (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9);
  });
  const flatGaps = Object.entries(gaps).flatMap(([subject, list]) => list.map((g) => `${subject}: ${g}`));

  return (
    <div className="rail-body">
      {concerns.length === 0 && (
        <div className="empty-note">
          No objections on the record yet. The architect files one when it thinks something is
          wrong; a critic reviews the design in the background and files its own.
        </div>
      )}
      {concerns.map((c: Concern) => (
        <div key={c.id} className="card" data-severity={c.severity} data-settled={c.status !== "open"}>
          <div className="head">
            <span className="tag">{c.severity}</span>
            <b className="mono" style={{ fontSize: 11 }}>{c.target}</b>
            <span className="spacer" style={{ flex: 1 }} />
            <span className="tag">{c.status === "open" ? c.source : c.status}</span>
          </div>
          <Markdown text={c.claim} style={{ color: "var(--ink)" }} />
          {c.alternative && <Markdown text={`instead: ${c.alternative}`} />}
          {c.resolution && <Markdown text={`→ ${c.resolution}`} className="md faint" />}
          {editable && c.status === "open" && <ConcernActions c={c} />}
        </div>
      ))}

      {flatGaps.length > 0 && (
        <>
          <div className="section-label" style={{ marginTop: 14 }}>
            thin — {flatGaps.length}, none required
          </div>
          {flatGaps.map((g) => (
            <div key={g} className="empty-note" style={{ padding: "2px 2px" }}>· {g}</div>
          ))}
        </>
      )}
    </div>
  );
}

function DecisionsPanel({ arch }: { arch: ArchState }) {
  if (arch.decisions.length === 0) {
    return <div className="rail-body"><div className="empty-note">No decisions recorded yet.</div></div>;
  }
  return (
    <div className="rail-body">
      {arch.decisions.map((d) => (
        <div key={d.id} className="card">
          <div className="head">
            <span className="tag">{d.category}</span>
            {d.status === "deferred" && <span className="tag">deferred</span>}
          </div>
          <h4>{d.topic} → {d.choice}</h4>
          <Markdown text={d.rationale} />
          {d.options.length > 0 && (
            <p className="faint mono" style={{ fontSize: 10.5 }}>
              weighed: {d.options.map((o) => o.name).join(" · ")}
              {d.options.length < 2 ? "  (no alternative)" : ""}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

function QuestionsPanel({ arch }: { arch: ArchState }) {
  if (arch.questions.length === 0) {
    return <div className="rail-body"><div className="empty-note">No open questions.</div></div>;
  }
  return (
    <div className="rail-body">
      {arch.questions.map((q) => (
        <div key={q.id} className="card" data-settled={!!q.resolution}>
          <div className="head">
            <span className="tag">{q.source}</span>
            {q.blocking && !q.resolution && <span className="tag">blocking</span>}
          </div>
          <Markdown text={q.question} style={{ color: "var(--ink)" }} />
          {q.answer && <Markdown text={`→ ${q.answer}`} className="md faint" />}
        </div>
      ))}
    </div>
  );
}

function FlowsPanel({ arch }: { arch: ArchState }) {
  if (arch.flows.length === 0) {
    return <div className="rail-body"><div className="empty-note">No flows recorded yet.</div></div>;
  }
  return (
    <div className="rail-body">
      {arch.flows.map((f) => (
        <div key={f.id} className="card">
          <div className="head"><span className="tag">{f.kind}</span></div>
          <h4>{f.name}</h4>
          <ol style={{ margin: 0, paddingLeft: 18 }}>
            {f.steps.map((s, i) => (
              <li key={i} style={{ fontSize: 11.5, color: "var(--muted)", lineHeight: 1.55 }}>
                <span className="mono">{s.src} → {s.dst}</span> · {s.action}
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}

export function Rail() {
  const arch = useSession((s) => s.arch);
  const gaps = useSession((s) => s.gaps);
  const permission = useSession((s) => s.permission);
  const tab = useCanvas((s) => s.railTab);
  const setTab = useCanvas((s) => s.setRailTab);
  /** The composer's text, held here because the gate spends it too: a ruling
   *  carries whatever you had typed as its reason, and the gate outlives the
   *  Chat tab it was raised in. */
  const [draft, setDraft] = useState("");

  const openConcerns = arch?.concerns.filter((c) => c.status === "open").length ?? 0;
  const openQuestions = arch?.questions.filter((q) => !q.resolution).length ?? 0;

  const tabs: { id: string; label: string; count?: number; alert?: boolean }[] = [
    { id: "chat", label: "Chat", alert: !!permission },
    { id: "concerns", label: "Concerns", count: openConcerns },
    { id: "decisions", label: "Decisions", count: arch?.decisions.length ?? 0 },
    { id: "questions", label: "Questions", count: openQuestions },
    { id: "flows", label: "Flows", count: arch?.flows.length ?? 0 },
  ];

  return (
    <aside className="rail">
      <RailGrip />
      <div className="rail-tabs">
        {tabs.map((t) => (
          <button key={t.id} data-on={tab === t.id} onClick={() => setTab(t.id)}>
            {t.label}
            {t.alert ? <span className="alert-dot" title="waiting on you" /> : null}
            {t.count ? <span className="count">{t.count}</span> : null}
          </button>
        ))}
      </div>

      {/* outside the tab switch: an unanswered ruling is the one thing that
          must not disappear because you went to read the concerns */}
      {permission && (
        <Gate req={permission} reason={draft.trim()} onRespond={() => setDraft("")} />
      )}

      {tab === "chat" && <Chat draft={draft} setDraft={setDraft} />}
      {tab !== "chat" && !arch && <div className="rail-body"><div className="empty-note">Waiting for state…</div></div>}
      {tab === "concerns" && arch && <ConcernsPanel arch={arch} gaps={gaps} />}
      {tab === "decisions" && arch && <DecisionsPanel arch={arch} />}
      {tab === "questions" && arch && <QuestionsPanel arch={arch} />}
      {tab === "flows" && arch && <FlowsPanel arch={arch} />}
    </aside>
  );
}
