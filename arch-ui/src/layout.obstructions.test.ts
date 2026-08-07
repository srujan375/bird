/**
 * Edge routing: which lines would be drawn through a card.
 *
 * The load-bearing tests here are the negative ones. An arc that fires when
 * nothing is in the way is a curve for no reason — mildly ugly. A straight line
 * ruled through two components reads as three connections where there is one,
 * and hides whatever text it crosses, which is the bug this exists to prevent.
 */
import { describe, expect, it } from "vitest";
import { obstructions } from "./layout";

const SIZE = { w: 216, h: 100 };
const key = (id: string) => `design:${id}`;

/** A row of cards, left to right, all on the same baseline. */
function row(...ids: string[]) {
  const positions: Record<string, { x: number; y: number }> = {};
  ids.forEach((id, i) => { positions[id === "" ? `_${i}` : key(id)] = { x: i * 340, y: 200 }; });
  return positions;
}

describe("obstructions", () => {
  it("leaves an edge between neighbours alone", () => {
    const blocked = obstructions(
      ["a", "b"], [{ src: "a", dst: "b" }], row("a", "b"), key, SIZE,
    );
    expect(blocked.size).toBe(0);
  });

  it("arcs an edge that skips over a card in the same row", () => {
    // a → c, with b parked between them: the classic layered-graph case
    const blocked = obstructions(
      ["a", "b", "c"], [{ src: "a", dst: "c" }], row("a", "b", "c"), key, SIZE,
    );
    expect(blocked.has("a->c")).toBe(true);
    // above b's top edge, not merely level with it
    expect(blocked.get("a->c")!).toBeLessThan(200);
  });

  it("does not arc over a card that is nowhere near the line", () => {
    // b is in the horizontal corridor but two rows down — the edge misses it
    const positions = row("a", "b", "c");
    positions[key("b")] = { x: 340, y: 900 };
    const blocked = obstructions(
      ["a", "b", "c"], [{ src: "a", dst: "c" }], positions, key, SIZE,
    );
    expect(blocked.size).toBe(0);
  });

  it("clears the highest card when several are in the way", () => {
    const positions = row("a", "b", "c", "d");
    positions[key("b")] = { x: 340, y: 200 };
    positions[key("c")] = { x: 680, y: 120 }; // higher up: this is the one to clear
    const blocked = obstructions(
      ["a", "b", "c", "d"], [{ src: "a", dst: "d" }], positions, key, SIZE,
    );
    expect(blocked.get("a->d")!).toBeLessThan(120);
  });

  it("ignores the edge's own endpoints", () => {
    // the two cards an edge connects are always 'in' its corridor at the ends;
    // counting them would arc every edge in the graph
    const blocked = obstructions(
      ["a", "b"], [{ src: "a", dst: "b" }], row("a", "b"), key, SIZE,
    );
    expect(blocked.has("a->b")).toBe(false);
  });

  it("says nothing about an edge whose ends have not been placed yet", () => {
    const blocked = obstructions(
      ["a", "ghost"], [{ src: "a", dst: "ghost" }], row("a"), key, SIZE,
    );
    expect(blocked.size).toBe(0);
  });
});
