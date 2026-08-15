/**
 * Server truth. Every field in here is written by an event and never by the UI.
 *
 * `arch_state` replaces the whole state object — no client-side diffing of the
 * design itself. The only thing we derive from the diff is what to *highlight*:
 * `changed` names the id that just moved, and recentlyChanged keeps it ringed
 * for a couple of seconds.
 */
import { create } from "zustand";
import type {
  ArchState, Component, ConnState, PermissionEvent, ReadyEvent, Renders, TranscriptItem, WireEvent,
} from "../types";

/** How long a just-changed id stays ringed. Exported because the canvas needs
 *  to read *when* something landed, and `recentlyChanged` stores the expiry —
 *  subtracting this is the only way back to the arrival time. */
export const CHANGE_RING_MS = 2200;

interface SessionState {
  conn: ConnState;
  ready: ReadyEvent | null;
  arch: ArchState | null;
  renders: Renders | null;
  tracker: string;
  gaps: Record<string, string[]>;
  changed: { kind: string; id: string } | null;
  recentlyChanged: Record<string, number>; // id -> expiry timestamp
  transcript: TranscriptItem[];
  stream: string | null; // in-flight assistant text
  running: boolean;
  permission: PermissionEvent | null;
  artifacts: string[];
  turnError: string | null;
  finalized: boolean;
  /** Turn accounting for the divider (§3): which turn, what it cost, how many
   *  tools it ran. Reset when the next turn starts. */
  turnNo: number;
  turnTools: number;
  turnTokens: number;
  /** Server truth for the composer's cumulative spend: every token this
   *  session consumed across the arch runner, its dispatches and the critic.
   *  The server owns the tally — turn_end carries the running total, we just
   *  display it. Zeroed (or re-seeded) on each fresh `ready`. */
  totalIn: number;
  totalOut: number;
  /**
   * Ids present in the *first* arch_state of this page load.
   *
   * §3: an entrance means "this arrived". On a resume — or any refresh
   * mid-session — the whole design arrives in one push, and animating all of
   * it would claim eleven components were just created. These are born
   * already-in instead.
   */
  bornWith: Record<string, true>;
  /**
   * Bumped every time the user says something — typed, tapped or dismissed.
   *
   * The transcript alone cannot carry this: the column only follows the stream
   * while you are already near the bottom, and an answer you gave while reading
   * back through the turn would land off-screen. Acting is the one event that
   * should always drag the view to your own words, so it is a signal of its own
   * rather than something Chat infers from the tail of the log.
   */
  acted: number;
  /** Concern ids the critic has already been credited with in the transcript.
   *  The critic never sends a message of its own — it files a Concern and the
   *  state push is the only trace — so its voice is derived, and derived
   *  exactly once. */
  announced: Record<string, true>;

  apply: (ev: WireEvent) => void;
  disconnect: () => void;
  clearPermission: () => void;
  setRunning: (v: boolean) => void;
  expireChanges: () => void;
}

/** The one argument worth showing for a call — whichever field names the thing
 *  it acted on. A tool row is only useful if you can tell *which* component it
 *  just touched. */
/**
 * A tool's result, as lines under its row (§2).
 *
 * The wire carries `details` — whatever the tool chose to report about itself —
 * not the output text, so this renders that rather than inventing a transcript
 * of output the page never received. Capped per §5: nothing in a tool result is
 * load-bearing enough to justify an unbounded block in a 350px column.
 */
export function resultLines(details: Record<string, unknown> | null | undefined): string[] {
  if (!details || typeof details !== "object") return [];
  const out: string[] = [];
  for (const [k, v] of Object.entries(details)) {
    if (v === null || v === undefined || v === "" || v === false) continue;
    if (k === "done") continue; // control flow, not a result
    const val = Array.isArray(v) ? v.join(", ") : String(v);
    out.push(`${k} · ${val.length > 90 ? val.slice(0, 90) + "…" : val}`);
    if (out.length === 7) break; // 6 shown, plus one to know there are more
  }
  return out;
}

export function toolArg(call: { name: string; arguments_json: string }): string {
  try {
    const a = JSON.parse(call.arguments_json || "{}");
    const arg =
      a.id || a.component_id || (a.src && `${a.src} → ${a.dst}`) || a.topic ||
      a.claim || a.node_id || a.variant_id || a.path || a.question ||
      a.query || a.url || a.name || "";
    return String(arg).slice(0, 72);
  } catch {
    return ""; // malformed arguments are the model's problem, not the page's
  }
}


export const useSession = create<SessionState>((set, get) => ({
  conn: "connecting",
  ready: null,
  arch: null,
  renders: null,
  tracker: "",
  gaps: {},
  changed: null,
  recentlyChanged: {},
  transcript: [],
  stream: null,
  running: false,
  permission: null,
  artifacts: [],
  turnError: null,
  finalized: false,
  turnNo: 0,
  turnTools: 0,
  turnTokens: 0,
  totalIn: 0,
  totalOut: 0,
  bornWith: {},
  announced: {},
  acted: 0,

  apply: (ev) => {
    switch (ev.type) {
      case "ready":
        // ready is the session boundary for token accounting too: a respawn
        // re-seeds the cumulative spend from the payload, a fresh connect
        // starts from zero (absent field means both look the same).
        set((s) => ({
          ready: ev,
          conn: s.conn === "complete" ? "complete" : "connected",
          totalIn: ev.input_tokens ?? 0,
          totalOut: ev.output_tokens ?? 0,
        }));
        break;

      case "arch_state": {
        const finalized = ev.phase === "finalized";
        set((s) => {
          const recentlyChanged = { ...s.recentlyChanged };
          if (ev.changed?.id) recentlyChanged[ev.changed.id] = Date.now() + CHANGE_RING_MS;

          // The critic reviews on its own thread and files Concerns; it never
          // speaks. Without this, an objection appears on the canvas with no
          // trace in the conversation of who raised it or when — which reads
          // as the architect quietly changing its mind.
          let transcript = s.transcript;
          const announced = s.announced;
          let nextAnnounced = announced;
          if (ev.changed?.kind === "concern" && !announced[ev.changed.id]) {
            const c = ev.state.concerns.find((x) => x.id === ev.changed!.id);
            if (c && c.source === "judge") {
              transcript = [...transcript, {
                t: "agent" as const,
                who: "critic" as const,
                at: Date.now(),
                text: c.alternative ? `${c.claim}\n\n_instead:_ ${c.alternative}` : c.claim,
              }];
              nextAnnounced = { ...announced, [ev.changed.id]: true as const };
            }
          }

          // first state wins: everything in it predates this page
          const bornWith = s.arch === null
            ? Object.fromEntries([
                ...Object.keys(ev.state.components).map((id) => [id, true as const]),
                ...ev.state.connections.map((c) => [`${c.src}->${c.dst}`, true as const]),
              ])
            : s.bornWith;

          return {
            transcript,
            announced: nextAnnounced,
            bornWith,
            arch: ev.state,
            renders: ev.renders,
            tracker: ev.tracker,
            gaps: ev.gaps ?? {},
            changed: ev.changed,
            recentlyChanged,
            // finalize closes the session: the gate is gone and the page is read-only
            ...(finalized && !s.finalized
              ? { finalized: true, conn: "complete" as ConnState, permission: null }
              : {}),
          };
        });
        break;
      }

      case "harness_event": {
        const { event, data } = ev;
        if (event === "run_start") {
          set((s) => ({
            transcript: data.task
              ? [...s.transcript, { t: "user" as const, text: data.task, at: Date.now() }]
              : s.transcript,
            running: true,
            stream: "",
            turnError: null,
            turnTools: 0,
            turnTokens: 0,
          }));
        } else if (event === "assistant_delta") {
          set((s) => ({ stream: (s.stream ?? "") + (data.text || "") }));
        } else if (event === "assistant") {
          set((s) => {
            const next = [...s.transcript];
            const at = Date.now();
            if (data.content) next.push({ t: "agent", text: data.content, at, who: "architect" });
            for (const call of data.tool_calls || []) {
              next.push({
                t: "tool",
                name: call.name,
                arg: toolArg(call),
                at,
                // optimistic: a call the model made is running until a result
                // says otherwise. The alternative — waiting for the result —
                // means the row appears after the work it describes.
                status: "running",
              });
            }
            return {
              transcript: next,
              stream: s.running ? "" : null,
              turnNo: data.turn ?? s.turnNo,
              turnTools: s.turnTools + (data.tool_calls?.length ?? 0),
              // input_tokens is the whole prompt, so the latest one *is* the
              // current context depth — not something to accumulate
              turnTokens: data.input_tokens ?? s.turnTokens,
            };
          });
        } else if (event === "tool_result") {
          set((s) => {
            // settle the most recent still-running row for this tool; calls
            // resolve in order, so the newest running one is this result's
            const next = [...s.transcript];
            for (let i = next.length - 1; i >= 0; i--) {
              const item = next[i];
              if (item.t === "tool" && item.name === data.name && item.status === "running") {
                next[i] = {
                  ...item,
                  status: data.is_error ? "error" : "ok",
                  lines: resultLines(data.details),
                };
                break;
              }
            }
            return { transcript: next };
          });
        } else if (event === "abort") {
          set((s) => ({
            transcript: [...s.transcript,
              { t: "notice", err: true, text: `✗ aborted: ${data.reason || ""}`, at: Date.now() }],
          }));
        }
        break;
      }

      case "permission_request":
        // Allowlisted, not open: an unknown kind means the harness is asking
        // something this page has no affordance for, and a request nobody can
        // answer would wedge the session behind a blocked tool call.
        if (ev.kind === "toplevel_approval" || ev.kind === "finalize" || ev.kind === "offer") {
          set({ permission: ev, ...(ev.kind === "finalize" ? { artifacts: ev.artifacts || [] } : {}) });
        }
        break;

      case "turn_end":
        set((s) => {
          const next = [...s.transcript];
          const at = Date.now();
          if (s.stream) next.push({ t: "agent", text: s.stream, at, who: "architect" });
          next.push({
            t: "turn",
            status: ev.status,
            message: ev.status === "error" ? s.turnError || "the turn failed" : null,
            at,
            n: s.turnNo,
            model: s.ready?.model ?? "",
            inTokens: s.turnTokens,
            tools: s.turnTools,
          });
          // any row still "running" when the turn closes never got a result —
          // an interrupt, or the turn died. Leaving it spinning forever would
          // be the page lying about what the harness is doing.
          for (let i = 0; i < next.length; i++) {
            const item = next[i];
            if (item.t === "tool" && item.status === "running") next[i] = { ...item, status: "ok" };
          }
          return {
            running: false,
            stream: null,
            transcript: next,
            // Server truth for the cumulative totals: what the session spent
            // so far (the server already folded in dispatches and the judge).
            // Absent only from an older server — keep the last known values.
            totalIn: ev.input_tokens ?? s.totalIn,
            totalOut: ev.output_tokens ?? s.totalOut,
          };
        });
        break;

      case "error":
        set((s) => ({
          turnError: ev.message || "the turn failed",
          transcript: [...s.transcript,
            { t: "notice" as const, err: true, text: `✗ ${ev.message || "error"}`, at: Date.now() }],
        }));
        break;

      case "bye":
        if (!get().finalized) get().disconnect();
        break;
    }
  },

  disconnect: () => set((s) => (s.finalized ? {} : { conn: "disconnected", running: false })),
  clearPermission: () => set({ permission: null }),
  setRunning: (v) => set({ running: v }),
  expireChanges: () =>
    set((s) => {
      const now = Date.now();
      const live = Object.entries(s.recentlyChanged).filter(([, exp]) => exp > now);
      if (live.length === Object.keys(s.recentlyChanged).length) return {};
      return { recentlyChanged: Object.fromEntries(live) };
    }),
}));

// ---------- POSTs ----------

async function post(path: string, body: unknown): Promise<void> {
  try {
    await fetch(path, { method: "POST", body: JSON.stringify(body ?? {}) });
  } catch {
    /* surfaced through the connection state, not a toast */
  }
}

/** A line in the transcript. Edits the user makes are session history too —
 *  they belong in the same log as everything else that changed the design. */
function notice(text: string, err = false): void {
  useSession.setState((s) => ({
    transcript: [...s.transcript, { t: "notice" as const, err, text, at: Date.now() }],
  }));
}

// ---------- mutations ----------

/**
 * A user edit: applied on the canvas immediately, then sent.
 *
 * The optimistic copy exists so typing feels like typing, and it is thrown
 * away the moment the server answers — either by the `arch_state` push that a
 * successful mutation triggers, or by an explicit rollback. The canvas must
 * never be left showing something the harness does not believe.
 */
export async function mutate(
  payload: Record<string, unknown>,
  optimistic?: (state: ArchState) => ArchState,
): Promise<boolean> {
  const before = useSession.getState().arch;
  let guess: ArchState | null = null;
  if (before && optimistic) {
    guess = optimistic(before);
    useSession.setState({ arch: guess });
  }
  let error = "";
  try {
    const res = await fetch("/mutate", { method: "POST", body: JSON.stringify(payload) });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      error = String(body.error || `the harness refused the edit (${res.status})`);
    }
  } catch {
    error = "could not reach the harness — the edit was not applied";
  }
  if (!error) return true;
  // roll back, unless a state push has already landed and superseded the guess
  if (guess && useSession.getState().arch === guess) useSession.setState({ arch: before });
  notice(`✗ ${error}`, true);
  return false;
}

/** Optimistic patch for a component edit — the only field-level guess we make. */
export function withComponent(
  state: ArchState,
  id: string,
  patch: Partial<Component>,
): ArchState {
  const current = state.components[id];
  if (!current) return state;
  return { ...state, components: { ...state.components, [id]: { ...current, ...patch } } };
}

export function editComponent(id: string, patch: Partial<Component>): Promise<boolean> {
  return mutate({ op: "component", id, ...patch }, (s) => withComponent(s, id, patch));
}

export function resolveConcern(id: string, status: string, resolution: string): Promise<boolean> {
  return mutate({ op: "concern", id, status, resolution }, (s) => ({
    ...s,
    concerns: s.concerns.map((c) => (c.id === id ? { ...c, status, resolution } : c)) as ArchState["concerns"],
  }));
}

/** Promotion reshapes the whole design, so there is no honest optimistic
 *  version of it — wait for the server's state and say so meanwhile. */
export function promoteVariant(variantId: string, replace: boolean): Promise<boolean> {
  return mutate({ op: "promote", variant_id: variantId, replace });
}

export function sendInput(text: string): void {
  const trimmed = text.trim();
  if (!trimmed) return;
  const { permission } = useSession.getState();
  // Replying while a *gate* is open is "request changes", the same as today's
  // page. Replying to an *offer* is the answer itself — the options are a
  // shortcut, not the whole answer space, and "about 5k" is a better fact than
  // whichever bucket the user would have rounded it into.
  if (permission?.kind === "offer") return respondToGate(true, trimmed);
  if (permission) return respondToGate(false, trimmed);
  // No echo here: a plain message *does* start a turn, and the harness's
  // `run_start` carries the text back — with any image path already rewritten
  // to the copy it saved, which is the version worth showing.
  useSession.setState((s) => ({ running: true, acted: s.acted + 1 }));
  void post("/input", { text: trimmed });
}

/**
 * Answer a gate or an offer — and say so in the transcript.
 *
 * The echo happens here rather than being left to the `run_start` echo that
 * covers an ordinary message, because a gate answer does not start a turn: it
 * unblocks a tool call inside the turn already running, so no `run_start` ever
 * arrives. Without this the chat column was the one surface that did not know
 * you had spoken — the answer reached the model, the brief and the questions
 * list, and your own words appeared nowhere in your own conversation.
 *
 * A tapped option and a typed answer are the same thing on the wire and are
 * echoed the same way; the options are a shortcut, not a different act.
 *
 * An answer with no text is a dismissal, and lands as a notice rather than an
 * empty bubble: "I don't know yet" is a real answer to an offer, and approving
 * a gate without comment is a real ruling. Both belong on the record.
 */
export function respondToGate(approved: boolean, feedback = ""): void {
  const req = useSession.getState().permission;
  if (!req) return;
  const at = Date.now();
  const echo: TranscriptItem = feedback
    ? { t: "user", text: feedback, at }
    : {
        t: "notice",
        at,
        text:
          req.kind === "offer" ? "— dismissed the question" :
          approved ? "— approved" : "— requested changes",
      };
  useSession.setState((s) => ({
    permission: null,
    running: true,
    transcript: [...s.transcript, echo],
    acted: s.acted + 1,
  }));
  void post("/permission", { id: req.id, approved, feedback });
}

export function interrupt(): void {
  void post("/interrupt", {});
}
