import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Zoomer } from "@arch/components/Zoomer";
import { onEdited, onFrameError } from "../board/edits";
import { setStageMessageHandler, type FrameMessage } from "../board/frameBus";
import { postToFrame, toScreen } from "../board/frames";
import { setStageApi } from "../board/stageApi";
import { clearSelection, select, setFocus, setTree, useUi } from "../board/ui";
import { useStage, type Bounds } from "../hooks/useStage";
import { clearPoster, useDrafts, usePosters, type Draft } from "../wire/drafts";
import { mutate, refusal, useSession } from "../wire/session";
import type { Selected, TreeNode, WireArtboard } from "../wire/types";
import { DraftFrame, FRAME_H, FRAME_W } from "./DraftFrame";
import { Frame } from "./Frame";
import { Loader } from "./Loader";
import { SelMenu } from "./SelMenu";

const GAP = 120;
const NAME_H = 26;

/** One place on the board: a finished artboard, or a draft standing in for
 *  one — in an artboard's slot when it is being rebuilt, after the last one
 *  when it is new. An artboard that has just arrived may still have the
 *  draft that stood in for it underneath, as a poster, until it has painted. */
interface Slot {
  key: string;
  x: number;
  w: number;
  h: number;
  ab?: WireArtboard;
  draft?: Draft;
  poster?: Draft;
}

const slotKey = (s: { ab?: WireArtboard; draft?: Draft }) =>
  s.draft ? "d:" + s.draft.key : "a:" + s.ab!.id;

export function Stage({ themeCss }: { themeCss: string }) {
  const session = useSession();
  const ui = useUi();
  const drafts = useDrafts();
  const posters = usePosters();
  const viewport = useRef<HTMLDivElement | null>(null);
  const world = useRef<HTMLDivElement | null>(null);
  const [sizes, setSizes] = useState<Record<string, [number, number]>>({});
  const { view, level, applyView, zoomAt, zoomStep, frame, nudgeX } = useStage(viewport, world);

  const readOnly = session.status === "finalized";

  /* The board, laid out as a row: every artboard in the harness's order, a
     rebuild's draft taking its artboard's place, and new drafts after. A
     frame that has not measured itself yet takes the size its draft had:
     the draft measured the same document as it was written, so the
     handover does not jump. */
  const slots = useMemo<Slot[]>(() => {
    const rebuilds = new Map(drafts.filter((d) => d.artboard).map((d) => [d.artboard!, d]));
    const out: Slot[] = [];
    let x = 0;
    const place = (s: { ab?: WireArtboard; draft?: Draft; poster?: Draft }) => {
      const key = slotKey(s);
      const [w, h] = sizes[key] ?? (s.poster ? sizes["d:" + s.poster.key] : undefined) ?? [FRAME_W, FRAME_H];
      out.push({ key, x, w, h, ...s });
      x += w + GAP;
    };
    for (const ab of session.artboards) {
      const d = rebuilds.get(ab.id);
      if (d) place({ ab, draft: d });
      else place({ ab, poster: sizes["a:" + ab.id] ? undefined : posters[ab.id] });
    }
    for (const d of drafts) if (!d.artboard) place({ draft: d });
    return out;
  }, [session.artboards, drafts, posters, sizes]);

  const bounds = useCallback((ids: string[] | null): Bounds | null => {
    const picked = ids ? slots.filter((s) => s.ab && ids.includes(s.ab.id)) : slots;
    if (!picked.length) return null;
    return {
      x0: Math.min(...picked.map((s) => s.x)),
      y0: -NAME_H,
      x1: Math.max(...picked.map((s) => s.x + s.w)),
      y1: Math.max(...picked.map((s) => s.h)),
    };
  }, [slots]);

  useEffect(() => {
    setStageApi({
      reveal: (ids) => frame(bounds(ids), 52, 1),
      nudgeX,
    });
    return () => setStageApi(null);
  }, [bounds, frame, nudgeX]);

  /* Fit when something new lands on the board — a frame, a draft, or the
     first measurement of a frame — and not when a draft merely grows. */
  const arrangement = slots.map((s) => s.key + (s.ab && sizes[s.key] ? "=" : "")).join("|");
  const seen = useRef("");
  useEffect(() => {
    if (arrangement === seen.current) return;
    const first = seen.current === "";
    seen.current = arrangement;
    if (!slots.length) return;
    const raf = requestAnimationFrame(() => frame(bounds(null), 52, 1, first ? 0 : 620));
    return () => cancelAnimationFrame(raf);
  }, [arrangement, slots.length, bounds, frame]);

  /* after every commit, not only when the camera moves: a frame name, a
     busy chip or the selection menu that has just mounted is inside the
     scaled world and has to be counter-scaled before it is painted */
  useLayoutEffect(() => { applyView(); });

  /* what the frames say back about the board — the capture round trip and
     forwarded keys are the frame bus's, heard whether or not this is mounted */
  useEffect(() => {
    /* the pinch, as Safari tells it: a scale relative to where it started */
    let pinchK: number | null = null;
    const onMessage = (id: string, msg: FrameMessage) => {
      switch (msg.type) {
        /* the board's gestures, made over an artboard: the frame forwards
           them and the point comes back through the frame's own transform */
        case "wheel": {
          const at = toScreen(id, Number(msg.x) || 0, Number(msg.y) || 0, view.current.k);
          if (msg.zoom && at) zoomAt(at.x, at.y, view.current.k * Math.exp(-(Number(msg.dy) || 0) / 260));
          else { view.current.x -= Number(msg.dx) || 0; view.current.y -= Number(msg.dy) || 0; applyView(); }
          break;
        }
        case "pinch": {
          if (msg.phase === "start") pinchK = view.current.k;
          else if (msg.phase === "change" && pinchK !== null) {
            const at = toScreen(id, Number(msg.x) || 0, Number(msg.y) || 0, view.current.k);
            if (at) zoomAt(at.x, at.y, pinchK * (Number(msg.scale) || 1));
          } else pinchK = null;
          break;
        }
        case "size": {
          const w = Math.max(320, Math.round(Number(msg.w) || FRAME_W));
          const h = Math.max(240, Math.round(Number(msg.h) || FRAME_H));
          setSizes((prev) => {
            const p = prev["a:" + id];
            return p && p[0] === w && p[1] === h ? prev : { ...prev, ["a:" + id]: [w, h] };
          });
          /* the frame has painted: the draft that stood in for it is done */
          clearPoster(id);
          break;
        }
        case "selected": {
          const s = msg as unknown as Selected;
          select({ artboard: id, selector: s.selector, tag: s.tag, leaf: Boolean(s.leaf),
                   text: s.text ?? "", box: s.box, props: s.props ?? {} });
          postToFrame(id, { type: "getTree" });
          break;
        }
        case "tree":
          setTree(id, msg.tree as TreeNode);
          break;
        case "edited":
          void onEdited(id);
          break;
        case "error":
          onFrameError(String(msg.message ?? ""));
          break;
      }
    };
    setStageMessageHandler(onMessage);
    return () => setStageMessageHandler(null);
  }, [applyView, view, zoomAt]);

  /* pan and zoom: wheel pans, ⌘/ctrl-wheel zooms, Safari's pinch arrives as
     gesture events and would page-zoom the browser otherwise */
  useEffect(() => {
    const vp = viewport.current;
    if (!vp) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) zoomAt(e.clientX, e.clientY, view.current.k * Math.exp(-e.deltaY / 260));
      else { view.current.x -= e.deltaX; view.current.y -= e.deltaY; applyView(); }
    };
    let gestureK: number | null = null;
    const onGestureStart = (e: Event) => { e.preventDefault(); gestureK = view.current.k; };
    const onGestureChange = (e: Event) => {
      if (gestureK == null) return;
      e.preventDefault();
      const g = e as Event & { clientX: number; clientY: number; scale: number };
      zoomAt(g.clientX, g.clientY, gestureK * g.scale);
    };
    const onGestureEnd = () => { gestureK = null; };
    vp.addEventListener("wheel", onWheel, { passive: false });
    vp.addEventListener("gesturestart", onGestureStart);
    vp.addEventListener("gesturechange", onGestureChange);
    vp.addEventListener("gestureend", onGestureEnd);
    return () => {
      vp.removeEventListener("wheel", onWheel);
      vp.removeEventListener("gesturestart", onGestureStart);
      vp.removeEventListener("gesturechange", onGestureChange);
      vp.removeEventListener("gestureend", onGestureEnd);
    };
  }, [applyView, view, zoomAt]);

  const pan = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.target !== viewport.current && e.target !== world.current) return; // frames keep their clicks
    pan.current = { x: e.clientX, y: e.clientY, vx: view.current.x, vy: view.current.y };
    viewport.current?.setAttribute("data-panning", "1");
    try { viewport.current?.setPointerCapture(e.pointerId); } catch { /* no active pointer */ }
    clearSelection();
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const p = pan.current;
    if (!p) return;
    view.current.x = p.vx + (e.clientX - p.x);
    view.current.y = p.vy + (e.clientY - p.y);
    applyView();
  };
  const onPointerUp = () => { pan.current = null; viewport.current?.removeAttribute("data-panning"); };

  const onDraftSize = useCallback((key: string, w: number, h: number) => {
    setSizes((prev) => {
      const p = prev["d:" + key];
      return p && p[0] === w && p[1] === h ? prev : { ...prev, ["d:" + key]: [w, h] };
    });
  }, []);

  /* the card's delete: the harness removes the artboard outright — the same
     door the model's design_delete_artboard uses. The page's own selection
     would outlive the frame it pointed at, so it goes first. */
  const deleteArtboard = useCallback(async (id: string) => {
    if (ui.selection?.artboard === id) clearSelection();
    const res = await mutate({ op: "delete_artboard", artboard: id });
    if (!res.ok) refusal(res.error ?? "delete failed");
  }, [ui.selection]);

  const drafting = drafts.length > 0;
  const loader = !session.artboards.length && !drafting && (session.running || session.status === "generating")
    ? "skeleton" : session.running && session.artboards.length ? "working" : null;

  /* the one static line, and only while the board is empty: everything
     that happens has the app bar's line, and a board with artboards on it
     explains itself */
  const hint = session.artboards.length || drafting ? null
    : readOnly ? "The session is closed · the board is read-only"
    : session.asking ? "Answer the question in the conversation and the designer starts"
    : "The designer will put the first artboard here · drag to pan · ⌘-scroll to zoom";

  const selSlot = ui.selection ? slots.find((s) => s.ab?.id === ui.selection!.artboard && !s.draft) : null;

  return (
    <section className="board" id="board" data-od-id="board">
      <div
        ref={viewport}
        className="viewport"
        id="viewport"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <div ref={world} className="world" id="world">
          {slots.map((s) => s.draft ? (
            <DraftFrame
              key={s.key}
              draft={s.draft}
              title={s.ab?.title ?? s.draft.title ?? "New artboard"}
              x={s.x} w={s.w} h={s.h}
              themeCss={themeCss}
              onSize={onDraftSize}
            />
          ) : (
            <Frame
              key={s.key}
              ab={s.ab!}
              html={session.html[s.ab!.id] ?? ""}
              reload={ui.reload[s.ab!.id] ?? 0}
              x={s.x} w={s.w} h={s.h}
              focused={ui.focus === s.ab!.id && !readOnly}
              chosen={s.ab!.id === session.finalizedArtboard}
              dim={readOnly && s.ab!.id !== session.finalizedArtboard}
              busy={!readOnly && session.working?.artboard === s.ab!.id ? session.working.verb : null}
              lit={ui.flash.ids.includes(s.ab!.id)}
              critique={session.critiques[s.ab!.id] ?? null}
              posterBehind={Boolean(s.poster)}
              onDelete={!readOnly && !session.running
                ? () => void deleteArtboard(s.ab!.id)
                : undefined}
              onFocus={() => { setFocus(s.ab!.id); postToFrame(s.ab!.id, { type: "getTree" }); }}
            />
          ))}
          {/* the picture a draft left behind, under the frame that replaced
              it, until that frame has painted — same key as the draft had,
              so React keeps the very document that was streamed in */}
          {slots.filter((s) => s.poster).map((s) => (
            <DraftFrame
              key={"d:" + s.poster!.key}
              draft={s.poster!}
              title={s.ab?.title ?? ""}
              x={s.x} w={s.w} h={s.h}
              themeCss={themeCss}
              onSize={onDraftSize}
              poster
            />
          ))}
          {ui.selection && selSlot && !readOnly ? <SelMenu sel={ui.selection} x={selSlot.x} /> : null}
        </div>
      </div>

      <Loader mode={loader} />

      {hint ? <p className="hint" id="hint">{hint}</p> : null}

      <Zoomer
        level={level}
        onIn={() => zoomStep(1.25)}
        onOut={() => zoomStep(1 / 1.25)}
        onFit={() => frame(bounds(null), 52, 1)}
        onTidy={() => { /* frames sit where the harness orders them */ }}
        canTidy={false}
      />
    </section>
  );
}
