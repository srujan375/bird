/**
 * Arranging the board.
 *
 * The harness says what the design *is*; it says nothing about what it looks
 * like. Before this module a column was a single stack — every box on its own
 * 200px row, in whatever order the graph's dictionary happened to iterate. Two
 * things followed from that, and both are why a real board read as confetti:
 *
 *  - **The rows were a constant and the boxes are not.** A stub renders 39px
 *    tall and a detailed box 171px, so every stub sat in the middle of 160px of
 *    nothing while a detailed box nearly touched the next one down. Rhythm has
 *    to come from what is actually in the box.
 *  - **Insertion order is not reading order.** A chain of five boxes could be
 *    dealt out as positions 13, 10, 12, 6, 7, so its wires crossed the whole
 *    board to get anywhere. What the wires say is the design; the arrangement
 *    is the thing that should make them short.
 *
 * So the two axes carry different things, and neither one is dictionary order:
 *
 *  - **Down the page is the design's flow.** Layers come from the wires across
 *    the *whole* board, not within a column. A chain that steps out into an
 *    approach and back — which is the normal shape, because the box a fork is
 *    about is exactly the one that has an approach — still reads downwards.
 *  - **Across the page is which approach.** A column is an approach's
 *    territory; the layout only decides how wide it needs to be.
 *
 * Boxes wired to nothing at all are not part of that story. They are the pieces
 * the design touches, and they go on a shelf underneath it rather than taking a
 * row each in the middle of the argument.
 *
 * Heights are *measured*, not guessed — `Board.tsx` feeds back what the DOM
 * rendered. `estimateHeight` covers the first frame only, before anything has
 * been measured. A box's height never depends on where it was put, so feeding
 * measurements back in settles after one pass; it does not oscillate.
 */
import { W } from "./geometry";
import type { BoardNode } from "./types";

/** Between one layer and the next. Enough for a wire to leave, curve, and land
 *  with its arrowhead reading as an arrowhead. */
const LAYER_GAP = 66;
/** Between two boxes sharing a layer. */
const SIB_GAP = 30;
/** Between the layered design and the shelf of unwired boxes under it. */
const SHELF_GAP = 76;
/** Inside a lane, around everything. */
export const PAD = 26;
/** Between one column and the next. */
export const COL_GAP = 40;
/** A column is at least this wide, so a lone box is not wearing a lane that
 *  fits it like a sleeve. */
const MIN_INNER = 300;
/** How wide a shelf runs before it wraps — three stubs across, which is about
 *  as much as reads as one group. */
const SHELF_INNER = 620;

export interface Cell {
  id: string;
  /** the column it belongs to — an approach, or the shared spine */
  col: string;
  w: number;
  h: number;
  /** where somebody put it; set means this box is no longer ours to place */
  fixed: { cx: number; y: number } | null;
}

export interface Link { from: string; to: string }

export interface Placement { cx: number; y: number }

export interface Extent { x: number; y: number; w: number; h: number }

export interface BoardLayout {
  /** where the layout would put each box, in absolute board coordinates. A box
   *  somebody has moved has an answer here too — it is just not the one drawn */
  at: Map<string, Placement>;
  /** where each column's territory sits, whether or not it draws a lane */
  cols: Map<string, Extent>;
}

/* ── heights ──────────────────────────────────────────────────────────── */

/* Tracks `.node` in board.css — the proposed card: 12/14 padding, a 15px
   display label, 13.5px sentence, a key/value strip at 12.5px mono, keyed
   list rows, and a wire-derived footer. Only has to be close — the DOM
   replaces the estimate on the next frame. */
const CHROME = 26;    // padding 12 + 12, border 1 + 1
const LINE_LABEL = 19;
const LINE_RESP = 19.5;
const LINE_MONO = 18.75;  // 12.5px * 1.5
const RESP_TOP = 8;
const SECTION_TOP = 23;   // margin 12 + rule 1 + padding 10
const ROW_H = 24;         // 12.5px/1.45 + 3+3 padding
const ROW_D = 16;         // a description line under a row
const FOOT_H = 16;

/** Roughly how many lines of `text` fit across `width` at `perChar` px. */
const lines = (text: string, width: number, perChar: number) =>
  text ? Math.max(1, Math.ceil((text.length * perChar) / Math.max(40, width))) : 0;

/** What this box will probably render as, for the one frame before the DOM has
 *  said. Never used once `Board.tsx` has a measurement for it. */
export function estimateHeight(n: BoardNode): number {
  const w = W[n.depth] ?? W.stub;
  const inner = w - 27; // horizontal padding + the lane rule
  // the kind pill sits beside the label and eats into its line
  const head = LINE_LABEL * lines(n.label, inner - n.kind.length * 7 - 22, 8);
  let h = CHROME + head;
  if (n.depth === "stub") return Math.round(h);
  if (n.resp) h += RESP_TOP + LINE_RESP * lines(n.resp, inner, 7);
  const facts = n.facts.filter(([, v]) => v);
  if (facts.length || n.tech) h += SECTION_TOP + (facts.length + (n.tech ? 1 : 0)) * LINE_MONO;
  if (n.depth === "detailed") {
    const rows = n.items.length ? n.items : n.rows.map((v) => ({ k: "", v, d: "" }));
    if (rows.length) {
      const shown = rows.slice(0, 6);
      h += SECTION_TOP + 20 + shown.length * ROW_H + shown.filter((r) => r.d).length * ROW_D;
      if (rows.length > 6) h += 18;
    }
    if (n.derived.length) h += SECTION_TOP - 1 + FOOT_H;
  }
  return Math.round(h);
}

/* ── the graph ────────────────────────────────────────────────────────── */

/** Who feeds a box and who it feeds. */
function sides(links: Link[]) {
  const preds = new Map<string, string[]>();
  const succs = new Map<string, string[]>();
  const push = (m: Map<string, string[]>, k: string, v: string) => {
    const list = m.get(k);
    if (list) list.push(v);
    else m.set(k, [v]);
  };
  for (const l of links) { push(preds, l.to, l.from); push(succs, l.from, l.to); }
  return { preds, succs };
}

/** Depth-first, dropping any edge that points back at something still open.
 *  A design can describe a cycle — a store a registry reads that also writes
 *  the store — and layering needs a direction, so one edge of each cycle stops
 *  counting for *placement*. It is still drawn; it just gets no say in what
 *  sits above what. */
function forwardEdges(ids: string[], links: Link[]): Link[] {
  const out = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const l of links) out.get(l.from)?.push(l.to);
  const state = new Map<string, 0 | 1 | 2>(ids.map((id) => [id, 0]));
  const keep: Link[] = [];
  const walk = (root: string) => {
    // explicit stack: a wide design should not depend on the JS call depth
    const stack: Array<{ id: string; i: number }> = [{ id: root, i: 0 }];
    state.set(root, 1);
    while (stack.length) {
      const top = stack[stack.length - 1];
      const kids = out.get(top.id) ?? [];
      if (top.i >= kids.length) { state.set(top.id, 2); stack.pop(); continue; }
      const next = kids[top.i++];
      if (state.get(next) === 1) continue; // back edge — not a layering signal
      keep.push({ from: top.id, to: next });
      if (state.get(next) === 0) { state.set(next, 1); stack.push({ id: next, i: 0 }); }
    }
  };
  const indeg = new Map<string, number>(ids.map((id) => [id, 0]));
  for (const l of links) indeg.set(l.to, (indeg.get(l.to) ?? 0) + 1);
  // sources first, so the natural roots become layer 0 rather than whichever
  // member of a cycle happened to be visited first
  for (const id of ids) if (!indeg.get(id) && state.get(id) === 0) walk(id);
  for (const id of ids) if (state.get(id) === 0) walk(id);
  return keep;
}

/** Kahn's order, sources first. */
function topo(ids: string[], dag: Link[]): string[] {
  const { succs } = sides(dag);
  const indeg = new Map<string, number>(ids.map((id) => [id, 0]));
  for (const l of dag) indeg.set(l.to, (indeg.get(l.to) ?? 0) + 1);
  const out = ids.filter((id) => !indeg.get(id));
  for (let i = 0; i < out.length; i++) {
    for (const w of succs.get(out[i]) ?? []) {
      indeg.set(w, indeg.get(w)! - 1);
      if (!indeg.get(w)) out.push(w);
    }
  }
  return out;
}

/**
 * Which layer each box sits in — every wire pointing down the page.
 *
 * The longest path from a source puts each box directly under the last thing
 * that feeds it, which is what you want everywhere except at the very top:
 * anything nothing feeds is forced into layer 0, so a board reverse-engineered
 * from a codebase opens with twenty boxes strung across the top and a long
 * diagonal underneath them.
 *
 * Only those boxes are wrong, so only those move — each dropped to sit just
 * above the highest thing it feeds. Nothing points *into* a box with no
 * predecessors, so moving one cannot turn another box's wire upwards, and
 * everything else keeps the layer that already had it hugging its own inputs.
 */
function layerOf(ids: string[], dag: Link[]): Map<string, number> {
  const { preds, succs } = sides(dag);
  const order = topo(ids, dag);

  const layer = new Map<string, number>(ids.map((id) => [id, 0]));
  for (const v of order) {
    for (const p of preds.get(v) ?? []) layer.set(v, Math.max(layer.get(v)!, layer.get(p)! + 1));
  }

  for (const id of ids) {
    if (preds.get(id)?.length) continue;
    const below = (succs.get(id) ?? []).map((s) => layer.get(s)!);
    if (below.length) layer.set(id, Math.min(...below) - 1);
  }

  /* A source with no successors at all keeps layer 0 while the rest have moved
     down, and a cycle's dropped edge can leave a box out of `order` entirely —
     either way the top of the board is wherever the highest box ended up. */
  const floor = Math.min(...layer.values());
  if (floor) for (const [id, v] of layer) layer.set(id, v - floor);
  return layer;
}

/* ── ordering within a layer ──────────────────────────────────────────── */

/** Sort each layer by where its neighbours in the layer above sit, then by
 *  where its neighbours below sit, and repeat — the usual way to stop wires
 *  crossing for no reason. Column order is the outer key throughout: a box may
 *  not leave its approach's territory to be near what it is wired to.
 *
 *  Two sweeps is enough for the boards this draws; more of them mostly
 *  reshuffles rows that were already fine. */
function order(rows: string[][], dag: Link[], colRank: Map<string, number>): void {
  const { preds, succs } = sides(dag);
  const sweep = (rel: Map<string, string[]>, from: number, to: number, step: number) => {
    for (let r = from; r !== to; r += step) {
      const above = new Map(rows[r - step].map((id, i) => [id, i]));
      const key = new Map<string, number>();
      rows[r].forEach((id, i) => {
        const near = (rel.get(id) ?? [])
          .map((n) => above.get(n))
          .filter((v): v is number => v !== undefined);
        /* nothing to be near: hold position, so an unrelated box is not dragged
           to the front of the row by a default of zero */
        key.set(id, near.length ? near.reduce((a, b) => a + b, 0) / near.length : i);
      });
      rows[r] = [...rows[r]].sort(
        (a, b) => colRank.get(a)! - colRank.get(b)! || key.get(a)! - key.get(b)!,
      );
    }
  };
  for (let pass = 0; pass < 2; pass++) {
    if (rows.length > 1) sweep(preds, 1, rows.length, 1);
    if (rows.length > 1) sweep(succs, rows.length - 2, -1, -1);
  }
}

/* ── x placement ──────────────────────────────────────────────────────── */

/** Pull each box towards the average of what it is wired to, then push its row
 *  apart again so nothing overlaps. Centring every row would be tidier on its
 *  own and would leave every wire slanting; this keeps a chain vertical and
 *  lets a fan spread under the thing it fans out of.
 *
 *  Only same-column wires get a vote, because `x` here is measured from the
 *  column's own axis and a box in the next column over is not on this ruler. */
function align(rows: string[][], size: Map<string, Cell>, dag: Link[], x: Map<string, number>): void {
  const { preds, succs } = sides(dag.filter(
    (l) => size.get(l.from)?.col === size.get(l.to)?.col,
  ));
  const settle = (row: string[], want: Map<string, number>) => {
    // left to right, then right to left: each box takes what it asked for
    // unless a neighbour is already standing there
    let edge = -Infinity;
    for (const id of row) {
      const half = size.get(id)!.w / 2;
      const put = Math.max(want.get(id)!, edge + SIB_GAP + half);
      want.set(id, put);
      edge = put + half;
    }
    edge = Infinity;
    for (let i = row.length - 1; i >= 0; i--) {
      const half = size.get(row[i])!.w / 2;
      const put = Math.min(want.get(row[i])!, edge - SIB_GAP - half);
      want.set(row[i], put);
      edge = put - half;
    }
    for (const id of row) x.set(id, want.get(id)!);
  };
  const pass = (rel: Map<string, string[]>, from: number, to: number, step: number) => {
    for (let r = from; r !== to; r += step) {
      // a row is laid out per column; each column's boxes settle among their own
      const byCol = new Map<string, string[]>();
      for (const id of rows[r]) {
        const col = size.get(id)!.col;
        const list = byCol.get(col);
        if (list) list.push(id); else byCol.set(col, [id]);
      }
      for (const [col, ids] of byCol) {
        const want = new Map<string, number>();
        for (const id of ids) {
          const near = (rel.get(id) ?? [])
            .filter((n) => size.get(n)?.col === col && x.has(n))
            .map((n) => x.get(n)!);
          want.set(id, near.length ? near.reduce((a, b) => a + b, 0) / near.length : x.get(id)!);
        }
        settle(ids, want);
      }
    }
  };
  for (let i = 0; i < 3; i++) {
    if (rows.length > 1) pass(preds, 1, rows.length, 1);
    if (rows.length > 1) pass(succs, rows.length - 2, -1, -1);
  }
}

/* ── the board ────────────────────────────────────────────────────────── */

/**
 * Arrange every box on the board.
 *
 * `colOrder` fixes which column sits where across the page; this decides how
 * wide each one has to be and what goes where inside it. Coordinates come back
 * absolute, with `originX`/`originY` as the board's top-left.
 */
export function layoutBoard(
  boxes: Cell[],
  links: Link[],
  colOrder: string[],
  origin: { x: number; y: number },
  /** extra room at the top of a column that draws a lane label */
  headFor: (col: string) => number,
): BoardLayout {
  const at = new Map<string, Placement>();
  const cols = new Map<string, Extent>();
  const size = new Map(boxes.map((b) => [b.id, b]));
  const rank = new Map(colOrder.map((c, i) => [c, i]));
  const colRank = new Map(boxes.map((b) => [b.id, rank.get(b.col) ?? 0]));

  /* A box somebody has dragged still gets a slot here, and then sits somewhere
     else. Skipping it would be tidier — no hole where it used to be — but it
     would mean the boxes around it close the gap, and then dragging one thing
     quietly moves two others. Where a box sits is the one thing on this board
     a person chose directly; nothing should move underneath that choice. So the
     layout says where every box *would* go and a hand-placed one overrides its
     own answer, leaving the rest of the arrangement exactly where it was. */
  const wired = new Set<string>();
  for (const l of links) { wired.add(l.from); wired.add(l.to); }
  const graphIds = boxes.filter((b) => wired.has(b.id)).map((b) => b.id);

  /* ── down the page: one layering for the whole board ── */
  const rows: string[][] = [];
  let dag: Link[] = [];
  if (graphIds.length) {
    dag = forwardEdges(graphIds, links);
    const layer = layerOf(graphIds, dag);
    const depth = Math.max(...graphIds.map((id) => layer.get(id)!)) + 1;
    for (let i = 0; i < depth; i++) rows.push([]);
    for (const id of graphIds) rows[layer.get(id)!].push(id);
    for (const row of rows) row.sort((a, b) => colRank.get(a)! - colRank.get(b)!);
    order(rows, dag, colRank);
  }

  /** each column's own top edge — an approach that only appears halfway down
   *  the design starts halfway down, which is what makes a fork look like one */
  const colTop = new Map<string, number>();
  const rowY: number[] = [];
  let y = origin.y;
  for (const row of rows) {
    for (const id of row) {
      const col = size.get(id)!.col;
      if (!colTop.has(col)) colTop.set(col, y);
    }
    rowY.push(y);
    y += Math.max(...row.map((id) => size.get(id)!.h)) + LAYER_GAP;
  }
  const flowBottom = rows.length ? y - LAYER_GAP : origin.y;

  /* Every column's label needs room above its first box, and a column that
     starts lower than another must not have its label collide with the box
     above it in the next column over — so the head is taken out of the box's
     own row rather than out of the gap. */
  const shift = new Map<string, number>();
  for (const col of colOrder) shift.set(col, headFor(col));

  /* ── across the page: x measured from each column's own axis ── */
  const localX = new Map<string, number>();
  const put = (ids: string[]) => {
    const span = ids.reduce((t, id) => t + size.get(id)!.w, 0) + SIB_GAP * (ids.length - 1);
    let left = -span / 2;
    for (const id of ids) { const w = size.get(id)!.w; localX.set(id, left + w / 2); left += w + SIB_GAP; }
  };
  for (const row of rows) {
    const byCol = new Map<string, string[]>();
    for (const id of row) {
      const col = size.get(id)!.col;
      const list = byCol.get(col);
      if (list) list.push(id); else byCol.set(col, [id]);
    }
    for (const ids of byCol.values()) put(ids);
  }
  align(rows, size, dag, localX);

  /* Aligning pulls boxes towards their wires, which leaves a column's contents
     sitting off its own axis — and then the shelf, which has no wires to be
     pulled by and is centred, lands somewhere else again. Re-centre what the
     design occupies so both halves of the column agree on where the middle is. */
  const bandOf = new Map<string, { lo: number; hi: number }>();
  for (const [id, lx] of localX) {
    const b = size.get(id)!;
    const band = bandOf.get(b.col);
    if (!band) bandOf.set(b.col, { lo: lx - b.w / 2, hi: lx + b.w / 2 });
    else { band.lo = Math.min(band.lo, lx - b.w / 2); band.hi = Math.max(band.hi, lx + b.w / 2); }
  }
  for (const [id, lx] of localX) {
    const band = bandOf.get(size.get(id)!.col)!;
    localX.set(id, lx - (band.lo + band.hi) / 2);
  }

  /* ── unwired boxes: a shelf under the design, per column ── */
  const shelfRows = new Map<string, string[][]>();
  for (const b of boxes) {
    if (wired.has(b.id)) continue;
    const packed = shelfRows.get(b.col) ?? [];
    if (!shelfRows.has(b.col)) shelfRows.set(b.col, packed);
    const row = packed[packed.length - 1];
    const span = row
      ? row.reduce((t, id) => t + size.get(id)!.w + SIB_GAP, 0) + b.w
      : b.w;
    if (row && span <= SHELF_INNER) row.push(b.id);
    else packed.push([b.id]);
  }

  /* ── how wide each column has to be, and where that puts it ── */
  const half = new Map<string, number>(colOrder.map((c) => [c, MIN_INNER / 2]));
  const widen = (col: string, reach: number) =>
    half.set(col, Math.max(half.get(col) ?? MIN_INNER / 2, reach));
  for (const [id, lx] of localX) {
    const b = size.get(id)!;
    widen(b.col, Math.abs(lx) + b.w / 2);
  }
  for (const [col, packed] of shelfRows) {
    for (const row of packed) {
      const span = row.reduce((t, id) => t + size.get(id)!.w, 0) + SIB_GAP * (row.length - 1);
      widen(col, span / 2);
    }
  }

  const axis = new Map<string, number>();
  let x = origin.x;
  for (const col of colOrder) {
    const w = half.get(col)! * 2 + PAD * 2;
    axis.set(col, x + w / 2);
    cols.set(col, { x, y: colTop.get(col) ?? origin.y, w, h: 0 });
    x += w + COL_GAP;
  }

  for (const [id, lx] of localX) at.set(id, { cx: axis.get(size.get(id)!.col)! + lx, y: 0 });
  rows.forEach((row, r) => {
    for (const id of row) {
      const spot = at.get(id);
      if (spot) spot.y = rowY[r] + shift.get(size.get(id)!.col)!;
    }
  });

  /* ── the shelves, once the design's own bottom is known ── */
  for (const [col, packed] of shelfRows) {
    let sy = rows.length ? flowBottom + SHELF_GAP : origin.y + shift.get(col)!;
    if (!colTop.has(col)) colTop.set(col, sy);
    for (const row of packed) {
      const span = row.reduce((t, id) => t + size.get(id)!.w, 0) + SIB_GAP * (row.length - 1);
      let left = axis.get(col)! - span / 2;
      for (const id of row) {
        const w = size.get(id)!.w;
        at.set(id, { cx: left + w / 2, y: sy });
        left += w + SIB_GAP;
      }
      sy += Math.max(...row.map((id) => size.get(id)!.h)) + SIB_GAP;
    }
  }

  /* ── what each column ended up covering ──
     A lane is the ground its boxes are standing on. A box somebody has dragged
     a little way still counts as standing on it, so the rectangle stretches to
     keep it — but one carried right across the board does not, and stretching
     to reach it would draw a lane the size of the board with two boxes in it
     and everything else underneath. Such a box keeps its column's colour on its
     own border, which is what says whose it is; the rectangle stays where the
     territory is. A column whose boxes have *all* been placed by hand has no
     other ground to describe, so there it follows them. */
  const span = (b: Cell, at: Placement) => ({
    x0: at.cx - b.w / 2 - PAD,
    y0: at.y - headFor(b.col) - PAD,
    x1: at.cx + b.w / 2 + PAD,
    y1: at.y + b.h + PAD,
  });
  const swallow = (
    seen: Map<string, { x0: number; y0: number; x1: number; y1: number }>,
    col: string,
    next: { x0: number; y0: number; x1: number; y1: number },
  ) => {
    const had = seen.get(col);
    if (!had) { seen.set(col, next); return; }
    had.x0 = Math.min(had.x0, next.x0);
    had.y0 = Math.min(had.y0, next.y0);
    had.x1 = Math.max(had.x1, next.x1);
    had.y1 = Math.max(had.y1, next.y1);
  };

  const ground = new Map<string, { x0: number; y0: number; x1: number; y1: number }>();
  for (const b of boxes) {
    const spot = at.get(b.id);
    if (spot && !b.fixed) swallow(ground, b.col, span(b, spot));
  }
  /** how far outside its lane a hand-placed box can be and still be in it */
  const SLACK = PAD * 2;
  const laid = new Map([...ground].map(([col, g]) => [col, { ...g }]));
  for (const b of boxes) {
    if (!b.fixed) continue;
    const next = span(b, b.fixed);
    // measured against the *auto* extent, so two boxes dragged to opposite
    // corners cannot chain a lane out across the gap between them
    const here = laid.get(b.col);
    const touching = here &&
      next.x0 < here.x1 + SLACK && next.x1 > here.x0 - SLACK &&
      next.y0 < here.y1 + SLACK && next.y1 > here.y0 - SLACK;
    if (touching || !here) swallow(ground, b.col, next);
  }

  for (const [col, e] of cols) {
    const g = ground.get(col);
    if (!g) { e.h = 0; continue; }
    e.x = Math.min(e.x, g.x0);
    e.w = Math.max(e.x + e.w, g.x1) - e.x;
    e.y = g.y0;
    e.h = g.y1 - g.y0;
  }

  return { at, cols };
}
