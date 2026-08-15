/**
 * Edge routing: where a line goes when it has to cross a column it is not in.
 *
 * The load-bearing test is the third one. A layered graph puts everything on a
 * row, so an edge that skips a layer runs along that row — straight through
 * whatever is parked in between, which reads as three connections where there
 * is one and hides whatever text it crosses. The layout answers that by
 * reserving the edge a lane in every column it crosses; these tests pin that
 * the lane exists, and that nothing is ever placed in it.
 */
import { describe, expect, it } from "vitest";
import { DESIGN_SIZE, layout, relayoutAll, routes, type GraphEdge } from "./layout";

const key = (id: string) => `design:${id}`;
const chain = (...ids: string[]): GraphEdge[] =>
  ids.slice(1).map((dst, i) => ({ src: ids[i], dst }));

describe("routes", () => {
  it("gives an edge between neighbours no lane — it has nothing to cross", () => {
    expect(routes(["a", "b"], chain("a", "b")).size).toBe(0);
  });

  it("gives an edge one waypoint per column it crosses", () => {
    const ids = ["a", "b", "c", "d"];
    const edges = [...chain("a", "b", "c", "d"), { src: "a", dst: "d" }];
    const lane = routes(ids, edges).get("a->d");

    expect(lane).toBeDefined();
    expect(lane!).toHaveLength(2); // b's column and c's
    expect(lane![0].x).toBeLessThan(lane![1].x); // left to right, in order
  });

  it("keeps the lane clear of every card in the columns it crosses", () => {
    const ids = ["a", "b", "c", "d"];
    const edges = [...chain("a", "b", "c", "d"), { src: "a", dst: "d" }];
    const placed = relayoutAll(ids, edges, key);
    const lane = routes(ids, edges).get("a->d")!;

    for (const point of lane) {
      for (const card of Object.values(placed)) {
        const inside =
          point.x > card.x && point.x < card.x + DESIGN_SIZE.w &&
          point.y > card.y && point.y < card.y + DESIGN_SIZE.h;
        expect(inside).toBe(false);
      }
    }
  });

  it("leaves a back edge alone — it loops under the graph, not through it", () => {
    // the ring: a pipeline whose last step reports back to its entry point
    const ids = ["a", "b", "c", "d"];
    const edges = [...chain("a", "b", "c", "d"), { src: "d", dst: "a" }];
    expect(routes(ids, edges).has("d->a")).toBe(false);
  });

  it("ignores edges to ids that are not in the graph", () => {
    expect(routes(["a"], [{ src: "a", dst: "ghost" }]).size).toBe(0);
  });

  it("is idempotent for the same graph", () => {
    const ids = ["a", "b", "c", "d"];
    const edges = [...chain("a", "b", "c", "d"), { src: "a", dst: "d" }];
    expect(routes(ids, edges)).toEqual(routes(ids, edges));
  });

  it("runs a long edge flat rather than sending it up and back down", () => {
    // the ordering has to put the lane where the edge is going, not wherever
    // there was room. A lane above its source and below its target is a line
    // that climbs, doubles back, and reads as two connections.
    const ids = ["a", "b", "c", "d", "e"];
    const edges = [
      ...chain("a", "b", "c", "d"),
      { src: "a", dst: "d" },
      { src: "e", dst: "b" },
    ];
    const placed = relayoutAll(ids, edges, key);
    const lane = routes(ids, edges).get("a->d")!;
    const from = placed[key("a")].y + DESIGN_SIZE.h / 2;
    const to = placed[key("d")].y + DESIGN_SIZE.h / 2;

    // total vertical travel, against the climb the edge actually owes
    const ys = [from, ...lane.map((p) => p.y), to];
    const travelled = ys.slice(1).reduce((sum, y, i) => sum + Math.abs(y - ys[i]), 0);
    expect(travelled).toBeLessThan(Math.abs(to - from) + DESIGN_SIZE.h);
  });

  it("routes in the same space the cards are placed in", () => {
    // a lane is only useful if it lands between the two columns it separates,
    // in the coordinates the canvas actually draws
    const ids = ["a", "b", "c"];
    const edges = [...chain("a", "b", "c"), { src: "a", dst: "c" }];
    const placed = layout(ids, edges, key, {});
    const lane = routes(ids, edges).get("a->c")!;

    expect(lane[0].x).toBeGreaterThan(placed[key("a")].x);
    expect(lane[0].x).toBeLessThan(placed[key("c")].x + DESIGN_SIZE.w);
  });
});
