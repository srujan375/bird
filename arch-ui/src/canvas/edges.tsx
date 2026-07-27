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

export const edgeTypes = { feedback: FeedbackEdge };
