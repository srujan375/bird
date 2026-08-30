import { describe, expect, it } from "vitest";
import { itemText, parseItem } from "./NodeCard";

describe("list rows as text", () => {
  it("reads key, value and note", () => {
    expect(parseItem("GET /orders — list them", "op")).toEqual({ k: "GET", v: "/orders", d: "list them" });
    expect(parseItem("tbl sessions", "op")).toEqual({ k: "tbl", v: "sessions", d: "" });
  });
  it("falls back to the kind's key when none is typed", () => {
    expect(parseItem("/orders", "GET")).toEqual({ k: "GET", v: "/orders", d: "" });
    expect(parseItem("place(cart) — reserves stock", "op")).toEqual({ k: "op", v: "place(cart)", d: "reserves stock" });
  });
  it("treats an empty row as removed and round-trips", () => {
    expect(parseItem("   ", "op")).toBeNull();
    const it_ = { k: "msg", v: "order.placed", d: "fan-out" };
    expect(parseItem(itemText(it_), "x")).toEqual(it_);
  });
});
