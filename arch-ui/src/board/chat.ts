import { useSyncExternalStore } from "react";
import { openingValue, optionAt, type PickerPayload } from "./picker";
import type { Attachment } from "./types";

/** The thread, as data.
 *
 *  Every turn the architect takes is one block: some lines, optionally a
 *  question with a picker under it, optionally the single quiet line its
 *  board machinery is allowed. */

export interface AskBlock {
  /** what the harness sent — the question, its rows, and how to confirm it */
  picker: PickerPayload;
  /** the row the radio group has checked, before it is confirmed. Selecting
   *  is not answering: the confirm button is, which is the whole point of the
   *  component. */
  selected: string;
  spent: boolean;
  pickedLabel?: string;
  /** the value behind pickedLabel, so a re-opened question opens on it */
  pickedValue?: string;
  /** answered in prose — no row was taken as-is, so none may claim it was */
  answeredInMessage?: boolean;
  /** picked while the architect was still writing: sent when the turn ends */
  queued?: boolean;
}

export interface BoardLine {
  text: string;
  ids: string[];
}

export type Turn =
  | { t: "say"; id: number; lines: string[]; line?: BoardLine; ask?: AskBlock }
  /** `via` names how they said it — absent for typing, "on the board" when the
   *  turn came from them drawing. Attributing a gesture to the keyboard would
   *  be a small lie in the one log that is supposed to be the record. */
  | { t: "you"; id: number; text?: string; files?: Attachment[]; via?: string;
      /** gestures that travelled with the message, if it came from the board */
      drew?: string[];
      /** boxes that were selected when it was sent, and so went to the
       *  architect as context. Shown because a question answered with help
       *  the transcript does not record reads later as a better guess than
       *  it was. */
      about?: string[] }
  | { t: "quiet"; id: number; text: string }
  /** a longer piece from the model's machinery — a critique, a report —
   *  folded under its title so it can be read without taking the thread over */
  | { t: "card"; id: number; title: string; text: string }
  | { t: "thinking"; id: number }
  /** the research turn: what the architect is doing, one line per kind of
   *  work, in place of the thinking dots and kept afterwards as the record */
  | { t: "progress"; id: number; steps: { step: string; text: string; done: boolean }[] };

export interface ChatState {
  turns: Turn[];
  open: boolean;
  unread: boolean;
  /** a question is on the table and has not been answered */
  pendingAsk: string | null;
}

let state: ChatState = { turns: [], open: true, unread: false, pendingAsk: null };
const listeners = new Set<() => void>();
const emit = () => { for (const l of listeners) l(); };

export const getChat = () => state;
export function setChat(patch: Partial<ChatState>) {
  state = { ...state, ...patch };
  emit();
}

export function useChat(): ChatState {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    getChat,
    getChat,
  );
}

let tid = 0;
export const nextTurnId = () => ++tid;

/** Append a turn. Anything the architect says while the rail is away leaves
 *  the one dot on the toggle. */
export function push(turn: Turn): number {
  const unread = state.unread || (turn.t !== "you" && !state.open);
  setChat({ turns: [...state.turns, turn], unread });
  return turn.id;
}

export function patchTurn(id: number, patch: Partial<Turn>) {
  setChat({
    turns: state.turns.map((t) => (t.id === id ? ({ ...t, ...patch } as Turn) : t)),
  });
}

export function dropTurn(id: number) {
  setChat({ turns: state.turns.filter((t) => t.id !== id) });
}

export function say(lines: string[]): number {
  return push({ t: "say", id: nextTurnId(), lines });
}

export function you(
  text?: string, files?: Attachment[], via?: string, drew?: string[],
  about?: string[],
): number {
  return push({ t: "you", id: nextTurnId(), text, files, via, drew, about });
}

export function quiet(text: string): number {
  return push({ t: "quiet", id: nextTurnId(), text });
}

export function card(title: string, text: string): number {
  return push({ t: "card", id: nextTurnId(), title, text });
}

/** An empty host block, for a turn that is only a question. */
export function block(): number {
  return push({ t: "say", id: nextTurnId(), lines: [] });
}

export function boardLine(host: number, text: string, ids: string[]) {
  patchTurn(host, { line: { text, ids } } as Partial<Turn>);
}

/** Put a question on the table, under the turn that raised it. */
export function ask(host: number, picker: PickerPayload) {
  patchTurn(host, {
    ask: { picker, selected: openingValue(picker), spent: false },
  } as Partial<Turn>);
  setChat({ pendingAsk: picker.id });
}

/** Move the checked row. Held here rather than in the component so the digit
 *  shortcuts, which fire from anywhere on the page, and the radio group are
 *  moving the same thing. */
export function selectAsk(host: number, value: string) {
  const turn = state.turns.find((t) => t.id === host);
  if (!turn || turn.t !== "say" || !turn.ask || turn.ask.spent || turn.ask.queued) return;
  patchTurn(host, { ask: { ...turn.ask, selected: value } } as Partial<Turn>);
}

export function spendAsk(host: number, pickedLabel: string, pickedValue?: string) {
  const turn = state.turns.find((t) => t.id === host);
  if (!turn || turn.t !== "say" || !turn.ask) return;
  patchTurn(host, { ask: { ...turn.ask, spent: true, queued: false, pickedLabel, pickedValue } } as Partial<Turn>);
  setChat({ pendingAsk: null });
}

/** A row taken while the architect is still writing. The harness parks the
 *  answer until the turn ends, so the dock says so rather than collapsing a
 *  question that has not actually been answered yet. `spendAsk` on the
 *  turn the answer starts is what settles it. */
export function queueAsk(host: number, pickedLabel: string, pickedValue: string) {
  const turn = state.turns.find((t) => t.id === host);
  if (!turn || turn.t !== "say" || !turn.ask || turn.ask.spent) return;
  patchTurn(host, { ask: { ...turn.ask, selected: pickedValue, queued: true, pickedLabel, pickedValue } } as Partial<Turn>);
}

/** Put an answered question back on the table — they pressed Change. It
 *  opens on the row they took, so changing your mind is one click, not two. */
export function reopenAsk(host: number) {
  const turn = state.turns.find((t) => t.id === host);
  if (!turn || turn.t !== "say" || !turn.ask || !turn.ask.spent) return;
  const value = turn.ask.pickedValue ?? "";
  const { pickedLabel: _l, pickedValue: _v, answeredInMessage: _m, ...rest } = turn.ask;
  patchTurn(host, {
    ask: { ...rest, spent: false, selected: optionAt(turn.ask.picker, value) ? value : rest.selected },
  } as Partial<Turn>);
  setChat({ pendingAsk: turn.ask.picker.id });
}

/** The question still on the table, and the turn holding it. Global shortcuts
 *  answer with this. */
export function openAsk(): { host: number; ask: AskBlock } | null {
  for (let i = state.turns.length - 1; i >= 0; i--) {
    const t = state.turns[i];
    if (t.t === "say" && t.ask && !t.ask.spent) return { host: t.id, ask: t.ask };
  }
  return null;
}

/** Whether this picker is already in the thread — the harness re-pushes its
 *  whole state on every change, and a question is asked once. */
export function hasAsk(id: string): boolean {
  return state.turns.some((t) => t.t === "say" && t.ask?.picker.id === id);
}

/** A question settled without a pick — typed instead. Rows stay as the record
 *  of what was offered, but none of them may claim it was taken. */
export function settleAskInMessage(host: number) {
  const turn = state.turns.find((t) => t.id === host);
  if (!turn || turn.t !== "say" || !turn.ask || turn.ask.spent) return;
  patchTurn(host, { ask: { ...turn.ask, spent: true, answeredInMessage: true } } as Partial<Turn>);
  setChat({ pendingAsk: null });
}

/** The three dots, for as long as a turn plausibly takes. */
export function think(ms: number): Promise<void> {
  const id = push({ t: "thinking", id: nextTurnId() });
  return new Promise((res) =>
    setTimeout(() => { dropTurn(id); res(); }, ms),
  );
}

export function resetChat() {
  state = { turns: [], open: state.open, unread: false, pendingAsk: null };
  tid = 0;
  emit();
}
