/**
 * Facet → sub-diagram.
 *
 * The tests that matter most here are the ones asserting a *missing* edge: the
 * page must draw what the harness recorded and nothing else. An invented data
 * flow between two modules would read exactly like a designed one.
 */
import { describe, expect, it } from "vitest";
import { facetGraph, ports } from "./graphs";
import type { Component, Connection, Facet } from "../types";

function comp(id: string, kind: Component["kind"], facet: Facet | null = null): Component {
  return {
    id, name: id, kind, responsibility: "does a thing", trace: [],
    existing: false, tech: null, data_owned: null, failure_notes: null,
    facet, origin: "",
  };
}

const ids = (g: { nodes: { id: string }[] }) => g.nodes.map((n) => n.id).sort();

describe("store → ER canvas", () => {
  const store = comp("db", "store", {
    facet_kind: "store",
    entities: [
      { name: "order", keys: "id", fields: ["id", "user_id", "status"], indexes: ["user_id"] },
      { name: "user", keys: "id", fields: ["id", "email"], indexes: [] },
      { name: "audit", keys: "id", fields: ["id", "blob"], indexes: [] },
    ],
    access_patterns: [], retention: null, migration_risk: null,
  });

  it("draws a card per entity", () => {
    expect(ids(facetGraph(store, {}))).toEqual(["entity:audit", "entity:order", "entity:user"]);
  });

  it("infers a relation from a *_id field, and says that it inferred it", () => {
    const g = facetGraph(store, {});
    expect(g.edges).toHaveLength(1);
    expect(g.edges[0]).toMatchObject({ source: "entity:order", target: "entity:user" });
    expect(String(g.edges[0].label)).toContain("inferred");
    expect(g.note).toContain("inferred from field names");
  });

  it("does not invent a relation for a bare id, or for a name nothing matches", () => {
    const lonely = comp("db", "store", {
      facet_kind: "store",
      entities: [{ name: "order", keys: "id", fields: ["id", "tenant_id"], indexes: [] }],
      access_patterns: [], retention: null, migration_risk: null,
    });
    const g = facetGraph(lonely, {});
    expect(g.edges).toEqual([]);
    expect(g.note).toBeUndefined();
  });

  it("does not point an entity at itself", () => {
    const selfish = comp("db", "store", {
      facet_kind: "store",
      entities: [{ name: "orders", keys: "id", fields: ["order_id"], indexes: [] }],
      access_patterns: [], retention: null, migration_risk: null,
    });
    expect(facetGraph(selfish, {}).edges).toEqual([]);
  });
});

describe("service → modules", () => {
  const svc = comp("api", "service", {
    facet_kind: "service",
    interface: ["createOrder()"],
    modules: [
      { name: "intake", purpose: "validates" },
      { name: "persistence", purpose: "writes" },
    ],
  });

  it("draws the modules and NOTHING between them", () => {
    const g = facetGraph(svc, {});
    expect(ids(g)).toEqual(["module:intake", "module:persistence"]);
    // a Module is a name and a purpose; nothing in the schema says what calls
    // what, and a drawn arrow would be a claim the harness never made
    expect(g.edges).toEqual([]);
    expect(g.note).toContain("nothing is drawn between them");
  });

  it("is empty when the facet records no modules", () => {
    const bare = comp("api", "service", {
      facet_kind: "service", interface: ["x()"], modules: null,
    });
    expect(facetGraph(bare, {}).nodes).toEqual([]);
  });
});

describe("queue → messages", () => {
  const bus = comp("bus", "queue", {
    facet_kind: "queue",
    messages: [
      { name: "OrderPlaced", schema: "{id}", ordering: "per id",
        delivery: "at-least-once", dlq_policy: "5 tries then orders.dlq" },
      { name: "Ignored", schema: "{}", ordering: "none",
        delivery: "at-most-once", dlq_policy: null },
    ],
  });

  it("hangs a dead-letter card off the message that has one", () => {
    const g = facetGraph(bus, {});
    expect(ids(g)).toEqual(["dlq:OrderPlaced", "msg:Ignored", "msg:OrderPlaced"]);
    expect(g.edges).toHaveLength(1);
    expect(g.edges[0]).toMatchObject({ source: "msg:OrderPlaced", target: "dlq:OrderPlaced" });
  });

  it("counts the messages with no dead-letter policy", () => {
    expect(facetGraph(bus, {}).note).toContain("1 of 2");
  });
});

describe("llm → task chain", () => {
  const llm = comp("pricing", "llm", {
    facet_kind: "llm",
    tasks: [{
      name: "explain", model_tier: "small", prompt_contract: "cart -> sentence",
      context_strategy: "the deltas only", fallback: "itemised diff",
      guardrails: "no promises", eval_hook: null, cost_envelope: null,
    }],
  });

  it("chains prompt → context → guardrails → fallback off the task", () => {
    const g = facetGraph(llm, {});
    expect(ids(g)).toEqual([
      "step:explain:context_strategy", "step:explain:fallback",
      "step:explain:guardrails", "step:explain:prompt_contract", "task:explain",
    ]);
    const links = g.edges.map((e) => `${e.source}->${e.target}`);
    expect(links).toEqual([
      "task:explain->step:explain:prompt_contract",
      "step:explain:prompt_contract->step:explain:context_strategy",
      "step:explain:context_strategy->step:explain:guardrails",
      "step:explain:guardrails->step:explain:fallback",
    ]);
    // the fallback is an escape hatch, not the next step
    expect(String(g.edges[3].label)).toBe("when it fails");
  });
});

describe("infra → deployment frames", () => {
  it("flags unit members that are not components in the design", () => {
    const infra = comp("cluster", "infra", {
      facet_kind: "infra",
      units: [{ name: "edge", components: ["api", "ghost"], scaling_policy: "2-10", region: null }],
      state_locality: "eu-west-1",
    });
    const g = facetGraph(infra, { api: comp("api", "api") });
    expect(ids(g)).toEqual(["unit:edge"]);
    expect(g.note).toContain("ghost");
  });
});

describe("api and black boxes", () => {
  it("api has no canvas — the endpoint table is the diagram", () => {
    const api = comp("api", "api", {
      facet_kind: "api",
      endpoints: [{ route: "/x", method: "GET", request: "", response: "",
                    auth: "none", errors: [], idempotency: null, pagination: null }],
    });
    expect(facetGraph(api, {})).toEqual({ nodes: [], edges: [] });
  });

  it("a component with no facet draws nothing", () => {
    expect(facetGraph(comp("x", "service"), {})).toEqual({ nodes: [], edges: [] });
  });
});

describe("ports", () => {
  const conn = (src: string, dst: string, label: string): Connection => ({
    src, dst, label, kind: "sync", mechanism: null, protocol: null,
    data: null, failure_mode: null,
  });

  it("names the real neighbours on the side they are on", () => {
    const components = {
      api: comp("api", "api"),
      db: comp("db", "store"),
      bus: comp("bus", "queue"),
    };
    components.api.name = "order-api";
    const wired = ports(components.db, [
      conn("api", "db", "writes"),
      conn("db", "bus", "publishes"),
      conn("api", "bus", "unrelated"),
    ], components);

    expect(wired.inbound).toEqual([
      { id: "api", name: "order-api", label: "writes", kind: "sync" },
    ]);
    expect(wired.outbound).toEqual([
      { id: "bus", name: "bus", label: "publishes", kind: "sync" },
    ]);
  });

  it("falls back to the id when the neighbour is unknown", () => {
    const wired = ports(comp("db", "store"), [conn("ghost", "db", "writes")], {});
    expect(wired.inbound[0].name).toBe("ghost");
  });
});
