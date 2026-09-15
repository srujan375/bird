import { beforeEach, describe, expect, it } from "vitest";
import { abandon, clearPoster, draftHtml, feed, getDrafts, getPosters, messageDone, resetDrafts, retireArrived, retireOldest, subscribe } from "./drafts";
import type { WireArtboard } from "./types";

const board = (...ids: [string, string][]): WireArtboard[] =>
  ids.map(([id, current]) => ({ id, title: id, current, versions: [], finalized: false }));

beforeEach(resetDrafts);

describe("feed", () => {
  it("opens a draft on the first piece of a design_create and names it when it can", () => {
    feed(0, "design_create", '{"ti', []);
    expect(getDrafts()).toEqual([{ key: "0:0", title: null, artboard: null, closed: false, fromVersion: null }]);
    feed(0, "", 'tle": "Hero — dark", "html": "<h1>', []);
    expect(getDrafts()[0].title).toBe("Hero — dark");
    expect(draftHtml("0:0")).toBe("<h1>");
  });

  it("ignores every other tool", () => {
    feed(0, "design_read", "{}", []);
    expect(getDrafts()).toEqual([]);
  });

  it("delivers pieces to a drawer, the backlog first", () => {
    feed(0, "design_create", '{"html": "<p>', []);
    const seen: string[] = [];
    const un = subscribe("0:0", (p) => seen.push(p));
    seen.push(draftHtml("0:0"));
    feed(0, "", "hi", []);
    feed(0, "", '</p>"}', []);
    expect(seen).toEqual(["<p>", "hi", "</p>"]);
    expect(getDrafts()[0].closed).toBe(true);
    un();
    feed(0, "", "", []);
    expect(seen).toHaveLength(3);
  });

  it("remembers where a rebuild started", () => {
    feed(0, "design_create", '{"artboard": "hero", "html": "<p>"', board(["hero", "v3"]));
    expect(getDrafts()[0]).toMatchObject({ artboard: "hero", fromVersion: "v3" });
  });

  it("keys drafts by message so indices can restart", () => {
    feed(0, "design_create", '{"html": "a"}', []);
    messageDone();
    feed(0, "design_create", '{"html": "b"}', []);
    expect(getDrafts().map((d) => d.key)).toEqual(["0:0", "1:0"]);
    expect(draftHtml("1:0")).toBe("b");
  });
});

describe("retiring", () => {
  it("retires a new draft when the artboard its title names arrives", () => {
    feed(0, "design_create", '{"title": "Hero — dark", "html": "x"}', []);
    feed(1, "design_create", '{"title": "Hero — warm", "html": "y"}', []);
    retireArrived([], board(["hero-dark", "v1"]));
    expect(getDrafts().map((d) => d.title)).toEqual(["Hero — warm"]);
  });

  it("retires a rebuild when its artboard moves on", () => {
    const before = board(["hero", "v2"]);
    feed(0, "design_create", '{"artboard": "hero", "html": "x"}', before);
    retireArrived(before, before);
    expect(getDrafts()).toHaveLength(1);
    retireArrived(before, board(["hero", "v3"]));
    expect(getDrafts()).toHaveLength(0);
  });

  it("gives an unaccounted-for new artboard to the oldest unclaimed draft", () => {
    feed(0, "design_create", '{"title": "Hero", "html": "x"}', []);
    feed(1, "design_create", '{"title": "Hero", "html": "y"}', []);
    retireArrived([], board(["hero", "v1"], ["hero-2", "v1"]));
    expect(getDrafts()).toHaveLength(0);
  });

  it("retires the oldest on a tool result, and everything on abandon", () => {
    feed(0, "design_create", '{"html": "x"}', []);
    feed(1, "design_create", '{"html": "y"}', []);
    retireOldest();
    expect(getDrafts().map((d) => d.key)).toEqual(["0:1"]);
    abandon();
    expect(getDrafts()).toEqual([]);
    expect(draftHtml("0:1")).toBe("");
  });
});

describe("the handover poster", () => {
  it("keeps the draft an arriving artboard retired, until the frame has painted", () => {
    feed(0, "design_create", '{"title": "Hero", "html": "<p>x</p>"}', []);
    retireArrived([], board(["hero", "v1"]));
    expect(getDrafts()).toEqual([]);
    expect(getPosters().hero?.key).toBe("0:0");
    clearPoster("hero");
    expect(getPosters()).toEqual({});
  });

  it("a rebuild's poster is keyed by the artboard it rebuilt", () => {
    feed(0, "design_create", '{"artboard": "hero", "html": "<p>"', board(["hero", "v3"]));
    retireArrived(board(["hero", "v3"]), board(["hero", "v4"]));
    expect(getPosters().hero?.key).toBe("0:0");
    resetDrafts();
    expect(getPosters()).toEqual({});
  });
});
