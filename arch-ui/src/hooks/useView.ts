import { useCallback, useRef, useState, type RefObject } from "react";
import type { Bounds } from "../board/geometry";

export interface View { x: number; y: number; k: number }

const clampK = (k: number) => Math.min(2, Math.max(0.28, k));
const reduced = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Pan and zoom, held in a ref and written straight to the transform.
 *
 *  This is the one place the port stays imperative, and deliberately: a wheel
 *  pan and the 620ms glide both run at frame rate, and nothing else in the tree
 *  depends on the camera — the world is laid out in board coordinates. Only the
 *  zoom percentage is React state, and only when the rounded number changes. */
export function useView(
  viewport: RefObject<HTMLElement | null>,
  world: RefObject<HTMLElement | null>,
  getBounds: (ids?: string[] | null) => Bounds | null,
) {
  const view = useRef<View>({ x: 0, y: 0, k: 1 });
  const [level, setLevel] = useState(100);
  const anim = useRef(0);

  const applyView = useCallback(() => {
    const w = world.current, vp = viewport.current;
    const v = view.current;
    if (w) w.style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`;
    if (vp) {
      vp.style.backgroundSize = `${24 * v.k}px ${24 * v.k}px`;
      vp.style.backgroundPosition = `${v.x}px ${v.y}px`;
    }
    const pct = Math.round(v.k * 100);
    setLevel((prev) => (prev === pct ? prev : pct));
  }, [viewport, world]);

  const setView = useCallback((next: View) => { view.current = next; applyView(); }, [applyView]);

  const zoomAt = useCallback((sx: number, sy: number, k2: number) => {
    const v = view.current;
    const k = clampK(k2);
    setView({ x: sx - (sx - v.x) * (k / v.k), y: sy - (sy - v.y) * (k / v.k), k });
  }, [setView]);

  /** Move the viewport somewhere over ~600ms. Jumping the board is the fastest
   *  way to lose the reader's place in it. */
  const glide = useCallback((to: View, ms = 620) => {
    cancelAnimationFrame(anim.current);
    if (reduced()) { setView(to); return; }
    const from = { ...view.current };
    const t0 = performance.now();
    const stepFn = (t: number) => {
      const p = Math.min(1, (t - t0) / ms);
      const e = 1 - Math.pow(1 - p, 3);
      setView({
        x: from.x + (to.x - from.x) * e,
        y: from.y + (to.y - from.y) * e,
        k: from.k + (to.k - from.k) * e,
      });
      if (p < 1) anim.current = requestAnimationFrame(stepFn);
    };
    anim.current = requestAnimationFrame(stepFn);
  }, [setView]);

  const frame = useCallback((ids: string[] | null, pad = 76, maxK = 1.05) => {
    const b = getBounds(ids);
    const vp = viewport.current;
    if (!b || !vp) return;
    const vw = vp.clientWidth, vh = vp.clientHeight;
    const k = clampK(Math.min(maxK, (vw - pad * 2) / (b.x1 - b.x0), (vh - pad * 2) / (b.y1 - b.y0)));
    glide({
      k,
      x: (vw - (b.x1 - b.x0) * k) / 2 - b.x0 * k,
      y: (vh - (b.y1 - b.y0) * k) / 2 - b.y0 * k,
    });
  }, [getBounds, glide, viewport]);

  /** Opening view: fit the three columns across so A, Shared and B are all on
   *  screen at a size you can actually read. Height is allowed to run off the
   *  bottom — this is a board, and Fit is one click away. */
  const fitNow = useCallback((pad = 64) => {
    const b = getBounds(null);
    const vp = viewport.current;
    if (!b || !vp) return;
    const vw = vp.clientWidth, vh = vp.clientHeight;
    const cw = b.x1 - b.x0, ch = b.y1 - b.y0;
    const k = clampK(Math.min(1, (vw - pad * 2) / cw));
    const y = ch * k <= vh - pad * 2 ? (vh - ch * k) / 2 - b.y0 * k : pad - b.y0 * k;
    setView({ k, x: (vw - cw * k) / 2 - b.x0 * k, y });
  }, [getBounds, setView, viewport]);

  const nudgeX = useCallback((dx: number, ms = 430) => {
    glide({ ...view.current, x: view.current.x + dx }, ms);
  }, [glide]);

  /** Screen point → board point. */
  const toWorld = useCallback((e: { clientX: number; clientY: number }) => {
    const vp = viewport.current;
    const r = vp ? vp.getBoundingClientRect() : { left: 0, top: 0 } as DOMRect;
    const v = view.current;
    return { x: (e.clientX - r.left - v.x) / v.k, y: (e.clientY - r.top - v.y) / v.k };
  }, [viewport]);

  return { view, level, applyView, setView, zoomAt, glide, frame, fitNow, nudgeX, toWorld };
}
