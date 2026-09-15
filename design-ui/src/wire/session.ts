import { useSyncExternalStore } from "react";
import {
  ask, boardLine, card, dropTurn, getChat, hasAsk, nextTurnId, patchTurn, push, reopenAsk, resetChat, say,
  settleAskInMessage, spendAsk, you,
} from "@arch/board/chat";
import type { PickerOption, PickerPayload } from "@arch/board/picker";
import { splitTask } from "@arch/wire/task";
import { setFocus } from "../board/ui";
import { abandon, feed, messageDone, resetDrafts, retireArrived, retireOldest } from "./drafts";
import { onCaptureRequest, watchCapture } from "./capture";
import { ToolArgs } from "./partial";
import type {
  ConnState, DesignStateEvent, Incoming, ReadyEvent, Status, ToolCallDelta, WireArtboard, WireCritique,
} from "./types";

/**
 * Harness truth. Every field here is written by an event and never by the page.
 *
 * `design_state` replaces the whole session wholesale — the page never diffs
 * it, except to notice which artboards have just arrived so the drafts that
 * were standing in for them can retire.
 */

/** What the designer's hands are on right now, read off the tool call it is
 *  streaming or running: the artboard, and the verb for the frame's chip. */
export interface Working { artboard: string; verb: string }

export interface SessionState {
  conn: ConnState;
  ready: ReadyEvent | null;
  status: Status | "";
  prompt: string;
  theme: string | null;
  themeCss: string;
  artboards: WireArtboard[];
  html: Record<string, string>;
  /** the critic's latest note per artboard */
  critiques: Record<string, WireCritique>;
  planCritique: string | null;
  /** hand edits the designer has not been shown; they ride the next message */
  pendingEdits: number;
  /** the harness's own selection, as of the last push */
  serverSelected: string | null;
  finalizedArtboard: string | null;
  /** the showcase phase's elevated artboard, or null outside the phase */
  showcaseArtboard: string | null;
  /** a question is on the table: the designer is not running */
  asking: boolean;
  /** the brief has gone out; intake answers are history now */
  intakeLocked: boolean;
  /** ids of the intake questions, so the host knows which asks it may reopen */
  intake: string[];
  running: boolean;
  /** the one transient line about what is happening right now — planning,
   *  capturing, the critic reading — or "" when nothing is worth saying */
  progress: string;
  working: Working | null;
  /** everything already on the board when this page connected: born in,
   *  not arrivals */
  bornWith: Record<string, true>;
  totalIn: number;
  totalOut: number;
}

let state: SessionState = {
  conn: "connecting",
  ready: null,
  status: "",
  prompt: "",
  theme: null,
  themeCss: "",
  artboards: [],
  html: {},
  critiques: {},
  planCritique: null,
  pendingEdits: 0,
  serverSelected: null,
  finalizedArtboard: null,
  showcaseArtboard: null,
  asking: false,
  intakeLocked: false,
  intake: [],
  running: false,
  progress: "",
  working: null,
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

export const titleOf = (id: string) => state.artboards.find((a) => a.id === id)?.title ?? id;

/* ── the one line about now ───────────────────────────────────────────── */

let progressTimer: ReturnType<typeof setTimeout> | null = null;

/** Say what is happening; with `ms`, only for that long. A later line
 *  replaces an earlier one outright — there is one slot, on purpose. */
function setProgress(text: string | null, ms?: number): void {
  if (progressTimer) { clearTimeout(progressTimer); progressTimer = null; }
  const next = text ?? "";
  if (state.progress !== next) set({ progress: next });
  if (next && ms) {
    progressTimer = setTimeout(() => {
      progressTimer = null;
      if (state.progress === next) set({ progress: "" });
    }, ms);
  }
}

/** How many items a critique carries — its bullets, or one for prose. */
export function critiqueNotes(text: string): number {
  const bullets = text.split("\n").filter((l) => /^\s*(?:[-*•]|\d+[.)])\s+/.test(l)).length;
  return bullets || (text.trim() ? 1 : 0);
}
const notesWord = (n: number) => `${n} ${n === 1 ? "note" : "notes"}`;
/** The critic's skip note rides in the same field as a real critique. */
const skipped = (text: string) => /^not critiqued\b/i.test(text.trim());

watchCapture((phase, artboard) => {
  const t = titleOf(artboard);
  if (phase === "requested") setProgress(`capturing ${t} for the critic`);
  else if (phase === "captured") setProgress(`the critic is reading ${t}`);
  else setProgress(`the page could not capture ${t}`, 6000);
});

/* ── turning harness events into a conversation ───────────────────────── */

/** The intake's composed opening message — `opening_message` in intake.py.
 *  It reaches the page as the turn's task, but nobody typed it: the answers
 *  were picked, and the brief was given before the questions. */
const OPENING = /^Direction: (.+?)\. Design system: (.+?)\.(?:\s*Brief:\s*([\s\S]*))?$/;

/** The first sentence of a refusal, for the line that reports it. */
const firstSentence = (s: string) => {
  const t = s.trim();
  const m = /^[\s\S]*?[.!?](?=\s|$)/.exec(t);
  return (m ? m[0] : t).slice(0, 140);
};

/** One tool call, in design terms, and the artboard it touched so "show me"
 *  has somewhere to go. */
export function toolLine(name: string, isError: boolean, details: Record<string, unknown> | null | undefined) {
  const d = (details ?? {}) as {
    artboard?: unknown; version?: unknown; theme?: unknown; finalized?: unknown; artboards?: unknown;
    error?: unknown; path?: unknown; applied?: unknown; failed?: unknown; ops?: unknown;
  };
  const aid = typeof d.artboard === "string" ? d.artboard : typeof d.finalized === "string" ? d.finalized : "";
  const v = typeof d.version === "string" ? d.version : "";
  const title = aid ? titleOf(aid) : "";
  const ids = aid ? [aid] : Array.isArray(d.artboards) ? d.artboards.map(String) : [];
  const verb = name.replace(/^design_/, "").replace(/_/g, " ");
  if (isError) {
    const why = typeof d.error === "string" && d.error.trim() ? ` — ${firstSentence(d.error)}` : "";
    return { said: `${verb} was refused${why}`, ids };
  }
  let said: string;
  switch (name) {
    case "design_create": said = v === "v1" ? `put ${title} on the board` : `rebuilt ${title} as ${v}`; break;
    case "design_edit": {
      const n = typeof d.applied === "number" ? d.applied : Array.isArray(d.ops) ? d.ops.length : 1;
      const failed = typeof d.failed === "number" && d.failed > 0 ? ` (${d.failed} refused)` : "";
      said = (n > 1 ? `made ${n} edits to ${title} → ${v}` : `edited ${title} → ${v}`) + failed;
      break;
    }
    case "design_undo": said = `undid ${title} → ${v}`; break;
    case "design_delete_artboard": said = `deleted ${title}`; break;
    case "design_set_theme": said = `set the ${String(d.theme ?? "")} theme`.replace("the  theme", "a theme"); break;
    case "design_finalize": said = `finalized ${title}`; break;
    case "design_read": said = `read ${title}`; break;
    case "design_themes": said = "listed the themes"; break;
    case "design_status": said = "checked where things stand"; break;
    case "design_plan": said = "planned the direction"; break;
    case "design_look": {
      const file = typeof d.path === "string" ? d.path.split("/").pop() ?? "" : "";
      said = file ? `looked at ${file}` : "looked at the image";
      break;
    }
    case "design_critique": said = `asked the critic about ${title}`; break;
    case "design_showcase": said = `moved ${title} to the showcase`; break;
    case "design_polish": said = `polished ${title}`; break;
    case "skill": said = "loaded a skill"; break;
    default: said = verb;
  }
  return { said, ids };
}

/** Everything a turn's machinery has done, as one line: three artboards put
 *  on the board are one act, not three lines with two of them lost, and
 *  twenty edits to one artboard are "made 20 edits", not twenty clauses. */
const list = (xs: string[]) =>
  xs.length <= 1 ? xs.join("") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1];
export function mergeSaid(said: string[]): string {
  const PUT = /^put (.+) on the board$/;
  const EDIT = /^(?:edited|made (\d+) edits to) (.+) → (v\d+)((?: \(\d+ refused\))?)$/;
  const puts: string[] = [];
  const edits = new Map<string, { n: number; v: string; tail: string }>();
  const rest: Array<string | { edit: string }> = [];
  for (const s of said) {
    const p = PUT.exec(s);
    if (p) { puts.push(p[1]); continue; }
    const e = EDIT.exec(s);
    if (e) {
      const cur = edits.get(e[2]);
      const n = e[1] ? Number(e[1]) : 1;
      if (cur) { cur.n += n; cur.v = e[3]; cur.tail = e[4] || cur.tail; }
      else { edits.set(e[2], { n, v: e[3], tail: e[4] }); rest.push({ edit: e[2] }); }
      continue;
    }
    rest.push(s);
  }
  const out = puts.length ? [`put ${list(puts)} on the board`] : [];
  for (const r of rest) {
    if (typeof r === "string") { out.push(r); continue; }
    const e = edits.get(r.edit)!;
    out.push((e.n > 1 ? `made ${e.n} edits to ${r.edit} → ${e.v}` : `edited ${r.edit} → ${e.v}`) + e.tail);
  }
  return "design · " + out.join(" · ");
}
/** what the open turn's machinery has said so far */
let turnSaid: { said: string[]; ids: string[] } = { said: [], ids: [] };

/** The turn the designer is speaking into, so its tool lines land under the
 *  text that describes them rather than in a block of their own. */
let openTurnId: number | null = null;
let thinkingId: number | null = null;

/** Text arrives a token at a time. It lands in a turn of its own that grows. */
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

/* ── what the designer is doing, and to what ──────────────────────────── */

/** The chip a frame wears while a call about it streams or runs. */
const VERB: Record<string, string> = {
  design_edit: "editing", design_undo: "editing", design_polish: "polishing",
  design_read: "reading", design_critique: "reviewing", design_delete_artboard: "deleting",
  design_showcase: "showcasing",
};

interface Call { name: string; args: ToolArgs }
/** the calls of the message being streamed, by index */
let calls = new Map<number, Call>();
/** the calls of the message being run, in order — a tool_result retires one */
let queue: Array<{ name: string; artboard: string | null }> = [];

/** Which artboard a call is about: the one it names, else the harness's own
 *  default — its selection, or the only one there is. */
function targetOf(c: Call): string | null {
  if (c.args.artboard) return c.args.artboard;
  if (state.serverSelected) return state.serverSelected;
  return state.artboards.length === 1 ? state.artboards[0].id : null;
}

function setWorking(name: string, artboard: string | null): void {
  const verb = VERB[name];
  const next = verb && artboard ? { artboard, verb } : null;
  const cur = state.working;
  if ((cur?.artboard ?? null) === (next?.artboard ?? null) && (cur?.verb ?? null) === (next?.verb ?? null)) return;
  set({ working: next });
}

function onToolDelta(d: ToolCallDelta): void {
  const index = d.index ?? 0;
  let c = calls.get(index);
  if (!c) { c = { name: d.name ?? "", args: new ToolArgs() }; calls.set(index, c); }
  if (d.name && !c.name) c.name = d.name;
  /* the designer is writing an artboard: draw it as it comes. The draft
     store decodes a create's arguments itself; the rest are read here for
     the artboard they name. */
  if (c.name === "design_create") feed(index, d.name ?? "", d.text ?? "", state.artboards);
  else c.args.feed(d.text ?? "");
  if (c.name === "design_plan") setProgress("planning the direction");
  else if (c.name in VERB) setWorking(c.name, targetOf(c));
}

/** The message is written; its calls are about to run, first one first. */
function callsWritten(): void {
  queue = [...calls.entries()].sort(([a], [b]) => a - b)
    .map(([, c]) => ({ name: c.name, artboard: c.name === "design_create" ? null : targetOf(c) }));
  calls = new Map();
  const head = queue[0];
  if (head) setWorking(head.name, head.artboard);
  else setWorking("", null);
}

/** One call has run: the designer's hands move to the next. */
function callRan(): void {
  queue.shift();
  const head = queue[0];
  if (head) setWorking(head.name, head.artboard);
  else setWorking("", null);
}

/* ── the plan critique, once, as a card ───────────────────────────────── */

let planCritiqueShown = false;
function showPlanCritique(text: string): void {
  if (planCritiqueShown || !text.trim() || skipped(text)) return;
  planCritiqueShown = true;
  card(`Plan critique · ${notesWord(critiqueNotes(text))}`, text);
}

/** The turn holding a question, if it has been asked. */
function turnOfAsk(id: string): number | null {
  for (const t of getChat().turns) if (t.t === "say" && t.ask?.picker.id === id) return t.id;
  return null;
}

/** A question the harness has on record: asked once, and settled here the
 *  moment the harness says it is — whichever side answered it. A late joiner
 *  gets every answered question back as its answered row. */
function ensureAsk(picker: PickerPayload) {
  let host = turnOfAsk(picker.id);
  if (host === null) {
    host = push({ t: "say", id: nextTurnId(), lines: [] });
    ask(host, picker);
  }
  if (picker.answered) {
    const turn = getChat().turns.find((t) => t.id === host);
    if (turn && turn.t === "say" && turn.ask && !turn.ask.spent) {
      spendAsk(host, picker.label ?? picker.value ?? "", picker.value);
    }
  }
}

function onDesignState(ev: DesignStateEvent) {
  const prev = state;
  const finalized = ev.status === "finalized";
  const bornWith = ev.replayed
    ? Object.fromEntries(ev.artboards.map((a) => [a.id, true as const]))
    : state.bornWith;
  const serverSelected = ev.selected ?? null;
  set({
    status: ev.status,
    prompt: ev.prompt ?? "",
    theme: ev.theme ?? null,
    themeCss: ev.theme_css ?? "",
    artboards: ev.artboards,
    html: ev.html ?? {},
    critiques: ev.critiques ?? {},
    planCritique: ev.plan_critique ?? null,
    pendingEdits: ev.pending_user_edits ?? 0,
    serverSelected,
    finalizedArtboard: ev.finalized_artboard ?? null,
    showcaseArtboard: ev.showcase_artboard ?? null,
    asking: Boolean(ev.ask),
    intakeLocked: Boolean(ev.intake_locked),
    intake: (ev.intake ?? []).map((p) => p.id),
    bornWith,
    ...(finalized && state.conn !== "complete" ? { conn: "complete" as ConnState } : {}),
  });
  /* The harness selects what it just made or rebuilt. Follow it when it
     moves; otherwise the page keeps its own place. */
  if (serverSelected && serverSelected !== prev.serverSelected) setFocus(serverSelected);
  /* an artboard that has arrived retires the draft that stood in for it */
  retireArrived(prev.artboards, ev.artboards);
  for (const p of ev.intake ?? []) ensureAsk(p);
  if (ev.ask && !hasAsk(ev.ask.id)) ensureAsk(ev.ask);
  /* a late joiner whose transcript buffer no longer holds the plan's result
     still gets the critique, off the state */
  if (ev.plan_critique) showPlanCritique(ev.plan_critique);
}

/** Why a turn stopped, in the page's words. "you interrupted it" is said
 *  only when the harness says it was the user: a stopped turn used to be
 *  blamed on the person whatever tore it down. */
export function stopCopy(reason: unknown): string {
  if (reason === "user") return "_You stopped the turn._";
  if (reason === "shutdown") return "_The session is closing._";
  return "_The turn stopped._";
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

    case "design_state":
      onDesignState(ev);
      break;

    case "capture_request":
      /* the critic wants the artboard as it renders: route the request to
         the mounted frame; the bridge's reply POSTs back to /capture */
      onCaptureRequest(ev);
      break;

    case "harness_event": {
      const { event, data } = ev;
      if (event === "run_start") {
        const opening = data.task ? OPENING.exec(data.task) : null;
        if (opening) {
          you(opening[3]?.trim() || undefined, undefined, "picked",
              [`Direction: ${opening[1]}`, `Design system: ${opening[2]}`]);
        } else if (data.task) {
          const { drew, about, picked, typed } = splitTask(data.task);
          if (drew.length || about.length || picked.length) {
            const via = !typed && drew.length && !picked.length ? "on the board"
              : !typed && picked.length ? "picked" : undefined;
            you(typed || undefined, undefined, via, drew, about);
          } else {
            you(data.task);
          }
          for (const t of getChat().turns) {
            if (t.t === "say" && t.ask && !t.ask.spent) {
              if (picked.length) spendAsk(t.id, picked[0]);
              else settleAskInMessage(t.id);
            }
          }
        }
        openTurnId = null;
        turnSaid = { said: [], ids: [] };
        finishStream();
        calls = new Map();
        queue = [];
        setProgress(null);
        set({ running: true, working: null });
        startThinking();
      } else if (event === "assistant_delta") {
        stopThinking();
        streamText += data.text || "";
        if (streamId === null) streamId = push({ t: "say", id: nextTurnId(), lines: [streamText] });
        else patchTurn(streamId, { lines: [streamText] } as never);
      } else if (event === "tool_call_delta") {
        onToolDelta(data as ToolCallDelta);
      } else if (event === "assistant") {
        stopThinking();
        messageDone();
        callsWritten();
        const settled = finishStream(data.content || undefined);
        const before = openTurnId;
        if (settled !== null) openTurnId = settled;
        else if (data.content) openTurnId = say([data.content]);
        else if (openTurnId === null) openTurnId = push({ t: "say", id: nextTurnId(), lines: [] });
        if (openTurnId !== before) turnSaid = { said: [], ids: [] };
        if (data.tool_calls?.length) startThinking();
      } else if (event === "tool_result") {
        const name = String(data.name ?? "tool");
        const details = (data.details ?? {}) as { artboard?: unknown; critique?: unknown };
        const line = toolLine(name, Boolean(data.is_error), data.details);
        if (openTurnId === null) { openTurnId = push({ t: "say", id: nextTurnId(), lines: [] }); turnSaid = { said: [], ids: [] }; }
        turnSaid = { said: [...turnSaid.said, line.said], ids: [...new Set([...turnSaid.ids, ...line.ids])] };
        boardLine(openTurnId, mergeSaid(turnSaid.said), turnSaid.ids);
        /* the call has run: the draft that stood in for it is done, whether
           or not the push that carries the artboard has retired it already */
        if (name === "design_create") retireOldest();
        callRan();
        /* what the critic said, if it was asked — for a moment in the bar,
           and the plan's note as a card in the thread */
        const critique = typeof details.critique === "string" ? details.critique : "";
        if (critique) {
          const about = typeof details.artboard === "string" ? titleOf(details.artboard) : "the plan";
          if (name === "design_plan") {
            showPlanCritique(critique);
            setProgress(skipped(critique) ? "the plan was not critiqued" : `critic: ${notesWord(critiqueNotes(critique))} on the plan`, 6000);
          } else if (skipped(critique)) {
            setProgress(`the critic could not see ${about}`, 6000);
          } else {
            setProgress(`critic: ${notesWord(critiqueNotes(critique))} on ${about}`, 6000);
          }
        } else if (state.progress === "planning the direction") {
          setProgress(null);
        }
      } else if (event === "abort") {
        stopThinking();
        abandon();
        setProgress(null);
        set({ working: null });
        say([`_The turn stopped: ${data.reason || "no reason given"}._`]);
      }
      break;
    }

    case "turn_end":
      stopThinking();
      finishStream();
      abandon();
      openTurnId = null;
      queue = [];
      setProgress(null);
      if (ev.status === "interrupted") say([stopCopy(ev.reason)]);
      set({
        running: false,
        working: null,
        totalIn: ev.input_tokens ?? state.totalIn,
        totalOut: ev.output_tokens ?? state.totalOut,
      });
      break;

    case "error":
      stopThinking();
      finishStream();
      abandon();
      setProgress(null);
      say([`_${ev.message || "the turn failed"}_`]);
      set({ running: false, working: null });
      break;

    case "bye":
      over = true;
      source?.close();
      source = null;
      if (state.status !== "finalized") set({ conn: "disconnected", running: false });
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

/** `subjects` is what was selected on the board when Send was pressed: an
 *  artboard, or an element in one as `artboard#selector`. The harness turns
 *  them into the element's own details and puts them in front of the message. */
export function sendInput(text: string, subjects: string[] = []): void {
  void post("/input", subjects.length ? { text, subjects } : { text });
}

/* ── attachments ─────────────────────────────────────────────────────── */

/** A File as base64, without the data-url prefix. FileReader rather than
 *  btoa(String.fromCharCode(...bytes)): spreading a multi-MB screenshot into
 *  Function.apply blows the argument-count limit. */
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const url = String(r.result || "");
      const comma = url.indexOf(",");
      resolve(comma >= 0 ? url.slice(comma + 1) : url);
    };
    r.onerror = () => reject(r.error ?? new Error("could not read the file"));
    r.readAsDataURL(file);
  });
}

/** Upload a pasted or dropped image to the harness. Resolves to the
 *  reference the server saved it under — the text the message carries, so the
 *  pump's ingest flow and the designer's design_look both see the same path —
 *  or null when the upload was refused. */
export async function uploadAttachment(file: File): Promise<string | null> {
  let data = "";
  try {
    data = await fileToBase64(file);
  } catch {
    return null; // the composer says so honestly in the message text
  }
  const res = await post("/upload", { name: file.name || "pasted-image.png", data });
  if (!res || !res.ok) return null;
  const body = await res.json().catch(() => null) as { path?: unknown } | null;
  const path = body?.path;
  return typeof path === "string" && path ? path : null;
}

/** Answering with a row of the picker. The harness settles the question on
 *  its own state and starts the turn itself when the intake is complete. */
export function sendAnswer(host: number, id: string, option: PickerOption): void {
  spendAsk(host, option.label, option.value);
  void post("/answer", { id, value: option.value }).then(async (res) => {
    if (res && !res.ok) {
      const body = await res.json().catch(() => ({} as { error?: string }));
      refusal(String(body.error || "the answer was refused"));
      reopenAsk(host);
    }
  });
}

/** Send what the user changed by hand, with no words: the harness composes
 *  the edits it has not shown the designer into the message itself. */
export function sendBoard(): void {
  void post("/board", {});
}

export function interrupt(): void {
  void post("/interrupt", {});
}

export interface MutateResult { ok: boolean; version?: string; error?: string }

/** An edit, an undo or a finalize, sent to the harness. The resulting state
 *  arrives on /events like every other change; the reply is only the verdict. */
export async function mutate(payload: Record<string, unknown>): Promise<MutateResult> {
  const res = await post("/mutate", payload);
  if (res === null) return { ok: false, error: "could not reach the harness — the edit was not applied" };
  const body = await res.json().catch(() => ({} as MutateResult));
  if (res.ok) return { ok: true, ...body };
  return { ok: false, error: String(body.error || `the harness refused the edit (${res.status})`) };
}

/** A refusal belongs in the conversation: it is the harness disagreeing, and
 *  that is the same channel everything else disagrees on. */
export function refusal(message: string): void {
  say([`_${message}_`]);
}

/* ── the connection ───────────────────────────────────────────────────── */

/**
 * One SSE connection at a time, for the life of the page. The harness replays
 * a joiner (ready, a bounded transcript buffer, the latest design_state, any
 * capture still waiting), so a refresh mid-session rebuilds everything
 * without special-casing — and so does a reconnect: a dropped connection
 * used to be the end of the page, its composer announcing the harness gone
 * while the server sat there serving. Now it is retried with backoff, the
 * transcript cleared just before the replay so nothing lands twice, and only
 * a `bye` is final.
 */
const BACKOFF_MAX_MS = 15000;
let source: EventSource | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let attempt = 0;
/** the harness said goodbye: nothing to reconnect to */
let over = false;

/** The replay is about to re-deliver the whole session: forget the copy of
 *  it this page built, or every turn would appear twice. */
function resetForReplay(): void {
  resetChat();
  resetDrafts();
  openTurnId = null; thinkingId = null; streamId = null; streamText = "";
  turnSaid = { said: [], ids: [] };
  calls = new Map(); queue = [];
  planCritiqueShown = false;
  setProgress(null);
  set({ working: null, bornWith: {} });
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
    /* a finished session's server goes away on purpose; so does one that
       said goodbye. Everything else is a drop worth coming back from. */
    if (over || state.status === "finalized" || state.conn === "complete") {
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

/** Tests and fixtures: back to nothing. */
export function resetSession(): void {
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  source?.close(); source = null; attempt = 0; over = false;
  if (progressTimer) { clearTimeout(progressTimer); progressTimer = null; }
  state = { ...state, conn: "connecting", status: "", prompt: "", theme: null, themeCss: "", artboards: [], html: {},
    critiques: {}, planCritique: null, pendingEdits: 0, serverSelected: null,
    finalizedArtboard: null, showcaseArtboard: null, asking: false, intakeLocked: false, intake: [], running: false,
    progress: "", working: null, bornWith: {} };
  openTurnId = null; thinkingId = null; streamId = null; streamText = "";
  turnSaid = { said: [], ids: [] }; calls = new Map(); queue = []; planCritiqueShown = false;
  emit();
}
