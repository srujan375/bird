import { describe, expect, it } from "vitest";
import { toBoard, isGreyed } from "./adapter";
import type { ArchState, WireNode } from "../wire/types";

/**
 * The adapter is where the harness's graph becomes a board, and the columns
 * are the claim: approaches side by side, the parts they share drawn once in
 * the middle, whatever lost still visible underneath with its reason.
 */

const node = (id: string, over: Partial<WireNode> = {}): WireNode => ({
  id, label: id, kind: "service", responsibility: "", tech: "", depth: "stub",
  detail: "", facts: {}, items: [], approaches: [], status: "active", notes: "", existing: false, parent: "",
  x: null, y: null, ...over,
});

function state(over: Partial<ArchState> = {}): ArchState {
  return {
    brief: { goal: "", actors: [], scale: "", constraints: [], non_goals: [] },
    nodes: {}, edges: [], approaches: {}, decisions: [], questions: [],
    annotations: [], handed_off: false, ...over,
  };
}

const fork = () => state({
  approaches: {
    a: { id: "a", name: "Pool", summary: "a pool you run", status: "active", rejected_reason: "" },
    b: { id: "b", name: "Functions", summary: "one per chunk", status: "active", rejected_reason: "" },
  },
  nodes: {
    db: node("db", { kind: "store" }),
    worker: node("worker", { approaches: ["a"] }),
    fn: node("fn", { approaches: ["b"] }),
  },
});

describe("columns", () => {
  it("puts the shared spine between the approaches", () => {
    const { lanes } = toBoard(fork());
    expect(lanes.map((l) => l.slot)).toEqual(["1", "s", "2"]);
    expect(lanes[1].name).toBe("Shared");
    // and it is physically in the middle
    expect(lanes[0].x).toBeLessThan(lanes[1].x);
    expect(lanes[1].x).toBeLessThan(lanes[2].x);
  });

  it("draws a box both takes use once, in the middle", () => {
    const { nodes } = toBoard(fork());
    const db = nodes.filter((n) => n.id === "db");
    expect(db).toHaveLength(1);
    expect(db[0].lane).toBe(" shared");
  });

  it("gives each box its column's tint", () => {
    const { nodes } = toBoard(fork());
    expect(nodes.find((n) => n.id === "worker")!.slot).toBe("1");
    expect(nodes.find((n) => n.id === "fn")!.slot).toBe("2");
  });

  /* Territory only works if no two columns wear the same tint. The tints used
     to be a two-slot alternation, so a third approach silently reused the
     first one's and the board read as a fork when it was a three-way. */
  it("gives a third approach a tint of its own", () => {
    const s = fork();
    s.approaches.c = { id: "c", name: "Batch", summary: "overnight", status: "active", rejected_reason: "" };
    s.nodes.job = node("job", { approaches: ["c"] });
    const slots = toBoard(s).lanes.filter((l) => l.k !== " shared").map((l) => l.slot);
    expect(new Set(slots).size).toBe(slots.length);
  });

  it("does not let a rejected approach wear a live one's tint", () => {
    const s = fork();
    s.approaches.c = { id: "c", name: "Batch", summary: "", status: "greyed", rejected_reason: "too slow" };
    s.nodes.job = node("job", { approaches: ["c"] });
    const slots = toBoard(s).lanes.filter((l) => l.k !== " shared").map((l) => l.slot);
    expect(new Set(slots).size).toBe(slots.length);
  });

  /* An approach keeps its tint when the board around it changes — losing is a
     status change, not a repaint of everything that stayed. */
  it("keeps an approach's tint when a rival is rejected", () => {
    const before = toBoard(fork()).lanes.find((l) => l.k === "b")!.slot;
    const s = fork();
    s.approaches.a = { ...s.approaches.a, status: "greyed", rejected_reason: "costly" };
    expect(toBoard(s).lanes.find((l) => l.k === "b")!.slot).toBe(before);
  });

  /* Dragging one box used to hand its row to the box below it, so moving one
     thing silently re-stacked everything under it. */
  it("does not re-stack a column when one box is moved out of it", () => {
    const s = state({ nodes: { a: node("a"), b: node("b"), c: node("c") } });
    const before = toBoard(s).nodes;
    const was = (id: string, list = before) => list.find((n) => n.id === id)!.y;
    const [yb, yc] = [was("b"), was("c")];

    s.nodes.a = node("a", { x: 900, y: 900 });  // the user drags the top box away
    const after = toBoard(s).nodes;

    expect(was("b", after)).toBe(yb);
    expect(was("c", after)).toBe(yc);
    expect(was("a", after)).toBe(900);
  });

  /* Two boxes wired to nothing are not a sequence, so they are not stacked as
     one — they share a shelf. What has to hold is that they do not land on top
     of each other. */
  it("gives boxes nobody has moved room of their own", () => {
    const { nodes } = toBoard(state({ nodes: { a: node("a"), b: node("b") } }));
    const [a, b] = ["a", "b"].map((id) => nodes.find((n) => n.id === id)!);
    expect(a.cx === b.cx && a.y === b.y).toBe(false);
  });

  it("has no shared lane to draw when there is no fork", () => {
    const s = state({ nodes: { api: node("api") } });
    expect(toBoard(s).lanes).toHaveLength(0);
  });
});

describe("what lost", () => {
  const settled = () => {
    const s = fork();
    s.approaches.b = { ...s.approaches.b, status: "greyed", rejected_reason: "cold starts" };
    return s;
  };

  it("keeps the losing column on the board, underneath, with its reason", () => {
    const { lanes } = toBoard(settled());
    const lost = lanes.find((l) => l.out)!;
    expect(lost.note).toBe("cold starts");
    expect(lost.y).toBeGreaterThan(lanes.find((l) => l.k === "a")!.y);
  });

  it("marks the survivor taken — but only once something has actually lost", () => {
    expect(toBoard(fork()).lanes.some((l) => l.taken)).toBe(false);
    expect(toBoard(settled()).lanes.find((l) => l.k === "a")!.taken).toBe(true);
  });

  it("greys the losing column's boxes and the wires touching them", () => {
    const s = settled();
    s.edges = [
      { src: "worker", dst: "db", label: "", kind: "sync", notes: "" },
      { src: "fn", dst: "db", label: "", kind: "sync", notes: "" },
    ];
    const { nodes, wires } = toBoard(s);
    expect(nodes.find((n) => n.id === "fn")!.out).toBe(true);
    expect(nodes.find((n) => n.id === "worker")!.out).toBeFalsy();
    expect(nodes.find((n) => n.id === "db")!.out).toBeFalsy();
    expect(wires.find((w) => w.from === "fn")!.out).toBe(true);
    expect(wires.find((w) => w.from === "worker")!.out).toBe(false);
  });

  it("keeps a box alive while one of its approaches survives", () => {
    const s = settled();
    s.nodes.queue = node("queue", { approaches: ["a", "b"] });
    expect(isGreyed(s, s.nodes.queue)).toBe(false);
    // it belongs to the survivor now, so that is where it is drawn
    expect(toBoard(s).nodes.find((n) => n.id === "queue")!.lane).toBe("a");
  });

  it("greys a box only once every approach it belonged to has lost", () => {
    const s = settled();
    s.approaches.a = { ...s.approaches.a, status: "greyed", rejected_reason: "rent" };
    s.nodes.queue = node("queue", { approaches: ["a", "b"] });
    expect(isGreyed(s, s.nodes.queue)).toBe(true);
  });
});

describe("arrangement", () => {
  it("lays out a board nobody has arranged", () => {
    const { nodes } = toBoard(fork());
    for (const n of nodes) {
      expect(Number.isFinite(n.cx)).toBe(true);
      expect(Number.isFinite(n.y)).toBe(true);
    }
    // the two boxes in the shared column do not sit on top of each other
    const shared = toBoard(state({
      nodes: { a: node("a"), b: node("b") },
      approaches: {
        x: { id: "x", name: "X", summary: "", status: "active", rejected_reason: "" },
        y: { id: "y", name: "Y", summary: "", status: "active", rejected_reason: "" },
      },
    })).nodes;
    expect(shared[0].cx === shared[1].cx && shared[0].y === shared[1].y).toBe(false);
  });

  it("lets a hand-placed box override the layout", () => {
    const s = fork();
    s.nodes.db = { ...s.nodes.db, x: 1234, y: 99 };
    const db = toBoard(s).nodes.find((n) => n.id === "db")!;
    expect([db.cx, db.y]).toEqual([1234, 99]);
  });
});

describe("detail and notes", () => {
  it("turns the harness's prose detail into the board's mono lines", () => {
    const s = state({ nodes: { db: node("db", { detail: "episodes(id)\n\nsegments(t0, t1)" }) } });
    expect(toBoard(s).nodes[0].rows).toEqual(["episodes(id)", "segments(t0, t1)"]);
  });

  it("sits a pinned note beside what it is about", () => {
    const s = fork();
    s.annotations = [{ id: "n1", text: "the bill", x: 0, y: 0, w: 190, anchor: "db" }];
    const db = toBoard(s).nodes.find((n) => n.id === "db")!;
    const note = toBoard(s).annos[0];
    expect(note.x).toBeGreaterThan(db.cx);
    expect(note.y).toBeCloseTo(db.y + 4);
  });

  it("leaves a note that has been dragged where it was put", () => {
    const s = fork();
    s.annotations = [{ id: "n1", text: "moved", x: 40, y: 900, w: 190, anchor: "db" }];
    expect(toBoard(s).annos[0]).toMatchObject({ x: 40, y: 900 });
  });
});

/**
 * Containers: a box with members is drawn around them when open, as one card
 * when folded, and the wires fold with it — so a big board reads as a handful
 * of subsystems before it reads as sixty boxes.
 */
const nested = () => state({
  nodes: {
    ingest: node("ingest", { kind: "group" }),
    parser: node("parser", { parent: "ingest" }),
    queue: node("queue", { kind: "queue", parent: "ingest" }),
    api: node("api", { kind: "api" }),
    db: node("db", { kind: "store" }),
  },
  edges: [
    { src: "api", dst: "parser", label: "raw", kind: "sync", notes: "" },
    { src: "parser", dst: "queue", label: "", kind: "async", notes: "" },
    { src: "queue", dst: "db", label: "rows", kind: "async", notes: "" },
    { src: "parser", dst: "db", label: "meta", kind: "sync", notes: "" },
  ],
});

describe("containers", () => {
  it("lays members out inside an open container", () => {
    const { nodes } = toBoard(nested(), {}, { ingest: false });
    const g = nodes.find((n) => n.id === "ingest")!;
    expect(g.group).toMatchObject({ folded: false, count: 2 });
    expect(g.group!.w).toBeGreaterThan(0);
    const gx0 = g.cx - g.group!.w / 2, gx1 = g.cx + g.group!.w / 2;
    for (const id of ["parser", "queue"]) {
      const m = nodes.find((n) => n.id === id)!;
      expect(m.parent).toBe("ingest");
      expect(m.cx).toBeGreaterThan(gx0);
      expect(m.cx).toBeLessThan(gx1);
      expect(m.y).toBeGreaterThan(g.y);
      expect(m.y).toBeLessThan(g.y + g.group!.h);
    }
    // the container is drawn before its members, so it sits under them
    expect(nodes.findIndex((n) => n.id === "ingest")).toBeLessThan(nodes.findIndex((n) => n.id === "parser"));
  });

  it("stacks members along their own wires", () => {
    const { nodes } = toBoard(nested(), {}, { ingest: false });
    const p = nodes.find((n) => n.id === "parser")!;
    const q = nodes.find((n) => n.id === "queue")!;
    expect(q.y).toBeGreaterThan(p.y);
  });

  it("folds members away and lands their wires on the container", () => {
    const { nodes, wires } = toBoard(nested(), {}, { ingest: true });
    expect(nodes.map((n) => n.id).sort()).toEqual(["api", "db", "ingest"]);
    expect(nodes.find((n) => n.id === "ingest")!.group!.folded).toBe(true);
    const keys = wires.map((w) => `${w.from}>${w.to}`).sort();
    // parser->queue is internal and gone; queue->db and parser->db bundle
    expect(keys).toEqual(["api>ingest", "ingest>db"]);
    const bundled = wires.find((w) => w.from === "ingest")!;
    expect(bundled.count).toBe(2);
    expect(bundled.label).toBeUndefined();
    expect(wires.find((w) => w.to === "ingest")!.label).toBe("raw");
  });

  it("opens small boards and folds big ones by default", () => {
    expect(toBoard(nested()).nodes.find((n) => n.id === "ingest")!.group!.folded).toBe(false);
    const big = nested();
    for (let i = 0; i < 30; i++) big.nodes["x" + i] = node("x" + i);
    expect(toBoard(big).nodes.find((n) => n.id === "ingest")!.group!.folded).toBe(true);
  });

  it("puts a member in its container's column", () => {
    const s = nested();
    s.approaches.a = { id: "a", name: "A", summary: "", status: "active", rejected_reason: "" };
    s.nodes.ingest.approaches = ["a"];
    const { nodes } = toBoard(s, {}, { ingest: false });
    expect(nodes.find((n) => n.id === "parser")!.lane).toBe("a");
  });

  it("nests containers inside containers", () => {
    const s = nested();
    s.nodes.parse2 = node("parse2", { kind: "group", parent: "ingest" });
    s.nodes.lexer = node("lexer", { parent: "parse2" });
    const { nodes } = toBoard(s, {}, { ingest: false, parse2: false });
    const outer = nodes.find((n) => n.id === "ingest")!;
    const inner = nodes.find((n) => n.id === "parse2")!;
    const leaf = nodes.find((n) => n.id === "lexer")!;
    expect(inner.group!.folded).toBe(false);
    expect(leaf.y).toBeGreaterThan(inner.y);
    expect(inner.y).toBeGreaterThan(outer.y);
    expect(outer.group!.h).toBeGreaterThan(inner.group!.h);
  });
});

describe("dragging inside a container", () => {
  it("keeps a hand-placed member where it was put and stretches the ground to it", () => {
    const s = nested();
    s.nodes.queue.x = 2000;
    s.nodes.queue.y = 40;
    const { nodes } = toBoard(s, {}, { ingest: false });
    const q = nodes.find((n) => n.id === "queue")!;
    const g = nodes.find((n) => n.id === "ingest")!;
    expect(q.cx).toBe(2000);
    expect(q.y).toBe(40);
    expect(g.cx + g.group!.w / 2).toBeGreaterThan(2000);
    expect(g.y).toBeLessThanOrEqual(40);
  });
});

describe("what a box carries", () => {
  it("lists the kind's facts in vocabulary order and names its list", () => {
    const s = state({ nodes: { db: node("db", { kind: "store", facts: { retention: "90d", engine: "pg" } }) } });
    const b = toBoard(s).nodes[0];
    expect(b.facts.map(([k]) => k)).toEqual(["engine", "model", "durability", "retention"]);
    expect(b.facts.find(([k]) => k === "engine")![1]).toBe("pg");
    expect(b.listName).toBe("entities");
  });

  it("reads who is on each side of the wires off the edges", () => {
    const s = nested();
    const q = toBoard(s, {}, { ingest: false }).nodes.find((n) => n.id === "queue")!;
    expect(q.derived).toEqual([
      { side: "producers", names: ["parser"] },
      { side: "consumers", names: ["db"] },
    ]);
  });
});
