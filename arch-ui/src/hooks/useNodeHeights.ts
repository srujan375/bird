import { useCallback, useRef, useState } from "react";
import type { Heights } from "../board/geometry";

/** Wire geometry needs the rendered height of every box, and a box grows when
 *  it deepens. The prototype read `offsetHeight` on every redraw; here a
 *  ResizeObserver feeds the heights back into render so the wires are derived
 *  state like everything else. */
export function useNodeHeights() {
  const [heights, setHeights] = useState<Heights>({});
  const els = useRef(new Map<string, HTMLElement>());
  const ro = useRef<ResizeObserver | null>(null);

  if (ro.current === null && typeof ResizeObserver !== "undefined") {
    ro.current = new ResizeObserver((entries) => {
      setHeights((prev) => {
        let next = prev;
        for (const e of entries) {
          const id = (e.target as HTMLElement).dataset.id;
          if (!id) continue;
          const h = Math.round((e.target as HTMLElement).offsetHeight);
          if (prev[id] !== h) {
            if (next === prev) next = { ...prev };
            next[id] = h;
          }
        }
        return next;
      });
    });
  }

  const register = useCallback((id: string, el: HTMLElement | null) => {
    const prev = els.current.get(id);
    if (prev && prev !== el) { ro.current?.unobserve(prev); els.current.delete(id); }
    if (el) {
      els.current.set(id, el);
      ro.current?.observe(el);
      setHeights((h) => (h[id] === el.offsetHeight ? h : { ...h, [id]: el.offsetHeight }));
    } else if (prev) {
      setHeights((h) => { const { [id]: _drop, ...rest } = h; return rest; });
    }
  }, []);

  const elementFor = useCallback((id: string) => els.current.get(id) ?? null, []);

  return { heights, register, elementFor };
}
