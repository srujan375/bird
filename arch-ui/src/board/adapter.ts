import type { ArchState, WireNode } from "../wire/types";
import { W } from "./geometry";
import { estimateHeight, layoutBoard, PAD, type Cell, type Link } from "./layout";
import type { Anno, BoardNode, Lane, Wire } from "./types";
import { KIND_FACTS, KIND_LIST, KIND_SIDES } from "./vocab";

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
/** Room at the top of an open container for its own name. */
export const HEAD_GROUP = 40;
/** A board bigger than this opens with its containers folded: the point of a
 *  container is that the reader meets six boxes before sixty. */
export const FOLD_ABOVE = 24;

/** Which boxes hold others, keyed by container id. Only a parent the board
 *  actually has counts; a dangling parent is a top-level box. */
export function membersOf(arch: ArchState): Map<string, WireNode[]> {
  const kids = new Map<string, WireNode[]>();
  for (const n of Object.values(arch.nodes)) {
    if (!n.parent || !arch.nodes[n.parent] || n.parent === n.id) continue;
    const list = kids.get(n.parent);
    if (list) list.push(n);
    else kids.set(n.parent, [n]);
  }
  return kids;
}

/** Container, its container, ... from the box outwards. Stops at a loop. */
function ancestors(arch: ArchState, id: string): string[] {
  const out: string[] = [];
  const seen = new Set([id]);
  let cur = arch.nodes[id];
  while (cur && cur.parent && arch.nodes[cur.parent] && !seen.has(cur.parent)) {
    out.push(cur.parent);
    seen.add(cur.parent);
    cur = arch.nodes[cur.parent];
  }
  return out;
}

/** Whether a container is drawn shut. */
export function foldedOf(
  arch: ArchState, folded: Record<string, boolean>, kids: Map<string, WireNode[]>,
): (id: string) => boolean {
  const byDefault = Object.keys(arch.nodes).length > FOLD_ABOVE;
  return (id) => kids.has(id) && (folded[id] ?? byDefault);
}

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
    // a member goes where its container goes
    if (n.parent && arch.nodes[n.parent] && n.parent !== n.id) continue;
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
export function toBoard(
  arch: ArchState,
  heights: Record<string, number> = {},
  folded: Record<string, boolean> = {},
): BoardView {
  const cols = columns(arch);
  const lanes: Lane[] = [];
  const nodes: BoardNode[] = [];
  const kids = membersOf(arch);
  const isFolded = foldedOf(arch, folded, kids);

  /* Who is on each side of a box's wires, named. Derived at render so it is
     never typed and never stale. */
  const sides = (n: WireNode) => {
    const [inName, outName] = KIND_SIDES[n.kind] ?? KIND_SIDES.service;
    const name = (id: string) => arch.nodes[id]?.label ?? id;
    const inc = arch.edges.filter((e) => e.dst === n.id).map((e) => name(e.src));
    const out = arch.edges.filter((e) => e.src === n.id).map((e) => name(e.dst));
    const uniq = (xs: string[]) => [...new Set(xs)];
    return [
      { side: inName, names: uniq(inc) },
      { side: outName, names: uniq(out) },
    ].filter((g) => g.names.length);
  };

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
    facts: (KIND_FACTS[n.kind] ?? []).map((k) => [k, n.facts?.[k] ?? ""] as [string, string]),
    listName: KIND_LIST[n.kind] ?? "items",
    items: (n.items ?? []).map((i) => ({ k: i.k ?? "", v: i.v ?? "", d: i.d ?? "" })),
    derived: sides(n),
    approaches: n.approaches,
    existing: n.existing,
    out: isGreyed(arch, n),
    parent: n.parent && arch.nodes[n.parent] ? n.parent : undefined,
    group: kids.has(n.id)
      ? { folded: isFolded(n.id), count: kids.get(n.id)!.length, w: 0, h: 0 }
      : undefined,
  });

  /** The box an edge end is drawn at: the outermost folded container around
   *  it, or the box itself when nothing around it is shut. */
  const shown = (id: string): string => {
    const chain = ancestors(arch, id);
    for (let i = chain.length - 1; i >= 0; i--) if (isFolded(chain[i])) return chain[i];
    return id;
  };

  /** Which member of `g` an id falls under — itself, or the member that
   *  contains it — or null when it is outside `g` altogether. */
  const under = (g: string, id: string): string | null => {
    if (arch.nodes[id]?.parent === g) return id;
    const chain = ancestors(arch, id);
    const i = chain.indexOf(g);
    return i > 0 ? chain[i - 1] : null;
  };

  /** Where the top-level box for an id is — itself, or the root container. */
  const topOf = (id: string): string => {
    const chain = ancestors(arch, id);
    return chain.length ? chain[chain.length - 1] : id;
  };

  /**
   * One open container, arranged. The members are laid out as a board of
   * their own — same layering, same shelf — in a single column, and the whole
   * thing becomes one cell of whatever holds it. Nothing is placed until the
   * container itself has been: `place` puts the members down relative to
   * wherever the outer arrangement (or a hand) put the container.
   *
   * A member somebody has dragged sits where they put it, same as any box,
   * and the container stretches to keep it — a member is on its container's
   * ground wherever that ground has to reach. The rest of the members keep
   * the arrangement the layout gave them.
   */
  interface Sub { w: number; h: number; place: (x0: number, y0: number, self: BoardNode) => void }
  const subtree = (g: WireNode, slot: string, lane: string): Sub => {
    const members = kids.get(g.id) ?? [];
    const built = new Map<string, BoardNode>();
    const subs = new Map<string, Sub>();
    const cells: Cell[] = [];
    for (const m of members) {
      const b = read(m);
      b.lane = lane;
      b.slot = slot;
      built.set(m.id, b);
      let w = W[b.depth] ?? W.stub;
      let h = heights[m.id] ?? estimateHeight(b);
      if (b.group && !b.group.folded) {
        const sub = subtree(m, slot, lane);
        subs.set(m.id, sub);
        w = sub.w;
        h = sub.h;
        b.group.w = w;
        b.group.h = h;
      }
      cells.push({ id: m.id, col: g.id, w, h, fixed: null });
    }
    const inner: Link[] = [];
    const seen = new Set<string>();
    for (const e of arch.edges) {
      const a = under(g.id, e.src), b = under(g.id, e.dst);
      if (!a || !b || a === b || seen.has(a + ">" + b)) continue;
      seen.add(a + ">" + b);
      inner.push({ from: a, to: b });
    }
    const laid = layoutBoard(cells, inner, [g.id], { x: 0, y: 0 }, () => 0);
    const ext = laid.cols.get(g.id) ?? { x: 0, y: 0, w: W.sketch, h: 0 };
    return {
      w: ext.w,
      h: HEAD_GROUP + ext.h,
      place: (x0, y0, self) => {
        let x1 = x0 + ext.w, y1 = y0 + HEAD_GROUP + ext.h;
        let gx0 = x0, gy0 = y0;
        for (const cell of cells) {
          const b = built.get(cell.id)!;
          const m = arch.nodes[cell.id];
          const spot = laid.at.get(cell.id);
          if (m.x !== null && m.y !== null) {
            b.cx = m.x;
            b.y = m.y;
          } else if (spot) {
            b.cx = x0 + (spot.cx - ext.x);
            b.y = y0 + HEAD_GROUP + (spot.y - ext.y);
          }
          nodes.push(b);
          subs.get(cell.id)?.place(b.cx - cell.w / 2, b.y, b);
          // the ground reaches whatever stands on it
          const w = b.group && !b.group.folded ? b.group.w : cell.w;
          const h = b.group && !b.group.folded ? b.group.h : cell.h;
          gx0 = Math.min(gx0, b.cx - w / 2 - PAD);
          gy0 = Math.min(gy0, b.y - HEAD_GROUP - PAD);
          x1 = Math.max(x1, b.cx + w / 2 + PAD);
          y1 = Math.max(y1, b.y + h + PAD);
        }
        if (self && self.group) {
          self.group.w = x1 - gx0;
          self.group.h = y1 - gy0;
          self.cx = gx0 + self.group.w / 2;
          self.y = gy0;
        }
      },
    };
  };

  /** Which column each box ended up in. */
  const home = new Map<string, string>();
  for (const c of cols) for (const n of c.nodes) home.set(n.id, c.key);
  const chromeOn = new Map(cols.map((c) => [c.key, c.chrome ? (c.out ? HEAD_OUT : HEAD) : 0]));
  const head = (col: string) => chromeOn.get(col) ?? 0;

  /* Down the page is decided at the top level: an edge between two members
     of different containers is, up here, an edge between the containers. */
  const links: Link[] = [];
  {
    const seen = new Set<string>();
    for (const e of arch.edges) {
      const a = topOf(e.src), b = topOf(e.dst);
      if (a === b || !home.has(a) || !home.has(b) || seen.has(a + ">" + b)) continue;
      seen.add(a + ">" + b);
      links.push({ from: a, to: b });
    }
  }

  /** One arrangement over a set of columns. The live board is arranged first;
   *  whatever lost is arranged again underneath it, on its own row, because a
   *  rejected approach is still part of the record and is not part of the
   *  design's flow. */
  const arrange = (group: Column[], top: number): number => {
    if (!group.length) return top;
    const keys = group.map((c) => c.key);
    const inGroup = new Set(keys);
    const built = new Map<string, BoardNode>();
    const subs = new Map<string, Sub>();
    const cells: Cell[] = [];
    for (const c of group) {
      for (const n of c.nodes) {
        const b = read(n);
        b.lane = c.key;
        b.slot = c.slot;
        built.set(n.id, b);
        let w = W[b.depth] ?? W.stub;
        let h = heights[n.id] ?? estimateHeight(b);
        if (b.group && !b.group.folded) {
          const sub = subtree(n, c.slot, c.key);
          subs.set(n.id, sub);
          w = sub.w;
          h = sub.h;
          b.group.w = w;
          b.group.h = h;
        }
        cells.push({
          id: n.id,
          col: c.key,
          w,
          h,
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
      // the container goes down before its members, so it is drawn under them
      nodes.push(b);
      subs.get(cell.id)?.place(b.cx - cell.w / 2, b.y, b);
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

  /* A wire runs between what is drawn. Edges whose ends both fold into the
     same container are its internals and are not drawn at all; several edges
     that fold onto the same two boxes become one wire that says how many it
     stands for, rather than a sheaf of parallel curves. */
  const bundle = new Map<string, Wire>();
  const wires: Wire[] = [];
  for (const e of arch.edges) {
    const from = shown(e.src), to = shown(e.dst);
    if (from === to) continue;
    const out = isGreyed(arch, arch.nodes[e.src]) || isGreyed(arch, arch.nodes[e.dst]);
    const key = from + ">" + to;
    const had = bundle.get(key);
    if (had) {
      had.count = (had.count ?? 1) + 1;
      had.label = undefined;
      had.out = had.out && out;
      continue;
    }
    const w: Wire = { from, to, label: e.label || undefined, kind: e.kind, notes: e.notes, out, count: 1 };
    bundle.set(key, w);
    wires.push(w);
  }

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
