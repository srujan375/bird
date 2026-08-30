/**
 * What a box of each kind carries — mirrored from KIND_FACTS / KIND_LIST in
 * src/bird/harnesses/arch/state.py. The harness validates against its copy;
 * this one only decides the order facts are drawn in, what the list is called,
 * and what the wire-derived footer calls each side.
 */
export const KIND_FACTS: Record<string, string[]> = {
  service: ["interface", "state", "scaling", "owner"],
  api: ["protocol", "auth", "versioning", "owner"],
  store: ["engine", "model", "durability", "retention"],
  queue: ["broker", "delivery", "ordering", "dlq"],
  ui: ["surface", "auth", "owner"],
  llm: ["provider", "model", "role", "context", "fallback"],
  external: ["vendor", "protocol", "sla"],
  infra: ["platform", "region", "scaling"],
  group: [],
};

export const KIND_LIST: Record<string, string> = {
  service: "operations", api: "endpoints", store: "entities", queue: "topics",
  ui: "screens", llm: "tools", external: "calls", infra: "resources", group: "members",
};

/** What the footer calls the boxes on each side of this kind's wires:
 *  [incoming, outgoing]. */
export const KIND_SIDES: Record<string, [string, string]> = {
  service: ["fed by", "feeds"],
  api: ["consumers", "calls"],
  store: ["writers", "reads"],
  queue: ["producers", "consumers"],
  ui: ["opened by", "talks to"],
  llm: ["fed by", "feeds"],
  external: ["used by", "calls"],
  infra: ["hosts", "depends on"],
  group: ["fed by", "feeds"],
};

/** The kinds a box can be, in the order the kind menu shows them. */
export const KINDS = ["service", "store", "queue", "api", "ui", "llm", "external", "infra"];

/** How many list rows a box shows before folding the rest into "+ N more". */
export const ROWS_SHOWN = 6;
