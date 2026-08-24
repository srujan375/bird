/** A turn's prompt, split back into the parts it was assembled from.
 *
 *  The harness puts context in front of what the user typed: what they drew
 *  since the last turn, and which boxes they had selected when they sent it.
 *  Both are prefixed blocks, mirrored from `harnesses/arch/session.py`.
 *
 *  The page has to take them apart again before showing the turn. If it does
 *  not, the user's own message renders with the raw prompt scaffolding inside
 *  it — and the record then says they typed something they never typed.
 */

export const BOARD_EDIT_PREFIX = "[the user changed the board]";
export const FOCUS_PREFIX = "[the user is pointing at]";

/** The bullets of a context block. The harness writes one `- item` per line; a
 *  focus block hangs indented detail under each, which is for the architect and
 *  not for the transcript. */
const bullets = (block: string) =>
  block.split("\n")
    .filter((l) => l.startsWith("- "))
    .map((l) => l.slice(2).trim())
    .filter(Boolean);

export interface SplitTask {
  /** gestures that travelled with the message */
  drew: string[];
  /** boxes it was pointing at */
  about: string[];
  /** the words the user actually typed */
  typed: string;
}

export function splitTask(task: string): SplitTask {
  const blocks = task.split("\n\n");
  let drew: string[] = [];
  let about: string[] = [];
  let i = 0;
  /* Context blocks come first and in no guaranteed order. Stop at the first
     block that is not one — everything from there is what was typed, including
     any blank lines inside it. */
  for (; i < blocks.length; i++) {
    if (blocks[i].startsWith(BOARD_EDIT_PREFIX)) drew = bullets(blocks[i]);
    else if (blocks[i].startsWith(FOCUS_PREFIX)) about = bullets(blocks[i]);
    else break;
  }
  return { drew, about, typed: blocks.slice(i).join("\n\n").trim() };
}
