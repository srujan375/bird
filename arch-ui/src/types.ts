/**
 * The wire contract, mirrored from src/mha/harnesses/arch/state.py and serve.py.
 *
 * ArchState is replaced wholesale by every `arch_state` event — the server is
 * the single source of truth for everything in here. Anything the browser owns
 * (node positions, which layer is showing) lives in the canvas store instead
 * and never appears in this file.
 */

// ---------- the loose sketch layer ----------

export type Depth = "stub" | "sketch" | "detailed";
export type VariantStatus = "draft" | "chosen" | "archived";

export interface SketchNode {
  id: string;
  label: string;
  kind: string; // free hint, NOT the strict KINDS enum
  note: string;
  depth: Depth;
  detail: string;
}

export interface SketchLink {
  src: string;
  dst: string;
  label: string;
  kind: string;
  note: string;
}

export interface Variant {
  id: string;
  name: string;
  summary: string;
  nodes: Record<string, SketchNode>;
  links: SketchLink[];
  status: VariantStatus;
  rejected_reason: string;
}

export interface Sketchbook {
  variants: Record<string, Variant>;
  active: string | null;
  notes: string[];
}

// ---------- the design layer ----------

export type Kind =
  | "service" | "api" | "gateway" | "store" | "queue" | "cache"
  | "job" | "ui" | "llm" | "external" | "infra";

export interface Endpoint {
  route: string; method: string; request: string; response: string;
  auth: string; errors: string[]; idempotency: string | null; pagination: string | null;
}
export interface Entity {
  name: string; keys: string; fields: string[]; indexes: string[];
}
export interface QueueMessage {
  name: string; schema: string; ordering: string; delivery: string; dlq_policy: string | null;
}
export interface Module { name: string; purpose: string }
export interface LlmTask {
  name: string; model_tier: string; prompt_contract: string; context_strategy: string;
  fallback: string; guardrails: string; eval_hook: string | null; cost_envelope: string | null;
}
export interface DeployUnit {
  name: string; components: string[]; scaling_policy: string; region: string | null;
}

export type Facet =
  | { facet_kind: "api"; endpoints: Endpoint[] }
  | { facet_kind: "store"; entities: Entity[]; access_patterns: string[]; retention: string | null; migration_risk: string | null }
  | { facet_kind: "queue"; messages: QueueMessage[] }
  | { facet_kind: "service"; interface: string[]; modules: Module[] | null }
  | { facet_kind: "llm"; tasks: LlmTask[] }
  | { facet_kind: "infra"; units: DeployUnit[]; state_locality: string };

export interface Component {
  id: string;
  name: string;
  kind: Kind;
  responsibility: string;
  trace: string[];
  existing: boolean;
  tech: string | null;
  data_owned: string | null;
  failure_notes: string | null;
  facet: Facet | null;
  origin: string; // "sketch:<variant>:<node>" when seeded by promote
}

export interface Connection {
  src: string; dst: string; label: string;
  kind: "sync" | "async" | "batch";
  mechanism: string | null; protocol: string | null;
  data: string | null; failure_mode: string | null;
}

export interface FlowStep { src: string; dst: string; action: string; note: string | null }
export interface Flow { id: string; name: string; kind: "happy" | "failure" | "background"; steps: FlowStep[] }

export interface Option { name: string; pros: string[]; cons: string[] }
export interface Decision {
  id: string; topic: string; category: string; options: Option[];
  choice: string; rationale: string; status: "decided" | "deferred";
}

export interface OpenQuestion {
  id: string; question: string; blocking: boolean; source: string;
  answer: string | null; resolution: string | null;
}

/** An objection on the record — the harness's memory of disagreement. */
export type Severity = "blocker" | "risk" | "smell";
export interface Concern {
  id: string;
  severity: Severity;
  target: string; // component/decision id, "brief", "user", or free text
  claim: string;
  alternative: string;
  status: "open" | "accepted" | "overruled" | "withdrawn";
  resolution: string;
  source: "model" | "judge" | "harness_audit";
}

export interface Obligation {
  component_id: string; facet: string; reason: string;
  status: "pending" | "done" | "waived";
}
export interface Amendment { turn: number; description: string; structural: boolean }

export interface Scale {
  users: string | null; reads_per_sec: string | null; writes_per_sec: string | null;
  data_volume: string | null; growth: string | null;
}
export interface Brief {
  goal: string; actors: string[]; scope: string; scale: Scale;
  latency: string | null; consistency: string | null; availability: string | null;
  deploy_target: string | null; constraints: string[]; non_goals: string[];
}

export type Phase =
  | "brainstorm" | "propose" | "toplevel_review"
  | "expand" | "resolved" | "finalized";

export interface ArchState {
  mode: string;
  phase: Phase;
  brief: Brief;
  sketchbook: Sketchbook;
  components: Record<string, Component>;
  connections: Connection[];
  flows: Flow[];
  decisions: Decision[];
  questions: OpenQuestion[];
  concerns: Concern[];
  obligations: Obligation[];
  amendments: Amendment[];
}

export interface Renders {
  toplevel: string;
  flows: Record<string, string>;
  facets: Record<string, { kind: string; mermaid?: string }>;
  sketches: Record<string, string>;
  active_sketch: string | null;
}

// ---------- events ----------

export interface ReadyEvent {
  type: "ready";
  model: string; kg: boolean; kg_ready: boolean;
  run_id: string; repo: string;
  skills: { name: string; description: string; source: string }[];
}

export interface ArchStateEvent {
  type: "arch_state";
  phase: Phase;
  state: ArchState;
  renders: Renders;
  tracker: string;
  /** Thinness keyed by subject: "api", "api->db", "d1". Advisory. */
  gaps: Record<string, string[]>;
  changed: { kind: string; id: string } | null;
}

export interface ToolCall { name: string; arguments_json: string }

export type HarnessEvent = {
  type: "harness_event";
  event: string;
  data: {
    task?: string; text?: string; content?: string;
    tool_calls?: ToolCall[]; name?: string; is_error?: boolean; reason?: string;
  };
};

/** Both gates. The extra fields are what the overhaul sends along so the user
 *  rules with the objections and gaps in front of them. */
export interface PermissionEvent {
  type: "permission_request";
  id: number;
  kind: "toplevel_approval" | "finalize";
  summary: string;
  artifacts?: string[];
  thin?: string[];
  gaps?: string[];
  concerns?: Concern[];
  blockers?: Concern[];
  questions?: string[];
  obligations?: string[];
}

export interface TurnEndEvent {
  type: "turn_end";
  status: "done" | "reply" | "interrupted" | "error" | string;
}

export type WireEvent =
  | ReadyEvent
  | ArchStateEvent
  | HarnessEvent
  | PermissionEvent
  | TurnEndEvent
  | { type: "error"; message?: string }
  | { type: "bye" };

// ---------- local view models ----------

export type ConnState = "connecting" | "connected" | "disconnected" | "complete";

export type TranscriptItem =
  | { t: "user"; text: string }
  | { t: "agent"; text: string }
  | { t: "notice"; text: string; err?: boolean }
  | { t: "turn"; status: string; message: string | null };

/** Which surface the canvas is drawing. */
export type Layer = "sketch" | "design";
