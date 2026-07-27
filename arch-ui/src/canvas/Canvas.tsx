/**
 * The canvas. One React Flow instance draws whichever layer is showing.
 *
 * The rules that make it feel stable while an agent mutates state underneath:
 *  - positions come from the overlay store, never from the server;
 *  - a state push only ever *adds* positions for ids it has never seen;
 *  - dragging pins a node, and pinned nodes are immovable for Tidy up.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useStore,
  type Edge,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  DESIGN_SIZE,
  SKETCH_SIZE,
  edgeKey,
  findBackEdges,
  layout,
  relayoutAll,
  type GraphEdge,
  type Size,
} from "../layout";
import { designKey, sketchKey, useCanvas, type XY } from "../store/canvas";
import { useSession } from "../store/session";
import { palette, useTheme, type Palette } from "../theme";
import { edgeTypes } from "./edges";
import {
  HANDLE,
  nodeTypes,
  type ComponentNodeData,
  type SketchNodeData,
} from "./nodes";
import type { ArchState, Concern, Layer, Variant } from "../types";

/** Below this a card's responsibility line stops being legible. */
const READABLE_ZOOM = 0.62;

const EDGE_STYLE: Record<string, { strokeDasharray?: string; strokeWidth: number }> = {
  sync: { strokeWidth: 1.6 },
  async: { strokeDasharray: "6 5", strokeWidth: 1.6 },
  batch: { strokeWidth: 2.6 },
};

/**
 * One edge, drawn so its direction is legible without tracing it.
 *
 * Forward edges run side to side along the layering. A feedback edge takes the
 * bottom anchors instead, so it loops under the diagram rather than cutting a
 * diagonal through the cards between its two ends.
 *
 * Bezier rather than orthogonal: a right-angled route has to pick a corner, and
 * every few pixels of a drag it picks a different one — the line snaps between
 * layouts while the card moves smoothly. A curve just bends.
 */
function wire(stroke: string, feedback: boolean): Partial<Edge> {
  return {
    type: feedback ? "feedback" : "default", // custom arc / bezier
    pathOptions: feedback ? undefined : { curvature: 0.32 },
    sourceHandle: feedback ? HANDLE.loopOut : HANDLE.out,
    targetHandle: feedback ? HANDLE.loopIn : HANDLE.in,
    markerEnd: { type: MarkerType.ArrowClosed, width: 15, height: 15, color: stroke },
    labelShowBg: true,
    labelBgPadding: [6, 3],
    labelBgBorderRadius: 5,
    labelBgStyle: { fill: "var(--canvas)", fillOpacity: 0.94, stroke: "var(--hairline)" },
    labelStyle: { fill: "var(--muted)", fontSize: 11 },
  } as Partial<Edge>;
}

function concernsByTarget(concerns: Concern[]): Map<string, Concern[]> {
  const map = new Map<string, Concern[]>();
  for (const c of concerns) {
    if (c.status !== "open") continue;
    if (!map.has(c.target)) map.set(c.target, []);
    map.get(c.target)!.push(c);
  }
  for (const list of map.values()) {
    list.sort((a, b) => rank(a.severity) - rank(b.severity));
  }
  return map;
}
const rank = (s: string) => (s === "blocker" ? 0 : s === "risk" ? 1 : 2);

/**
 * Cards and edges are built apart from positions on purpose.
 *
 * Dragging one node changes one position, sixty times a second. Rebuilding
 * every card's `data` on each of those frames gives every card a new prop
 * identity and re-renders the whole canvas — and rebuilding the edges re-runs
 * the back-edge search over the entire graph with it. Neither depends on where
 * a card sits, so neither belongs on the drag path.
 */
function designCards(
  arch: ArchState,
  gaps: Record<string, string[]>,
  concerns: Map<string, Concern[]>,
  recentlyChanged: Record<string, number>,
): Record<string, ComponentNodeData> {
  const owed = new Set(arch.obligations.filter((o) => o.status === "pending").map((o) => o.component_id));
  const out: Record<string, ComponentNodeData> = {};
  for (const c of Object.values(arch.components)) {
    out[designKey(c.id)] = {
      component: c,
      gaps: gaps[c.id] ?? [],
      concerns: concerns.get(c.id) ?? [],
      owes: owed.has(c.id),
      changed: (recentlyChanged[c.id] ?? 0) > Date.now(),
    };
  }
  return out;
}

const ORIGIN: XY = { x: 0, y: 0 };

/**
 * Node objects, reusing the ones that did not change.
 *
 * Dragging rebuilds this list on every frame, and a node object with a new
 * identity is a node React Flow re-renders — drag one card and all of them
 * repaint. Handing back the previous object for every card that did not move
 * keeps the repaint to the one under the cursor.
 */
function useNodeObjects(
  cards: Record<string, { [k: string]: unknown }>,
  type: string,
  size: Size,
  positions: Record<string, XY>,
  selected: string | null,
): Node[] {
  const cache = useRef(new Map<string, Node>());
  return useMemo(() => {
    const next = new Map<string, Node>();
    const out = Object.entries(cards).map(([key, data]) => {
      const position = positions[key] ?? ORIGIN;
      const isSelected = selected === key;
      const prev = cache.current.get(key);
      const node =
        prev && prev.data === data && prev.position === position &&
        prev.selected === isSelected && prev.type === type
          ? prev
          : {
              id: key,
              type,
              position,
              // A size hint, not a size. These objects are rebuilt on every
              // state push, which throws away whatever React Flow measured onto
              // the last set — and a node of unknown size is a node the minimap
              // refuses to draw.
              initialWidth: size.w,
              initialHeight: size.h,
              selected: isSelected,
              data,
            };
      next.set(key, node);
      return node;
    });
    cache.current = next;
    return out;
  }, [cards, type, size, positions, selected]);
}

function designEdges(
  arch: ArchState,
  gaps: Record<string, string[]>,
  ink: Palette,
): Edge[] {
  const back = findBackEdges(
    Object.keys(arch.components),
    arch.connections.map((c) => ({ src: c.src, dst: c.dst })),
  );
  return arch.connections.map((conn, i) => {
    const key = `${conn.src}->${conn.dst}`;
    const thin = (gaps[key] ?? []).length > 0;
    const feedback = back.has(edgeKey(conn.src, conn.dst));
    const stroke = thin ? ink.changed : feedback ? ink.edgeBack : ink.edge;
    return {
      id: `${key}:${conn.label}:${i}`,
      source: designKey(conn.src),
      target: designKey(conn.dst),
      label: conn.mechanism ? `${conn.label} via ${conn.mechanism}` : conn.label,
      className: feedback ? "feedback" : undefined,
      ...wire(stroke, feedback),
      style: { ...EDGE_STYLE[conn.kind], stroke },
    };
  });
}

function sketchCards(
  variant: Variant,
  concerns: Map<string, Concern[]>,
  recentlyChanged: Record<string, number>,
): Record<string, SketchNodeData> {
  const out: Record<string, SketchNodeData> = {};
  for (const n of Object.values(variant.nodes)) {
    out[sketchKey(variant.id, n.id)] = {
      node: n,
      concerns: concerns.get(n.id) ?? [],
      changed: (recentlyChanged[n.id] ?? 0) > Date.now(),
    };
  }
  return out;
}

function sketchEdges(variant: Variant, ink: Palette): Edge[] {
  const back = findBackEdges(
    Object.keys(variant.nodes),
    variant.links.map((l) => ({ src: l.src, dst: l.dst })),
  );
  return variant.links.map((l, i) => {
    const feedback = back.has(edgeKey(l.src, l.dst));
    const stroke = feedback ? ink.edgeBack : ink.sketchLine;
    return {
      id: `${variant.id}:${l.src}->${l.dst}:${i}`,
      source: sketchKey(variant.id, l.src),
      target: sketchKey(variant.id, l.dst),
      label: l.label || undefined,
      className: feedback ? "feedback" : undefined,
      ...wire(stroke, feedback),
      style: { ...EDGE_STYLE[l.kind] ?? EDGE_STYLE.sync, stroke },
    };
  });
}

interface Props {
  layer: Layer;
  variant: Variant | null;
}

function CanvasInner({ layer, variant }: Props) {
  const arch = useSession((s) => s.arch);
  const gapsBySubject = useSession((s) => s.gaps);
  const recentlyChanged = useSession((s) => s.recentlyChanged);
  const expireChanges = useSession((s) => s.expireChanges);
  const finalized = useSession((s) => s.finalized);

  const positions = useCanvas((s) => s.positions);
  const dragTo = useCanvas((s) => s.dragTo);
  const setPosition = useCanvas((s) => s.setPosition);
  const setPositions = useCanvas((s) => s.setPositions);
  const setViewport = useCanvas((s) => s.setViewport);
  const savedViewport = useCanvas((s) => s.viewport);
  const selected = useCanvas((s) => s.selected);
  const select = useCanvas((s) => s.select);

  const { setViewport: moveTo } = useReactFlow();
  const flowW = useStore((s) => s.width);
  const flowH = useStore((s) => s.height);

  // A viewport the user chose is theirs. Only a pan or zoom they made counts —
  // React Flow reports its own moves with a null event.
  const userMoved = useRef(savedViewport !== null);
  /** The layout a frame is owed for, and a tick to make the effect notice. */
  const owed = useRef<{ pts: XY[]; size: Size } | null>(null);
  const [frameTick, askForFrame] = useReducer((n: number) => n + 1, 0);

  const concerns = useMemo(() => concernsByTarget(arch?.concerns ?? []), [arch?.concerns]);

  // ---- ids + edges for whichever layer is showing ----
  const graph = useMemo(() => {
    if (layer === "sketch" && variant) {
      return {
        ids: Object.keys(variant.nodes),
        edges: variant.links.map((l): GraphEdge => ({ src: l.src, dst: l.dst })),
        keyOf: (id: string) => sketchKey(variant.id, id),
        size: SKETCH_SIZE,
      };
    }
    return {
      ids: Object.keys(arch?.components ?? {}),
      edges: (arch?.connections ?? []).map((c): GraphEdge => ({ src: c.src, dst: c.dst })),
      keyOf: designKey,
      size: DESIGN_SIZE,
    };
  }, [layer, variant, arch?.components, arch?.connections]);

  /**
   * The shape of the graph — not its contents. Two pushes that add a
   * responsibility but no component or connection have the same shape, and the
   * canvas must not move for those.
   */
  const shape = useMemo(
    () =>
      JSON.stringify([
        layer,
        variant?.id ?? null,
        [...graph.ids].sort(),
        graph.edges.map((e) => edgeKey(e.src, e.dst)).sort(),
      ]),
    [layer, variant?.id, graph],
  );
  const lastShape = useRef("");

  /**
   * Re-flow on every change of shape, holding only what the user pinned.
   *
   * Placing each node once, as it arrives, is what produced the single column:
   * components are written before the connections between them, so the first
   * node to appear has no edges to be layered along and every one after it just
   * slides down out of the way. Laying the whole graph out again once the edges
   * exist is what makes the diagram readable without pressing Tidy up — and it
   * still never moves a card that was placed by hand.
   */
  useEffect(() => {
    if (graph.ids.length === 0 || shape === lastShape.current) return;
    lastShape.current = shape;

    const { positions: live, pinned: pins } = useCanvas.getState();
    const hold: Record<string, XY> = {};
    for (const k of pins) if (live[k]) hold[k] = live[k];

    const placed = layout(graph.ids, graph.edges, graph.keyOf, hold, graph.size);
    const moved = Object.entries(placed).filter(
      ([k, p]) => live[k]?.x !== p.x || live[k]?.y !== p.y,
    );
    if (moved.length) setPositions(Object.fromEntries(moved));

    // Frame against the layout we just computed, not the one in the store: the
    // store is a render behind, and framing a chain against the column it used
    // to be lands at the wrong zoom on whichever push wins the race.
    const now = { ...live, ...placed };
    owed.current = {
      pts: graph.ids.map((id) => now[graph.keyOf(id)]).filter(Boolean),
      size: graph.size,
    };
    askForFrame();
  }, [shape, graph, setPositions]);

  // Tidy up asks for a frame explicitly, and that one overrides a chosen viewport
  const refitNonce = useCanvas((s) => s.refitNonce);
  useEffect(() => {
    if (refitNonce === 0) return;
    const live = useCanvas.getState().positions;
    userMoved.current = false;
    owed.current = {
      pts: graph.ids.map((id) => live[graph.keyOf(id)]).filter(Boolean),
      size: graph.size,
    };
    askForFrame();
    // graph is deliberately not a dep: this fires for the nonce, nothing else
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refitNonce]);

  /**
   * Frame the graph — but never so far out that the cards stop being readable.
   *
   * A seven-stage pipeline is 2,000px of a 1,200px viewport, and fitting all of
   * it means 0.55× zoom, where a component card is a grey smudge. Below the
   * legibility floor we stop zooming out and anchor on the entry point instead,
   * because the first thing you want to read is where the graph starts, not its
   * middle. The minimap and a scroll wheel cover the rest.
   *
   * The bounds are ours, not React Flow's: `getNodesBounds` reports position
   * only until the cards have been measured, which is a frame or two after the
   * one where they first appear — and framing against a graph 216px narrower
   * than the real one is how you land at the wrong zoom.
   */
  useEffect(() => {
    const req = owed.current;
    if (!req || !req.pts.length || userMoved.current) return;
    if (!flowW || !flowH) return; // container not measured yet; this reruns when it is

    const { pts, size } = req;
    const box = {
      x: Math.min(...pts.map((p) => p.x)),
      y: Math.min(...pts.map((p) => p.y)),
      w: Math.max(...pts.map((p) => p.x)) - Math.min(...pts.map((p) => p.x)) + size.w,
      h: Math.max(...pts.map((p) => p.y)) - Math.min(...pts.map((p) => p.y)) + size.h,
    };

    const PAD = 48;
    const room = { w: flowW - PAD * 2, h: flowH - PAD * 2 };
    const whole = Math.min(room.w / box.w, room.h / box.h);
    const zoom = Math.min(1.05, Math.max(READABLE_ZOOM, whole));
    const place = (span: number, avail: number, origin: number) =>
      span * zoom <= avail
        ? (avail - span * zoom) / 2 + PAD - origin * zoom // centred
        : PAD - origin * zoom;                            // anchored at the start

    owed.current = null;
    moveTo(
      { x: place(box.w, room.w, box.x), y: place(box.h, room.h, box.y), zoom },
      { duration: 280 },
    );
  }, [frameTick, flowW, flowH, moveTo]);

  // let the amber "just changed" ring lapse
  useEffect(() => {
    if (Object.keys(recentlyChanged).length === 0) return;
    const t = setTimeout(expireChanges, 700);
    return () => clearTimeout(t);
  }, [recentlyChanged, expireChanges]);

  const theme = useTheme((s) => s.theme);
  const ink = useMemo(() => palette(), [theme]);

  const sketching = layer === "sketch" && variant !== null;

  // everything that does not depend on where a card sits
  const cards = useMemo(() => {
    if (!arch) return {};
    return sketching && variant
      ? sketchCards(variant, concerns, recentlyChanged)
      : designCards(arch, gapsBySubject, concerns, recentlyChanged);
  }, [arch, sketching, variant, concerns, recentlyChanged, gapsBySubject]);

  const edges = useMemo(() => {
    if (!arch) return [];
    return sketching && variant ? sketchEdges(variant, ink) : designEdges(arch, gapsBySubject, ink);
  }, [arch, sketching, variant, gapsBySubject, ink]);

  // and the thin part that does
  const nodes = useNodeObjects(
    cards,
    sketching ? "sketch" : "component",
    sketching ? SKETCH_SIZE : DESIGN_SIZE,
    positions,
    selected,
  );

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const ch of changes) {
        if (ch.type === "position" && ch.position) {
          if (ch.dragging) dragTo(ch.id, ch.position); // in flight: follow the cursor
          else setPosition(ch.id, ch.position);        // dropped: pin it, and save
        }
        if (ch.type === "select") select(ch.selected ? ch.id : null);
      }
    },
    [dragTo, setPosition, select],
  );

  const openDialog = useCanvas((s) => s.openComponentDialog);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodesChange={onNodesChange}
      onNodeDoubleClick={(_, node) => {
        if (node.type === "component") openDialog(node.id.replace(/^design:/, ""));
      }}
      onMoveEnd={(e, vp) => {
        if (e) userMoved.current = true;
        setViewport(vp);
      }}
      defaultViewport={savedViewport ?? undefined}
      nodesDraggable={!finalized}
      nodesConnectable={false}
      elementsSelectable
      proOptions={{ hideAttribution: true }}
      minZoom={0.2}
      maxZoom={3}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--grid)" />
      {/* concrete colours, not tokens: the minimap paints SVG fill attributes,
          which do not resolve a custom property — same trap as the arrowheads */}
      <MiniMap
        pannable
        zoomable
        nodeStrokeWidth={2}
        nodeColor={ink.edge}
        nodeStrokeColor={ink.edge}
        maskColor="rgba(127, 127, 127, 0.16)"
      />
      {/* bottom-right, above the minimap: bottom-left is the hint strip's, and
          two things in one corner means one of them can't be clicked */}
      <Controls showInteractive={false} position="bottom-right" />
    </ReactFlow>
  );
}

/** Tidy up: recompute every position, discarding pins. */
export function useTidyUp(layer: Layer, variant: Variant | null) {
  const arch = useSession((s) => s.arch);
  const clearPins = useCanvas((s) => s.clearPins);
  const setPositions = useCanvas((s) => s.setPositions);
  const requestRefit = useCanvas((s) => s.requestRefit);
  return useCallback(() => {
    if (!arch) return;
    const fresh =
      layer === "sketch" && variant
        ? relayoutAll(
            Object.keys(variant.nodes),
            variant.links.map((l) => ({ src: l.src, dst: l.dst })),
            (id) => sketchKey(variant.id, id),
            SKETCH_SIZE,
          )
        : relayoutAll(
            Object.keys(arch.components),
            arch.connections.map((c) => ({ src: c.src, dst: c.dst })),
            designKey,
            DESIGN_SIZE,
          );
    clearPins();
    setPositions(fresh);
    requestRefit();
  }, [arch, layer, variant, clearPins, setPositions, requestRefit]);
}

export function Canvas(props: Props) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
