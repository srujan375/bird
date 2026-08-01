/**
 * The rail's left edge, as a handle.
 *
 * A transcript and a diagram want opposite things from the same window, and
 * which one you are reading changes minute to minute — so the split is the
 * user's, not ours. The width persists like the rest of the overlay, and
 * carries into the next run: nobody wants to re-drag this every session.
 */
import { useEffect, useRef } from "react";
import { DEFAULT_RAIL, clampRail, useCanvas } from "../store/canvas";

const STEP = 24; // one arrow-key nudge

export function RailGrip() {
  const width = useCanvas((s) => s.railWidth);
  const drag = useCanvas((s) => s.dragRail);
  const commit = useCanvas((s) => s.setRailWidth);
  const from = useRef<{ x: number; w: number } | null>(null);

  // a window that shrank under a saved width would leave no canvas at all
  useEffect(() => {
    const onResize = () => {
      const { railWidth, setRailWidth } = useCanvas.getState();
      if (clampRail(railWidth) !== railWidth) setRailWidth(railWidth);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const stop = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!from.current) return;
    from.current = null;
    delete document.body.dataset.resizing;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    commit(useCanvas.getState().railWidth); // the drop is what gets written
  };

  return (
    <div
      className="rail-grip"
      role="separator"
      aria-orientation="vertical"
      aria-label="rail width"
      aria-valuenow={width}
      tabIndex={0}
      title="drag to resize · double-click to reset"
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        e.preventDefault(); // otherwise the drag selects transcript text
        e.currentTarget.focus(); // ...which also cancels the focus preventDefault would have given us
        from.current = { x: e.clientX, w: width };
        e.currentTarget.setPointerCapture(e.pointerId);
        document.body.dataset.resizing = "col";
      }}
      onPointerMove={(e) => {
        if (!from.current) return;
        drag(from.current.w + (from.current.x - e.clientX)); // rail grows leftward
      }}
      onPointerUp={stop}
      onPointerCancel={stop}
      onDoubleClick={() => commit(DEFAULT_RAIL)}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft") { e.preventDefault(); commit(width + STEP); }
        else if (e.key === "ArrowRight") { e.preventDefault(); commit(width - STEP); }
        else if (e.key === "Home") { e.preventDefault(); commit(DEFAULT_RAIL); }
      }}
    />
  );
}
