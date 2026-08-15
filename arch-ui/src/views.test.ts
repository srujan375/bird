/**
 * Views are projections, so what matters is that they project *this* state and
 * never invent, drop or duplicate anything that isn't derivable from it.
 */
import { describe, expect, it } from "vitest";
import {
  BOARD, CONTEXT, SYSTEM, WHOLE, flowView, goalLabel, listViews, projectView, resolveView,
} from "./views";
import type { ArchState, Component, Connection, Flow } from "./types";

const LABEL_CAP = 47; // 46 characters plus the ellipsis

const comp = (id: string, extra: Partial<Component> = {}): Component => ({
  id,
  name: id,
  kind: "service",
  responsibility: "does a thing",
  trace: [],
  existing: false,
  tech: null,
  data_owned: null,
  failure_notes: null,
  facet: null,
  origin: "",
  ...extra,
});

const conn = (src: string, dst: string, extra: Partial<Connection> = {}): Connection => ({
  src, dst, label: `${src} to ${dst}`, kind: "sync",
  mechanism: null, protocol: null, data: null, failure_mode: null,
  ...extra,
});

const flow = (id: string, steps: [string, string][], kind: Flow["kind"] = "happy"): Flow => ({
  id, name: id, kind,
  steps: steps.map(([src, dst]) => ({ src, dst, action: `${src}→${dst}`, note: null })),
});

function state(over: Partial<ArchState> = {}): ArchState {
  const components = [
    comp("mcp", { existing: true }),
    comp("client", { kind: "external" }),
    comp("token"), comp("keys"), comp("store", { kind: "store" }),
  ];
  return {
    mode: "feature",
    phase: "expand",
    brief: { goal: "OAuth for MCP", actors: [], scope: "production", scale: {} as never,
      latency: null, consistency: null, availability: null, deploy_target: null,
      constraints: [], non_goals: [] },
    sketchbook: { variants: {}, active: null } as never,
    components: Object.fromEntries(components.map((c) => [c.id, c])),
    connections: [
      conn("client", "token"), conn("token", "keys"),
      conn("token", "store"), conn("keys", "store"), conn("mcp", "token"),
    ],
    flows: [flow("f1", [["client", "token"], ["token", "keys"]])],
    decisions: [], questions: [], concerns: [], obligations: [], amendments: [],
    ...over,
  };
}

describe("projectView", () => {
  it("draws everything for the whole design", () => {
    const arch = state();
    const p = projectView(arch, WHOLE);
    expect(Object.keys(p.components)).toHaveLength(5);
    expect(p.connections).toHaveLength(5);
    expect(p.synthetic.size).toBe(0);
  });

  it("rolls the new work into one box for the context view", () => {
    const p = projectView(state(), CONTEXT);
    // what the design is built against stays; what it is built of does not
    expect(Object.keys(p.components).sort()).toEqual(["client", "mcp", SYSTEM]);
    expect(p.synthetic).toEqual(new Set([SYSTEM]));
    expect(p.components[SYSTEM].name).toBe("OAuth for MCP");
  });

  it("keeps only the connections that cross the boundary, merged", () => {
    const p = projectView(state(), CONTEXT);
    const pairs = p.connections.map((c) => `${c.src}->${c.dst}`).sort();
    expect(pairs).toEqual([`client->${SYSTEM}`, `mcp->${SYSTEM}`]);
    // token→keys, token→store and keys→store are internal: not this view's business
    expect(p.connections).toHaveLength(2);
  });

  it("says how many connections a rolled edge stands for, rather than picking one", () => {
    const arch = state({
      connections: [conn("client", "token", { label: "authorises" }), conn("client", "keys")],
    });
    const rolled = projectView(arch, CONTEXT).connections.find((c) => c.src === "client")!;
    expect(rolled.label).toBe("2 connections");
  });

  it("does not offer a context view when there is nothing to roll up against", () => {
    const arch = state({
      components: { a: comp("a"), b: comp("b") }, // no existing, no external
      connections: [conn("a", "b")],
      flows: [],
    });
    expect(listViews(arch).map((v) => v.id)).toEqual([WHOLE]);
    // and asking for one anyway falls back to the whole design, not to nothing
    expect(Object.keys(projectView(arch, CONTEXT).components)).toHaveLength(2);
  });

  it("draws a flow as only the components on it", () => {
    const p = projectView(state(), flowView("f1"));
    expect(Object.keys(p.components).sort()).toEqual(["client", "keys", "token"]);
    expect(p.connections.map((c) => `${c.src}->${c.dst}`)).toEqual(["client->token", "token->keys"]);
  });

  it("keeps the real connection's detail when the flow walks one", () => {
    const arch = state({
      connections: [conn("client", "token", { kind: "async", mechanism: "SQS", failure_mode: "retries" })],
      flows: [flow("f1", [["client", "token"]])],
    });
    const [edge] = projectView(arch, flowView("f1")).connections;
    expect(edge.mechanism).toBe("SQS");
    expect(edge.failure_mode).toBe("retries");
  });

  it("still draws a step that has no connection recorded for it", () => {
    const arch = state({ connections: [], flows: [flow("f1", [["client", "token"]])] });
    const [edge] = projectView(arch, flowView("f1")).connections;
    expect(edge.label).toBe("client→token");
  });

  it("draws a repeated hop once", () => {
    const arch = state({ flows: [flow("f1", [["client", "token"], ["token", "keys"], ["client", "token"]])] });
    expect(projectView(arch, flowView("f1")).connections).toHaveLength(2);
  });

  it("ignores a step naming a component that has since been deleted", () => {
    const arch = state({ flows: [flow("f1", [["client", "gone"], ["client", "token"]])] });
    const p = projectView(arch, flowView("f1"));
    expect(Object.keys(p.components).sort()).toEqual(["client", "token"]);
    expect(p.connections).toHaveLength(1);
  });
});

describe("goalLabel", () => {
  it("leaves a goal that already reads as a label alone", () => {
    expect(goalLabel("OAuth for MCP")).toBe("OAuth for MCP");
  });

  it("cuts a sentence at a word, not mid-word", () => {
    const long =
      "Enable selected tasks to be assigned to sub-agents that complete in parallel, " +
      "with isolated workspaces and results merged back into the main session";
    const out = goalLabel(long);
    expect(out.length).toBeLessThanOrEqual(LABEL_CAP);
    expect(out.endsWith("\u2026")).toBe(true);
    expect(long).toContain(out.slice(0, -1)); // a prefix, not a mangling
  });

  it("names the box something when there is no goal at all", () => {
    expect(goalLabel("   ")).toBe("The new design");
  });
});

describe("listViews", () => {
  it("offers context, the whole design, and one view per flow", () => {
    const views = listViews(state());
    expect(views.map((v) => v.id)).toEqual([CONTEXT, WHOLE, BOARD, flowView("f1")]);
    // context: system + 2 actors · whole: 5 · board: the design frame + 1 ladder
    expect(views.map((v) => v.count)).toEqual([3, 5, 2, 3]);
  });

  it("skips a flow with no steps", () => {
    const arch = state({ flows: [flow("empty", [])] });
    expect(listViews(arch).map((v) => v.id)).not.toContain(flowView("empty"));
  });

  it("does not offer a board with nothing to sit beside the design", () => {
    // a board of one frame is the frame
    expect(listViews(state({ flows: [] })).map((v) => v.id)).not.toContain(BOARD);
  });

  it("offers nothing before anything is promoted", () => {
    expect(listViews(state({ components: {} }))).toEqual([]);
    expect(listViews(null)).toEqual([]);
  });
});

describe("resolveView", () => {
  it("falls back to the whole design when the view's flow is gone", () => {
    // the case that matters: you are reading a flow and the architect renames it
    expect(resolveView(state(), flowView("f1"))).toBe(flowView("f1"));
    expect(resolveView(state({ flows: [] }), flowView("f1"))).toBe(WHOLE);
    expect(resolveView(state(), null)).toBe(WHOLE);
  });
});
