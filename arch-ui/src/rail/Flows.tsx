/**
 * The Flows column, handover §4.
 *
 * It stays open beside the chat rather than living behind a tab, and the reason
 * is specific: the other tabs that were removed were *views of the design* —
 * the same components, re-listed. Flows is not. It is the only place the
 * sequence of a system is written down, and it is most useful while you are
 * typing about it. A tab you have to leave the conversation to read gets read
 * once.
 *
 * Hovering a flow lights its connections on the canvas and dims everything
 * else; playing one walks the steps. Both are canvas-store flips, so the
 * highlighting is CSS and a slow render never stutters the walk.
 */
import { useEffect } from "react";
import { useCanvas } from "../store/canvas";
import { useSession } from "../store/session";
import type { ArchState, Flow } from "../types";

/** §4: one step per 820ms. Slow enough to follow a hop, fast enough that a
 *  six-step flow does not outlast your attention. */
const STEP_MS = 820;

const GROUPS: { kind: Flow["kind"]; label: string }[] = [
  { kind: "happy", label: "happy path" },
  { kind: "failure", label: "failure" },
  { kind: "background", label: "background" },
];

/** Drives playback: advances a step every STEP_MS and stops itself at the end. */
function usePlayback(flows: Flow[]): void {
  const playing = useCanvas((s) => s.flowPlaying);
  const step = useCanvas((s) => s.flowStep);
  const setFlowStep = useCanvas((s) => s.setFlowStep);
  const playFlow = useCanvas((s) => s.playFlow);

  useEffect(() => {
    if (!playing) return;
    const flow = flows.find((f) => f.id === playing);
    if (!flow || flow.steps.length === 0) { playFlow(null); return; }
    if (step >= flow.steps.length - 1) {
      // hold the last step on screen for one beat, then release the canvas
      const done = setTimeout(() => playFlow(null), STEP_MS);
      return () => clearTimeout(done);
    }
    const next = setTimeout(() => setFlowStep(step + 1), STEP_MS);
    return () => clearTimeout(next);
  }, [playing, step, flows, setFlowStep, playFlow]);
}

function FlowRow({ flow }: { flow: Flow }) {
  const lit = useCanvas((s) => s.flowLit);
  const playing = useCanvas((s) => s.flowPlaying);
  const step = useCanvas((s) => s.flowStep);
  const litFlow = useCanvas((s) => s.litFlow);
  const playFlow = useCanvas((s) => s.playFlow);

  const on = lit === flow.id;
  const isPlaying = playing === flow.id;
  const pct = isPlaying && flow.steps.length
    ? ((step + 1) / flow.steps.length) * 100
    : 0;

  return (
    <div
      className="flow-row"
      data-on={on}
      onMouseEnter={() => litFlow(flow.id)}
      onMouseLeave={() => litFlow(null)}
    >
      <div className="flow-top">
        <button
          className="flow-play"
          title={isPlaying ? "stop" : "play this flow on the canvas"}
          onClick={() => playFlow(isPlaying ? null : flow.id)}
        >
          {isPlaying ? "■" : "▶"}
        </button>
        <span className="flow-name" title={flow.name}>{flow.name}</span>
        <span className="mono flow-count">
          {flow.steps.length} step{flow.steps.length === 1 ? "" : "s"}
        </span>
      </div>

      <div className="flow-scrub"><div className="flow-fill" style={{ width: `${pct}%` }} /></div>

      {/* the steps read src → dst then the action, so a row maps onto the
          canvas without a legend */}
      <ol className="flow-steps" data-open={on}>
        {flow.steps.map((s, i) => (
          <li key={i} data-on={isPlaying && i === step}>
            <span className="mono pair">{s.src} → {s.dst}</span> {s.action}
          </li>
        ))}
      </ol>
    </div>
  );
}

export function Flows({ arch }: { arch: ArchState | null }) {
  const flows = arch?.flows ?? [];
  usePlayback(flows);
  const decisions = arch?.decisions.length ?? 0;
  const questions = arch?.questions.filter((q) => !q.resolution).length ?? 0;

  return (
    <div className="flows">
      <div className="flows-head">
        <span className="section-label">flows</span>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="mono tiny faint">{flows.length || ""}</span>
      </div>

      <div className="flows-body">
        {flows.length === 0 && (
          <div className="empty-note">
            No flows yet. A flow is the sequence a request actually takes — it is
            what turns a set of boxes into a system.
          </div>
        )}

        {GROUPS.map(({ kind, label }) => {
          const rows = flows.filter((f) => f.kind === kind);
          if (rows.length === 0) return null;
          return (
            <div key={kind} className="flow-group">
              <div className="flow-kind"><span className="dotk" data-kind={kind} />{label}</div>
              {rows.map((f) => <FlowRow key={f.id} flow={f} />)}
            </div>
          );
        })}

        {/* Their absence is an answer, not a gap: two tabs were removed and a
            reader who remembers them deserves to be told where they went. */}
        <div className="flows-foot faint">
          {decisions} decision{decisions === 1 ? "" : "s"} and {questions} open
          question{questions === 1 ? "" : "s"} are carried to the finalize gate and
          into the handoff bundle — the two moments they change what you press.
        </div>
      </div>
    </div>
  );
}

export function FlowsColumn() {
  const arch = useSession((s) => s.arch);
  return <Flows arch={arch} />;
}
