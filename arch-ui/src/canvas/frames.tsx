/**
 * The two things board mode draws that no other view does.
 *
 * Both are inert: not selectable, not draggable, no handles. A frame is a label
 * around a region and a ladder is a read of a flow — neither is a thing you
 * edit, and letting either take a click would put a selection on something the
 * harness has never heard of.
 *
 * Inert is not the same as static, though, and these two were: they had no
 * entrance at all, so switching to the board dropped the frame and every ladder
 * onto the canvas in one frame. They take the same arrival the cards do, and
 * the same burst queue orders them — the frame mounts first, so the region is
 * drawn before the flows read out of it.
 */
import type { NodeProps } from "@xyflow/react";
import type { Rung } from "../board";
import type { Component, Flow } from "../types";
import { useEntrance } from "./nodes";

export interface FrameNodeData extends Record<string, unknown> {
  label: string;
  note: string;
  w: number;
  h: number;
}

export function FrameNode({ data }: NodeProps) {
  const { label, note, w, h } = data as FrameNodeData;
  // never born: the board is entered, and entering it is the arrival
  const inn = useEntrance(false);
  return (
    <div className="frame" data-in={inn} style={{ width: w, height: h }}>
      <div className="frame-title">
        {label}
        <span className="frame-note">{note}</span>
      </div>
    </div>
  );
}

export interface LaneNodeData extends Record<string, unknown> {
  flow: Flow;
  rungs: Rung[];
  components: Record<string, Component>;
  w: number;
  h: number;
}

/**
 * A flow, drawn as a sequence rather than as a second component diagram.
 *
 * This is what makes the board honest. The same component appears in the design
 * frame and in every ladder that touches it, and that is fine precisely because
 * a chip in a ladder is visibly a different kind of claim from a card in the
 * design — one says "the request goes here next", the other says "this exists".
 * Draw the ladder as boxes and arrows like the design and the two blur, and
 * then the reader is entitled to ask which one is true.
 */
export function LaneNode({ data }: NodeProps) {
  const { flow, rungs, components, w, h } = data as LaneNodeData;
  const inn = useEntrance(false);
  return (
    <div className="lane" data-kind={flow.kind} data-in={inn} style={{ width: w, height: h }}>
      <div className="lane-title">
        <span className="dotk" data-kind={flow.kind} />
        {flow.name}
        <span className="spacer" />
        <span className="mono lane-count">{flow.steps.length}</span>
      </div>
      <ol className="lane-rungs">
        {rungs.map((r, i) => (
          <li key={i}>
            {r.via !== null && (
              <div className="lane-step" data-jump={r.jump}>
                <span className="lane-arrow">↓</span>
                <span className="lane-action" title={r.via}>{r.via}</span>
              </div>
            )}
            {r.jump && r.via === null && <div className="lane-break">then</div>}
            <div className="lane-chip" data-kind={components[r.id]?.kind}>
              {components[r.id]?.name ?? r.id}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

export const boardNodeTypes = { frame: FrameNode, lane: LaneNode };
