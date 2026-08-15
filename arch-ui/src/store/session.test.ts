/**
 * The session store: every event the wire can carry, and what a user edit does
 * when the harness says no.
 *
 * The rollback tests are the point. An optimistic edit that fails silently
 * leaves the canvas showing something the harness does not believe, which is
 * the one state this page must never be in.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  editComponent, mutate, promoteVariant, resolveConcern, sendInput, toolArg, useSession,
} from "./session";
import type { ArchState, Component, WireEvent } from "../types";

const INITIAL = useSession.getState();

function component(id: string, name = id): Component {
  return {
    id, name, kind: "service", responsibility: "does a thing", trace: [],
    existing: false, tech: null, data_owned: null, failure_notes: null,
    facet: null, origin: "",
  };
}

function archState(overrides: Partial<ArchState> = {}): ArchState {
  return {
    mode: "system", phase: "propose",
    brief: {
      goal: "ship it", actors: [], scope: "internal",
      scale: { users: null, reads_per_sec: null, writes_per_sec: null,
               data_volume: null, growth: null },
      latency: null, consistency: null, availability: null, deploy_target: null,
      constraints: [], non_goals: [],
    },
    sketchbook: { variants: {}, active: null, notes: [] },
    components: { api: component("api") },
    connections: [], flows: [], decisions: [], questions: [],
    concerns: [], obligations: [], amendments: [],
    ...overrides,
  };
}

const stateEvent = (state: ArchState, changed = null): WireEvent => ({
  type: "arch_state", phase: state.phase, state,
  renders: { toplevel: "", flows: {}, facets: {}, sketches: {}, active_sketch: null },
  tracker: "", gaps: {}, changed,
});

const apply = (ev: WireEvent) => useSession.getState().apply(ev);

beforeEach(() => useSession.setState(INITIAL, true));
afterEach(() => vi.unstubAllGlobals());

// ---------------------------------------------------------------- events

describe("apply", () => {
  it("replaces server truth wholesale", () => {
    apply(stateEvent(archState()));
    apply(stateEvent(archState({ components: { db: component("db") } })));
    expect(Object.keys(useSession.getState().arch!.components)).toEqual(["db"]);
  });

  it("rings what just changed, and lets it lapse", () => {
    vi.useFakeTimers();
    apply(stateEvent(archState(), { kind: "component", id: "api" } as never));
    expect(useSession.getState().recentlyChanged).toHaveProperty("api");

    vi.advanceTimersByTime(5_000);
    useSession.getState().expireChanges();
    expect(useSession.getState().recentlyChanged).toEqual({});
    vi.useRealTimers();
  });

  it("streams assistant text, then keeps the finished message", () => {
    apply({ type: "harness_event", event: "run_start", data: { task: "design it" } });
    apply({ type: "harness_event", event: "assistant_delta", data: { text: "hel" } });
    apply({ type: "harness_event", event: "assistant_delta", data: { text: "lo" } });
    expect(useSession.getState().stream).toBe("hello");

    apply({ type: "turn_end", status: "reply" });
    const s = useSession.getState();
    expect(s.stream).toBeNull();
    expect(s.running).toBe(false);
    expect(s.transcript.map((i) => [i.t, "text" in i ? i.text : i.status])).toEqual([
      ["user", "design it"],
      ["agent", "hello"],
      ["turn", "reply"],
    ]);
    // §3: the transcript names who is speaking, and the critic is not the
    // architect — see the critic test below
    expect(s.transcript.filter((i) => i.t === "agent").map((i: any) => i.who)).toEqual(["architect"]);
  });

  it("turns tool calls into one row each, named and still running", () => {
    apply({
      type: "harness_event", event: "assistant",
      data: {
        content: "here you go",
        tool_calls: [
          { name: "component", arguments_json: '{"id":"api"}' },
          { name: "connect", arguments_json: '{"src":"api","dst":"db"}' },
        ],
      },
    });
    const items = useSession.getState().transcript;
    expect(items.map((i) => i.t)).toEqual(["agent", "tool", "tool"]);
    expect(items.filter((i) => i.t === "tool").map((i: any) => [i.name, i.arg, i.status]))
      .toEqual([
        ["component", "api", "running"],
        ["connect", "api → db", "running"],
      ]);
  });

  it("a tool result settles its own row, not the other one", () => {
    apply({
      type: "harness_event", event: "assistant",
      data: {
        tool_calls: [
          { name: "component", arguments_json: '{"id":"api"}' },
          { name: "connect", arguments_json: '{"src":"api","dst":"db"}' },
        ],
      },
    });
    apply({ type: "harness_event", event: "tool_result", data: { name: "connect", is_error: true } });
    expect(useSession.getState().transcript.filter((i) => i.t === "tool")
      .map((i: any) => [i.name, i.status]))
      .toEqual([["component", "running"], ["connect", "error"]]);
  });

  it("turn_end token totals are the server's running total, re-seeded on a new ready", () => {
    // an older server carries no totals at all — hold what we had (zero here),
    // never flash a misleading reset
    apply({ type: "turn_end", status: "done" });
    expect(useSession.getState().totalIn).toBe(0);
    expect(useSession.getState().totalOut).toBe(0);

    apply({ type: "turn_end", status: "done", input_tokens: 100, output_tokens: 25 });
    expect(useSession.getState().totalIn).toBe(100);
    expect(useSession.getState().totalOut).toBe(25);

    // a fresh ready is the session boundary: totals go back to zero
    apply({ type: "ready", model: "m", kg: false, kg_ready: false, run_id: "r", repo: ".", skills: [] });
    expect(useSession.getState().totalIn).toBe(0);
    expect(useSession.getState().totalOut).toBe(0);

    // a respawn re-seeds from the payload instead of showing 0 / 0
    apply({
      type: "ready", model: "m", kg: false, kg_ready: false, run_id: "r", repo: ".", skills: [],
      input_tokens: 300, output_tokens: 40,
    });
    expect(useSession.getState().totalIn).toBe(300);
    expect(useSession.getState().totalOut).toBe(40);
  });

  it("a row still running when the turn closes is not left spinning forever", () => {
    // an interrupt, or a turn that died: the result never arrives, and a
    // permanent "···" would be the page lying about what the harness is doing
    apply({
      type: "harness_event", event: "assistant",
      data: { tool_calls: [{ name: "component", arguments_json: '{"id":"api"}' }] },
    });
    apply({ type: "turn_end", status: "interrupted" });
    expect(useSession.getState().transcript.filter((i) => i.t === "tool")
      .map((i: any) => i.status)).toEqual(["ok"]);
  });

  it("the critic gets its own voice, once, from the concern it filed", () => {
    // The critic never sends a message — it files a Concern on its own thread
    // and the state push is the only trace. Without this the objection appears
    // with no record of who raised it.
    const judged = archState({
      concerns: [{
        id: "c1", severity: "risk", target: "api", claim: "unbounded growth",
        alternative: "add a ttl", source: "judge", status: "open", resolution: null,
      } as any],
    });
    apply({ ...stateEvent(judged), changed: { kind: "concern", id: "c1" } } as any);
    apply({ ...stateEvent(judged), changed: { kind: "concern", id: "c1" } } as any);

    const critic = useSession.getState().transcript.filter(
      (i) => i.t === "agent" && (i as any).who === "critic",
    );
    expect(critic).toHaveLength(1);
    expect((critic[0] as any).text).toContain("unbounded growth");
  });

  it("an architect-filed concern is not credited to the critic", () => {
    const own = archState({
      concerns: [{
        id: "c1", severity: "risk", target: "api", claim: "I was wrong earlier",
        alternative: "", source: "model", status: "open", resolution: null,
      } as any],
    });
    apply({ ...stateEvent(own), changed: { kind: "concern", id: "c1" } } as any);
    expect(useSession.getState().transcript.filter((i) => i.t === "agent")).toHaveLength(0);
  });

  it("finalizing closes the session and clears any open gate", () => {
    apply({ type: "permission_request", id: 1, kind: "finalize", summary: "?" });
    expect(useSession.getState().permission).not.toBeNull();

    apply(stateEvent(archState({ phase: "finalized" })));
    const s = useSession.getState();
    expect(s.finalized).toBe(true);
    expect(s.conn).toBe("complete");
    expect(s.permission).toBeNull();
  });

  it("a finalized session is never shown as disconnected", () => {
    apply(stateEvent(archState({ phase: "finalized" })));
    apply({ type: "bye" });
    useSession.getState().disconnect();
    expect(useSession.getState().conn).toBe("complete");
  });

  it("typing while an offer is open is the answer, not a rejection", async () => {
    // The options are a shortcut, not the whole answer space. "about 5k" is a
    // better fact than whichever bucket the user would have rounded it into —
    // and sending it as approved:false would throw the number away and record
    // the question as deferred.
    const posts: { path: string; body: any }[] = [];
    vi.stubGlobal("fetch", async (path: string, init: { body: string }) => {
      posts.push({ path, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({ ok: true }) };
    });
    apply({
      type: "permission_request", id: 7, kind: "offer", summary: "",
      question: "How many users?", options: ["~1k", "~1M"],
    });
    sendInput("about 5k");
    await Promise.resolve();
    expect(posts[0].path).toBe("/permission");
    expect(posts[0].body).toMatchObject({ id: 7, approved: true, feedback: "about 5k" });
  });

  it("typing while an approval gate is open still requests changes", async () => {
    const posts: { path: string; body: any }[] = [];
    vi.stubGlobal("fetch", async (path: string, init: { body: string }) => {
      posts.push({ path, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({ ok: true }) };
    });
    apply({ type: "permission_request", id: 8, kind: "toplevel_approval", summary: "?" });
    sendInput("use postgres instead");
    await Promise.resolve();
    expect(posts[0].body).toMatchObject({ id: 8, approved: false,
                                          feedback: "use postgres instead" });
  });

  it("a dropped connection before finalize is", () => {
    apply(stateEvent(archState()));
    useSession.getState().disconnect();
    expect(useSession.getState().conn).toBe("disconnected");
  });
});

describe("toolArg", () => {
  it("picks whichever field names the thing the call acted on", () => {
    expect(toolArg({ name: "expand", arguments_json: '{"component_id":"db"}' })).toBe("db");
    expect(toolArg({ name: "connect", arguments_json: '{"src":"api","dst":"db"}' }))
      .toBe("api → db");
    expect(toolArg({ name: "concern", arguments_json: '{"claim":"this will not scale"}' }))
      .toBe("this will not scale");
  });

  it("survives arguments that are not JSON at all", () => {
    // a malformed tool call is the model's problem; a page that throws while
    // rendering the transcript makes it everyone's
    expect(toolArg({ name: "mystery", arguments_json: "not json" })).toBe("");
  });

});

// ------------------------------------------------------------- mutations

function stubFetch(...responses: { ok: boolean; body?: unknown }[]) {
  const calls: { path: string; body: unknown }[] = [];
  const queue = [...responses];
  vi.stubGlobal("fetch", async (path: string, init: { body: string }) => {
    calls.push({ path, body: JSON.parse(init.body) });
    const next = queue.shift() ?? { ok: true };
    return { ok: next.ok, json: async () => next.body ?? {} };
  });
  return calls;
}

describe("mutate", () => {
  it("applies the edit before the server has answered", async () => {
    let seenDuringFlight: string | undefined;
    vi.stubGlobal("fetch", async () => {
      seenDuringFlight = useSession.getState().arch!.components.api.name;
      return { ok: true, json: async () => ({}) };
    });
    apply(stateEvent(archState()));

    await editComponent("api", { name: "order-api" });
    expect(seenDuringFlight).toBe("order-api");
  });

  it("sends the op the harness expects", async () => {
    const calls = stubFetch({ ok: true });
    apply(stateEvent(archState()));

    await editComponent("api", { responsibility: "the front door" });
    expect(calls).toEqual([{
      path: "/mutate",
      body: { op: "component", id: "api", responsibility: "the front door" },
    }]);
  });

  it("rolls back and says why when the harness refuses", async () => {
    stubFetch({ ok: false, body: { error: "a component needs a name." } });
    apply(stateEvent(archState()));

    const ok = await editComponent("api", { name: "" });
    expect(ok).toBe(false);
    expect(useSession.getState().arch!.components.api.name).toBe("api");
    expect(useSession.getState().transcript.at(-1)).toMatchObject({
      t: "notice", err: true, text: "✗ a component needs a name.",
    });
  });

  it("rolls back when the harness cannot be reached at all", async () => {
    vi.stubGlobal("fetch", async () => { throw new Error("connection refused"); });
    apply(stateEvent(archState()));

    expect(await editComponent("api", { name: "gone" })).toBe(false);
    expect(useSession.getState().arch!.components.api.name).toBe("api");
    expect(String((useSession.getState().transcript.at(-1) as { text: string }).text))
      .toContain("could not reach the harness");
  });

  it("does not clobber a state push that landed while the edit was in flight", async () => {
    let resolve!: (v: unknown) => void;
    vi.stubGlobal("fetch", () => new Promise((r) => {
      resolve = r;
    }));
    apply(stateEvent(archState()));

    const pending = editComponent("api", { name: "optimistic" });
    // the server's own truth arrives first — a rejection must not undo it
    apply(stateEvent(archState({ components: { api: component("api", "from-the-server") } })));
    resolve({ ok: false, json: async () => ({ error: "no" }) });
    await pending;

    expect(useSession.getState().arch!.components.api.name).toBe("from-the-server");
  });

  it("settles a concern optimistically", async () => {
    const calls = stubFetch({ ok: true });
    apply(stateEvent(archState({
      concerns: [{
        id: "c1", severity: "blocker", target: "api", claim: "no",
        alternative: "", status: "open", resolution: "", source: "model",
      }],
    })));

    await resolveConcern("c1", "overruled", "pilot only");
    expect(calls[0].body).toEqual({
      op: "concern", id: "c1", status: "overruled", resolution: "pilot only",
    });
    expect(useSession.getState().arch!.concerns[0]).toMatchObject({
      status: "overruled", resolution: "pilot only",
    });
  });

  it("promotes without guessing at the result", async () => {
    const calls = stubFetch({ ok: true });
    const before = archState();
    apply(stateEvent(before));

    await promoteVariant("v2", true);
    expect(calls[0].body).toEqual({ op: "promote", variant_id: "v2", replace: true });
    // promotion reshapes the whole design; there is no honest optimistic version
    expect(useSession.getState().arch).toEqual(before);
  });

  it("is a no-op with no state to patch", async () => {
    const calls = stubFetch({ ok: true });
    await mutate({ op: "component", id: "api", name: "x" }, () => {
      throw new Error("must not be called");
    });
    expect(calls).toHaveLength(1);
  });
});
