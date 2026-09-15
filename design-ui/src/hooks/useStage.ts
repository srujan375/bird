import { useCallback, useRef, useState, type RefObject } from "react";

export interface View { x: number; y: number; k: number }
export interface Bounds { x0: number; y0: number; x1: number; y1: number }

/* Artboards are 1280px wide and there may be four of them: the board has to
   zoom out much further than the arch board ever needs to. */
const K_MIN = 0.05;
const K_MAX = 4;
const clampK = (k: number) => Math.min(K_MAX, Math.max(K_MIN, k));
const reduced = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Pan and zoom, held in a ref and written straight to the transform.
 *
 *  Deliberately imperative: a wheel pan and the glide both run at frame rate,
 *  and nothing else in the tree depends on the camera — the frames are laid
 *  out in board coordinates. Only the zoom percentage is React state, and
 *  only when the rounded number changes. Anything inside the world marked
 *  `.stay` is counter-scaled so it keeps its size on screen: frame names and
 *  the selection menu, the way every canvas tool draws its labels. */
export function useStage(
  viewport: RefObject<HTMLElement | null>,
  world: RefObject<HTMLElement | null>,
) {
  const view = useRef<View>({ x: 0, y: 0, k: 1 });
  const [level, setLevel] = useState(100);
  const anim = useRef(0);

  const applyView = useCallback(() => {
    const w = world.current, vp = viewport.current;
    const v = view.current;
    if (w) {
      w.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`;
      const inv = `scale(${1 / v.k})`;
      for (const el of w.querySelectorAll<HTMLElement>(".stay")) el.style.transform = inv;
    }
    if (vp) {
      vp.style.backgroundSize = `${24 * v.k}px ${24 * v.k}px`;
      vp.style.backgroundPosition = `${v.x}px ${v.y}px`;
    }
    const pct = Math.round(v.k * 100);
    setLevel((prev) => (prev === pct ? prev : pct));
  }, [viewport, world]);

  const setView = useCallback((next: View) => { view.current = next; applyView(); }, [applyView]);

  /** Zoom about a screen point (client coordinates). */
  const zoomAt = useCallback((cx: number, cy: number, k2: number) => {
    const vp = viewport.current;
    const r = vp ? vp.getBoundingClientRect() : { left: 0, top: 0 } as DOMRect;
    const sx = cx - r.left, sy = cy - r.top;
    const v = view.current;
    const k = clampK(k2);
    setView({ x: sx - (sx - v.x) * (k / v.k), y: sy - (sy - v.y) * (k / v.k), k });
  }, [setView, viewport]);

  const zoomStep = useCallback((factor: number) => {
    const vp = viewport.current;
    if (!vp) return;
    const r = vp.getBoundingClientRect();
    zoomAt(r.left + r.width / 2, r.top + r.height / 2, view.current.k * factor);
  }, [viewport, zoomAt]);

  /** Move the viewport somewhere over ~600ms. Jumping the board is the fastest
   *  way to lose the reader's place in it. */
  const glide = useCallback((to: View, ms = 620) => {
    cancelAnimationFrame(anim.current);
    if (reduced() || ms <= 0) { setView(to); return; }
    const from = { ...view.current };
    const t0 = performance.now();
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / ms);
      const e = 1 - Math.pow(1 - p, 3);
      setView({
        x: from.x + (to.x - from.x) * e,
        y: from.y + (to.y - from.y) * e,
        k: from.k + (to.k - from.k) * e,
      });
      if (p < 1) anim.current = requestAnimationFrame(step);
    };
    anim.current = requestAnimationFrame(step);
  }, [setView]);

  /** The view that shows `b` whole, at most at `maxK`. */
  const fitting = useCallback((b: Bounds, pad: number, maxK: number): View | null => {
    const vp = viewport.current;
    if (!vp) return null;
    const vw = vp.clientWidth, vh = vp.clientHeight;
    const bw = Math.max(1, b.x1 - b.x0), bh = Math.max(1, b.y1 - b.y0);
    const k = clampK(Math.min(maxK, (vw - pad * 2) / bw, (vh - pad * 2) / bh));
    return { k, x: (vw - bw * k) / 2 - b.x0 * k, y: (vh - bh * k) / 2 - b.y0 * k };
  }, [viewport]);

  const frame = useCallback((b: Bounds | null, pad = 52, maxK = 1, ms = 620) => {
    if (!b) return;
    const to = fitting(b, pad, maxK);
    if (to) glide(to, ms);
  }, [fitting, glide]);

  const nudgeX = useCallback((dx: number, ms = 430) => {
    glide({ ...view.current, x: view.current.x + dx }, ms);
  }, [glide]);

  return { view, level, applyView, setView, zoomAt, zoomStep, glide, frame, nudgeX };
}
