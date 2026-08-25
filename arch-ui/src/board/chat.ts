import { useSyncExternalStore } from "react";
import type { Attachment } from "./types";

/** The thread, as data.
 *
 *  Every turn the architect takes is one block: some lines, optionally a
 *  question with reply chips, optionally the single quiet line its board
 *  machinery is allowed. */

export interface AskOption {
  label: string;
  /** the one-line consequence of taking this row */
  cost?: string;
  rec?: boolean;
  favor?: "a" | "b";
  act?: string;
  /** re-ask instead of advancing — "show me the numbers first" */
  stay?: boolean;
  reply: string[];
}

export interface AskBlock {
  question: string;
  opts: AskOption[];
  spent: boolean;
  pickedLabel?: string;
  /** answered in prose — no row was taken as-is, so none may claim it was */
  answeredInMessage?: boolean;
  onPick: (o: AskOption) => void;
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
  | { t: "thinking"; id: number };

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

/** An empty host block, for a turn that is only a question. */
export function block(): number {
  return push({ t: "say", id: nextTurnId(), lines: [] });
}

export function boardLine(host: number, text: string, ids: string[]) {
  patchTurn(host, { line: { text, ids } } as Partial<Turn>);
}

export function ask(host: number, question: string, opts: AskOption[], onPick: (o: AskOption) => void) {
  patchTurn(host, {
    ask: { question, opts, spent: false, onPick },
  } as Partial<Turn>);
  setChat({ pendingAsk: question });
}

export function spendAsk(host: number, pickedLabel: string) {
  const turn = state.turns.find((t) => t.id === host);
  if (!turn || turn.t !== "say" || !turn.ask) return;
  patchTurn(host, { ask: { ...turn.ask, spent: true, pickedLabel } } as Partial<Turn>);
  setChat({ pendingAsk: null });
}

/** The question still on the table, if any. Global shortcuts answer with this. */
export function openAsk(): AskBlock | null {
  for (let i = state.turns.length - 1; i >= 0; i--) {
    const t = state.turns[i];
    if (t.t === "say" && t.ask && !t.ask.spent) return t.ask;
  }
  return null;
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
