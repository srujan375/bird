import { sendOp } from "../board/edits";
import type { Selection } from "../board/ui";
import type { Op } from "../wire/types";

/** The quick menu that belongs to the selected element, floating over it on
 *  the board. Positioned in board coordinates, counter-scaled by the stage. */
export function SelMenu({ sel, x }: { sel: Selection; x: number }) {
  const parts = sel.selector.split(">");
  const parent = parts.slice(0, -1).join(">");
  const index = Number(parts[parts.length - 1]);
  const act = (label: string, op: Op, title: string) => (
    <button type="button" title={title} onClick={() => sendOp(op)}>{label}</button>
  );
  return (
    <div className="selmenu stay" role="toolbar" aria-label="selected element"
         style={{ left: x + sel.box.x, top: sel.box.y }} data-od-id="selection-menu">
      {act("Duplicate", { op: "duplicate", selector: sel.selector }, "Duplicate this element after itself")}
      {act("Delete", { op: "delete", selector: sel.selector }, "Delete this element")}
      {parent ? <span className="sep" /> : null}
      {parent && index > 0
        ? act("↑", { op: "move", selector: sel.selector, new_parent: parent, index: index - 1 }, "Move up")
        : null}
      {parent
        ? act("↓", { op: "move", selector: sel.selector, new_parent: parent, index: index + 1 }, "Move down")
        : null}
    </div>
  );
}
