import { describe, expect, it } from "vitest";
import { estimateHeight, layoutBoard, type Cell, type Link } from "./layout";
import type { BoardNode } from "./types";

/**
 * The arrangement's claims, each one a thing that used to be wrong.
 *
 * The old layout gave every box a 200px row in dictionary order. So a stub sat
 * in the middle of 160px of nothing, a chain of five could be dealt out in any
 * order at all, and the wires went the long way round to find each other. These
 * are the rules that replaced it.
 */

const cell = (id: string, over: Partial<Cell> = {}): Cell =>
  ({ id, col: "s", w: 176, h: 40, fixed: null, ...over });

const chain = (...ids: string[]): Link[] =>
  ids.slice(1).map((to, i) => ({ from: ids[i], to }));

const noHead = () => 0;
const lay = (boxes: Cell[], links: Link[] = [], cols = ["s"]) =>
  layoutBoard(boxes, links, cols, { x: 0, y: 0 }, noHead);

const yOf = (r: ReturnType<typeof lay>, id: string) => r.at.get(id)!.y;
const xOf = (r: ReturnType<typeof lay>, id: string) => r.at.get(id)!.cx;

describe("down the page is the flow", () => {
  it("puts a wire's source above its target", () => {
    const r = lay([cell("a"), cell("b"), cell("c")], chain("a", "b", "c"));
    expect(yOf(r, "a")).toBeLessThan(yOf(r, "b"));
    expect(yOf(r, "b")).toBeLessThan(yOf(r, "c"));
  });

  /* The graph decides the order, not the order the boxes arrived in. This is
     the whole reason a five-box chain used to be dealt out as rows 13, 10, 12,
     6, 7 with its wires crossing the board to get anywhere. */
  it("reads the same however the boxes are handed to it", () => {
    const ids = ["a", "b", "c", "d"];
    const links = chain(...ids);
    const forwards = lay(ids.map((id) => cell(id)), links);
    const backwards = lay([...ids].reverse().map((id) => cell(id)), links);
    for (const id of ids) expect(yOf(backwards, id)).toBe(yOf(forwards, id));
  });

  /* A design can describe a cycle. Layering needs a direction, so one edge of
     it stops counting — but the layout still has to finish, and every box in
     the cycle still has to land somewhere. */
  it("survives a cycle", () => {
    const r = lay(
      [cell("a"), cell("b"), cell("c")],
      [...chain("a", "b", "c"), { from: "c", to: "a" }],
    );
    for (const id of ["a", "b", "c"]) expect(Number.isFinite(yOf(r, id))).toBe(true);
    expect(yOf(r, "a")).toBeLessThan(yOf(r, "c"));
  });

  it("does not put a box below itself", () => {
    const r = lay([cell("a")], [{ from: "a", to: "a" }]);
    expect(Number.isFinite(yOf(r, "a"))).toBe(true);
  });

  /* Every box that nothing feeds used to land in layer 0, so a board derived
     from a codebase opened with twenty boxes across the top and a long diagonal
     underneath. A source is pulled down towards what it feeds. */
  it("does not pile every source into the top row", () => {
    const boxes = ["hub", "s1", "s2", "s3", "deep1", "deep2", "deep3"].map((id) => cell(id));
    const links = [
      ...chain("s1", "deep1", "deep2", "deep3", "hub"),
      { from: "s2", to: "hub" },
      { from: "s3", to: "hub" },
    ];
    const r = lay(boxes, links);
    const top = yOf(r, "s1");
    // s2 and s3 feed the very bottom and have no business at the very top
    expect(yOf(r, "s2")).toBeGreaterThan(top);
    expect(yOf(r, "s3")).toBeGreaterThan(top);
    expect(yOf(r, "s2")).toBeLessThan(yOf(r, "hub"));
  });
});

describe("rows are sized to what is in the boxes", () => {
  /* The row height was a constant, and a rendered box is anywhere from 39px to
     171px. A stub therefore sat in the middle of 160px of nothing. */
  it("leaves the same gap under a short box as a tall one", () => {
    const short = lay([cell("a", { h: 39 }), cell("b")], chain("a", "b"));
    const tall = lay([cell("a", { h: 171 }), cell("b")], chain("a", "b"));
    expect(yOf(short, "b") - 39).toBe(yOf(tall, "b") - 171);
  });

  it("clears the tallest box in a row", () => {
    const r = lay(
      [cell("a"), cell("tall", { h: 171 }), cell("short", { h: 39 }), cell("z")],
      [{ from: "a", to: "tall" }, { from: "a", to: "short" },
       { from: "tall", to: "z" }, { from: "short", to: "z" }],
    );
    expect(yOf(r, "z")).toBeGreaterThanOrEqual(yOf(r, "tall") + 171);
  });
});

describe("boxes wired to nothing", () => {
  /* Eight lone stubs stacked one per row is what turned a real board into a
     ribbon eight screens tall. They are the pieces the design touches, not
     steps in it, so they pack. */
  it("packs them side by side instead of stacking them", () => {
    const loose = ["p", "q", "r"].map((id) => cell(id));
    const r = lay(loose);
    expect(new Set(loose.map((b) => yOf(r, b.id))).size).toBe(1);
    expect(new Set(loose.map((b) => xOf(r, b.id))).size).toBe(3);
  });

  it("keeps them clear of the design they hang off", () => {
    const r = lay([cell("a"), cell("b"), cell("loose")], chain("a", "b"));
    expect(yOf(r, "loose")).toBeGreaterThan(yOf(r, "b"));
  });

  it("wraps a shelf rather than running off the side", () => {
    const many = Array.from({ length: 8 }, (_, i) => cell("n" + i));
    const r = lay(many);
    expect(new Set(many.map((b) => yOf(r, b.id))).size).toBeGreaterThan(1);
  });
});

describe("a box somebody moved", () => {
  /* Skipping a moved box's slot would close the gap it left, so dragging one
     thing would move two others. Where a box sits is the one thing on the board
     a person chose directly. */
  it("leaves everything else exactly where it was", () => {
    const ids = ["a", "b", "c"];
    const links = chain("a", "b", "c");
    const before = lay(ids.map((id) => cell(id)), links);
    const after = lay(
      ids.map((id) => cell(id, id === "a" ? { fixed: { cx: 900, y: 900 } } : {})),
      links,
    );
    for (const id of ["b", "c"]) {
      expect(xOf(after, id)).toBe(xOf(before, id));
      expect(yOf(after, id)).toBe(yOf(before, id));
    }
  });

  /* A lane is the ground its boxes stand on. Stretching it to reach one that
     has been carried across the board draws a rectangle the size of the board
     with two boxes in it and everything else underneath. */
  it("does not drag its lane across the board with it", () => {
    const near = lay([cell("a"), cell("b", { fixed: { cx: 90, y: 120 } })], chain("a", "b"));
    const far = lay([cell("a"), cell("b", { fixed: { cx: 90, y: 4000 } })], chain("a", "b"));
    expect(far.cols.get("s")!.h).toBeLessThan(near.cols.get("s")!.h);
  });

  it("takes its lane with it when it is the only box in the column", () => {
    const r = lay([cell("only", { fixed: { cx: 700, y: 800 } })]);
    const e = r.cols.get("s")!;
    expect(e.y).toBeLessThanOrEqual(800);
    expect(e.y + e.h).toBeGreaterThanOrEqual(840);
  });
});

describe("columns", () => {
  it("keeps a box inside its own column", () => {
    const r = lay(
      [cell("l", { col: "left" }), cell("m"), cell("rr", { col: "right" })],
      [],
      ["left", "s", "right"],
    );
    expect(xOf(r, "l")).toBeLessThan(xOf(r, "m"));
    expect(xOf(r, "m")).toBeLessThan(xOf(r, "rr"));
  });

  /* The columns are approaches; the layers are the flow. A chain that steps out
     into an approach and back still has to read downwards. */
  it("layers across columns, not within them", () => {
    const r = lay(
      [cell("a"), cell("mid", { col: "x" }), cell("b")],
      chain("a", "mid", "b"),
      ["s", "x"],
    );
    expect(yOf(r, "a")).toBeLessThan(yOf(r, "mid"));
    expect(yOf(r, "mid")).toBeLessThan(yOf(r, "b"));
  });

  it("gives a column with nothing in it no rectangle at all", () => {
    const r = lay([cell("a")], [], ["s", "empty"]);
    expect(r.cols.get("empty")!.h).toBe(0);
    expect(r.cols.get("s")!.h).toBeGreaterThan(0);
  });
});

describe("estimated heights", () => {
  const box = (over: Partial<BoardNode> = {}): BoardNode => ({
    id: "n", lane: "", slot: "", cx: 0, y: 0, kind: "service", depth: "stub",
    label: "n", resp: "", tech: "", rows: [], approaches: [], existing: false,
    ...over,
  });

  /* Only ever used for the frame before the DOM has reported a real number, so
     it has to be in the right neighbourhood, not exact. The figures come from
     measuring the rendered board. */
  it("lands near what the board actually renders", () => {
    expect(estimateHeight(box({ label: "permissions" }))).toBeGreaterThan(30);
    expect(estimateHeight(box({ label: "permissions" }))).toBeLessThan(50);
    const detailed = box({
      depth: "detailed",
      label: "MCP config discovery",
      kind: "store",
      resp: "find and merge MCP server definitions across the skills-style chain",
      tech: "JSON, same precedence semantics as skills.py",
      rows: ["Precedence, first-wins on server-name collisions"],
    });
    expect(estimateHeight(detailed)).toBeGreaterThan(120);
    expect(estimateHeight(detailed)).toBeLessThan(190);
  });

  it("grows with the prose rather than the depth alone", () => {
    const short = box({ depth: "sketch", resp: "one line" });
    const long = box({ depth: "sketch", resp: "one line ".repeat(20) });
    expect(estimateHeight(long)).toBeGreaterThan(estimateHeight(short));
  });
});
