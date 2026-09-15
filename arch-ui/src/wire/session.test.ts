import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getChat, resetChat } from "../board/chat";
import { applyEvent, connect, connection, getSession, resetSession, stopCopy } from "./session";

/* The rail's wire, on the arch board: the same reconnect and the same
 * honesty about who stopped a turn as the design page. */

beforeEach(() => { resetSession(); resetChat(); vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("why a turn stopped", () => {
  it("blames the user only when the harness says so", () => {
    expect(stopCopy("user")).toBe("_You stopped the turn._");
    expect(stopCopy("shutdown")).toBe("_The session is closing._");
    expect(stopCopy(undefined)).toBe("_The turn stopped._");
  });

  it("is said in the thread on an interrupted turn_end", () => {
    applyEvent({ type: "turn_end", status: "interrupted", reason: "user" });
    applyEvent({ type: "turn_end", status: "done" });
    const said = getChat().turns.map((t) => (t.t === "say" ? t.lines[0] : ""));
    expect(said).toEqual(["_You stopped the turn._"]);
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

  it("retries a dropped connection with backoff and clears the thread before the replay", () => {
    connect();
    FakeES.all[0].onmessage!({ data: JSON.stringify({ type: "harness_event", event: "assistant", data: { content: "hi" } }) });
    expect(getChat().turns).toHaveLength(1);
    FakeES.all[0].onerror!();
    expect(getSession().conn).toBe("reconnecting");
    vi.advanceTimersByTime(1000);
    expect(FakeES.all).toHaveLength(2);
    FakeES.all[1].onerror!();
    vi.advanceTimersByTime(2000);
    expect(FakeES.all).toHaveLength(3);
    expect(connection().attempt).toBe(2);
    FakeES.all[2].onopen!();
    expect(getChat().turns).toHaveLength(0);
    expect(connection().attempt).toBe(0);
  });

  it("stops retrying after a bye", () => {
    connect();
    applyEvent({ type: "bye" });
    expect(getSession().conn).toBe("disconnected");
    FakeES.all[0].onerror?.();
    vi.advanceTimersByTime(60000);
    expect(FakeES.all).toHaveLength(1);
  });
});

describe("the five elements' wire", () => {
  it("keeps the scribe's state and drops board lines from the thread while it is on", () => {
    applyEvent({ type: "ready", model: "m", kg: false, kg_ready: false, run_id: "r", repo: "/", skills: [], scribe: "haiku" });
    expect(getSession().scribeOn).toBe(true);
    applyEvent({ type: "scribe", state: "drawing", queued: 2, recent: [{ text: "added X", ids: ["x"] }] });
    expect(getSession().scribe?.queued).toBe(2);
    applyEvent({ type: "harness_event", event: "tool_result", data: { name: "canvas", details: { summary: "Board: 1 node(s) added.", subjects: ["x"] } } });
    expect(getChat().turns.some((t) => t.t === "say" && t.line)).toBe(false);
  });

  it("keeps board lines in the thread without a scribe", () => {
    applyEvent({ type: "ready", model: "m", kg: false, kg_ready: false, run_id: "r", repo: "/", skills: [] });
    applyEvent({ type: "harness_event", event: "tool_result", data: { name: "canvas", details: { summary: "Board: 1 node(s) added.", subjects: ["x"] } } });
    expect(getChat().turns.some((t) => t.t === "say" && t.line?.text === "board · 1 node(s) added.")).toBe(true);
  });

  it("turns research events into one progress turn that ticks and is kept", () => {
    applyEvent({ type: "harness_event", event: "run_start", data: { task: "design it" } });
    applyEvent({ type: "harness_event", event: "research", data: { step: "repo", text: "Reading the repo", done: false } });
    applyEvent({ type: "harness_event", event: "research", data: { step: "web", text: "Looking at how others build this", done: false } });
    applyEvent({ type: "harness_event", event: "research", data: { step: "repo", text: "Reading the repo", done: true } });
    const progress = getChat().turns.filter((t) => t.t === "progress");
    expect(progress).toHaveLength(1);
    expect(getChat().turns.some((t) => t.t === "thinking")).toBe(false);
    const steps = (progress[0] as { steps: { step: string; done: boolean }[] }).steps;
    expect(steps.map((s) => [s.step, s.done])).toEqual([["repo", true], ["web", false]]);
    applyEvent({ type: "turn_end", status: "done" });
    const after = getChat().turns.find((t) => t.t === "progress") as { steps: { done: boolean }[] };
    expect(after.steps.every((s) => s.done)).toBe(true);
  });

  it("carries the frontier off the state push", () => {
    const state = { brief: { goal: "", actors: [], scale: "", constraints: [], non_goals: [] }, nodes: {}, edges: [], approaches: {}, decisions: [], questions: [], annotations: [], handed_off: false };
    applyEvent({ type: "arch_state", status: "open", state, renders: { board: "" }, noticing: [], changed: null,
      frontier: { fork: null, open: [{ id: "a", label: "A", kind: "api", depth: "stub", why: "unelaborated" }], more: 0, closed: [] } });
    expect(getSession().frontier?.open[0].id).toBe("a");
  });
});
