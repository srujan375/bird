/**
 * The page's half of the capture round trip. The harness asks for a png of an
 * artboard as it renders (a `capture_request` over SSE, so the vision critic
 * can judge the real render); this routes the request to the mounted frame,
 * and the bridge's reply POSTs back to /capture. The harness waits a budget
 * sized to the document it is rastering (10–30s) and sends this side's own
 * deadline in the request, strictly inside its waiter — so a heavy artboard
 * gets the time it needs while a frame that is gone, never mounted, or too
 * slow to raster is still an error reply, never a silence the harness would
 * have to time out on its own.
 *
 * The request is posted only once the frame has loaded (`whenFrameReady`):
 * the harness asks for the capture on the same push that hands the page the
 * new document, and a message posted into a frame that is still loading is
 * lost without a trace — which used to skip half of every session's
 * critiques at exactly the page deadline.
 */
import { postToFrame, whenFrameReady } from "../board/frames";

/** Fallback for a request that carries no deadline — an older harness. */
const REPLY_TIMEOUT_MS = 8000;

interface Pending {
  id: string;
  artboard: string;
  timer: ReturnType<typeof setTimeout>;
}

let pending: Pending | null = null;

/** What the page tells the rest of the app about a capture: it has been
 *  handed to the frame, it came back, or it failed. Display only. */
export type CapturePhase = "requested" | "captured" | "failed";
let watcher: ((phase: CapturePhase, artboard: string) => void) | null = null;
export function watchCapture(fn: ((phase: CapturePhase, artboard: string) => void) | null): void {
  watcher = fn;
}

async function post(path: string, body: unknown): Promise<void> {
  try {
    await fetch(path, { method: "POST", body: JSON.stringify(body ?? {}) });
  } catch {
    /* the connection state already says what happened; nothing else to do */
  }
}

/** Settle the round trip: one answer per request, and only from the request
 *  still in the slot — a late reply to a superseded or timed-out capture is
 *  dropped rather than posted. */
function answer(id: string, body: Record<string, unknown>): void {
  if (pending?.id === id) {
    clearTimeout(pending.timer);
    watcher?.(typeof body.png === "string" ? "captured" : "failed", pending.artboard);
    pending = null;
  }
  void post("/capture", { id, ...body });
}

/** A capture_request from the harness: route it to the mounted frame. */
export function onCaptureRequest(ev: { id?: unknown; artboard?: unknown; deadline?: unknown }): void {
  const id = String(ev.id ?? "");
  const artboard = String(ev.artboard ?? "");
  if (!id || !artboard) return;
  // one slot here too: a newer request supersedes an older one, and the old
  // one is answered so the harness's waiter is never left holding a slot
  if (pending && pending.id !== id) answer(pending.id, { error: "superseded" });
  // the deadline is the harness's own budget minus its margin, so this timer
  // always fires before the waiter gives up; a missing or malformed one falls
  // back to the old flat 8s rather than trusting a broken payload
  const ms = typeof ev.deadline === "number" && Number.isFinite(ev.deadline) && ev.deadline > 0
    ? ev.deadline * 1000
    : REPLY_TIMEOUT_MS;
  const timer = setTimeout(
    () => answer(id, { error: "the page did not raster the artboard in time" }),
    ms,
  );
  pending = { id, artboard, timer };
  watcher?.("requested", artboard);
  // held until the frame has loaded — and only posted if this request is
  // still the one in the slot when it has. A frame that never mounts leaves
  // the deadline timer to answer for it.
  whenFrameReady(artboard, () => {
    if (pending?.id === id) postToFrame(artboard, { type: "capture", id });
  });
}

/** The bridge's reply: {type:'captured', id, png}. Matched by frame — the
 *  source identifies the artboard even if the id went missing — or by id. */
export function captured(artboardId: string, msg: { id?: unknown; png?: unknown }): void {
  if (!pending) return;
  const id = String(msg.id ?? "");
  if (pending.artboard !== artboardId && id !== pending.id) return;
  const png = typeof msg.png === "string" ? msg.png : "";
  answer(pending.id, png ? { png } : { error: "no image came back" });
}

/** The bridge refused — its `error` reply names the command it could not run. */
export function captureFailed(artboardId: string, message: string): void {
  if (!pending || pending.artboard !== artboardId) return;
  answer(pending.id, { error: message || "the frame could not capture" });
}

/** The request in flight, if any — for a status line. */
export const pendingCapture = () => (pending ? { id: pending.id, artboard: pending.artboard } : null);

/** Tests and fixtures: forget any in-flight request. */
export function resetCapture(): void {
  if (pending) clearTimeout(pending.timer);
  pending = null;
}
