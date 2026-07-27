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
  editComponent, mutate, noticeFor, promoteVariant, resolveConcern, useSession,
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
    expect(s.transcript).toEqual([
      { t: "user", text: "design it" },
      { t: "agent", text: "hello" },
      { t: "turn", status: "reply", message: null },
    ]);
  });

  it("turns tool calls into one activity line each", () => {
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
    expect(useSession.getState().transcript.map((t) => "text" in t && t.text)).toEqual([
      "here you go", "+ component api", "→ connect api → db",
    ]);
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

  it("a dropped connection before finalize is", () => {
    apply(stateEvent(archState()));
    useSession.getState().disconnect();
    expect(useSession.getState().conn).toBe("disconnected");
  });
});

describe("noticeFor", () => {
  it("picks the argument that identifies the call", () => {
    expect(noticeFor({ name: "expand", arguments_json: '{"component_id":"db"}' }))
      .toBe("▸ expand db");
    expect(noticeFor({ name: "concern", arguments_json: '{"claim":"this will not scale"}' }))
      .toBe("⚑ concern this will not scale");
    expect(noticeFor({ name: "mystery", arguments_json: "not json" })).toBe("· mystery");
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
    expect(useSession.getState().transcript.at(-1)).toEqual({
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
