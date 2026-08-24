import { useRef } from "react";
import type { Turn } from "../board/chat";
import type { Attachment } from "../board/types";
import { RAIL_MAX, RAIL_MIN } from "../hooks/useRail";
import { Composer } from "./Composer";
import { Thread } from "./Thread";

interface Props {
  turns: Turn[];
  tip: string;
  rail: number;
  setRail: (px: number) => number;
  onSizingStart: () => void;
  onSizingEnd: () => void;
  onCollapse: () => void;
  onOpenShot: (a: Attachment) => void;
  /** the design was handed off: the board and the composer are read-only */
  readOnly: boolean;
  /** why, in the user's terms */
  readOnlyReason: string;
}

/** The rail you can put away, and the seam you can drag.
 *
 *  The interior keeps its width and slides out of the clip, so the
 *  conversation never reflows on the way to being put away. */
export function Chat({
  turns, tip, rail, setRail, onSizingStart, onSizingEnd, onCollapse, onOpenShot, readOnly, readOnlyReason,
}: Props) {
  const grip = useRef<HTMLDivElement | null>(null);

  const onGripDown = (e: React.PointerEvent) => {
    e.preventDefault();
    const el = grip.current;
    if (!el) return;
    el.dataset.on = "1";
    onSizingStart();
    /* Guarded: a capture failure must not abort the handler before it attaches
       the move listeners below, or the seam grabs and then does nothing. */
    try { el.setPointerCapture(e.pointerId); } catch { /* no active pointer */ }
    const x0 = e.clientX;
    const w0 = rail;
    const move = (ev: PointerEvent) => setRail(w0 - (ev.clientX - x0));
    const done = () => {
      delete el.dataset.on;
      onSizingEnd();
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", done);
      el.removeEventListener("pointercancel", done);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", done);
    el.addEventListener("pointercancel", done);
  };

  return (
    <aside className="chat" id="chat" data-od-id="chat">
      <div
        className="grip"
        id="grip"
        ref={grip}
        role="separator"
        tabIndex={0}
        aria-orientation="vertical"
        aria-label="Resize the conversation"
        aria-valuenow={rail}
        aria-valuemin={RAIL_MIN}
        aria-valuemax={RAIL_MAX}
        data-od-id="chat-resize"
        onPointerDown={onGripDown}
        onDoubleClick={onCollapse}
        onKeyDown={(e) => {
          const step = e.shiftKey ? 48 : 12;
          if (e.key === "ArrowLeft") { e.preventDefault(); setRail(rail + step); }
          else if (e.key === "ArrowRight") { e.preventDefault(); setRail(rail - step); }
        }}
      />
      <div className="chat-inner">
        <Thread turns={turns} onOpen={onOpenShot} />
        <Composer tip={tip} disabled={readOnly} reason={readOnlyReason} />
      </div>
    </aside>
  );
}
