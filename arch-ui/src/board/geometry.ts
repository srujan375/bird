import type { CSSProperties } from "react";
import type { Anno, BoardNode, Lane, Wire } from "./types";

/** A box's width is its fidelity. Widening on deepen is the whole reason
 *  positions are stored as a centre-x. */
export const W: Record<string, number> = { stub: 176, sketch: 216, detailed: 252 };
export const STEP = ["stub", "sketch", "detailed"] as const;

export const snap = (v: number) => Math.round(v / 8) * 8;

/** A column's territory tint, handed to an element as custom properties so the
 *  stylesheet carries one rule per surface instead of one per approach. `slot`
 *  is a ramp step ("1".."5") or "s" for the shared spine. The values live in
 *  board.css and are never chosen here — the model picks how many approaches
 *  there are and their order; the design system picks what they look like. */
export function laneVars(slot: string): CSSProperties {
  return {
    "--lane-tint": `var(--ramp-${slot}-tint)`,
    "--lane-edge": `var(--ramp-${slot}-edge)`,
    "--lane-line": `var(--ramp-${slot}-line)`,
  } as CSSProperties;
}

export interface Rect { x: number; y: number; w: number; h: number }
export type Heights = Record<string, number>;

export function rect(n: BoardNode, heights: Heights): Rect {
  const w = W[n.depth];
  return { x: n.cx - w / 2, y: n.y, w, h: heights[n.id] ?? 44 };
}

/** How far a wire bows out of the edge it leaves. Held between a floor that
 *  keeps the curve reading as a curve and a ceiling that stops a long wire
 *  swinging out across half the board on its way. */
const bow = (span: number) => Math.min(150, Math.max(40, Math.abs(span) * 0.42));

/**
 * Where a wire leaves and lands.
 *
 * Sideways when the boxes are further apart across the page than down it,
 * top-to-bottom otherwise — but measured between the boxes' *edges*, not their
 * centres. Centres will call two boxes horizontally separated while they still
 * overlap horizontally, and the wire then leaves the right-hand edge of a box
 * to reach something whose left edge is further left than where it started, so
 * it doubles back over itself in a loop. The gap between the facing edges is
 * the thing that decides it.
 */
export function ends(a: Rect, b: Rect) {
  const gapX = Math.max(b.x - (a.x + a.w), a.x - (b.x + b.w));
  const gapY = Math.max(b.y - (a.y + a.h), a.y - (b.y + b.h));
  if (gapX > gapY) {
    const right = b.x > a.x;
    const p0 = { x: right ? a.x + a.w : a.x, y: a.y + a.h / 2 };
    const p1 = { x: right ? b.x : b.x + b.w, y: b.y + b.h / 2 };
    const d = bow(p1.x - p0.x);
    return [p0, { x: p0.x + (right ? d : -d), y: p0.y }, { x: p1.x + (right ? -d : d), y: p1.y }, p1] as const;
  }
  const down = b.y + b.h / 2 > a.y + a.h / 2;
  const p0 = { x: a.x + a.w / 2, y: down ? a.y + a.h : a.y };
  const p1 = { x: b.x + b.w / 2, y: down ? b.y : b.y + b.h };
  const d = bow(p1.y - p0.y);
  return [p0, { x: p0.x, y: p0.y + (down ? d : -d) }, { x: p1.x, y: p1.y + (down ? -d : d) }, p1] as const;
}

export interface WirePath {
  key: string;
  d: string;
  out: boolean;
  fresh: boolean;
  label?: string;
  lx: number;
  ly: number;
}

/** A label's footprint, near enough. 10.5px mono is about 6.1px a character,
 *  and the text is centred on its anchor. */
const LABEL_H = 14;
const labelHalf = (text: string) => (text.length * 6.1) / 2 + 4;

/** Two wires leaving the same box land their labels within a few pixels of
 *  each other — a fan-out of two puts both midpoints on nearly the same line —
 *  and the result is one word printed over another. Nudge them apart down the
 *  page, keeping each near its own wire. Order is stable, so a label does not
 *  jump sides when an unrelated box moves. */
function spread(paths: WirePath[]): void {
  const labelled = paths.filter((p) => p.label);
  for (let i = 1; i < labelled.length; i++) {
    for (let guard = 0; guard < 8; guard++) {
      const p = labelled[i];
      const hit = labelled
        .slice(0, i)
        .find((q) =>
          Math.abs(q.lx - p.lx) < labelHalf(q.label!) + labelHalf(p.label!) &&
          Math.abs(q.ly - p.ly) < LABEL_H);
      if (!hit) break;
      p.ly = hit.ly + (p.ly >= hit.ly ? LABEL_H : -LABEL_H);
    }
  }
}

export function wirePaths(
  nodes: BoardNode[],
  wires: Wire[],
  heights: Heights,
  fresh: Set<string> | null,
): WirePath[] {
  const byId = (id: string) => nodes.find((n) => n.id === id);
  const out: WirePath[] = [];
  for (const wr of wires) {
    const a = byId(wr.from);
    const b = byId(wr.to);
    if (!a || !b) continue;
    const [p0, c1, c2, p1] = ends(rect(a, heights), rect(b, heights));
    out.push({
      key: wr.from + ">" + wr.to,
      d: `M${p0.x} ${p0.y} C${c1.x} ${c1.y} ${c2.x} ${c2.y} ${p1.x} ${p1.y}`,
      out: Boolean(wr.out || a.out || b.out),
      fresh: Boolean(fresh && fresh.has(wr.from + ">" + wr.to)),
      label: wr.label,
      /* the point the curve actually passes through at its middle, which for a
         symmetric cubic is the midpoint of its ends */
      lx: (p0.x + p1.x) / 2,
      ly: (p0.y + p1.y) / 2 - 6,
    });
  }
  spread(out);
  return out;
}

export interface Bounds { x0: number; y0: number; x1: number; y1: number }

export function bounds(
  nodes: BoardNode[],
  annos: Anno[],
  lanes: Lane[],
  heights: Heights,
  ids?: string[] | null,
): Bounds | null {
  const list = ids ? nodes.filter((n) => ids.includes(n.id)) : nodes;
  if (!list.length && (ids || !lanes.length)) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of list) {
    const r = rect(n, heights);
    x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y);
    x1 = Math.max(x1, r.x + r.w); y1 = Math.max(y1, r.y + r.h);
  }
  if (!ids) {
    for (const a of annos) {
      x0 = Math.min(x0, a.x); y0 = Math.min(y0, a.y);
      x1 = Math.max(x1, a.x + (a.w || 180)); y1 = Math.max(y1, a.y + 60);
    }
    /* Lanes too: an approach rejected before anything was drawn under it has no
       boxes, only a rectangle and the reason it lost. Fitting to the boxes
       alone leaves that off the bottom of the screen. */
    for (const l of lanes) {
      x0 = Math.min(x0, l.x); y0 = Math.min(y0, l.y);
      x1 = Math.max(x1, l.x + l.w); y1 = Math.max(y1, l.y + l.h);
    }
  }
  return { x0, y0, x1, y1 };
}
