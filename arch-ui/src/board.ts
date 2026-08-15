/**
 * Board mode: every diagram at once, packed on one canvas.
 *
 * The reason this is a mode and not the default: a board is an excellent
 * read-only artifact and a mediocre working surface. Mid-turn the architect is
 * adding components under you, and a board that repacks while you are reading
 * frame four is worse than a single diagram that grows in place. So the working
 * view stays full-size and single, and this is what you zoom out into when you
 * want the whole thing in one screenshot.
 *
 * Two rules keep it from being the duplication mess it could easily be:
 *
 *  1. The design frame draws the *same nodes at the same keys* as the design
 *     view. Not a copy laid out again — the same arrangement, framed. Drag a
 *     card here and it moves there, because there is one arrangement.
 *  2. A flow is not drawn as a second component diagram. It is a sequence, and
 *     it is drawn as one — a ladder of chips. Nobody mistakes it for a claim
 *     about structure, which is the whole reason the same component can appear
 *     in the design frame and in three ladders without confusing anyone.
 */
import type { ArchState, Flow } from "./types";
import type { XY } from "./store/canvas";
import type { Size } from "./layout";

export interface Box extends XY { w: number; h: number }

/** One rung: a component, and how the flow got to it. */
export interface Rung {
  id: string;
  /** The step's action, or null for the rung the flow starts on. */
  via: string | null;
  /** True when this rung does not follow from the one above — the flow jumped. */
  jump: boolean;
}

export interface Lane extends Box {
  flow: Flow;
  rungs: Rung[];
}

export interface Board {
  design: Box;
  lanes: Lane[];
  /** Everything, for the framer. */
  bounds: Box;
}

const PAD = 44;          // breathing room inside a frame, around its contents
const TITLE = 30;        // the frame's label strip
const GUTTER = 90;       // between frames
export const LANE_W = 268;
const RUNG_H = 26;
const STEP_H = 28;       // the arrow and its label, between two rungs
const LANE_PAD = 14;

/**
 * The flow as a ladder.
 *
 * Consecutive steps usually share a node (`a→b`, `b→c`), so the ladder is the
 * chain those hops make. When a step does not follow from the last — the flow
 * jumps to somewhere else — its source starts a fresh rung, marked, rather than
 * being silently welded onto a sequence it is not part of.
 */
export function rungs(flow: Flow, known: (id: string) => boolean): Rung[] {
  const out: Rung[] = [];
  for (const step of flow.steps) {
    if (!known(step.src) || !known(step.dst)) continue;
    const tail = out.length ? out[out.length - 1].id : null;
    if (step.src !== tail) {
      out.push({ id: step.src, via: null, jump: out.length > 0 });
    }
    out.push({ id: step.dst, via: step.action, jump: false });
  }
  return out;
}

export function laneHeight(count: number): number {
  if (count === 0) return TITLE + LANE_PAD * 2;
  return TITLE + LANE_PAD * 2 + count * RUNG_H + (count - 1) * STEP_H;
}

/**
 * Where the design frame ends and the ladders begin.
 *
 * Quantised on purpose. The ladders sit to the right of the design, so without
 * this every component the architect adds nudges the whole column sideways
 * while you are reading it. Rounding the design's width up to a step means the
 * board only moves when it genuinely has to, and then by a visible amount
 * rather than by eleven pixels.
 */
const QUANTUM = 400;
const quantise = (w: number) => Math.ceil(w / QUANTUM) * QUANTUM;

/** The board for `arch`, given where the design's cards currently sit. */
export function board(
  arch: ArchState | null,
  positions: Record<string, XY>,
  keyOf: (id: string) => string,
  size: Size,
): Board {
  const ids = Object.keys(arch?.components ?? {});
  const at = ids.map((id) => positions[keyOf(id)]).filter(Boolean);

  const spread = at.length
    ? {
        x: Math.min(...at.map((p) => p.x)),
        y: Math.min(...at.map((p) => p.y)),
        w: Math.max(...at.map((p) => p.x)) - Math.min(...at.map((p) => p.x)) + size.w,
        h: Math.max(...at.map((p) => p.y)) - Math.min(...at.map((p) => p.y)) + size.h,
      }
    : { x: 0, y: 0, w: size.w, h: size.h };

  const design: Box = {
    x: spread.x - PAD,
    y: spread.y - PAD - TITLE,
    w: spread.w + PAD * 2,
    h: spread.h + PAD * 2 + TITLE,
  };

  /**
   * The ladders sit in a row beside the design, not stacked under each other.
   *
   * Stacked, a thirteen-step flow is 700px of column against a design frame
   * 150px tall, and the board becomes one short diagram with a canyon of white
   * space beneath it — which then fits at a zoom where nothing is readable. Side
   * by side the board's height is the tallest ladder and its width grows the
   * way a screen already does.
   */
  const known = (id: string) => !!arch?.components[id];
  const left = design.x + quantise(design.w) + GUTTER;
  const lanes: Lane[] = [];
  for (const flow of arch?.flows ?? []) {
    const rs = rungs(flow, known);
    if (!rs.length) continue;
    lanes.push({
      flow,
      rungs: rs,
      x: left + lanes.length * (LANE_W + GUTTER / 2),
      y: design.y,
      w: LANE_W,
      h: laneHeight(rs.length),
    });
  }

  const right = lanes.length
    ? lanes[lanes.length - 1].x + LANE_W
    : design.x + design.w;
  const bottom = Math.max(design.y + design.h, ...lanes.map((l) => l.y + l.h), design.y);
  return {
    design,
    lanes,
    bounds: { x: design.x, y: design.y, w: right - design.x, h: bottom - design.y },
  };
}
