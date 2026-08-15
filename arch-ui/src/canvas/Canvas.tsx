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
  routes,
  type GraphEdge,
  type Size,
} from "../layout";
import { board as packBoard } from "../board";
import { designKey, keyId, sketchKey, useCanvas, viewKey, type XY } from "../store/canvas";
import { CHANGE_RING_MS, useSession } from "../store/session";
import { palette, useTheme, type Palette } from "../theme";
import { edgeTypes } from "./edges";
import { NOTE_COL, Notes } from "./Notes";
import type { Box } from "../board";
import { boardNodeTypes } from "./frames";
import {
  HANDLE,
  nodeTypes,
  type ComponentNodeData,
  type SketchNodeData,
} from "./nodes";
import type { ArchState, Concern, Layer, Variant } from "../types";
import { BOARD, projectView, resolveView, type Projection } from "../views";

/**
 * How far out the framer is allowed to zoom, and where the canvas switches to
 * reading a graph rather than reading its cards.
 *
 * The floor used to be 0.62 — the point where a card's 11.5px responsibility
 * line stops being legible — and a seventeen-component design fits at 0.34, so
 * the framer gave up and anchored on the entry point instead: a partial view,
 * at a zoom where the body text was mush anyway. Below TERSE the card drops
 * that text and sets its name large (`.terse`, theme.css), which is legible a
 * good deal further out, so the floor can go with it.
 */
const READABLE_ZOOM = 0.42;
/** Below this a card is its kind and its name, nothing else. */
const TERSE_ZOOM = 0.72;
/** Below this an edge label appears on hover, on selection, or on a lit flow. */
const LABEL_ZOOM = 0.85;
/**
 * The board's floor instead of READABLE_ZOOM.
 *
 * Everywhere else, refusing to zoom out past legibility is right — a graph too
 * small to read is worse than a graph you have to pan. The board is the one
 * place that reasoning inverts: it exists to be seen whole, and a card at this
 * size is a coloured block that tells you where things are, which is what you
 * came to the board for. Leaning in is one scroll away.
 */
const BOARD_ZOOM = 0.2;

/** How long a card's two-beat entrance takes (pop 200ms + settle 400ms), and
 *  therefore how long an arrow into it waits before it starts drawing. Must
 *  match the `.node` animation in theme.css. */
const ENTRANCE_MS = 600;

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
function wire(stroke: string, feedback: boolean, lane?: XY[]): Partial<Edge> {
  // A feedback edge loops underneath rather than through the layering, so it
  // never gets a lane — it is already going around.
  const routed = !feedback && lane !== undefined && lane.length > 0;
  return {
    type: feedback ? "feedback" : routed ? "routed" : "default", // custom / bezier
    data: routed ? { points: lane } : undefined,
    pathOptions: feedback || routed ? undefined : { curvature: 0.32 },
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
function questionsByTarget(questions: ArchState["questions"]): Map<string, string[]> {
  const map = new Map<string, string[]>();
  for (const q of questions) {
    // `resolution === null` is open; a question with no target is about the
    // design generally and belongs in the rail, not on a card.
    if (q.resolution || !q.target) continue;
    if (!map.has(q.target)) map.set(q.target, []);
    map.get(q.target)!.push(q.question);
  }
  return map;
}

/**
 * What a hovered or playing flow lights up (§4).
 *
 * `null` members mean nothing is lit — deliberately distinct from an empty set,
 * which would mean "a flow is lit and touches nothing" and would correctly dim
 * the entire canvas. A flow with no steps must not black the room out.
 */
export interface FlowLight {
  comps: Set<string> | null;
  edges: Set<string> | null;
  /** the one hop currently playing, as `src->dst` */
  step: string | null;
}

const DARK: FlowLight = { comps: null, edges: null, step: null };

function flowLight(arch: ArchState | null, litId: string | null, step: number): FlowLight {
  if (!arch || !litId) return DARK;
  const flow = arch.flows.find((f) => f.id === litId);
  if (!flow || flow.steps.length === 0) return DARK;
  const comps = new Set<string>();
  const edges = new Set<string>();
  for (const s of flow.steps) {
    comps.add(s.src);
    comps.add(s.dst);
    edges.add(`${s.src}->${s.dst}`);
  }
  const current = step >= 0 && step < flow.steps.length ? flow.steps[step] : null;
  return { comps, edges, step: current ? `${current.src}->${current.dst}` : null };
}

function designCards(
  arch: ArchState,
  keyOf: (id: string) => string,
  shown: Projection,
  gaps: Record<string, string[]>,
  concerns: Map<string, Concern[]>,
  questions: Map<string, string[]>,
  recentlyChanged: Record<string, number>,
  light: FlowLight,
  bornWith: Record<string, true>,
): Record<string, ComponentNodeData> {
  const owed = new Set(arch.obligations.filter((o) => o.status === "pending").map((o) => o.component_id));
  const out: Record<string, ComponentNodeData> = {};
  for (const c of Object.values(shown.components)) {
    out[keyOf(c.id)] = {
      component: c,
      gaps: gaps[c.id] ?? [],
      concerns: concerns.get(c.id) ?? [],
      questions: questions.get(c.id) ?? [],
      owes: owed.has(c.id),
      changed: (recentlyChanged[c.id] ?? 0) > Date.now(),
      dim: light.comps !== null && !light.comps.has(c.id),
      lit: light.comps !== null && light.comps.has(c.id),
      born: !!bornWith[c.id],
      // a rolled-up box has no internals of its own to open
      synthetic: shown.synthetic.has(c.id),
    };
  }
  return out;
}

const ORIGIN: XY = { x: 0, y: 0 };

/** The graph's own node types, plus the two only board mode draws. */
const allNodeTypes = { ...nodeTypes, ...boardNodeTypes };

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

/**
 * Colour carries one thing: how the connection behaves. Forward in `--edge`,
 * feedback in `--edge-back`, and that is the lot.
 *
 * It used to also paint a *thin* connection — one with no `failure_mode` — in
 * `--changed`, the loudest colour on the canvas. At production scope that is
 * every connection nobody has written a failure mode for yet, which early on is
 * most of them, so the diagram's most salient signal was its least important
 * fact and sync-vs-async drowned underneath it. Thinness is still reported: the
 * tracker lists it, the card carries a `thin` pill, and the rail has it in full.
 */
function designEdges(
  keyOf: (id: string) => string,
  shown: Projection,
  ink: Palette,
  light: FlowLight,
  recentlyChanged: Record<string, number>,
  lanes: Map<string, XY[]>,
): Edge[] {
  const back = findBackEdges(
    Object.keys(shown.components),
    shown.connections.map((c) => ({ src: c.src, dst: c.dst })),
  );
  return shown.connections.map((conn) => {
    const key = `${conn.src}->${conn.dst}`;
    const feedback = back.has(edgeKey(conn.src, conn.dst));
    const stroke = feedback ? ink.edgeBack : ink.edge;
    // Class names rather than inline colour: the highlight is a CSS transition
    // on an attribute flip (§7), so playback stepping every 820ms never depends
    // on React re-rendering the edge in time.
    const onFlow = light.edges !== null && light.edges.has(key);
    // §7: a connection that just arrived draws itself along its own path —
    // but only once the components it runs between have finished arriving.
    // `connect` refuses an endpoint that does not exist yet, so a new edge
    // always follows its components; when it follows them *closely* the arrow
    // would otherwise be drawn into a card still mid-pop.
    const now = Date.now();
    const fresh = (recentlyChanged[key] ?? 0) > now;
    const landed = Math.max(
      (recentlyChanged[conn.src] ?? 0) - CHANGE_RING_MS,
      (recentlyChanged[conn.dst] ?? 0) - CHANGE_RING_MS,
    );
    const wait = fresh ? Math.max(0, landed + ENTRANCE_MS - now) : 0;
    const cls = [
      feedback ? "feedback" : "",
      light.edges === null ? "" : onFlow ? "lit" : "dim",
      light.step === key ? "walking" : "",
      fresh ? "drawing" : "",
    ].filter(Boolean).join(" ");
    return {
      // src->dst alone: `connect` upserts by the pair, so there is exactly one
      // connection per pair and the label/index are not needed to disambiguate.
      // They were active harm — an index shifts when a connection is inserted
      // ahead of this one, which changes the id, which remounts the edge in the
      // middle of a live turn.
      id: key,
      source: keyOf(conn.src),
      target: keyOf(conn.dst),
      label: conn.mechanism ? `${conn.label} via ${conn.mechanism}` : conn.label,
      className: cls || undefined,
      ...wire(stroke, feedback, lanes.get(key)),
      // inline, because the wait is per-edge: the shorthand in the stylesheet
      // sets no delay, and a longhand set here wins over it
      style: { ...EDGE_STYLE[conn.kind], stroke, animationDelay: `${wait}ms` },
    };
  });
}

function sketchCards(
  variant: Variant,
  concerns: Map<string, Concern[]>,
  recentlyChanged: Record<string, number>,
  bornWith: Record<string, true>,
): Record<string, SketchNodeData> {
  const out: Record<string, SketchNodeData> = {};
  for (const n of Object.values(variant.nodes)) {
    out[sketchKey(variant.id, n.id)] = {
      node: n,
      concerns: concerns.get(n.id) ?? [],
      changed: (recentlyChanged[n.id] ?? 0) > Date.now(),
      born: !!bornWith[n.id],
    };
  }
  return out;
}

function sketchEdges(variant: Variant, ink: Palette, lanes: Map<string, XY[]>): Edge[] {
  const back = findBackEdges(
    Object.keys(variant.nodes),
    variant.links.map((l) => ({ src: l.src, dst: l.dst })),
  );
  return variant.links.map((l, i) => {
    const feedback = back.has(edgeKey(l.src, l.dst));
    const stroke = feedback ? ink.edgeBack : ink.sketchLine;
    const lane = lanes.get(`${l.src}->${l.dst}`);
    return {
      id: `${variant.id}:${l.src}->${l.dst}:${i}`,
      source: sketchKey(variant.id, l.src),
      target: sketchKey(variant.id, l.dst),
      label: l.label || undefined,
      className: feedback ? "feedback" : undefined,
      ...wire(stroke, feedback, lane),
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
  /**
   * Semantic zoom, as two booleans rather than the zoom itself.
   *
   * Reading `transform[2]` here would re-render the whole canvas on every frame
   * of a pinch. A selector that returns a threshold test only produces a new
   * value when the threshold is actually crossed, so the repaint happens once,
   * where the reader can see the point of it.
   */
  const terse = useStore((s) => s.transform[2] < TERSE_ZOOM);
  const quietLabels = useStore((s) => s.transform[2] < LABEL_ZOOM);

  // A viewport the user chose is theirs. Only a pan or zoom they made counts —
  // React Flow reports its own moves with a null event.
  const userMoved = useRef(savedViewport !== null);
  /** The layout a frame is owed for, and a tick to make the effect notice. */
  const owed = useRef<{ pts: XY[]; size: Size; also?: Box | null } | null>(null);
  const [frameTick, askForFrame] = useReducer((n: number) => n + 1, 0);

  // Which diagram is on screen. Resolved against live state, because a view
  // names a flow and a flow can be renamed or removed under you mid-session.
  const chosenView = useCanvas((s) => s.view);
  const view = useMemo(() => resolveView(arch, chosenView), [arch, chosenView]);
  const shown = useMemo(() => projectView(arch, view), [arch, view]);

  const concerns = useMemo(() => concernsByTarget(arch?.concerns ?? []), [arch?.concerns]);
  const questions = useMemo(() => questionsByTarget(arch?.questions ?? []), [arch?.questions]);

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
      ids: Object.keys(shown.components),
      edges: shown.connections.map((c): GraphEdge => ({ src: c.src, dst: c.dst })),
      // The board is the design seen from further out, not a second copy of
      // it: same keys, so the same positions, so dragging a card in either
      // place moves the one card there is.
      keyOf: view === BOARD ? designKey : (id: string) => viewKey(view, id),
      size: DESIGN_SIZE,
    };
  }, [layer, variant, shown, view]);

  /**
   * The shape of the graph — not its contents. Two pushes that add a
   * responsibility but no component or connection have the same shape, and the
   * canvas must not move for those.
   */
  const shape = useMemo(
    () =>
      JSON.stringify([
        layer,
        view,
        variant?.id ?? null,
        [...graph.ids].sort(),
        graph.edges.map((e) => edgeKey(e.src, e.dst)).sort(),
      ]),
    [layer, view, graph],
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
      // board mode is a whole-board fit; the ladders are the right-hand edge
      also: view === BOARD ? packBoard(arch, now, designKey, DESIGN_SIZE).bounds : null,
    };
    askForFrame();
  }, [shape, graph, view, arch, setPositions]);

  // Tidy up asks for a frame explicitly, and that one overrides a chosen viewport
  const refitNonce = useCanvas((s) => s.refitNonce);
  useEffect(() => {
    if (refitNonce === 0) return;
    const live = useCanvas.getState().positions;
    userMoved.current = false;
    owed.current = {
      pts: graph.ids.map((id) => live[graph.keyOf(id)]).filter(Boolean),
      size: graph.size,
      also: view === BOARD ? packBoard(arch, live, designKey, DESIGN_SIZE).bounds : null,
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

    const { pts, size, also } = req;
    const box = {
      x: Math.min(...pts.map((p) => p.x)),
      y: Math.min(...pts.map((p) => p.y)),
      w: Math.max(...pts.map((p) => p.x)) - Math.min(...pts.map((p) => p.x)) + size.w,
      h: Math.max(...pts.map((p) => p.y)) - Math.min(...pts.map((p) => p.y)) + size.h,
    };
    if (also) {
      const x = Math.min(box.x, also.x);
      const y = Math.min(box.y, also.y);
      box.w = Math.max(box.x + box.w, also.x + also.w) - x;
      box.h = Math.max(box.y + box.h, also.y + also.h) - y;
      box.x = x;
      box.y = y;
    }

    const PAD = 48;
    // §2 reserves the right margin for the note column, so the graph is framed
    // into what is left of the canvas. Reserved unconditionally, not just when
    // a concern exists: a canvas that re-frames itself the moment the critic
    // files something would move every card out from under the reader.
    const room = { w: flowW - NOTE_COL - PAD * 2, h: flowH - PAD * 2 };
    const whole = Math.min(room.w / box.w, room.h / box.h);
    const zoom = Math.min(1.05, Math.max(also ? BOARD_ZOOM : READABLE_ZOOM, whole));
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

  const bornWith = useSession((s) => s.bornWith);
  const flowLit = useCanvas((s) => s.flowLit);
  const flowStep = useCanvas((s) => s.flowStep);
  const light = useMemo(() => flowLight(arch, flowLit, flowStep), [arch, flowLit, flowStep]);

  // everything that does not depend on where a card sits
  const cards = useMemo(() => {
    if (!arch) return {};
    return sketching && variant
      ? sketchCards(variant, concerns, recentlyChanged, bornWith)
      : designCards(arch, graph.keyOf, shown, gapsBySubject, concerns, questions,
                    recentlyChanged, light, bornWith);
  }, [arch, graph, shown, sketching, variant, concerns, questions, recentlyChanged, gapsBySubject,
      light, bornWith]);

  // The lane each long edge runs down. A property of the graph's shape, not of
  // where its cards happen to sit, so it is off the drag path entirely: moving
  // a card moves the two ends of its edges and nothing else.
  const lanes = useMemo(
    () => routes(graph.ids, graph.edges, graph.size),
    [graph],
  );

  const edges = useMemo(() => {
    if (!arch) return [];
    return sketching && variant
      ? sketchEdges(variant, ink, lanes)
      : designEdges(graph.keyOf, shown, ink, light, recentlyChanged, lanes);
  }, [arch, graph, shown, sketching, variant, ink, light, recentlyChanged, lanes]);

  /**
   * The board's furniture: the frame around the design and a ladder per flow.
   *
   * Computed from where the cards actually are, not from the grid, so a frame
   * drawn around a hand-arranged design still contains it. They are appended
   * after layout rather than passed through it — a frame is not a participant
   * in the graph, it is a rectangle drawn behind one.
   */
  const boarding = !sketching && view === BOARD;
  const boardNodes = useMemo(() => {
    if (!boarding || !arch) return [];
    const b = packBoard(arch, positions, designKey, DESIGN_SIZE);
    const out: Node[] = [
      {
        id: "board:frame:design",
        type: "frame",
        position: { x: b.design.x, y: b.design.y },
        draggable: false,
        selectable: false,
        zIndex: -1,
        data: {
          label: "The design",
          note: `${Object.keys(arch.components).length} components`,
          w: b.design.w,
          h: b.design.h,
        },
      },
    ];
    for (const lane of b.lanes) {
      out.push({
        id: `board:lane:${lane.flow.id}`,
        type: "lane",
        position: { x: lane.x, y: lane.y },
        draggable: false,
        selectable: false,
        data: {
          flow: lane.flow,
          rungs: lane.rungs,
          components: arch.components,
          w: lane.w,
          h: lane.h,
        },
      });
    }
    return out;
  }, [boarding, arch, positions]);

  // and the thin part that does
  const nodes = useNodeObjects(
    cards,
    sketching ? "sketch" : "component",
    sketching ? SKETCH_SIZE : DESIGN_SIZE,
    positions,
    selected,
  );
  const allNodes = useMemo(
    () => (boardNodes.length ? [...boardNodes, ...nodes] : nodes),
    [boardNodes, nodes],
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
      nodes={allNodes}
      edges={edges}
      nodeTypes={allNodeTypes}
      edgeTypes={edgeTypes}
      onNodesChange={onNodesChange}
      onNodeDoubleClick={(_, node) => {
        if (node.type !== "component") return;
        const id = keyId(view === BOARD ? "design" : view, node.id);
        if (!shown.synthetic.has(id)) openDialog(id);
      }}
      onMoveEnd={(e, vp) => {
        if (e) userMoved.current = true;
        setViewport(vp);
      }}
      defaultViewport={savedViewport ?? undefined}
      nodesDraggable={!finalized}
      nodesConnectable={false}
      elementsSelectable
      className={[terse ? "terse" : "", quietLabels ? "quiet-labels" : ""]
        .filter(Boolean)
        .join(" ")}
      proOptions={{ hideAttribution: true }}
      minZoom={0.2}
      maxZoom={3}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--grid)" />

      {/* §5: objections live in the canvas's right margin, beside the thing
          they are against. A child of ReactFlow rather than of the viewport —
          the notes must not pan or zoom away from the reader. */}
      <Notes />
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
  const chosenView = useCanvas((s) => s.view);
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
        : (() => {
            const view = resolveView(arch, chosenView);
            const shown = projectView(arch, view);
            return relayoutAll(
              Object.keys(shown.components),
              shown.connections.map((c) => ({ src: c.src, dst: c.dst })),
              (id) => viewKey(view, id),
              DESIGN_SIZE,
            );
          })();
    // §7: a damped spring with a small overshoot, not a jump. The class is
    // transient — a permanent transition on the node transform would also
    // animate dragging, which must track the cursor exactly.
    document.body.classList.add("tidying");
    window.setTimeout(() => document.body.classList.remove("tidying"), 600);
    clearPins(
      layer === "sketch" && variant
        ? `sketch:${variant.id}:`
        : `${resolveView(arch, chosenView)}:`,
    );
    setPositions(fresh);
    requestRefit();
  }, [arch, layer, variant, chosenView, clearPins, setPositions, requestRefit]);
}

export function Canvas(props: Props) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
