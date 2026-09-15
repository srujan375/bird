/**
 * The frames on the board, reachable by artboard id.
 *
 * Each artboard renders in a sandboxed iframe carrying editor-bridge.js, and
 * the page talks to it over postMessage: `select`, `edit`, `getTree`, `size`,
 * `capture` out; `selected`, `tree`, `edited`, `size`, `captured`, `key`,
 * `error` back. The registry is what maps a reply's `source` window back to
 * the artboard it came from.
 *
 * Readiness is tracked here too. A message posted into a frame whose document
 * is still loading lands in about:blank, or in a document whose bridge has
 * not installed its listener yet, and is silently lost — which is how half of
 * every session's automatic critiques used to time out: the capture request
 * arrived on the same push that set the frame's srcdoc. `whenFrameReady`
 * holds a message until the frame's load event has fired.
 */

const frames = new Map<string, HTMLIFrameElement>();
const ready = new Set<string>();
/** callbacks waiting for a frame that is loading — or not even mounted yet:
 *  a request can arrive before React has rendered the frame it is for */
const waiting = new Map<string, Array<() => void>>();

export const registerFrame = (id: string, el: HTMLIFrameElement) => { frames.set(id, el); };

/** The frame is gone. Its readiness goes with it; anything still waiting is
 *  kept, because a remount (StrictMode's double effect, a slot re-keyed) is
 *  followed by a load that will flush it, and every waiter guards its own
 *  staleness. `resetFrames` is the hard clear. */
export const unregisterFrame = (id: string) => { frames.delete(id); ready.delete(id); };

/** A new document is about to be loaded into the frame: until its load event
 *  fires, nothing posted to it can be trusted to arrive. */
export function markFrameLoading(id: string): void {
  ready.delete(id);
}

/** The frame's document has loaded, bridge included: run whatever waited. */
export function markFrameReady(id: string): void {
  ready.add(id);
  const q = waiting.get(id);
  if (!q) return;
  waiting.delete(id);
  for (const fn of q) fn();
}

export const isFrameReady = (id: string) => ready.has(id);

/** Run now if the frame is ready, otherwise once it is. */
export function whenFrameReady(id: string, fn: () => void): void {
  if (ready.has(id)) { fn(); return; }
  let q = waiting.get(id);
  if (!q) { q = []; waiting.set(id, q); }
  q.push(fn);
}

export function postToFrame(id: string, msg: Record<string, unknown>): void {
  const win = frames.get(id)?.contentWindow;
  if (win) win.postMessage(msg, "*");
}

export function idOfSource(source: MessageEventSource | null): string | null {
  for (const [id, el] of frames) if (el.contentWindow === source) return id;
  return null;
}

/** A point in a frame's own document, as a point on the screen. The frame
 *  is scaled by the world's transform, so its bounding box is the scaled one
 *  and its own coordinates are not. */
export function toScreen(id: string, x: number, y: number, k: number): { x: number; y: number } | null {
  const el = frames.get(id);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + x * k, y: r.top + y * k };
}

/** Tests and fixtures: forget every frame, its readiness and its waiters. */
export function resetFrames(): void {
  frames.clear();
  ready.clear();
  waiting.clear();
}
