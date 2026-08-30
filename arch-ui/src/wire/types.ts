/**
 * The wire contract, mirrored from src/bird/harnesses/arch/state.py and serve.py.
 *
 * `ArchState` is replaced wholesale by every `arch_state` event — the harness
 * is the single source of truth for everything in here. Nothing the browser
 * owns belongs in this file.
 */

export type Depth = "stub" | "sketch" | "detailed";
export type Status = "active" | "greyed";
export type EdgeKind = "sync" | "async" | "batch";

export interface WireNode {
  id: string;
  label: string;
  kind: string;
  responsibility: string;
  tech: string;
  depth: Depth;
  detail: string;
  /** the kind's fixed facts, key -> value (see board/vocab.ts) */
  facts: Record<string, string>;
  /** the box's list: endpoints, tables, topics, tools */
  items: WireItem[];
  /** approach ids this box belongs to; empty = shared by all of them */
  approaches: string[];
  status: Status;
  notes: string;
  existing: boolean;
  /** id of the box this one sits inside; "" = top level */
  parent: string;
  /** where somebody put it; null = never arranged, lay it out */
  x: number | null;
  y: number | null;
}

export interface WireItem { k: string; v: string; d: string }

export interface WireEdge {
  src: string;
  dst: string;
  label: string;
  kind: EdgeKind;
  notes: string;
}

export interface Approach {
  id: string;
  name: string;
  summary: string;
  status: Status;
  rejected_reason: string;
}

export interface Option { name: string; pros: string[]; cons: string[] }

export interface Decision {
  id: string;
  topic: string;
  options: Option[];
  choice: string;
  rationale: string;
  source: "model" | "user";
  pragmatism_note: string;
}

export interface Question {
  id: string;
  question: string;
  recommendation: string;
  answer: string;
  status: "open" | "answered" | "deferred";
}

export interface Annotation {
  id: string;
  text: string;
  x: number;
  y: number;
  w: number;
  /** the box it hangs off, or "" for a note on the canvas itself */
  anchor: string;
}

export interface Brief {
  goal: string;
  actors: string[];
  scale: string;
  constraints: string[];
  non_goals: string[];
}

export interface ArchState {
  brief: Brief;
  nodes: Record<string, WireNode>;
  edges: WireEdge[];
  approaches: Record<string, Approach>;
  decisions: Decision[];
  questions: Question[];
  annotations: Annotation[];
  handed_off: boolean;
}

// ---------- events ----------

export interface ReadyEvent {
  type: "ready";
  model: string;
  kg: boolean;
  kg_ready: boolean;
  context_window?: number;
  input_tokens?: number;
  output_tokens?: number;
  run_id: string;
  repo: string;
  skills: { name: string; description: string; source: string }[];
}

export interface ArchStateEvent {
  type: "arch_state";
  /** open until the design is handed off; the only lifecycle this harness has */
  status: "open" | "handed_off";
  state: ArchState;
  renders: { board: string };
  /** what the harness noticed — advisory, never a task list */
  noticing: string[];
  changed: { kind: string; id: string } | null;
  /** how much the user has drawn that the architect has not been shown yet */
  pending_edits?: number;
  /** set only on the copy replayed to a late joiner: this design was already
   *  here when the page opened, so none of it is an arrival */
  replayed?: boolean;
}

export interface ToolCall { name: string; arguments_json: string }

export type HarnessEvent = {
  type: "harness_event";
  event: string;
  data: {
    task?: string;
    text?: string;
    content?: string;
    tool_calls?: ToolCall[];
    name?: string;
    is_error?: boolean;
    reason?: string;
    turn?: number;
    input_tokens?: number;
    output_tokens?: number;
    details?: Record<string, unknown> | null;
  };
};

export interface TurnEndEvent {
  type: "turn_end";
  status: "done" | "reply" | "interrupted" | "error" | string;
  input_tokens?: number;
  output_tokens?: number;
}

export type Incoming =
  | ReadyEvent
  | ArchStateEvent
  | HarnessEvent
  | TurnEndEvent
  | { type: "error"; message?: string }
  | { type: "bye" };

export type ConnState = "connecting" | "connected" | "disconnected" | "complete";
