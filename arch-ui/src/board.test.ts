/**
 * The board's two jobs: read a flow as a sequence without lying about it, and
 * pack the frames so the architect adding a component mid-turn does not shuffle
 * the board you are reading.
 */
import { describe, expect, it } from "vitest";
import { LANE_W, board, laneHeight, rungs } from "./board";
import { DESIGN_SIZE } from "./layout";
import type { ArchState, Component, Flow } from "./types";

const comp = (id: string): Component => ({
  id, name: id, kind: "service", responsibility: "", trace: [], existing: false,
  tech: null, data_owned: null, failure_notes: null, facet: null, origin: "",
});

const flow = (id: string, steps: [string, string][]): Flow => ({
  id, name: id, kind: "happy",
  steps: steps.map(([src, dst]) => ({ src, dst, action: `${src}->${dst}`, note: null })),
});

const state = (ids: string[], flows: Flow[]): ArchState => ({
  mode: "feature", phase: "expand",
  brief: { goal: "g", actors: [], scope: "production", scale: {} as never, latency: null,
    consistency: null, availability: null, deploy_target: null, constraints: [], non_goals: [] },
  sketchbook: { variants: {}, active: null } as never,
  components: Object.fromEntries(ids.map((i) => [i, comp(i)])),
  connections: [], flows, decisions: [], questions: [], concerns: [],
  obligations: [], amendments: [],
});

const key = (id: string) => `design:${id}`;
const row = (ids: string[]) =>
  Object.fromEntries(ids.map((id, i) => [key(id), { x: i * 300, y: 0 }]));

describe("rungs", () => {
  it("reads a chain of hops as one ladder", () => {
    const r = rungs(flow("f", [["a", "b"], ["b", "c"]]), () => true);
    expect(r.map((x) => x.id)).toEqual(["a", "b", "c"]);
    expect(r.map((x) => x.via)).toEqual([null, "a->b", "b->c"]);
    expect(r.some((x) => x.jump)).toBe(false);
  });

  it("marks a step that does not follow from the last, rather than welding it on", () => {
    // a->b then c->d: the flow went somewhere else, and saying so is the whole
    // difference between a sequence and a list of pairs
    const r = rungs(flow("f", [["a", "b"], ["c", "d"]]), () => true);
    expect(r.map((x) => x.id)).toEqual(["a", "b", "c", "d"]);
    expect(r[2].jump).toBe(true);
    expect(r[0].jump).toBe(false); // the first rung is a start, not a jump
  });

  it("drops a step naming a component that is gone", () => {
    const r = rungs(flow("f", [["a", "ghost"], ["a", "b"]]), (id) => id !== "ghost");
    expect(r.map((x) => x.id)).toEqual(["a", "b"]);
  });

  it("repeats a component the flow returns to", () => {
    // a->b->a is a real shape (call and response) and the ladder must show both
    const r = rungs(flow("f", [["a", "b"], ["b", "a"]]), () => true);
    expect(r.map((x) => x.id)).toEqual(["a", "b", "a"]);
  });
});

describe("board", () => {
  it("frames the design around where the cards actually are", () => {
    const arch = state(["a", "b"], []);
    const b = board(arch, row(["a", "b"]), key, DESIGN_SIZE);
    expect(b.design.x).toBeLessThan(0);                        // padding, outside the cards
    expect(b.design.x + b.design.w).toBeGreaterThan(300 + DESIGN_SIZE.w);
    expect(b.lanes).toHaveLength(0);
  });

  it("puts the ladders in a row beside the design, not stacked under it", () => {
    const arch = state(["a", "b"], [flow("f1", [["a", "b"]]), flow("f2", [["b", "a"]])]);
    const b = board(arch, row(["a", "b"]), key, DESIGN_SIZE);
    expect(b.lanes).toHaveLength(2);
    expect(b.lanes[0].y).toBe(b.lanes[1].y);                   // same top
    expect(b.lanes[1].x).toBeGreaterThanOrEqual(b.lanes[0].x + LANE_W);
    expect(b.lanes[0].x).toBeGreaterThan(b.design.x + b.design.w - LANE_W);
  });

  it("does not move the ladders when the design grows within a quantum", () => {
    // the rule that makes the board tolerable while the architect is working:
    // a component landing mid-turn must not slide the column you are reading
    const flows = [flow("f1", [["a", "b"]])];
    const small = board(state(["a", "b"], flows), row(["a", "b"]), key, DESIGN_SIZE);
    const grown = board(
      state(["a", "b", "c"], flows),
      { ...row(["a", "b"]), [key("c")]: { x: 380, y: 200 } }, // wider, same quantum
      key,
      DESIGN_SIZE,
    );
    expect(grown.design.w).toBeGreaterThan(small.design.w); // the frame did grow
    expect(grown.lanes[0].x).toBe(small.lanes[0].x);        // the ladders did not move
  });

  it("moves them once the design outgrows the quantum, and then visibly", () => {
    const flows = [flow("f1", [["a", "b"]])];
    const ids = ["a", "b", "c", "d", "e", "f", "g", "h"];
    const wide = board(state(ids, flows), row(ids), key, DESIGN_SIZE);
    const narrow = board(state(["a", "b"], flows), row(["a", "b"]), key, DESIGN_SIZE);
    expect(wide.lanes[0].x - narrow.lanes[0].x).toBeGreaterThanOrEqual(400);
  });

  it("skips a flow with nothing left to draw", () => {
    const arch = state(["a"], [flow("f1", [["gone", "alsogone"]])]);
    expect(board(arch, row(["a"]), key, DESIGN_SIZE).lanes).toHaveLength(0);
  });

  it("bounds everything, so the framer can fit the whole board", () => {
    const arch = state(["a", "b"], [flow("f1", [["a", "b"], ["b", "a"]])]);
    const b = board(arch, row(["a", "b"]), key, DESIGN_SIZE);
    const lane = b.lanes[0];
    expect(b.bounds.x + b.bounds.w).toBeGreaterThanOrEqual(lane.x + lane.w);
    expect(b.bounds.y + b.bounds.h).toBeGreaterThanOrEqual(lane.y + lane.h);
    expect(b.bounds.x).toBeLessThanOrEqual(b.design.x);
  });

  it("survives a design nothing has been placed for yet", () => {
    const b = board(state(["a"], []), {}, key, DESIGN_SIZE);
    expect(Number.isFinite(b.bounds.w)).toBe(true);
    expect(Number.isFinite(b.bounds.h)).toBe(true);
  });

  it("grows a ladder with its rungs", () => {
    expect(laneHeight(6)).toBeGreaterThan(laneHeight(3));
  });
});
