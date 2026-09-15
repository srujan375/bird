import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getChat, resetChat } from "@arch/board/chat";
import { resetFrames } from "../board/frames";
import { resetCapture } from "./capture";
import { resetDrafts } from "./drafts";
import {
  applyEvent, connect, connection, getSession, mergeSaid, resetSession, stopCopy, toolLine,
} from "./session";
import type { WireArtboard } from "./types";

const hero: WireArtboard = { id: "hero", title: "Hero — dark", current: "v3", versions: [], finalized: false };
const board = (extra: Record<string, unknown> = {}) =>
  applyEvent({ type: "design_state", status: "ready", changed: null, artboards: [hero], html: {}, ...extra } as never);

beforeEach(() => {
  resetSession();
  resetChat();
  resetDrafts();
  resetFrames();
  resetCapture();
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true })));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("tool lines", () => {
  it("names every design tool by what it did", () => {
    board();
    expect(toolLine("design_plan", false, {}).said).toBe("planned the direction");
    expect(toolLine("design_look", false, { path: ".bird/s/attachments/shot.png" }).said).toBe("looked at shot.png");
    expect(toolLine("design_critique", false, { artboard: "hero" }).said).toBe("asked the critic about Hero — dark");
    expect(toolLine("design_showcase", false, { artboard: "hero" }).said).toBe("moved Hero — dark to the showcase");
    expect(toolLine("design_polish", false, { artboard: "hero", version: "v4" }).said).toBe("polished Hero — dark");
    expect(toolLine("design_set_theme", false, { theme: "apple" }).said).toBe("set the apple theme");
    expect(toolLine("design_themes", false, {}).said).toBe("listed the themes");
  });

  it("a batch of edits is one line with the count", () => {
    board();
    expect(toolLine("design_edit", false, { artboard: "hero", version: "v9", applied: 5 }).said)
      .toBe("made 5 edits to Hero — dark → v9");
    expect(toolLine("design_edit", false, { artboard: "hero", version: "v9", applied: 4, failed: 1 }).said)
      .toBe("made 4 edits to Hero — dark → v9 (1 refused)");
    expect(toolLine("design_edit", false, { artboard: "hero", version: "v4" }).said).toBe("edited Hero — dark → v4");
  });

  it("a refusal carries the first sentence of its reason", () => {
    const line = toolLine("design_edit", true, {
      error: "selector '0>1>4' no longer resolves — the document changed under it. Read it again before editing.",
    });
    expect(line.said).toBe("edit was refused — selector '0>1>4' no longer resolves — the document changed under it.");
    expect(toolLine("design_polish", true, {}).said).toBe("polish was refused");
  });

  it("merges a turn's edits per artboard and its creates into one act", () => {
    expect(mergeSaid(["edited Hero → v2", "put A on the board", "edited Hero → v3", "put B on the board", "read Hero", "made 3 edits to Nav → v5"]))
      .toBe("design · put A and B on the board · made 2 edits to Hero → v3 · read Hero · made 3 edits to Nav → v5");
  });
});

describe("why a turn stopped", () => {
  it("blames the user only when the harness says so", () => {
    expect(stopCopy("user")).toBe("_You stopped the turn._");
    expect(stopCopy("shutdown")).toBe("_The session is closing._");
    expect(stopCopy(undefined)).toBe("_The turn stopped._");
    expect(stopCopy("watchdog")).toBe("_The turn stopped._");
  });

  it("says it in the thread on turn_end", () => {
    applyEvent({ type: "turn_end", status: "interrupted" });
    applyEvent({ type: "turn_end", status: "interrupted", reason: "user" } as never);
    const lines = getChat().turns.map((t) => (t.t === "say" ? t.lines[0] : ""));
    expect(lines).toEqual(["_The turn stopped._", "_You stopped the turn._"]);
    expect(getSession().running).toBe(false);
  });
});

describe("the one line about now", () => {
  it("says a capture is on, then what the critic found, then lets it go", () => {
    board();
    applyEvent({ type: "capture_request", id: "cap-1", artboard: "hero", deadline: 10 });
    expect(getSession().progress).toBe("capturing Hero — dark for the critic");
    applyEvent({ type: "harness_event", event: "tool_result",
      data: { name: "design_create", is_error: false, details: { artboard: "hero", version: "v1", critique: "- tighten the hero\n- one CTA" } } });
    expect(getSession().progress).toBe("critic: 2 notes on Hero — dark");
    vi.advanceTimersByTime(6000);
    expect(getSession().progress).toBe("");
  });

  it("says when the critic could not see the artboard", () => {
    board();
    applyEvent({ type: "harness_event", event: "tool_result",
      data: { name: "design_create", is_error: false, details: { artboard: "hero", version: "v1", critique: "not critiqued: the page could not capture: the page did not raster the artboard in time" } } });
    expect(getSession().progress).toBe("the critic could not see Hero — dark");
  });

  it("clears on turn_end", () => {
    board();
    applyEvent({ type: "harness_event", event: "tool_call_delta", data: { index: 0, name: "design_plan", text: '{"plan":"' } as never });
    expect(getSession().progress).toBe("planning the direction");
    applyEvent({ type: "turn_end", status: "done" });
    expect(getSession().progress).toBe("");
  });
});

describe("what the designer's hands are on", () => {
  it("follows the streamed call's artboard, then the running one, then lets go", () => {
    board({ artboards: [hero, { ...hero, id: "nav", title: "Nav" }] });
    applyEvent({ type: "harness_event", event: "run_start", data: { task: "tighten it" } });
    applyEvent({ type: "harness_event", event: "tool_call_delta", data: { index: 0, name: "design_edit", text: '{"artboard":"nav","op":"set_style"' } as never });
    expect(getSession().working).toEqual({ artboard: "nav", verb: "editing" });
    applyEvent({ type: "harness_event", event: "tool_call_delta", data: { index: 1, name: "design_read", text: '{"artboard":"hero"}' } as never });
    expect(getSession().working).toEqual({ artboard: "hero", verb: "reading" });
    applyEvent({ type: "harness_event", event: "assistant", data: { tool_calls: [{ name: "design_edit", arguments_json: "{}" }, { name: "design_read", arguments_json: "{}" }] } });
    expect(getSession().working).toEqual({ artboard: "nav", verb: "editing" });
    applyEvent({ type: "harness_event", event: "tool_result", data: { name: "design_edit", is_error: false, details: { artboard: "nav", version: "v4" } } });
    expect(getSession().working).toEqual({ artboard: "hero", verb: "reading" });
    applyEvent({ type: "harness_event", event: "tool_result", data: { name: "design_read", is_error: false, details: { artboard: "hero" } } });
    expect(getSession().working).toBeNull();
  });
});

describe("the critiques", () => {
  it("ride the state push, per artboard, with the plan's note and the pending hand edits", () => {
    board({ critiques: { hero: { version: "v3", kind: "render", text: "- fix" } }, plan_critique: "- name a signature", pending_user_edits: 2 });
    const s = getSession();
    expect(s.critiques.hero).toEqual({ version: "v3", kind: "render", text: "- fix" });
    expect(s.planCritique).toBe("- name a signature");
    expect(s.pendingEdits).toBe(2);
  });

  it("the plan critique becomes one card in the thread, never two", () => {
    board();
    applyEvent({ type: "harness_event", event: "tool_result",
      data: { name: "design_plan", is_error: false, details: { ok: true, plan: "stored", critique: "- a\n- b\n- c" } } });
    board({ plan_critique: "- a\n- b\n- c" });
    const cards = getChat().turns.filter((t) => t.t === "card");
    expect(cards).toHaveLength(1);
    expect(cards[0].t === "card" && cards[0].title).toBe("Plan critique · 3 notes");
  });
});

describe("the connection", () => {
  class FakeES {
    static all: FakeES[] = [];
    onopen: (() => void) | null = null;
    onmessage: ((e: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    closed = false;
    constructor(public url: string) { FakeES.all.push(this); }
    close() { this.closed = true; }
  }
  beforeEach(() => { FakeES.all = []; vi.stubGlobal("EventSource", FakeES); });

  it("comes back from a drop with backoff, clears the thread for the replay, and only a bye is final", () => {
    connect();
    expect(FakeES.all).toHaveLength(1);
    FakeES.all[0].onmessage!({ data: JSON.stringify({ type: "ready", model: "m", kg: false, kg_ready: false, run_id: "r", repo: "x", skills: [] }) });
    expect(getSession().conn).toBe("connected");
    applyEvent({ type: "harness_event", event: "assistant", data: { content: "hello" } });
    expect(getChat().turns).toHaveLength(1);

    FakeES.all[0].onerror!();
    expect(getSession().conn).toBe("reconnecting");
    expect(FakeES.all[0].closed).toBe(true);
    vi.advanceTimersByTime(999);
    expect(FakeES.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeES.all).toHaveLength(2);

    FakeES.all[1].onerror!();
    vi.advanceTimersByTime(1999);
    expect(FakeES.all).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeES.all).toHaveLength(3);
    expect(connection().attempt).toBe(2);

    /* the replay is coming: what this page built goes first */
    FakeES.all[2].onopen!();
    expect(getChat().turns).toHaveLength(0);
    expect(connection().attempt).toBe(0);
    FakeES.all[2].onmessage!({ data: JSON.stringify({ type: "ready", model: "m", kg: false, kg_ready: false, run_id: "r", repo: "x", skills: [] }) });
    expect(getSession().conn).toBe("connected");

    applyEvent({ type: "bye" });
    expect(getSession().conn).toBe("disconnected");
    FakeES.all[2].onerror?.();
    vi.advanceTimersByTime(30000);
    expect(FakeES.all).toHaveLength(3);
  });

  it("the backoff caps at fifteen seconds", () => {
    connect();
    for (let i = 0; i < 6; i++) {
      FakeES.all[FakeES.all.length - 1].onerror!();
      vi.advanceTimersByTime(15000);
    }
    expect(FakeES.all).toHaveLength(7);
    FakeES.all[6].onerror!();
    vi.advanceTimersByTime(14999);
    expect(FakeES.all).toHaveLength(7);
    vi.advanceTimersByTime(1);
    expect(FakeES.all).toHaveLength(8);
  });
});
