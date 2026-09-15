/**
 * Every message a frame sends, heard once, for the life of the page.
 *
 * The board's own listener lives in Stage and is gone whenever Stage is —
 * in the showcase phase, say. The capture round trip and the forwarded keys
 * must not go with it: a polish in showcase is critiqued off a capture of
 * the showcase frame, and that reply used to land on a page with nobody
 * listening, so every showcase critique timed out. So the listener that
 * never leaves is here; the board's gestures are handed to whatever Stage
 * has registered, if one is mounted.
 */
import { captureFailed, captured } from "../wire/capture";
import { idOfSource } from "./frames";
import { dispatchFrameKey } from "./keys";

export type FrameMessage = { type?: string } & Record<string, unknown>;

let stage: ((id: string, msg: FrameMessage) => void) | null = null;
/** Stage's own handler — wheel, pinch, size, selection, edits — while mounted. */
export const setStageMessageHandler = (fn: ((id: string, msg: FrameMessage) => void) | null) => { stage = fn; };

function onMessage(e: MessageEvent): void {
  const msg = e.data as FrameMessage;
  if (!msg || typeof msg !== "object") return;
  const id = idOfSource(e.source);
  if (id === null) return; // not one of ours
  switch (msg.type) {
    case "captured":
      /* the bridge's screenshot for the critique loop — wire/capture matches
         it to the pending request and POSTs it back */
      captured(id, msg as { id?: unknown; png?: unknown });
      return;
    case "error":
      if (msg.command === "capture") { captureFailed(id, String(msg.message ?? "")); return; }
      break;
    case "key":
      dispatchFrameKey(msg as { key?: unknown; meta?: unknown; ctrl?: unknown; shift?: unknown });
      return;
  }
  stage?.(id, msg);
}

let started = false;
export function startFrameBus(): void {
  if (started || typeof addEventListener !== "function") return;
  started = true;
  addEventListener("message", onMessage);
}
