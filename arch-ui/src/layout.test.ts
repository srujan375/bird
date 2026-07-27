/**
 * Layout's one promise: a node the user placed is never moved.
 *
 * Everything else here is prettiness. This is the rule that makes an agent
 * mutating the graph underneath you tolerable, so it is the rule with tests.
 */
import { describe, expect, it } from "vitest";
import { DESIGN_SIZE, edgeKey, findBackEdges, layout, relayoutAll, type GraphEdge } from "./layout";
import type { XY } from "./store/canvas";

const key = (id: string) => `design:${id}`;
const chain = (...ids: string[]): GraphEdge[] =>
  ids.slice(1).map((dst, i) => ({ src: ids[i], dst }));

describe("layout", () => {
  it("returns positions only for ids it has not seen", () => {
    const first = layout(["a", "b"], chain("a", "b"), key, {});
    expect(Object.keys(first).sort()).toEqual(["design:a", "design:b"]);

    const second = layout(["a", "b", "c"], chain("a", "b", "c"), key, first);
    expect(Object.keys(second)).toEqual(["design:c"]);
  });

  it("never moves an existing node, however the graph grows", () => {
    const placed: Record<string, XY> = {
      "design:a": { x: 17, y: 42 },
      "design:b": { x: 900, y: -300 },
    };
    const before = structuredClone(placed);

    const added = layout(
      ["a", "b", "c", "d", "e"],
      chain("a", "b", "c", "d", "e"),
      key,
      placed,
    );

    expect(placed).toEqual(before);           // the input is not mutated
    expect(added).not.toHaveProperty("design:a");
    expect(added).not.toHaveProperty("design:b");
    expect(Object.keys(added).sort()).toEqual(["design:c", "design:d", "design:e"]);
  });

  it("slides a new node clear of the ones already on the canvas", () => {
    // every hand-placed card sits exactly where the layout would want to put a
    // new one, so the new one has to give way rather than land on top
    const occupied: Record<string, XY> = {};
    for (let i = 0; i < 4; i++) occupied[`design:old${i}`] = { x: 0, y: i * 60 };

    const added = layout(["fresh"], [], key, occupied);
    const pos = added["design:fresh"];
    for (const taken of Object.values(occupied)) {
      const clear =
        Math.abs(pos.x - taken.x) >= DESIGN_SIZE.w + 24 ||
        Math.abs(pos.y - taken.y) >= DESIGN_SIZE.h + 18;
      expect(clear).toBe(true);
    }
  });

  it("is idempotent for the same graph", () => {
    const edges = chain("a", "b", "c");
    expect(layout(["a", "b", "c"], edges, key, {}))
      .toEqual(layout(["a", "b", "c"], edges, key, {}));
  });

  it("layers along the edges, left to right", () => {
    const p = layout(["a", "b", "c"], chain("a", "b", "c"), key, {});
    expect(p["design:a"].x).toBeLessThan(p["design:b"].x);
    expect(p["design:b"].x).toBeLessThan(p["design:c"].x);
  });

  it("survives a cycle", () => {
    const cyclic = [...chain("a", "b", "c"), { src: "c", dst: "a" }];
    const p = layout(["a", "b", "c"], cyclic, key, {});
    expect(Object.keys(p)).toHaveLength(3);
    expect(Object.values(p).every((xy) => Number.isFinite(xy.x) && Number.isFinite(xy.y)))
      .toBe(true);
  });

  it("ignores edges to ids that are not in the graph", () => {
    const p = layout(["a"], [{ src: "a", dst: "ghost" }], key, {});
    expect(Object.keys(p)).toEqual(["design:a"]);
  });

  it("lays a ring out as a line, not around the houses", () => {
    // the real shape that broke it: a pipeline whose last step reports back to
    // its entry point. Layering along that one edge dragged the entry point to
    // the far right and drew every forward edge backwards.
    const ids = ["tool", "scoping", "inference", "facets", "mapping", "population", "gap"];
    const edges = [...chain(...ids), { src: "gap", dst: "tool" }];

    const back = findBackEdges(ids, edges);
    expect([...back]).toEqual([edgeKey("gap", "tool")]);

    const p = layout(ids, edges, key, {});
    const xs = ids.map((id) => p[key(id)].x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b)); // strictly left to right
    expect(new Set(xs).size).toBe(ids.length);         // one node per column
  });

  it("calls a self-loop a back edge, and still places the node", () => {
    const back = findBackEdges(["a", "b"], [{ src: "a", dst: "a" }, { src: "a", dst: "b" }]);
    expect([...back]).toEqual([edgeKey("a", "a")]);

    const p = layout(["a", "b"], [{ src: "a", dst: "a" }, { src: "a", dst: "b" }], key, {});
    expect(p[key("a")].x).toBeLessThan(p[key("b")].x);
  });

  it("finds no back edges in a graph that has none", () => {
    // a diamond: two paths to the same node is not a cycle
    const edges = [
      { src: "a", dst: "b" },
      { src: "a", dst: "c" },
      { src: "b", dst: "d" },
      { src: "c", dst: "d" },
    ];
    expect(findBackEdges(["a", "b", "c", "d"], edges).size).toBe(0);

    const p = layout(["a", "b", "c", "d"], edges, key, {});
    expect(p[key("b")].x).toBe(p[key("c")].x);          // same layer
    expect(p[key("b")].y).not.toBe(p[key("c")].y);      // but not on top of each other
    expect(p[key("d")].x).toBeGreaterThan(p[key("b")].x);
  });

  it("ignores back edges that point outside the graph", () => {
    expect(findBackEdges(["a"], [{ src: "a", dst: "ghost" }]).size).toBe(0);
  });

  it("Tidy up re-places everything, pins included", () => {
    const pinned = { "design:a": { x: 999, y: 999 } };
    const fresh = relayoutAll(["a", "b"], chain("a", "b"), key);
    expect(Object.keys(fresh).sort()).toEqual(["design:a", "design:b"]);
    expect(fresh["design:a"]).not.toEqual(pinned["design:a"]);
  });
});
