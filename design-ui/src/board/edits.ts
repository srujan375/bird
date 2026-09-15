import { mutate, refusal, getSession } from "../wire/session";
import type { Op } from "../wire/types";
import { postToFrame } from "./frames";
import { clearSelection, getUi, reloadFrame, setPendingOp, setSelfApplied } from "./ui";

/**
 * Editing is optimistic: an op is applied in the frame that owns it, then
 * posted to the harness, which is the document of record. A refusal puts the
 * frame back on the last html the harness confirmed.
 */

const STRUCTURAL = new Set(["insert", "delete", "duplicate", "move"]);

/** Apply in the frame for instant feedback; the frame answers `edited`, which
 *  is when the op goes to the harness. */
export function sendOp(op: Op): void {
  const { pendingOp, selection } = getUi();
  const { status } = getSession();
  if (pendingOp || status === "finalized" || !selection) return;
  setPendingOp({ op, artboard: selection.artboard });
  postToFrame(selection.artboard, { type: "edit", op: op.op, selector: op.selector, payload: op });
}

/** The frame did it: now the harness's copy has to. */
export async function onEdited(artboard: string): Promise<void> {
  const pending = getUi().pendingOp;
  setPendingOp(null);
  if (!pending || pending.artboard !== artboard) return;
  const res = await mutate({ ...pending.op, artboard });
  if (res.ok) {
    if (res.version) setSelfApplied({ artboard, version: res.version });
    if (STRUCTURAL.has(pending.op.op)) {
      clearSelection();                 // every selector past this one now means something else
      postToFrame(artboard, { type: "getTree" });
    }
    return;
  }
  /* the harness refused what the frame already did: its copy is the document */
  refusal(res.error ?? "the edit was refused");
  clearSelection();
  reloadFrame(artboard);
}

/** The frame itself refused — the op never left the page. */
export function onFrameError(message: string): void {
  setPendingOp(null);
  refusal(message || "the edit was refused");
}
