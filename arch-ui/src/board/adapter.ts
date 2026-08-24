import type { ArchState, WireNode } from "../wire/types";
import { W } from "./geometry";
import { estimateHeight, layoutBoard, type Cell, type Link } from "./layout";
import type { Anno, BoardNode, Lane, Wire } from "./types";

/**
 * The harness's graph, arranged into a board.
 *
 * The harness stores *what the design is* — boxes, edges, which approach each
 * box belongs to. It does not store what the board looks like, except where a
 * person has dragged something: `x`/`y` are null until somebody arranges them.
 * So this module supplies the arrangement, and any hand-placed box overrides it.
 *
 * The columns are the point. Approaches sit side by side with the shared spine
 * between them, so a fork is visible where it actually forks and the boxes both
 * takes use are drawn once, in the middle.
 *
 * What happens *inside* a column is `layout.ts`: boxes are layered along their
 * wires rather than stacked in dictionary order, and rows are sized to what is
 * actually in the boxes. This module decides where the columns go and how wide
 * they are — which is now a question the layout answers, not a constant.
 */

/** Room above a lane's first box for the lane's own label. A rejected lane
 *  gets more, because its note is the reason it lost rather than a one-line
 *  summary, and that reason is most of what the lane is still there for. */
const HEAD = 46;
const HEAD_OUT = 122;
/** An approach with nothing drawn under it is a rectangle and a paragraph, so
 *  it is sized to the paragraph. */
const EMPTY_LANE_W = 470;
const RAMP = 5;    // territory steps the stylesheet defines (--ramp-1..5)
const TOP = 30;
/** Between the live board and the row of rejected approaches under it. */
const LOST_GAP = 88;
const SHARED = " shared"; // not a legal approach id, so it cannot collide

/** A box is out when it was greyed itself, or when every approach it belonged
 *  to lost. A shared box outlives all of them; a box with one surviving label
 *  stays live, which is what makes a hybrid work. */
export function isGreyed(arch: ArchState, n: WireNode | undefined): boolean {
  if (!n) return false;
  if (n.status === "greyed") return true;
  if (!n.approaches.length) return false;
  return n.approaches.every((a) => arch.approaches[a]?.status === "greyed");
}

/** Which column a box lives in. A box in several live approaches has no single
 *  column, so it joins the shared spine — which is what it has become. */
function columnOf(arch: ArchState, n: WireNode): string {
  const known = n.approaches.filter((a) => arch.approaches[a]);
  if (known.length === 1) return known[0];
  if (known.length > 1) {
    const survivors = known.filter((a) => arch.approaches[a].status === "active");
    if (survivors.length === 1) return survivors[0];
  }
  return SHARED;
}

interface Column {
  key: string;
  /** which step of the stylesheet's territory ramp this column wears — "s" for
   *  the shared spine, "1".."5" for approaches. A slot, not an identity. */
  slot: string;
  name: string;
  note: string;
  out: boolean;
  taken: boolean;
  /** whether the column draws a lane behind its boxes. The shared spine is
   *  shared *between* approaches — with nothing to sit between, the label is
   *  noise, so the boxes are laid out and the rectangle is not drawn. */
  chrome: boolean;
  nodes: WireNode[];
}

function columns(arch: ArchState): Column[] {
  const bucket = new Map<string, WireNode[]>();
  for (const n of Object.values(arch.nodes)) {
    const k = columnOf(arch, n);
    const list = bucket.get(k);
    if (list) list.push(n);
    else bucket.set(k, [n]);
  }

  const approaches = Object.values(arch.approaches);
  const live = approaches.filter((a) => a.status === "active");
  const lost = approaches.filter((a) => a.status === "greyed");

  /* Colour goes by position in the approach list, not by live/lost order or by
     where the column lands on screen — so an approach keeps its tint when a
     rival appears beside it or when it loses and drops to the row below. Past
     RAMP the hues repeat; five is the ramp the stylesheet defines, and two
     approaches sharing a tint is the honest failure, not a sixth colour
     invented here. */
  const slots = new Map<string, string>([[SHARED, "s"]]);
  approaches.forEach((a, i) => slots.set(a.id, String((i % RAMP) + 1)));

  const asColumn = (id: string): Column => {
    if (id === SHARED) {
      return {
        key: SHARED,
        slot: "s",
        name: "Shared",
        note: "same either way",
        out: false,
        taken: false,
        chrome: live.length > 1,
        nodes: bucket.get(SHARED) ?? [],
      };
    }
    const a = arch.approaches[id];
    return {
      key: id,
      slot: slots.get(id) ?? "1",
      name: a.name,
      note: a.status === "greyed" ? a.rejected_reason : a.summary,
      out: a.status === "greyed",
      /* the survivor is only "taken" once something else has actually lost — a
         single approach nobody argued against has not won anything */
      taken: a.status === "active" && lost.length > 0 && live.length === 1,
      chrome: true,
      nodes: bucket.get(id) ?? [],
    };
  };

  /* The shared spine goes between the live approaches — half of them to its
     left, half to its right. With a single approach that puts the spine first,
     which is the right way round: one approach is not a fork, it is a note on
     one part of a design the spine is otherwise carrying, and the spine is what
     you read first. */
  const half = Math.floor(live.length / 2);
  const leftIds = live.slice(0, half).map((a) => a.id);
  const rightIds = live.slice(half).map((a) => a.id);
  const ordered = [...leftIds, SHARED, ...rightIds]
    .map(asColumn)
    // an empty spine with no fork to sit between is nothing at all
    .filter((c) => c.nodes.length > 0 || c.chrome);

  return [...ordered, ...lost.map((a) => asColumn(a.id))];
}

export interface BoardView {
  lanes: Lane[];
  nodes: BoardNode[];
  wires: Wire[];
  annos: Anno[];
}

/** The board as it should be drawn.
 *
 *  `heights` is what the DOM actually rendered, fed back by `Board.tsx`. The
 *  arrangement needs it: a stub is 39px and a detailed box can be 171, and a
 *  column spaced for the tallest is the ribbon of whitespace this used to draw.
 *  A box's height does not depend on where it is put, so feeding measurements
 *  into the thing that puts them settles after one pass. Anything unmeasured —
 *  a box on its very first frame — is estimated. */
export function toBoard(arch: ArchState, heights: Record<string, number> = {}): BoardView {
  const cols = columns(arch);
  const lanes: Lane[] = [];
  const nodes: BoardNode[] = [];

  const read = (n: WireNode): BoardNode => ({
    id: n.id,
    lane: "",
    slot: "",
    cx: 0,
    y: 0,
    kind: n.kind,
    depth: n.depth,
    label: n.label,
    resp: n.responsibility,
    tech: n.tech,
    /* `detail` is prose in the harness and mono lines on the board: one line
       per line, which is how it is written and how it reads */
    rows: n.detail.split("\n").map((r) => r.trim()).filter(Boolean),
    approaches: n.approaches,
    existing: n.existing,
    out: isGreyed(arch, n),
  });

  /** Which column each box ended up in. */
  const home = new Map<string, string>();
  for (const c of cols) for (const n of c.nodes) home.set(n.id, c.key);
  const chromeOn = new Map(cols.map((c) => [c.key, c.chrome ? (c.out ? HEAD_OUT : HEAD) : 0]));
  const head = (col: string) => chromeOn.get(col) ?? 0;

  const links: Link[] = arch.edges
    .filter((e) => e.src !== e.dst && home.has(e.src) && home.has(e.dst))
    .map((e) => ({ from: e.src, to: e.dst }));

  /** One arrangement over a set of columns. The live board is arranged first;
   *  whatever lost is arranged again underneath it, on its own row, because a
   *  rejected approach is still part of the record and is not part of the
   *  design's flow. */
  const arrange = (group: Column[], top: number): number => {
    if (!group.length) return top;
    const keys = group.map((c) => c.key);
    const inGroup = new Set(keys);
    const built = new Map<string, BoardNode>();
    const cells: Cell[] = [];
    for (const c of group) {
      for (const n of c.nodes) {
        const b = read(n);
        b.lane = c.key;
        b.slot = c.slot;
        built.set(n.id, b);
        cells.push({
          id: n.id,
          col: c.key,
          w: W[b.depth] ?? W.stub,
          h: heights[n.id] ?? estimateHeight(b),
          fixed: n.x !== null && n.y !== null ? { cx: n.x, y: n.y } : null,
        });
      }
    }
    const laid = layoutBoard(
      cells,
      links.filter((l) => inGroup.has(home.get(l.from)!) && inGroup.has(home.get(l.to)!)),
      keys,
      { x: TOP, y: top },
      head,
    );

    let bottom = top;
    for (const cell of cells) {
      const b = built.get(cell.id)!;
      const spot = cell.fixed ?? laid.at.get(cell.id);
      if (spot) { b.cx = spot.cx; b.y = spot.y; }
      nodes.push(b);
    }
    let right = TOP;
    const empty: Column[] = [];
    for (const c of group) {
      const e = laid.cols.get(c.key);
      if (!e) continue;
      if (!e.h) { if (c.chrome) empty.push(c); continue; }
      bottom = Math.max(bottom, e.y + e.h);
      right = Math.max(right, e.x + e.w);
      if (c.chrome) {
        lanes.push({
          k: c.key, slot: c.slot, name: c.name, note: c.note,
          x: e.x, y: e.y, w: e.w, h: e.h, out: c.out, taken: c.taken,
        });
      }
    }

    /* An approach with no boxes under it is still a lane — for one that lost,
       its rectangle is the only place the reason is written down. It has no
       content to be sized or placed by, so it goes after everything that does,
       rather than at the column the ordering gave it, where it would sit on top
       of whatever somebody had dragged into that corner. */
    for (const c of empty) {
      const h = head(c.key) + 26;
      lanes.push({
        k: c.key, slot: c.slot, name: c.name, note: c.note,
        x: right, y: top, w: EMPTY_LANE_W, h, out: c.out, taken: c.taken,
      });
      right += EMPTY_LANE_W + 40;
      bottom = Math.max(bottom, top + h);
    }
    return bottom;
  };

  const bottom = arrange(cols.filter((c) => !c.out), TOP);
  // whatever lost goes on a row of its own underneath — still on the board,
  // still carrying the reason it lost
  arrange(cols.filter((c) => c.out), bottom + LOST_GAP);

  const wires: Wire[] = arch.edges.map((e) => ({
    from: e.src,
    to: e.dst,
    label: e.label || undefined,
    kind: e.kind,
    notes: e.notes,
    out: isGreyed(arch, arch.nodes[e.src]) || isGreyed(arch, arch.nodes[e.dst]),
  }));

  const placed = new Map(nodes.map((n) => [n.id, n]));
  const annos: Anno[] = arch.annotations.map((a) => {
    if (a.anchor && a.x === 0 && a.y === 0) {
      // pinned but never dragged: sit it beside what it is about
      const host = placed.get(a.anchor);
      if (host) {
        return {
          id: a.id,
          x: host.cx + (W[host.depth] ?? W.stub) / 2 + 26,
          y: host.y + 4,
          w: a.w || 190,
          text: a.text,
          anchor: a.anchor,
        };
      }
    }
    return { id: a.id, x: a.x, y: a.y, w: a.w || 190, text: a.text, anchor: a.anchor };
  });

  return { lanes, nodes, wires, annos };
}
