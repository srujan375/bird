import { useSyncExternalStore } from "react";
import { ask, boardLine, dropTurn, getChat, hasAsk, nextTurnId, patchTurn, push, queueAsk, resetChat, say, setChat, settleAskInMessage, spendAsk, you } from "../board/chat";
import type { PickerOption } from "../board/picker";
import { splitTask } from "./task";
import { flash } from "../board/ui";
import type { ArchState, ConnState, Frontier, Incoming, ReadyEvent, ScribeEvent } from "./types";

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
  /** what is still askable, and what the user closed — from the harness */
  frontier: Frontier | null;
  /** the scribe's state, when one is drawing the board a step behind */
  scribe: ScribeEvent | null;
  /** a scribe is on: the board's lines belong to its strip, not the thread */
  scribeOn: boolean;
  /** the research turn's steps, in order */
  research: { step: string; text: string; done: boolean }[];
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
  frontier: null,
  scribe: null,
  scribeOn: false,
  research: [],
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
/** the research turn's list, while it is the live indicator */
let progressId: number | null = null;

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
        scribeOn: Boolean(ev.scribe),
      });
      break;

    case "scribe":
      set({ scribe: ev });
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
        frontier: ev.frontier ?? state.frontier,
        bornWith,
        ...(handedOff && !state.handedOff ? { handedOff: true, conn: "complete" as ConnState } : {}),
      });
      /* The one question on the table, if there is one. The harness re-pushes
         its whole state on every change, so the id is what makes this idempotent
         — and because it only ever sends the earliest open question, the next
         one appears here exactly when the last is answered. */
      if (ev.ask && !hasAsk(ev.ask.id)) {
        if (openTurnId === null) openTurnId = push({ t: "say", id: nextTurnId(), lines: [] });
        ask(openTurnId, ev.ask);
      }
      break;
    }

    case "harness_event": {
      const { event, data } = ev;
      if (event === "run_start") {
        if (data.task) {
          /* A message can be drawn, typed, pointed, or all three — the harness
             sends them as one turn, so the page has to show every half or the
             typed words disappear from the record. */
          const { drew, about, picked, typed } = splitTask(data.task);
          /* A bare pick says nothing here: the row it answered has already
             collapsed into the answered block above, which is the record. A
             turn reading "you · picked" under it is the same fact twice. */
          const bare = picked.length && !typed && !drew.length && !about.length;
          if (bare) {
            /* nothing to add to the thread */
          } else if (drew.length || about.length || picked.length) {
            const via = !typed && drew.length && !picked.length ? "on the board"
              : !typed && picked.length ? "picked" : undefined;
            you(typed || undefined, undefined, via, drew, about);
          } else {
            you(data.task);
          }
          /* A pick settles the question it answered; a typed reply with no
             pick means they answered in prose — the rows stay as the record
             of what was offered, but none of them was taken. */
          for (const t of getChat().turns) {
            if (t.t === "say" && t.ask && !t.ask.spent) {
              if (picked.length) spendAsk(t.id, picked[0]);
              else settleAskInMessage(t.id);
            }
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
        /* With a scribe on, the board's lines belong to its strip: the thread
           carries the argument only. The halo still says what moved. */
        if (!state.scribeOn) {
          if (openTurnId === null) openTurnId = push({ t: "say", id: nextTurnId(), lines: [] });
          boardLine(openTurnId, line.text, line.ids);
        }
        /* halo everything that call touched, so a change that lands while you
           are reading elsewhere is still visible when you look back */
        if (line.ids.length) flash(line.ids);
      } else if (event === "research") {
        /* the research turn: one line per kind of work, in place of the
           thinking dots, kept afterwards as the record of the first minute */
        const step = String(data.step ?? "");
        const text = String(data.text ?? step);
        const done = Boolean(data.done);
        const steps = state.research.some((r) => r.step === step)
          ? state.research.map((r) => (r.step === step ? { ...r, text, done } : r))
          : [...state.research, { step, text, done }];
        set({ research: steps });
        stopThinking();
        if (progressId === null) progressId = push({ t: "progress", id: nextTurnId(), steps });
        else patchTurn(progressId, { steps } as never);
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
      if (progressId !== null) {
        /* the list stays as the record; nothing in it is live any more */
        const steps = state.research.map((r) => ({ ...r, done: true }));
        patchTurn(progressId, { steps } as never);
        set({ research: steps });
        progressId = null;
      }
      if (ev.status === "interrupted") say([stopCopy(ev.reason)]);
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
      over = true;
      source?.close();
      source = null;
      if (!state.handedOff) set({ conn: "disconnected", running: false });
      break;
  }
}

/** Why a turn stopped, in the page's words. "you interrupted it" is said
 *  only when the harness says it was the user. */
export function stopCopy(reason: unknown): string {
  if (reason === "user") return "_You stopped the turn._";
  if (reason === "shutdown") return "_The session is closing._";
  return "_The turn stopped._";
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
 * Answering with a row of the picker.
 *
 * The answer goes to the harness, not to the message box: the harness settles
 * the question on its own state and starts the turn itself, prefixed so the
 * transcript records "picked" rather than words the user never typed. That
 * order matters — the state is what decides which question is next, so the
 * page can never get ahead of it.
 */
export function sendAnswer(host: number, id: string, option: PickerOption): void {
  /* While the architect is still writing, the harness parks the answer until
     the turn ends. The dock says so; the turn the answer starts is what
     collapses the question (run_start carries the pick). */
  if (state.running) queueAsk(host, option.label, option.value);
  else spendAsk(host, option.label, option.value);  // it reads as done the frame it happens
  void post("/answer", { id, value: option.value });
}

/** The user closes a branch from the frontier, or reopens one. */
export function closeBranch(op: "settle" | "out_of_scope" | "reopen", id: string): Promise<string | null> {
  return mutate({ op, id });
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
 * One SSE connection at a time, for the life of the page. The harness replays
 * a joiner (ready, a bounded transcript buffer, the latest arch_state), so a
 * refresh mid-session rebuilds everything without special-casing — and so
 * does a reconnect: a dropped connection used to be the end of the page, its
 * composer announcing the harness gone while the server sat there serving.
 * Now it is retried with backoff, the transcript cleared just before the
 * replay so nothing lands twice, and only a `bye` is final.
 */
const BACKOFF_MAX_MS = 15000;
let source: EventSource | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let attempt = 0;
/** the harness said goodbye: nothing to reconnect to */
let over = false;

function resetForReplay(): void {
  resetChat();
  openTurnId = null; thinkingId = null; streamId = null; streamText = ""; progressId = null;
  set({ bornWith: {}, research: [] });
}

export function connect(): void {
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  if (typeof EventSource === "undefined") return;
  source?.close();
  const es = new EventSource("/events");
  source = es;
  es.onopen = () => {
    attempt = 0;
    if (state.conn === "reconnecting") resetForReplay();
  };
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
    if (source !== es) return; // an older connection, already replaced
    es.close();
    source = null;
    if (over || state.handedOff || state.conn === "complete") {
      if (state.conn !== "complete") set({ conn: "disconnected", running: false });
      return;
    }
    const delay = Math.min(BACKOFF_MAX_MS, 1000 * 2 ** attempt);
    attempt++;
    if (state.conn !== "reconnecting") set({ conn: "reconnecting" });
    retryTimer = setTimeout(connect, delay);
  };
}

/** How the page is doing on the wire, for tests. */
export const connection = () => ({ attempt, retrying: retryTimer !== null, over });

/** Tests: back to a fresh page. */
export function resetSession(): void {
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  source?.close(); source = null; attempt = 0; over = false;
  state = { ...state, conn: "connecting", arch: null, noticing: [], changed: null, running: false, handedOff: false,
    pendingEdits: 0, bornWith: {}, frontier: null, scribe: null, scribeOn: false, research: [] };
  openTurnId = null; thinkingId = null; streamId = null; streamText = ""; progressId = null;
  emit();
}

export const chatIsOpen = () => getChat().open;
export const markChat = setChat;
