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
import type { XY } from "../store/canvas";

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
 * The routed edge: a hop that crosses a column it does not belong to.
 *
 * `api → db` when a queue and a worker sit between them used to be drawn as an
 * arc *over* both of their cards — measured against live positions, re-measured
 * on every drag, and growing taller with each card it had to clear until it
 * read as a bow across the diagram rather than a connection.
 *
 * The layout reserves a lane in every column such an edge crosses, so there is
 * nothing left to clear: `data.points` are the middles of those lanes, and the
 * edge simply runs down them. A Catmull-Rom spline through the points keeps it
 * a single continuous line rather than a chain of segments with corners, and
 * two short stubs at the ends make it leave and arrive horizontally, so the
 * arrowhead still meets the card square-on.
 */
const STUB = 20;

function spline(pts: XY[]): string {
  if (pts.length < 2) return "";
  let d = `M${pts[0].x},${pts[0].y}`;
  for (let i = 0; i < pts.length - 1; i++) {
    // the two neighbours a Catmull-Rom segment needs; the ends borrow themselves
    const p0 = pts[i - 1] ?? pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[i + 2] ?? p2;
    d +=
      ` C${p1.x + (p2.x - p0.x) / 6},${p1.y + (p2.y - p0.y) / 6}` +
      ` ${p2.x - (p3.x - p1.x) / 6},${p2.y - (p3.y - p1.y) / 6}` +
      ` ${p2.x},${p2.y}`;
  }
  return d;
}

/** The middle of the lane run — where a label sits clear of both cards. */
function midpoint(pts: XY[], fallback: XY): XY {
  if (pts.length === 0) return fallback;
  if (pts.length % 2) return pts[(pts.length - 1) / 2];
  const a = pts[pts.length / 2 - 1];
  const b = pts[pts.length / 2];
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export function RoutedEdge({
  sourceX, sourceY, targetX, targetY,
  markerEnd, style, label, data,
  labelStyle, labelShowBg, labelBgStyle, labelBgPadding, labelBgBorderRadius,
  interactionWidth,
}: EdgeProps) {
  const lane = (data?.points as XY[] | undefined) ?? [];
  const mid = midpoint(lane, { x: (sourceX + targetX) / 2, y: (sourceY + targetY) / 2 });
  const path = spline([
    { x: sourceX, y: sourceY },
    { x: sourceX + STUB, y: sourceY },
    ...lane,
    { x: targetX - STUB, y: targetY },
    { x: targetX, y: targetY },
  ]);

  return (
    <BaseEdge
      path={path}
      markerEnd={markerEnd}
      style={style}
      label={label}
      labelX={mid.x}
      labelY={mid.y}
      labelStyle={labelStyle}
      labelShowBg={labelShowBg}
      labelBgStyle={labelBgStyle}
      labelBgPadding={labelBgPadding}
      labelBgBorderRadius={labelBgBorderRadius}
      interactionWidth={interactionWidth}
    />
  );
}

export const edgeTypes = { feedback: FeedbackEdge, routed: RoutedEdge };
