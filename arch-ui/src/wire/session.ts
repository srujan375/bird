import { useSyncExternalStore } from "react";
import { boardLine, dropTurn, getChat, nextTurnId, patchTurn, push, say, setChat, you } from "../board/chat";
import { splitTask } from "./task";
import { flash } from "../board/ui";
import type { ArchState, ConnState, Incoming, ReadyEvent } from "./types";

/**
 * Harness truth. Every field here is written by an event and never by the page.
 *
 * `arch_state` replaces the whole design wholesale — the page never diffs it.
 * The only thing derived from a push is what to *highlight*: `changed` names
 * the id that just moved.
 */

export interface SessionState {
  conn: ConnState;
  ready: ReadyEvent | null;
  arch: ArchState | null;
  noticing: string[];
  changed: { kind: string; id: string } | null;
  running: boolean;
  handedOff: boolean;
  /** how much the user has drawn that the architect has not been shown */
  pendingEdits: number;
  /** everything that was already on the board when this page connected —
   *  boxes by id, wires as "src>dst". A refresh delivers the whole design at
   *  once, and animating it would claim eleven boxes were just created. These
   *  are born already-in. */
  bornWith: Record<string, true>;
  totalIn: number;
  totalOut: number;
}

let state: SessionState = {
  conn: "connecting",
  ready: null,
  arch: null,
  noticing: [],
  changed: null,
  running: false,
  handedOff: false,
  pendingEdits: 0,
  bornWith: {},
  totalIn: 0,
  totalOut: 0,
};

const listeners = new Set<() => void>();
const emit = () => { for (const l of listeners) l(); };
export const getSession = () => state;
const set = (patch: Partial<SessionState>) => { state = { ...state, ...patch }; emit(); };

export function useSession(): SessionState {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    getSession,
    getSession,
  );
}

/* ── turning harness events into a conversation ───────────────────────── */

/** The one line the architect's machinery gets, and the boxes it touched so
 *  "show me" has somewhere to go. */
function toolLine(name: string, details: Record<string, unknown> | null | undefined) {
  const d = (details ?? {}) as { subjects?: unknown; nodes?: unknown; summary?: unknown };
  const subjects = Array.isArray(d.subjects) ? d.subjects.map(String) : [];
  const nodes = Array.isArray(d.nodes) ? d.nodes.map(String) : [];
  /* the tool's own first line, which already says what it did; the tool name
     alone tells you a call happened but not what changed */
  const said = typeof d.summary === "string" && d.summary ? d.summary : name;
  return { text: "board · " + said.replace(/^Board: /, ""), ids: subjects.length ? subjects : nodes };
}

/** The turn the architect is speaking into, so its tool lines land under the
 *  text that describes them rather than in a block of their own. */
let openTurnId: number | null = null;
let thinkingId: number | null = null;

/** Text arrives a token at a time. It lands in a turn of its own that grows,
 *  rather than appearing whole when the turn is already over — the waiting is
 *  most of the turn, and a page that shows nothing during it reads as stuck. */
let streamId: number | null = null;
let streamText = "";

function finishStream(final?: string) {
  if (streamId === null) return null;
  const id = streamId;
  const text = final ?? streamText;
  streamId = null;
  streamText = "";
  if (!text) { dropTurn(id); return null; }
  patchTurn(id, { lines: [text] } as never);
  return id;
}

function stopThinking() {
  if (thinkingId !== null) { dropTurn(thinkingId); thinkingId = null; }
}

function startThinking() {
  if (thinkingId === null) thinkingId = push({ t: "thinking", id: nextTurnId() });
}

export function applyEvent(ev: Incoming): void {
  switch (ev.type) {
    case "ready":
      set({
        ready: ev,
        conn: state.conn === "complete" ? "complete" : "connected",
        totalIn: ev.input_tokens ?? 0,
        totalOut: ev.output_tokens ?? 0,
      });
      break;

    case "arch_state": {
      const handedOff = ev.status === "handed_off";
      /* Only a replayed push is history. A live one is something that just
         happened, however early in the session it happens to be. */
      const bornWith = ev.replayed
        ? Object.fromEntries([
            ...Object.keys(ev.state.nodes).map((id) => [id, true as const]),
            ...ev.state.edges.map((e) => [`${e.src}>${e.dst}`, true as const]),
          ])
        : state.bornWith;
      set({
        arch: ev.state,
        noticing: ev.noticing ?? [],
        changed: ev.changed,
        pendingEdits: ev.pending_edits ?? 0,
        bornWith,
        ...(handedOff && !state.handedOff ? { handedOff: true, conn: "complete" as ConnState } : {}),
      });
      break;
    }

    case "harness_event": {
      const { event, data } = ev;
      if (event === "run_start") {
        if (data.task) {
          /* A message can be drawn, typed, pointed, or all three — the harness
             sends them as one turn, so the page has to show every half or the
             typed words disappear from the record. */
          const { drew, about, typed } = splitTask(data.task);
          if (drew.length || about.length) {
            const via = !typed && drew.length ? "on the board" : undefined;
            you(typed || undefined, undefined, via, drew, about);
          } else {
            you(data.task);
          }
        }
        openTurnId = null;
        finishStream();
        set({ running: true });
        startThinking();
      } else if (event === "assistant_delta") {
        stopThinking();
        streamText += data.text || "";
        if (streamId === null) streamId = push({ t: "say", id: nextTurnId(), lines: [streamText] });
        else patchTurn(streamId, { lines: [streamText] } as never);
      } else if (event === "assistant") {
        stopThinking();
        /* the streamed copy and the final one are the same words — settle the
           turn that has been growing rather than saying it all twice */
        const settled = finishStream(data.content || undefined);
        if (settled !== null) openTurnId = settled;
        else if (data.content) openTurnId = say([data.content]);
        else if (openTurnId === null) openTurnId = push({ t: "say", id: nextTurnId(), lines: [] });
        if (data.tool_calls?.length) startThinking();
      } else if (event === "tool_result") {
        const line = toolLine(String(data.name ?? "tool"), data.details);
        if (openTurnId === null) openTurnId = push({ t: "say", id: nextTurnId(), lines: [] });
        boardLine(openTurnId, line.text, line.ids);
        /* halo everything that call touched, so a change that lands while you
           are reading elsewhere is still visible when you look back */
        if (line.ids.length) flash(line.ids);
      } else if (event === "abort") {
        stopThinking();
        say([`_The turn stopped: ${data.reason || "no reason given"}._`]);
      }
      break;
    }

    case "turn_end":
      stopThinking();
      finishStream();
      openTurnId = null;
      set({
        running: false,
        totalIn: ev.input_tokens ?? state.totalIn,
        totalOut: ev.output_tokens ?? state.totalOut,
      });
      break;

    case "error":
      stopThinking();
      finishStream();
      say([`_${ev.message || "the turn failed"}_`]);
      set({ running: false });
      break;

    case "bye":
      if (!state.handedOff) set({ conn: "disconnected", running: false });
      break;
  }
}

/* ── talking back ─────────────────────────────────────────────────────── */

async function post(path: string, body: unknown): Promise<Response | null> {
  try {
    return await fetch(path, { method: "POST", body: JSON.stringify(body ?? {}) });
  } catch {
    return null; // surfaced through the connection state, not a toast
  }
}

/** `subjects` is what was selected on the board when Send was pressed. The
 *  harness turns the ids into the boxes' own details and puts them in front of
 *  the message, so a question that points at something arrives knowing what it
 *  pointed at. */
export function sendInput(text: string, subjects: string[] = []): void {
  void post("/input", subjects.length ? { text, subjects } : { text });
}

/**
 * Send what the user drew.
 *
 * Explicit on purpose. An earlier version watched for the board to go quiet
 * and submitted by itself, which spent a model call every time the user paused
 * to think and asked the architect to respond to half-finished gestures. Only
 * the person drawing knows when they have finished a sentence.
 */
export function sendBoard(): void {
  void post("/board", {});
}

export function interrupt(): void {
  void post("/interrupt", {});
}

/**
 * A user edit: sent, and rolled back onto the page if the harness refuses.
 *
 * There is no optimistic copy of the design here — `arch_state` is a full
 * replacement and the push that a successful mutation triggers arrives in
 * milliseconds. What the page *does* hold optimistically is position, because
 * a box that snaps back under your cursor while the round trip lands is worse
 * than one that arrives a frame late.
 */
export async function mutate(payload: Record<string, unknown>): Promise<string | null> {
  const res = await post("/mutate", payload);
  if (res === null) return "could not reach the harness — the edit was not applied";
  if (res.ok) return null;
  const body = await res.json().catch(() => ({} as { error?: string }));
  return String(body.error || `the harness refused the edit (${res.status})`);
}

/** A refusal belongs in the conversation: it is the harness disagreeing, and
 *  that is the same channel everything else disagrees on. */
export function refusal(message: string): void {
  say([`_${message}_`]);
}

/* ── the connection ───────────────────────────────────────────────────── */

/**
 * One SSE connection for the life of the page. The harness replays late
 * joiners (ready, a bounded transcript buffer, the latest arch_state), so a
 * refresh mid-session rebuilds everything without special-casing.
 */
export function connect(): void {
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    let parsed: Incoming;
    try {
      parsed = JSON.parse(e.data) as Incoming;
    } catch {
      return; // a malformed frame must never take the page down
    }
    applyEvent(parsed);
  };
  es.onerror = () => {
    es.close();
    if (!getSession().handedOff) set({ conn: "disconnected", running: false });
  };
}

export const chatIsOpen = () => getChat().open;
export const markChat = setChat;
