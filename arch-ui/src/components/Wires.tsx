import { memo, useEffect, useRef } from "react";
import type { WirePath } from "../board/geometry";

/** A fresh wire draws itself in: the path length becomes its dash, and the
 *  offset animates to zero. That needs `getTotalLength()` on the real element,
 *  so it happens in an effect rather than in render.
 *
 *  The dash has to be cleaned up afterwards, and that is the part worth being
 *  careful about. `stroke-dasharray` is set to the path's length *at the moment
 *  it was drawn*; the animation ends but the inline style does not. Move either
 *  end of the wire and the geometry changes underneath a dash pattern sized for
 *  the old one — so a solid line renders as one dash, then a gap, then nothing.
 *  React never clears it either, because React did not set it. */
function useDrawIn(paths: WirePath[]) {
  const layer = useRef<SVGGElement | null>(null);
  useEffect(() => {
    const g = layer.current;
    if (!g) return;

    const undash = (p: SVGPathElement) => {
      p.classList.remove("drawing");
      p.style.strokeDasharray = "";
      p.style.strokeDashoffset = "";
    };

    /* Anything not currently drawing itself in gets its own length forgotten —
       including a wire whose animation was cut short by a re-render. */
    for (const p of g.querySelectorAll<SVGPathElement>("path.wire")) {
      if (p.dataset.fresh !== "1") undash(p);
    }

    const fresh = g.querySelectorAll<SVGPathElement>('path[data-fresh="1"]');
    if (!fresh.length) return;
    const ends: Array<[SVGPathElement, () => void]> = [];
    const raf = requestAnimationFrame(() => {
      for (const p of fresh) {
        const L = p.getTotalLength();
        p.style.strokeDasharray = String(L);
        p.style.strokeDashoffset = String(L);
        p.style.opacity = "1";
        const end = () => undash(p);
        ends.push([p, end]);
        p.addEventListener("animationend", end, { once: true });
        requestAnimationFrame(() => p.classList.add("drawing"));
      }
    });
    return () => {
      cancelAnimationFrame(raf);
      for (const [p, end] of ends) p.removeEventListener("animationend", end);
    };
  }, [paths]);
  return layer;
}

interface Props {
  paths: WirePath[];
  /** the wire being dragged out of a port, if any */
  temp: string | null;
  /** boxes whose wires should light up — the one under the pointer and the
   *  one selected. With any set, every other wire steps back. */
  hot: Set<string>;
}

/** Past this many wires, labels are read by pointing, not all at once. */
const QUIET_ABOVE = 18;

function WiresImpl({ paths, temp, hot }: Props) {
  const layer = useDrawIn(paths);
  const focus = hot.size > 0;
  const quiet = paths.length > QUIET_ABOVE;
  return (
    <svg
      className="wires"
      id="wires"
      aria-hidden="true"
      {...(focus ? { "data-focus": "1" } : {})}
      {...(quiet ? { "data-quiet": "1" } : {})}
    >
      <defs>
        <marker id="head" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L8 4 L0 8 Z" />
        </marker>
        <marker id="head-out" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L8 4 L0 8 Z" />
        </marker>
        <marker id="head-lit" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L8 4 L0 8 Z" />
        </marker>
      </defs>
      <g id="wire-layer" ref={layer}>
        {paths.map((p) => {
          const lit = focus && (hot.has(p.from) || hot.has(p.to));
          return (
          <g key={p.key} {...(lit ? { "data-lit": "1" } : {})}>
            <path
              className="wire"
              d={p.d}
              {...(p.fresh ? { "data-fresh": "1", style: { opacity: 0 } } : {})}
              {...(p.out ? { "data-out": "1" } : {})}
              {...(lit ? { "data-lit": "1" } : {})}
              markerEnd={`url(#${lit ? "head-lit" : p.out ? "head-out" : "head"})`}
            />
            {p.label ? (
              <text className="wire-label" x={p.lx} y={p.ly} textAnchor="middle">{p.label}</text>
            ) : null}
          </g>
          );
        })}
        {temp ? (
          <path id="tmp-wire" className="wire" d={temp}
                markerEnd="url(#head)" style={{ strokeDasharray: "4 4" }} />
        ) : null}
      </g>
    </svg>
  );
}

export const Wires = memo(WiresImpl);
