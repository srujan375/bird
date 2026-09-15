/** The canvas loading state, derived from the same state every push
 *  re-renders — no timers, no extra events.
 *
 *  An empty board waiting on its first artboard gets a skeleton of the cards
 *  to come; with live drafting that lasts only until the first piece of html
 *  arrives. A board that already has artboards keeps them visible underneath
 *  and shows a slim indeterminate bar at the top of the stage — the header
 *  pill already names the status, so no second pill. */
export function Loader({ mode }: { mode: "skeleton" | "working" | null }) {
  if (mode === null) return null;
  if (mode === "working") {
    return <div className="canvas-working" aria-hidden="true"><div className="canvas-working-bar" /></div>;
  }
  return (
    <div className="canvas-loader" role="status" aria-live="polite">
      {[0, 1, 2, 3].map((i) => <div className="skeleton-card" key={i} />)}
      <div className="canvas-loader-title">Designing your options…</div>
      <div className="canvas-loader-sub">Artboards appear here as the designer writes them.</div>
    </div>
  );
}
