/**
 * The feedback edge: a loop drawn under the graph.
 *
 * It needs its own shape. A stock bezier sets its control points from the
 * distance between its two ends, and a loop leaves and re-enters at the same
 * height — distance zero, no curve, a flat line ruled straight through every
 * card in between. An orthogonal route avoids that but re-picks its corners
 * every few pixels of a drag, which is the snapping the curves were meant to be
 * rid of. So: a cubic that dips below both ends by an amount set by how far it
 * has to travel. It always reads as a loop, and it bends rather than jumps.
 */
import { BaseEdge, type EdgeProps } from "@xyflow/react";

/** Deep enough to clear the row it passes under, shallow enough to stay read. */
function dipFor(span: number): number {
  return Math.min(220, Math.max(72, span * 0.16));
}

export function FeedbackEdge({
  sourceX, sourceY, targetX, targetY,
  markerEnd, style, label,
  labelStyle, labelShowBg, labelBgStyle, labelBgPadding, labelBgBorderRadius,
  interactionWidth,
}: EdgeProps) {
  const dip = dipFor(Math.abs(targetX - sourceX));
  const path =
    `M${sourceX},${sourceY} ` +
    `C${sourceX},${sourceY + dip} ${targetX},${targetY + dip} ${targetX},${targetY}`;

  return (
    <BaseEdge
      path={path}
      markerEnd={markerEnd}
      style={style}
      label={label}
      // the curve's own midpoint: a cubic with both handles pulled down by `dip`
      // sits exactly three quarters of the way there at t = 0.5
      labelX={(sourceX + targetX) / 2}
      labelY={(sourceY + targetY) / 2 + dip * 0.75}
      labelStyle={labelStyle}
      labelShowBg={labelShowBg}
      labelBgStyle={labelBgStyle}
      labelBgPadding={labelBgPadding}
      labelBgBorderRadius={labelBgBorderRadius}
      interactionWidth={interactionWidth}
    />
  );
}

/**
 * The skip edge: a forward hop that has to clear the cards it flies over.
 *
 * A stock bezier between two ends on the same row is very nearly a straight
 * line, so a connection that skips a layer — `api → db` when a queue and a
 * worker sit between them — is ruled straight through both of their cards.
 * That reads as three connections where there is one, and it hides whatever
 * text it crosses.
 *
 * Same answer as the feedback edge, mirrored: arc *over* rather than dip under,
 * by however much it takes to clear the tallest card in the way. `data.top` is
 * the y the curve must stay above, measured by the canvas; the control points
 * go higher than that, because a cubic peaks well short of its handles.
 */
export function SkipEdge({
  sourceX, sourceY, targetX, targetY,
  markerEnd, style, label, data,
  labelStyle, labelShowBg, labelBgStyle, labelBgPadding, labelBgBorderRadius,
  interactionWidth,
}: EdgeProps) {
  const top = (data?.top as number | undefined) ?? Math.min(sourceY, targetY);
  // Solve the cubic at t=0.5 for the handle height that puts the *curve* at
  // `top`: B(.5) = (p0 + 3c1 + 3c2 + p3) / 8, with both handles at `handle`.
  const handle = (8 * top - sourceY - targetY) / 6;
  const lift = Math.min(sourceY, targetY) - top;
  // Pull the handles inward as the arc gets tall, so a big clearance bulges
  // upward rather than ballooning sideways across its neighbours.
  const dx = Math.max(24, Math.min(140, Math.abs(targetX - sourceX) * 0.28 - lift * 0.12));
  const path =
    `M${sourceX},${sourceY} ` +
    `C${sourceX + dx},${handle} ${targetX - dx},${handle} ${targetX},${targetY}`;

  return (
    <BaseEdge
      path={path}
      markerEnd={markerEnd}
      style={style}
      label={label}
      labelX={(sourceX + targetX) / 2}
      labelY={(sourceY + targetY + 6 * handle) / 8}
      labelStyle={labelStyle}
      labelShowBg={labelShowBg}
      labelBgStyle={labelBgStyle}
      labelBgPadding={labelBgPadding}
      labelBgBorderRadius={labelBgBorderRadius}
      interactionWidth={interactionWidth}
    />
  );
}

export const edgeTypes = { feedback: FeedbackEdge, skip: SkipEdge };
