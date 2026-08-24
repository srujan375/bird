import { useEffect, useState } from "react";

/** The prototype adds `.arriving` on insert and takes it off two frames later,
 *  so the element paints in its offset state once and then transitions out of
 *  it. One frame is not enough — the browser can coalesce the style write with
 *  the insert and the transition never runs.
 *
 *  `enabled` is how a resume stays honest. A refresh mid-session delivers the
 *  whole design in one push, and animating all of it would claim eleven boxes
 *  had just been created. Those are born already-in.
 */
export function useArriving(enabled = true): boolean {
  const [arriving, setArriving] = useState(enabled);
  useEffect(() => {
    if (!enabled) return;
    let raf2 = 0;
    const raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => setArriving(false));
    });
    return () => { cancelAnimationFrame(raf1); cancelAnimationFrame(raf2); };
  }, [enabled]);
  return enabled && arriving;
}
