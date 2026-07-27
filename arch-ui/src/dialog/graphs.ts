/**
 * Facet → sub-diagram. Pure: state in, React Flow nodes and edges out.
 *
 * The rule these follow is the same one the harness follows — draw what is
 * recorded, never what would look good. Where the schema has no edges (service
 * modules carry a name and a purpose and nothing about what calls what), the
 * cards are laid out and left unconnected rather than wired into an invented
 * chain. The one derived edge in here, ER relations, says so on the label.
 */
import type { Edge, Node } from "@xyflow/react";
import type { Component, Connection, DeployUnit, Entity, Facet, LlmTask, Module, QueueMessage } from "../types";

const GAP_X = 64;
const GAP_Y = 34;
// ER relations carry a label, and a label that lands under a card is worse
// than no label at all — entities get their own, wider gutter
const ENTITY_GAP_X = 130;

export interface SubGraph {
  nodes: Node[];
  edges: Edge[];
  /** Shown under the canvas when a sub-diagram is deliberately edgeless. */
  note?: string;
}

const EMPTY: SubGraph = { nodes: [], edges: [] };

export function facetGraph(
  comp: Component,
  components: Record<string, Component>,
): SubGraph {
  const f: Facet | null = comp.facet;
  if (!f) return EMPTY;
  switch (f.facet_kind) {
    case "store": return storeGraph(f.entities);
    case "service": return serviceGraph(f.modules ?? []);
    case "queue": return queueGraph(f.messages);
    case "infra": return infraGraph(f.units, components);
    case "llm": return llmGraph(f.tasks);
    default: return EMPTY; // api is tabular — see the dialog body
  }
}

// ---------------------------------------------------------------- store (ER)

const ENTITY_W = 216;

function entityHeight(fields: number, indexes: number): number {
  return 52 + Math.max(fields, 1) * 16 + (indexes ? 26 : 0);
}

/** `user_id` on an entity, when an entity called `user`/`users` exists. */
function referencedEntity(field: string, names: Map<string, string>): string | null {
  const m = /^(.+?)[_-]?(ids?)$/i.exec(field.trim());
  if (!m) return null;
  const key = normalize(m[1]);
  return key ? names.get(key) ?? null : null;
}

function normalize(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]/g, "").replace(/s$/, "");
}

function storeGraph(entities: Entity[]): SubGraph {
  if (entities.length === 0) return EMPTY;
  const names = new Map(entities.map((e) => [normalize(e.name), e.name]));
  const perRow = entities.length > 4 ? 3 : 2;
  const rowHeights: number[] = [];
  entities.forEach((e, i) => {
    const row = Math.floor(i / perRow);
    const h = entityHeight(e.fields.length, e.indexes.length);
    rowHeights[row] = Math.max(rowHeights[row] ?? 0, h);
  });
  const rowTop = rowHeights.map((_, i) =>
    rowHeights.slice(0, i).reduce((a, h) => a + h + GAP_Y, 0));

  const nodes: Node[] = entities.map((e, i) => ({
    id: `entity:${e.name}`,
    type: "entity",
    position: { x: (i % perRow) * (ENTITY_W + ENTITY_GAP_X), y: rowTop[Math.floor(i / perRow)] },
    data: { entity: e },
  }));

  const edges: Edge[] = [];
  for (const e of entities) {
    for (const field of e.fields) {
      const target = referencedEntity(field, names);
      if (!target || target === e.name) continue;
      edges.push({
        id: `rel:${e.name}:${field}`,
        source: `entity:${e.name}`,
        target: `entity:${target}`,
        label: `${field} ·  inferred`,
        labelShowBg: true,
        style: { strokeDasharray: "4 4", strokeWidth: 1.2, stroke: "var(--hairline)" },
      });
    }
  }
  return {
    nodes,
    edges,
    note: edges.length
      ? "relations are inferred from field names — the schema records entities, not foreign keys"
      : undefined,
  };
}

// ------------------------------------------------------------- service

const MODULE_W = 200;

function serviceGraph(modules: Module[]): SubGraph {
  if (modules.length === 0) return EMPTY;
  const perRow = 3;
  const nodes: Node[] = modules.map((m, i) => ({
    id: `module:${m.name}`,
    type: "module",
    position: { x: (i % perRow) * (MODULE_W + GAP_X), y: Math.floor(i / perRow) * (96 + GAP_Y) },
    data: { module: m },
  }));
  return {
    nodes,
    edges: [],
    note: "a module records a name and a purpose — nothing says what calls what, so nothing is drawn between them",
  };
}

// --------------------------------------------------------------- queue

const MESSAGE_W = 250;

function queueGraph(messages: QueueMessage[]): SubGraph {
  if (messages.length === 0) return EMPTY;
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  let y = 0;
  for (const m of messages) {
    const id = `msg:${m.name}`;
    nodes.push({ id, type: "message", position: { x: 0, y }, data: { message: m } });
    if (m.dlq_policy) {
      const dlq = `dlq:${m.name}`;
      nodes.push({
        id: dlq, type: "dlq",
        position: { x: MESSAGE_W + GAP_X, y: y + 12 },
        data: { policy: m.dlq_policy },
      });
      edges.push({
        id: `to-${dlq}`, source: id, target: dlq, label: "on failure",
        labelShowBg: true,
        style: { strokeDasharray: "5 4", strokeWidth: 1.2, stroke: "var(--danger)" },
      });
    }
    y += 132 + GAP_Y;
  }
  const undlq = messages.filter((m) => !m.dlq_policy).length;
  return {
    nodes,
    edges,
    note: undlq
      ? `${undlq} of ${messages.length} messages have no dead-letter policy recorded`
      : undefined,
  };
}

// --------------------------------------------------------------- infra

const UNIT_W = 250;

function infraGraph(units: DeployUnit[], components: Record<string, Component>): SubGraph {
  if (units.length === 0) return EMPTY;
  const perRow = 2;
  const nodes: Node[] = units.map((u, i) => ({
    id: `unit:${u.name}`,
    type: "unit",
    position: { x: (i % perRow) * (UNIT_W + GAP_X), y: Math.floor(i / perRow) * (150 + GAP_Y) },
    data: { unit: u },
  }));
  const unknown = units
    .flatMap((u) => u.components)
    .filter((cid) => !(cid in components));
  return {
    nodes,
    edges: [],
    note: unknown.length ? `hosts ids not in the design: ${unknown.join(", ")}` : undefined,
  };
}

// ----------------------------------------------------------------- llm

const STEP_W = 180;
const CHAIN: { key: "prompt_contract" | "context_strategy" | "guardrails" | "fallback"; label: string }[] = [
  { key: "prompt_contract", label: "prompt" },
  { key: "context_strategy", label: "context" },
  { key: "guardrails", label: "guardrails" },
  { key: "fallback", label: "fallback" },
];

function llmGraph(tasks: LlmTask[]): SubGraph {
  if (tasks.length === 0) return EMPTY;
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  tasks.forEach((t, ti) => {
    const y = ti * (128 + GAP_Y * 2);
    nodes.push({
      id: `task:${t.name}`, type: "task",
      position: { x: 0, y: y + 18 }, data: { task: t },
    });
    let prev = `task:${t.name}`;
    CHAIN.forEach((step, si) => {
      const id = `step:${t.name}:${step.key}`;
      nodes.push({
        id, type: "step",
        position: { x: (si + 1) * (STEP_W + GAP_X) + 24, y },
        data: { label: step.label, text: t[step.key] },
      });
      // the fallback hangs off guardrails as an escape hatch, not a next step
      const escape = step.key === "fallback";
      edges.push({
        id: `chain:${t.name}:${si}`,
        source: prev,
        target: id,
        label: escape ? "when it fails" : undefined,
        labelShowBg: escape,
        style: escape
          ? { strokeWidth: 1.2, strokeDasharray: "5 4", stroke: "var(--changed)" }
          : { strokeWidth: 1.2, stroke: "var(--hairline)" },
      });
      prev = id;
    });
  });
  return { nodes, edges };
}

// --------------------------------------------------------------- ports

export interface Port { id: string; name: string; label: string; kind: string }

/** The real inbound/outbound neighbours, so internals stay anchored to the
 *  system graph the dialog is floating over. */
export function ports(
  comp: Component,
  connections: Connection[],
  components: Record<string, Component>,
): { inbound: Port[]; outbound: Port[] } {
  const name = (id: string) => components[id]?.name ?? id;
  return {
    inbound: connections
      .filter((c) => c.dst === comp.id)
      .map((c) => ({ id: c.src, name: name(c.src), label: c.label, kind: c.kind })),
    outbound: connections
      .filter((c) => c.src === comp.id)
      .map((c) => ({ id: c.dst, name: name(c.dst), label: c.label, kind: c.kind })),
  };
}
