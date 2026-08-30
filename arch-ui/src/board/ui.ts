import { useSyncExternalStore } from "react";
import type { NodeField, Selection, Tool } from "./types";

/**
 * What the browser owns.
 *
 * The design belongs to the harness and arrives whole on every `arch_state`
 * push. What is left over is genuinely local: which box is selected, which
 * tool is armed, what is haloed, and where a box is *right now* while a finger
 * is still on it.
 *
 * That last one is why `drafts` exists. A drag sends a `move` when it ends, not
 * on every frame, so between the drop and the push that answers it the harness
 * still believes the old position. Without a local draft the box would snap
 * back under the cursor and then jump forward again.
 */

export interface UiState {
  selected: Selection | null;
  tool: Tool;
  /** boxes to halo, plus a nonce so the same set can flash twice */
  flash: { ids: string[]; nonce: number };
  /** id -> position, held only until the harness confirms it */
  drafts: Record<string, { cx: number; y: number }>;
  /** note id -> where it is and what it hangs off, until the harness has it.
   *  The anchor is captured when the note is *made*, not when it is committed:
   *  putting a note down selects the note, so by commit time the box it was
   *  pinned to is no longer what is selected. */
  noteDrafts: Record<string, { x: number; y: number; anchor?: string }>;
  /** a box being renamed or a note being typed into */
  editing: { t: "node" | "anno"; id: string; field?: NodeField } | null;
  /** container id -> whether it is folded shut. Unset means the board's
   *  default for its size; this is the person's override, and it is theirs
   *  alone — the harness never hears about it. */
  folded: Record<string, boolean>;
  /** the box under the pointer — what the wires light up for */
  hot: string | null;
}

let state: UiState = {
  selected: null,
  tool: "select",
  flash: { ids: [], nonce: 0 },
  drafts: {},
  noteDrafts: {},
  editing: null,
  folded: {},
  hot: null,
};

const listeners = new Set<() => void>();
const emit = () => { for (const l of listeners) l(); };

export const getUi = () => state;
export const setUi = (patch: Partial<UiState>) => { state = { ...state, ...patch }; emit(); };

export function useUi(): UiState {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    getUi,
    getUi,
  );
}

export const select = (selected: Selection | null) => setUi({ selected });
export const setTool = (tool: Tool) => setUi({ tool });
export const setEditing = (editing: UiState["editing"]) => setUi({ editing });
export const setHot = (hot: string | null) => { if (hot !== state.hot) setUi({ hot }); };
export const setFolded = (id: string, folded: boolean) =>
  setUi({ folded: { ...state.folded, [id]: folded } });

/** Halo a set of boxes. The nonce lets the same set flash twice in a row. */
export const flash = (ids: string[]) =>
  setUi({ flash: { ids, nonce: state.flash.nonce + 1 } });

export function draftNode(id: string, cx: number, y: number) {
  setUi({ drafts: { ...state.drafts, [id]: { cx, y } } });
}

export function clearNodeDraft(id: string) {
  if (!(id in state.drafts)) return;
  const { [id]: _gone, ...rest } = state.drafts;
  setUi({ drafts: rest });
}

export function draftNote(id: string, x: number, y: number, anchor?: string) {
  const held = state.noteDrafts[id];
  setUi({
    noteDrafts: {
      ...state.noteDrafts,
      [id]: { x, y, anchor: anchor ?? held?.anchor },
    },
  });
}

export function clearNoteDraft(id: string) {
  if (!(id in state.noteDrafts)) return;
  const { [id]: _gone, ...rest } = state.noteDrafts;
  setUi({ noteDrafts: rest });
}

let uid = 0;
export const nextLocalId = (prefix: string) => prefix + ++uid;
