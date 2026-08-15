/**
 * Views: which slice of the design the canvas is drawing.
 *
 * One canvas holding every component was never a decision, it was the only
 * thing the state model allowed — `components` is a flat map, so the canvas
 * drew all of it. But a seventeen-component design is not one diagram anybody
 * wants; it is a context diagram, two flow diagrams and a wiring diagram, and
 * the harness already records everything needed to separate them.
 *
 * So this file is projections, not new state. Nothing here is stored, nothing
 * is sent to the server, and every view is a pure function of the same
 * `ArchState` the whole design view draws. Switching views cannot lose work
 * because there is no work in a view.
 */
import type { ArchState, Component, Connection, Flow } from "./types";

/** `design` | `context` | `flow:<id>` */
export type ViewId = string;

export const WHOLE: ViewId = "design";
export const CONTEXT: ViewId = "context";
/**
 * Every diagram at once, packed on one canvas — see board.ts.
 *
 * It draws the design's own nodes, at the design's own keys, so it is the same
 * arrangement seen from further out rather than a second copy of it. That is
 * why it projects exactly like the whole design does.
 */
export const BOARD: ViewId = "board";
export const flowView = (id: string): ViewId => `flow:${id}`;

/**
 * The box a context view rolls the new work up into.
 *
 * A tilde cannot appear in a component id (the harness holds them to
 * kebab-case), so this can never collide with something real — which matters,
 * because the canvas has to know not to offer a facet dialog for it.
 */
export const SYSTEM = "~system";

export interface View {
  id: ViewId;
  label: string;
  kind: "context" | "design" | "flow" | "board";
  /** How many boxes it draws — shown on the switch, so the trade is visible. */
  count: number;
  detail?: string;
}

export interface Projection {
  components: Record<string, Component>;
  connections: Connection[];
  /** Ids drawn but not real: no internals to open, no concerns of their own. */
  synthetic: Set<string>;
}

/** Everything the design is built *against* rather than by: what a context
 *  diagram keeps as its own box while the rest rolls up. */
const isActor = (c: Component) => c.existing || c.kind === "external";

/** How much of a goal fits on a card before it stops being a name. */
const LABEL_MAX = 46;

/**
 * A goal is a sentence; a card wants a label.
 *
 * `brief.goal` is prose — "Enable selected tasks to be assigned to sub-agents
 * that complete in parallel, with isolated workspaces and results merged back
 * into the main session" — and setting that as a name gives the context view
 * one box five lines tall. Cut at a word, not mid-syllable; the full text is
 * still in the title bar above the canvas.
 */
export function goalLabel(goal: string): string {
  const flat = goal.replace(/\s+/g, " ").trim();
  if (!flat) return "The new design";
  if (flat.length <= LABEL_MAX) return flat;
  const cut = flat.slice(0, LABEL_MAX);
  const space = cut.lastIndexOf(" ");
  return `${(space > LABEL_MAX * 0.5 ? cut.slice(0, space) : cut).replace(/[,;:.]$/, "")}\u2026`;
}

function contextRollup(arch: ArchState): Projection | null {
  const all = Object.values(arch.components);
  const actors = all.filter(isActor);
  const inner = all.filter((c) => !isActor(c));
  // Nothing to roll up, or nothing to roll up *against*: the context view would
  // be the whole design with an extra box on it, so it does not get offered.
  if (inner.length < 2 || actors.length === 0) return null;

  const system: Component = {
    id: SYSTEM,
    name: goalLabel(arch.brief.goal ?? ""),
    // the one client-side kind; the band paints it neutral, which is what a
    // box standing for a whole design should read as
    kind: "system",
    responsibility:
      `${inner.length} components and ${arch.connections.length} connections, ` +
      "drawn whole in the design view.",
    trace: [],
    existing: false,
    tech: null,
    data_owned: null,
    failure_notes: null,
    facet: null,
    origin: "",
  };

  const components: Record<string, Component> = { [SYSTEM]: system };
  for (const a of actors) components[a.id] = a;

  const at = (id: string) => (components[id] ? id : SYSTEM);
  const merged = new Map<string, Connection[]>();
  for (const conn of arch.connections) {
    const src = at(conn.src);
    const dst = at(conn.dst);
    if (src === dst) continue; // internal wiring: not what this view is about
    const key = `${src}->${dst}`;
    if (!merged.has(key)) merged.set(key, []);
    merged.get(key)!.push(conn);
  }

  const connections = [...merged].map(([key, group]) => {
    const [src, dst] = key.split("->");
    const one = group.length === 1;
    return {
      ...group[0],
      src,
      dst,
      // a rolled edge that borrowed one connection's label would be a lie about
      // the other four
      label: one ? group[0].label : `${group.length} connections`,
      kind: group.every((c) => c.kind === group[0].kind) ? group[0].kind : "sync",
    } as Connection;
  });

  return { components, connections, synthetic: new Set([SYSTEM]) };
}

function flowProjection(arch: ArchState, flow: Flow): Projection {
  const components: Record<string, Component> = {};
  for (const step of flow.steps) {
    for (const ref of [step.src, step.dst]) {
      const c = arch.components[ref];
      if (c) components[ref] = c;
    }
  }

  // The real connection where there is one — it carries the mechanism, the
  // protocol and the failure mode, and a flow view that dropped those would be
  // a prettier diagram about less. The step's action is the fallback.
  const real = new Map(arch.connections.map((c) => [`${c.src}->${c.dst}`, c]));
  const seen = new Map<string, Connection>();
  for (const step of flow.steps) {
    if (!components[step.src] || !components[step.dst]) continue;
    const key = `${step.src}->${step.dst}`;
    if (seen.has(key)) continue;
    const found = real.get(key);
    seen.set(
      key,
      found ?? {
        src: step.src,
        dst: step.dst,
        label: step.action,
        kind: "sync",
        mechanism: null,
        protocol: null,
        data: null,
        failure_mode: step.note,
      },
    );
  }

  return { components, connections: [...seen.values()], synthetic: new Set() };
}

const whole = (arch: ArchState): Projection => ({
  components: arch.components,
  connections: arch.connections,
  synthetic: new Set(),
});

/** What the canvas draws for `view`. Falls back to the whole design rather
 *  than to nothing: a view can go away mid-session when its flow is renamed. */
export function projectView(arch: ArchState | null, view: ViewId): Projection {
  if (!arch) return { components: {}, connections: [], synthetic: new Set() };
  if (view === CONTEXT) return contextRollup(arch) ?? whole(arch);
  // The board frames the design rather than re-drawing it; board.ts adds the
  // frame and the ladders on top of exactly these nodes.
  if (view === BOARD) return whole(arch);
  if (view.startsWith("flow:")) {
    const flow = arch.flows.find((f) => f.id === view.slice("flow:".length));
    if (flow && flow.steps.length) return flowProjection(arch, flow);
  }
  return whole(arch);
}

const FLOW_LABEL: Record<Flow["kind"], string> = {
  happy: "happy path",
  failure: "failure",
  background: "background",
};

/**
 * The views worth offering, widest first.
 *
 * A view that draws the same picture as the one next to it is not offered at
 * all — a switch with three buttons that do the same thing teaches the reader
 * that the switch does nothing.
 */
export function listViews(arch: ArchState | null): View[] {
  if (!arch) return [];
  const all = Object.keys(arch.components).length;
  if (all === 0) return [];

  const out: View[] = [];
  const ctx = contextRollup(arch);
  if (ctx) {
    out.push({
      id: CONTEXT,
      label: "Context",
      kind: "context",
      count: Object.keys(ctx.components).length,
      detail: "the design as one box, beside what it talks to",
    });
  }
  out.push({ id: WHOLE, label: "Whole design", kind: "design", count: all });
  // A board of one frame is the frame. It only earns the switch once there is
  // something to sit beside the design.
  const laneable = arch.flows.filter((f) => f.steps.length).length;
  if (laneable) {
    out.push({
      id: BOARD,
      label: "Board",
      kind: "board",
      count: 1 + laneable,
      detail: "the design and every flow at once, for reading and handing off",
    });
  }
  for (const flow of arch.flows) {
    if (!flow.steps.length) continue;
    const on = new Set<string>();
    for (const s of flow.steps) {
      if (arch.components[s.src]) on.add(s.src);
      if (arch.components[s.dst]) on.add(s.dst);
    }
    if (!on.size) continue;
    out.push({
      id: flowView(flow.id),
      label: flow.name,
      kind: "flow",
      count: on.size,
      detail: `${FLOW_LABEL[flow.kind]} · ${flow.steps.length} steps`,
    });
  }
  return out;
}

/** A view id is only usable while the thing it names still exists. */
export function resolveView(arch: ArchState | null, wanted: ViewId | null): ViewId {
  if (!wanted) return WHOLE;
  return listViews(arch).some((v) => v.id === wanted) ? wanted : WHOLE;
}
