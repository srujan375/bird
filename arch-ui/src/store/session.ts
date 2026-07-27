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

const CHANGE_RING_MS = 2200;

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

  apply: (ev: WireEvent) => void;
  disconnect: () => void;
  clearPermission: () => void;
  setRunning: (v: boolean) => void;
  expireChanges: () => void;
}

const NOTICE_GLYPHS: Record<string, string> = {
  variant: "◇", node: "+", link: "→", splice: "⤙", depth: "↕", promote: "⤴",
  brief: "≡", component: "+", connect: "→", flow: "◇", decide: "◆",
  concern: "⚑", ask: "?", answer: "✓", expand: "▸", amend_toplevel: "±", done: "⛳",
  read: "≡", kg_query: "⌕", WebSearch: "⌕", WebFetch: "≡", skill: "☰",
};

/** One compact activity line per tool call, keeping today's glyph vocabulary. */
export function noticeFor(call: { name: string; arguments_json: string }): string {
  let arg = "";
  try {
    const a = JSON.parse(call.arguments_json || "{}");
    arg =
      a.id || a.component_id || (a.src && `${a.src} → ${a.dst}`) || a.topic ||
      a.claim || a.node_id || a.variant_id || a.path || a.question ||
      a.query || a.url || a.name || "";
  } catch {
    /* leave arg empty */
  }
  const glyph = NOTICE_GLYPHS[call.name] || "·";
  return `${glyph} ${call.name}${arg ? " " + String(arg).slice(0, 72) : ""}`;
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

  apply: (ev) => {
    switch (ev.type) {
      case "ready":
        set((s) => ({ ready: ev, conn: s.conn === "complete" ? "complete" : "connected" }));
        break;

      case "arch_state": {
        const finalized = ev.phase === "finalized";
        set((s) => {
          const recentlyChanged = { ...s.recentlyChanged };
          if (ev.changed?.id) recentlyChanged[ev.changed.id] = Date.now() + CHANGE_RING_MS;
          return {
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
            transcript: data.task ? [...s.transcript, { t: "user", text: data.task }] : s.transcript,
            running: true,
            stream: "",
            turnError: null,
          }));
        } else if (event === "assistant_delta") {
          set((s) => ({ stream: (s.stream ?? "") + (data.text || "") }));
        } else if (event === "assistant") {
          set((s) => {
            const next = [...s.transcript];
            if (data.content) next.push({ t: "agent", text: data.content });
            for (const call of data.tool_calls || []) next.push({ t: "notice", text: noticeFor(call) });
            return { transcript: next, stream: s.running ? "" : null };
          });
        } else if (event === "tool_result" && data.is_error) {
          set((s) => ({
            transcript: [...s.transcript,
              { t: "notice", err: true, text: `✗ ${data.name} — see the agent's next step` }],
          }));
        } else if (event === "abort") {
          set((s) => ({
            transcript: [...s.transcript, { t: "notice", err: true, text: `✗ aborted: ${data.reason || ""}` }],
          }));
        }
        break;
      }

      case "permission_request":
        if (ev.kind === "toplevel_approval" || ev.kind === "finalize") {
          set({ permission: ev, ...(ev.kind === "finalize" ? { artifacts: ev.artifacts || [] } : {}) });
        }
        break;

      case "turn_end":
        set((s) => {
          const next = [...s.transcript];
          if (s.stream) next.push({ t: "agent", text: s.stream });
          next.push({
            t: "turn",
            status: ev.status,
            message: ev.status === "error" ? s.turnError || "the turn failed" : null,
          });
          return { running: false, stream: null, transcript: next };
        });
        break;

      case "error":
        set((s) => ({
          turnError: ev.message || "the turn failed",
          transcript: [...s.transcript, { t: "notice", err: true, text: `✗ ${ev.message || "error"}` }],
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
  useSession.setState((s) => ({ transcript: [...s.transcript, { t: "notice", err, text }] }));
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
  // replying while a gate is open is "request changes", the same as today's page
  if (permission) return respondToGate(false, trimmed);
  useSession.setState({ running: true });
  void post("/input", { text: trimmed });
}

export function respondToGate(approved: boolean, feedback = ""): void {
  const req = useSession.getState().permission;
  if (!req) return;
  useSession.setState({ permission: null, running: true });
  void post("/permission", { id: req.id, approved, feedback });
}

export function interrupt(): void {
  void post("/interrupt", {});
}
