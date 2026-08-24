/** The board as the canvas draws it.
 *
 *  This is a *view* of the harness's graph, not a second copy of it. Everything
 *  here is derived by `adapter.ts` from an `arch_state` push, except the
 *  arrangement, which the harness stores only once somebody has arranged it.
 */

export type Depth = "stub" | "sketch" | "detailed";
export type LaneKey = string;
export type Tool = "select" | "node" | "note";

export interface Lane {
  /** the approach id (or the shared spine) — what groups the boxes */
  k: LaneKey;
  /** which step of the stylesheet's territory ramp this column wears: "s" for
   *  the shared spine, "1".."5" for the approaches around it. A slot, not an
   *  identity — the tints are there to tell two columns apart, not to mean
   *  anything on their own. */
  slot: string;
  name: string;
  note: string;
  x: number;
  y: number;
  w: number;
  h: number;
  /** on the record as not taken */
  out?: boolean;
  /** the one that won */
  taken?: boolean;
}

export interface BoardNode {
  id: string;
  lane: LaneKey;
  /** its lane's tint slot, so a box picks up its column's edge colour */
  slot: string;
  /** centre-x; the box is laid out around it so widening on deepen stays put */
  cx: number;
  y: number;
  kind: string;
  depth: Depth;
  label: string;
  resp: string;
  tech: string;
  rows: string[];
  approaches: string[];
  existing: boolean;
  out?: boolean;
}

export interface Wire {
  from: string;
  to: string;
  label?: string;
  kind?: string;
  notes?: string;
  out?: boolean;
}

export interface Anno {
  id: string;
  x: number;
  y: number;
  w: number;
  text: string;
  /** the box it hangs off, or "" for a note on the canvas itself */
  anchor?: string;
}

export type Selection = { t: "node" | "anno"; id: string };

export interface Attachment {
  id: string;
  name: string;
  size: number;
  img: boolean;
  url: string | null;
}
