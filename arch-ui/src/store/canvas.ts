/**
 * The client overlay: everything about *looking at* the architecture.
 *
 * The server never sees any of this, and no event ever overwrites it. It is
 * persisted per run id so a mid-session refresh — which replays the whole
 * transcript and state from the server — comes back to the same viewport with
 * the same hand-placed nodes.
 *
 * Node keys are namespaced (`design:<id>`, `sketch:<variant>:<node>`) because
 * both layers are live at once and their ids can collide.
 */
import { create } from "zustand";
import type { Layer } from "../types";

export interface XY { x: number; y: number }

const STORAGE_PREFIX = "bird_arch_canvas:";
const CHAT_KEY = "bird_arch_chat_h"; // shared with the old page on purpose
const RAIL_KEY = "bird_arch_rail_w"; // width follows you into the next run, like the chat height
export const DEFAULT_RAIL = 380;
const MIN_RAIL = 260;
/** The canvas keeps at least this much of the window, however far the rail is dragged. */
const MIN_STAGE = 320;

/** Kept honest against the viewport: a width saved on a wide monitor must not
 *  swallow the canvas whole when the same session is reopened on a laptop. */
export function clampRail(w: number, viewport = window.innerWidth): number {
  const max = Math.max(MIN_RAIL, Math.min(760, viewport - MIN_STAGE));
  return Math.round(Math.min(max, Math.max(MIN_RAIL, w)));
}

export interface Overlay {
  positions: Record<string, XY>;
  pinned: string[];
  viewport: { x: number; y: number; zoom: number } | null;
  layer: Layer | null; // null = follow the session (design once something is promoted)
  variant: string | null; // which sketch variant is on the canvas
  railTab: string;
  railWidth: number;
  chatHeight: number;
  /** Which component's internals are open. Persisted like everything else here:
   *  a refresh mid-read lands back on the same page of the same sheet. */
  openComponent: string | null;
  dialogTab: string;
  dialogSize: { w: number; h: number };
  dialogFull: boolean;
}

function emptyOverlay(): Overlay {
  return {
    positions: {},
    pinned: [],
    viewport: null,
    layer: null,
    variant: null,
    railTab: "chat",
    railWidth: clampRail(Number(localStorage.getItem(RAIL_KEY)) || DEFAULT_RAIL),
    chatHeight: Number(localStorage.getItem(CHAT_KEY)) || 320,
    openComponent: null,
    dialogTab: "",
    dialogSize: { w: 760, h: 520 },
    dialogFull: false,
  };
}

interface CanvasState extends Overlay {
  runId: string | null;
  selected: string | null; // namespaced node key
  /** Bumped to ask the canvas to frame the graph again. Ephemeral, never saved:
   *  Tidy up lives in the top bar, outside the React Flow provider. */
  refitNonce: number;
  requestRefit: () => void;
  restore: (runId: string) => void;
  dragTo: (key: string, xy: XY) => void;
  setPosition: (key: string, xy: XY, pin?: boolean) => void;
  setPositions: (batch: Record<string, XY>) => void;
  unpin: (key: string) => void;
  clearPins: () => void;
  setViewport: (v: { x: number; y: number; zoom: number }) => void;
  setLayer: (l: Layer | null) => void;
  setVariant: (v: string | null) => void;
  setRailTab: (t: string) => void;
  /** A frame of a rail drag: width only, not written to storage. */
  dragRail: (w: number) => void;
  setRailWidth: (w: number) => void;
  setChatHeight: (h: number) => void;
  select: (key: string | null) => void;
  isPinned: (key: string) => boolean;
  openComponentDialog: (id: string | null) => void;
  setDialogTab: (t: string) => void;
  setDialogSize: (s: { w: number; h: number }) => void;
  setDialogFull: (v: boolean) => void;
}

function save(runId: string | null, o: Overlay): void {
  if (!runId) return;
  try {
    localStorage.setItem(STORAGE_PREFIX + runId, JSON.stringify(o));
    localStorage.setItem(CHAT_KEY, String(o.chatHeight));
    localStorage.setItem(RAIL_KEY, String(o.railWidth));
  } catch {
    /* private mode / quota — the session still works, it just won't persist */
  }
}

export const useCanvas = create<CanvasState>((set, get) => ({
  ...emptyOverlay(),
  runId: null,
  selected: null,
  refitNonce: 0,

  requestRefit: () => set((s) => ({ refitNonce: s.refitNonce + 1 })),

  restore: (runId) => {
    if (get().runId === runId) return;
    let overlay = emptyOverlay();
    try {
      const raw = localStorage.getItem(STORAGE_PREFIX + runId);
      if (raw) overlay = { ...overlay, ...(JSON.parse(raw) as Partial<Overlay>) };
    } catch {
      /* corrupt entry: fall back to a fresh overlay rather than failing to boot */
    }
    set({ ...overlay, railWidth: clampRail(overlay.railWidth), runId });
  },

  /**
   * A frame of a drag in flight: position only.
   *
   * Nodes are a controlled prop, so every intermediate position has to make it
   * back into the store or React Flow re-adopts the stale one and drags the
   * card back out from under the cursor. It must not persist, though — a
   * `localStorage.setItem` of the whole overlay on every mousemove is its own
   * kind of stutter. The drop, through `setPosition`, is what saves and pins.
   */
  dragTo: (key, xy) => set((s) => ({ positions: { ...s.positions, [key]: xy } })),

  setPosition: (key, xy, pin = true) =>
    set((s) => {
      const next: Overlay = {
        ...current(s),
        positions: { ...s.positions, [key]: xy },
        pinned: pin && !s.pinned.includes(key) ? [...s.pinned, key] : s.pinned,
      };
      save(s.runId, next);
      return next;
    }),

  /** Auto-layout results: written without pinning, so Tidy up can still move them. */
  setPositions: (batch) =>
    set((s) => {
      const next: Overlay = { ...current(s), positions: { ...s.positions, ...batch } };
      save(s.runId, next);
      return next;
    }),

  unpin: (key) =>
    set((s) => {
      const next: Overlay = { ...current(s), pinned: s.pinned.filter((k) => k !== key) };
      save(s.runId, next);
      return next;
    }),

  clearPins: () =>
    set((s) => {
      const next: Overlay = { ...current(s), pinned: [], positions: {} };
      save(s.runId, next);
      return next;
    }),

  setViewport: (viewport) =>
    set((s) => {
      const next: Overlay = { ...current(s), viewport };
      save(s.runId, next);
      return next;
    }),

  setLayer: (layer) => set((s) => persist(s, { layer })),
  setVariant: (variant) => set((s) => persist(s, { variant })),
  setRailTab: (railTab) => set((s) => persist(s, { railTab })),

  // same split as a node drag: every frame is state, only the drop is storage
  dragRail: (railWidth) => set({ railWidth: clampRail(railWidth) }),
  setRailWidth: (railWidth) => set((s) => persist(s, { railWidth: clampRail(railWidth) })),

  setChatHeight: (chatHeight) => set((s) => persist(s, { chatHeight })),
  select: (selected) => set({ selected }),
  isPinned: (key) => get().pinned.includes(key),

  // opening a component resets to its first tab; closing leaves the size alone
  openComponentDialog: (openComponent) =>
    set((s) => persist(s, { openComponent, dialogTab: "", ...(openComponent ? {} : { dialogFull: false }) })),
  setDialogTab: (dialogTab) => set((s) => persist(s, { dialogTab })),
  setDialogSize: (dialogSize) => set((s) => persist(s, { dialogSize })),
  setDialogFull: (dialogFull) => set((s) => persist(s, { dialogFull })),
}));

function current(s: CanvasState): Overlay {
  return {
    positions: s.positions,
    pinned: s.pinned,
    viewport: s.viewport,
    layer: s.layer,
    variant: s.variant,
    railTab: s.railTab,
    railWidth: s.railWidth,
    chatHeight: s.chatHeight,
    openComponent: s.openComponent,
    dialogTab: s.dialogTab,
    dialogSize: s.dialogSize,
    dialogFull: s.dialogFull,
  };
}

function persist(s: CanvasState, patch: Partial<Overlay>): Partial<Overlay> {
  const next = { ...current(s), ...patch };
  save(s.runId, next);
  return patch;
}

export const designKey = (id: string) => `design:${id}`;
export const sketchKey = (variant: string, id: string) => `sketch:${variant}:${id}`;
