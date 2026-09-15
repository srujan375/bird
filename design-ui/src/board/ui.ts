import { useSyncExternalStore } from "react";
import type { Op, Selected, TreeNode } from "../wire/types";
import { postToFrame } from "./frames";

/**
 * What the browser owns.
 *
 * The design belongs to the harness and arrives whole on every `design_state`
 * push. What is left over is genuinely local: which frame is being worked in,
 * which element is selected inside it, the element trees the frames have
 * reported, and the one edit that is in flight between a frame and the
 * harness.
 */

export type Selection = Selected & { artboard: string };

export interface UiState {
  /** the artboard being worked in — the page's own, not the harness's */
  focus: string | null;
  selection: Selection | null;
  /** the selector to put back after a frame reloads under a selection */
  lastSelector: string | null;
  /** artboard id -> the element tree its frame last reported */
  trees: Record<string, TreeNode>;
  /** one op applied in a frame and not yet acked by the harness. A second
   *  edit must not race it, and a refusal has to know what to roll back. */
  pendingOp: { op: Op; artboard: string } | null;
  /** a version THIS page produced. Its push carries html the frame already
   *  shows, so adopting it must not reload the frame under the user. */
  selfApplied: { artboard: string; version: string } | null;
  /** artboard -> a counter bumped to force a reload from the harness's copy */
  reload: Record<string, number>;
  /** frames to halo, plus a nonce so the same set can flash twice */
  flash: { ids: string[]; nonce: number };
}

let state: UiState = {
  focus: null,
  selection: null,
  lastSelector: null,
  trees: {},
  pendingOp: null,
  selfApplied: null,
  reload: {},
  flash: { ids: [], nonce: 0 },
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

/** The frame draws the selection outline itself; dropping the selection
 *  here has to take that outline with it, or Escape leaves a blue box on
 *  an element the page no longer considers selected. */
const unoutline = () => { if (state.selection) postToFrame(state.selection.artboard, { type: "deselect" }); };

export const setFocus = (focus: string | null) => {
  if (focus === state.focus) return;
  /* a selection belongs to the frame it was made in */
  unoutline();
  setUi({ focus, selection: null, lastSelector: null });
};

export const select = (selection: Selection | null) => {
  if (!selection) unoutline();
  setUi({ selection, lastSelector: selection?.selector ?? null, focus: selection?.artboard ?? state.focus });
};

export const clearSelection = () => {
  if (!state.selection && !state.lastSelector) return;
  unoutline();
  setUi({ selection: null, lastSelector: null });
};

export const setTree = (artboard: string, tree: TreeNode) =>
  setUi({ trees: { ...state.trees, [artboard]: tree } });

export const setPendingOp = (pendingOp: UiState["pendingOp"]) => setUi({ pendingOp });
export const setSelfApplied = (selfApplied: UiState["selfApplied"]) => setUi({ selfApplied });

/** Take the self-applied mark if it is this artboard at this version. */
export function adoptSelfApplied(artboard: string, version: string): boolean {
  const s = state.selfApplied;
  if (!s || s.artboard !== artboard || s.version !== version) return false;
  setUi({ selfApplied: null });
  return true;
}

export const reloadFrame = (artboard: string) =>
  setUi({ reload: { ...state.reload, [artboard]: (state.reload[artboard] ?? 0) + 1 } });

export const flash = (ids: string[]) =>
  setUi({ flash: { ids, nonce: state.flash.nonce + 1 } });

/** Tests and fixtures: back to nothing. */
export function resetUi(): void {
  state = { ...state, focus: null, selection: null, lastSelector: null, trees: {}, pendingOp: null, selfApplied: null, reload: {} };
  emit();
}
