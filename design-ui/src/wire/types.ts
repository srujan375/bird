import type { PickerPayload } from "@arch/board/picker";
import type { HarnessEvent, ReadyEvent, TurnEndEvent } from "@arch/wire/types";

/**
 * The wire contract, mirrored from src/bird/harnesses/design/session.py.
 *
 * `design_state` is replaced wholesale by every push — the harness is the
 * single source of truth for everything in here. Nothing the browser owns
 * belongs in this file.
 */

export type Status = "asking" | "generating" | "ready" | "showcase" | "finalized";

export interface WireVersion {
  id: string;
  /** generated | refined | edited | themed | polished */
  kind: string;
  /** "<actor>: <op>" for an edit, "generated" for the first one */
  note: string;
}

export interface WireArtboard {
  id: string;
  title: string;
  current: string;
  versions: WireVersion[];
  finalized: boolean;
}

/** The critic's latest note on an artboard: which version it judged, whether
 *  it saw a render or a polish, and the fix-list itself. */
export interface WireCritique {
  version: string;
  kind: "render" | "polish" | string;
  text: string;
}

export interface DesignStateEvent {
  type: "design_state";
  status: Status;
  changed: { kind: string; id: string } | null;
  /** artboard id -> the critic's latest note on it */
  critiques?: Record<string, WireCritique>;
  /** the critic's note on the plan, once design_plan has run */
  plan_critique?: string | null;
  /** edits the user made by hand that the designer has not been shown yet;
   *  the server puts them ahead of the next message on its own */
  pending_user_edits?: number;
  /** the brief the session opened with */
  prompt?: string;
  /** the active design system, or null until one is set, and its tokens —
   *  what a draft is drawn in before the harness has injected them */
  theme?: string | null;
  theme_css?: string;
  themes?: string[];
  artboards: WireArtboard[];
  /** artboard id -> the current version's document, bridge included */
  html: Record<string, string>;
  /** the one question on the table, if any */
  ask?: PickerPayload | null;
  /** every question the session has asked, answered ones included */
  intake?: PickerPayload[];
  /** the brief has gone out: answers are history now, not settings */
  intake_locked?: boolean;
  /** the harness's own selection — the last artboard it made or rebuilt */
  selected?: string;
  finalized_artboard?: string;
  /** the showcase phase's elevated artboard — set only while status is
   *  "showcase": the status swaps the view, this names the frame it renders */
  showcase_artboard?: string;
  /** set only on the copy replayed to a late joiner */
  replayed?: boolean;
}

/** The harness asking for a png of an artboard as it renders — the critique
 *  loop's screenshot request. Answered by wire/capture.ts routing it to the
 *  mounted frame and POSTing the bridge's reply back to /capture. */
export interface CaptureRequestEvent {
  type: "capture_request";
  /** the id the answer must carry back */
  id: string;
  artboard: string;
  version?: string;
  /** this side's raster deadline in seconds, derived by the harness from the
   *  document's size and kept strictly inside its own waiter */
  deadline?: number;
}

export type { HarnessEvent, ReadyEvent, TurnEndEvent };

/** A tool-call fragment as serve.py streams it: `index` is the call's slot in
 *  the message, `name` the function as far as it has been announced, `text`
 *  the next piece of its arguments_json. */
export interface ToolCallDelta { index?: number; name?: string; text?: string }

export type Incoming =
  | ReadyEvent
  | DesignStateEvent
  | HarnessEvent
  | TurnEndEvent
  | CaptureRequestEvent
  | { type: "error"; message?: string }
  | { type: "bye" };

export type ConnState = "connecting" | "connected" | "reconnecting" | "disconnected" | "complete";

/** What the bridge inside a frame says about the element that was clicked. */
export interface Selected {
  selector: string;
  tag: string;
  leaf: boolean;
  text: string;
  box: { x: number; y: number; width: number; height: number };
  props: Record<string, string>;
}

/** The bridge's element tree, for the layers panel. */
export interface TreeNode {
  tag: string;
  sel: string;
  id?: string;
  cls?: string;
  /** what the element shows: its words, an image's alt, a field's placeholder */
  text?: string;
  children: TreeNode[];
}

/** One op from dom.py's vocabulary, as the page sends it. */
export interface Op {
  op: "set_text" | "set_style" | "insert" | "delete" | "duplicate" | "move";
  selector: string;
  text?: string;
  props?: Record<string, string>;
  html?: string;
  parent?: string;
  position?: number;
  new_parent?: string;
  index?: number;
}
