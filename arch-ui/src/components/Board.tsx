import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toBoard } from "../board/adapter";
import { bounds as calcBounds, rect, snap, STEP, wirePaths } from "../board/geometry";
import type { BoardNode, Tool } from "../board/types";
import {
  clearNodeDraft, clearNoteDraft, draftNode, draftNote, flash, getUi,
  nextLocalId, select, setEditing, setTool, useUi,
} from "../board/ui";
import { setViewApi } from "../board/viewApi";
import { mutate, refusal, useSession } from "../wire/session";
import { useNodeHeights } from "../hooks/useNodeHeights";
import { useView } from "../hooks/useView";
import { Annotation } from "./Annotation";
import { Dock } from "./Dock";
import { Lanes } from "./Lanes";
import { NodeCard, type NodeAct } from "./NodeCard";
import { Wires } from "./Wires";
import { Zoomer } from "./Zoomer";

type Drag =
  | { mode: "pan"; sx: number; sy: number; vx: number; vy: number }
  | { mode: "node"; id: string; ox: number; oy: number; moved: boolean }
  | { mode: "anno"; id: string; ox: number; oy: number; moved: boolean }
  | { mode: "wire"; from: string };

const EMPTY = { lanes: [], nodes: [], wires: [], annos: [] };

export function Board({ setTip }: { setTip: (t: string) => void }) {
  const { arch, handedOff, bornWith } = useSession();
  const ui = useUi();
  const viewport = useRef<HTMLDivElement | null>(null);
  const world = useRef<HTMLDivElement | null>(null);
  const { heights, register } = useNodeHeights();
  const registerAnno = useCallback(() => { /* annotations need no measurement */ }, []);

  const [tempWire, setTempWire] = useState<string | null>(null);
  const drag = useRef<Drag | null>(null);


  /** The harness's design, arranged — with any drag still under a finger
   *  layered on top, so a box never snaps back while its `move` is in flight.
   *
   *  `heights` goes in because the arrangement is spaced to what the boxes
   *  actually render as. That looks circular and is not: a box's height depends
   *  on its content and its width, never on where it sits, so the first paint
   *  uses an estimate, the observer reports the truth, and the second settles. */
  const board = useMemo(() => {
    const view = arch ? toBoard(arch, heights) : EMPTY;
    if (!Object.keys(ui.drafts).length && !Object.keys(ui.noteDrafts).length) return view;
    return {
      ...view,
      nodes: view.nodes.map((n) => (ui.drafts[n.id] ? { ...n, ...ui.drafts[n.id] } : n)),
      annos: view.annos.map((a) => (ui.noteDrafts[a.id] ? { ...a, ...ui.noteDrafts[a.id] } : a)),
    };
  }, [arch, heights, ui.drafts, ui.noteDrafts]);

  const byId = useCallback(
    (id: string) => board.nodes.find((n) => n.id === id),
    [board.nodes],
  );

  const getBounds = useCallback(
    (ids?: string[] | null) => calcBounds(board.nodes, board.annos, board.lanes, heights, ids),
    [board.nodes, board.annos, board.lanes, heights],
  );
  const { view, level, applyView, zoomAt, frame, fitNow, nudgeX, toWorld } =
    useView(viewport, world, getBounds);

  /* the camera the chat and the rail are allowed to drive */
  useEffect(() => {
    setViewApi({ frame, nudgeX, fitNow });
    return () => setViewApi(null);
  }, [frame, nudgeX, fitNow]);

  /* A wire the architect has just drawn draws itself in. Which ones are new is
     the page's own observation — the harness replaces the design wholesale and
     says nothing about what changed within it. */
  const [freshWires, setFreshWires] = useState<Set<string> | null>(null);
  const seenWires = useRef<Set<string> | null>(null);
  useEffect(() => {
    const keys = new Set(board.wires.map((w) => `${w.from}>${w.to}`));
    if (seenWires.current === null) {
      // anything that predates the page is not an arrival; anything else is
      seenWires.current = new Set([...keys].filter((k) => bornWith[k]));
    }
    const added = [...keys].filter((k) => !seenWires.current!.has(k));
    seenWires.current = keys;
    if (!added.length) return;
    setFreshWires(new Set(added));
    const t = setTimeout(() => setFreshWires(null), 1200);
    return () => clearTimeout(t);
  }, [board.wires, bornWith]);

  const paths = useMemo(
    () => wirePaths(board.nodes, board.wires, heights, freshWires),
    [board.nodes, board.wires, heights, freshWires],
  );

  /* One opening fit, once the design has been measured — *every* box of it.
     The arrangement is spaced to the boxes' real heights, so fitting after the
     first measurement arrives frames a board that is about to change size, and
     the page opens at a zoom that was right for a layout nobody ever saw. */
  const fitted = useRef(false);
  useEffect(() => {
    if (fitted.current || !board.nodes.length) return;
    if (board.nodes.some((n) => heights[n.id] === undefined)) return;
    fitted.current = true;
    fitNow(64);
  }, [board.nodes, heights, fitNow]);

  /* The board draws itself box by box the first time it arrives — the same
     choreography a turn from the architect gets, so an arrival always reads as
     an arrival rather than a page that was already finished. */
  const choreographed = useRef(false);
  useEffect(() => {
    if (choreographed.current || !board.nodes.length) return;
    choreographed.current = true;
    const els = [...(world.current?.querySelectorAll<HTMLElement>(".node") ?? [])];
    els.forEach((el, i) => { el.style.transitionDelay = Math.min(i * 55, 520) + "ms"; });
    const t = setTimeout(() => { for (const el of els) el.style.transitionDelay = ""; }, 1300);
    return () => clearTimeout(t);
  }, [board.nodes.length]);

  useEffect(() => {
    const onResize = () => applyView();
    addEventListener("resize", onResize);
    return () => removeEventListener("resize", onResize);
  }, [applyView]);

  /* ── talking to the harness ─────────────────────────────────────────── */

  const send = useCallback(async (payload: Record<string, unknown>) => {
    const error = await mutate(payload);
    if (error) refusal(error);
    return !error;
  }, []);

  const readOnly = handedOff;

  const addBoxAt = async (x: number, y: number) => {
    if (readOnly) return;
    const id = nextLocalId("box-");
    const ok = await send({ op: "add_box", id, label: "New box", x: snap(x), y: snap(y) });
    setTool("select");
    if (!ok) return;
    select({ t: "node", id });
    setTimeout(() => setEditing({ t: "node", id }), 30);
  };

  const addNoteAt = (x: number, y: number) => {
    if (readOnly) return;
    /* A note is born on the page and only reaches the harness once it has
       words in it — an empty note is a gesture, not a thought. Whatever box
       was selected when you reached for the note is what it is about. */
    const id = nextLocalId("note-");
    const sel = getUi().selected;
    draftNote(id, snap(x), snap(y), sel?.t === "node" ? sel.id : undefined);
    select({ t: "anno", id });
    setTimeout(() => setEditing({ t: "anno", id }), 30);
    setTool("select");
  };

  const removeSelected = async () => {
    const sel = getUi().selected;
    if (!sel || readOnly) return;
    if (sel.t === "anno") {
      clearNoteDraft(sel.id);
      select(null);
      if (board.annos.some((a) => a.id === sel.id)) await send({ op: "note", id: sel.id, text: "" });
      return;
    }
    select(null);
    await send({ op: "remove_box", id: sel.id });
  };

  const runDeepen = async (id: string) => {
    const n = byId(id);
    if (!n || readOnly) return;
    const i = STEP.indexOf(n.depth);
    if (i >= STEP.length - 1) return;
    const has = i === 0 ? n.resp : n.tech || n.rows.length;
    if (!has) {
      refusal(`Nothing is recorded for ${n.label} yet — ask the architect what it owns.`);
      return;
    }
    await send({ op: "node", id, depth: STEP[i + 1] });
    flash([id]);
  };

  const onAct = (act: NodeAct, id: string) => {
    if (act === "rename") setEditing({ t: "node", id });
    if (act === "deepen") void runDeepen(id);
    if (act === "delete") { select({ t: "node", id }); void removeSelected(); }
    if (act === "connect") {
      select({ t: "node", id });
      setTip("Drag the dot on the right edge onto another box");
    }
  };

  const commitLabel = useCallback(async (id: string, after: string) => {
    setEditing(null);
    const before = byId(id)?.label ?? "";
    if (after === before) return;
    await mutate({ op: "node", id, label: after }).then((e) => e && refusal(e));
  }, [byId]);

  const cancelEdit = useCallback(() => setEditing(null), []);

  const commitAnno = useCallback(async (id: string, text: string) => {
    setEditing(null);
    const known = board.annos.find((a) => a.id === id);
    const draft = getUi().noteDrafts[id];
    if (!text) {
      clearNoteDraft(id);
      if (known && !draft) await mutate({ op: "note", id, text: "" }).then((e) => e && refusal(e));
      return;
    }
    if (known && known.text === text && !draft) return;
    const payload: Record<string, unknown> = { op: "note", id, text };
    if (draft) { payload.x = draft.x; payload.y = draft.y; }
    if (!known && draft?.anchor) payload.anchor = draft.anchor;
    const error = await mutate(payload);
    if (error) refusal(error);
    else clearNoteDraft(id);
  }, [board.annos]);

  /* ── pointer — one handler decides between panning, dragging and connecting ── */

  const capture = (id: number) => {
    try { viewport.current?.setPointerCapture(id); } catch { /* no active pointer */ }
  };

  const onPointerDown = (e: React.PointerEvent) => {
    const t = e.target as HTMLElement;
    if (t.closest('[data-role="bar"]')) return;
    if (t.getAttribute && t.getAttribute("contenteditable") === "true") return;

    const port = t.closest('[data-role="port"]');
    const nodeEl = t.closest<HTMLElement>(".node");
    const annoEl = t.closest<HTMLElement>(".anno");
    const p = toWorld(e);
    const tool = getUi().tool;

    if (port && nodeEl && !readOnly) {
      e.preventDefault();
      drag.current = { mode: "wire", from: nodeEl.dataset.id! };
      capture(e.pointerId);
      return;
    }
    if (nodeEl && e.button === 0 && tool === "select") {
      const n = byId(nodeEl.dataset.id!);
      if (!n) return;
      e.preventDefault();
      select({ t: "node", id: n.id });
      drag.current = { mode: "node", id: n.id, ox: p.x - n.cx, oy: p.y - n.y, moved: false };
      capture(e.pointerId);
      return;
    }
    if (annoEl && e.button === 0 && tool === "select") {
      const a = board.annos.find((x) => x.id === annoEl.dataset.id);
      if (!a) return;
      e.preventDefault();
      select({ t: "anno", id: a.id });
      drag.current = { mode: "anno", id: a.id, ox: p.x - a.x, oy: p.y - a.y, moved: false };
      capture(e.pointerId);
      return;
    }

    // empty board
    if (tool === "node") { void addBoxAt(p.x, p.y); return; }
    if (tool === "note") { addNoteAt(p.x, p.y); return; }
    select(null);
    drag.current = { mode: "pan", sx: e.clientX, sy: e.clientY, vx: view.current.x, vy: view.current.y };
    if (viewport.current) viewport.current.dataset.panning = "1";
    capture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    if (d.mode === "pan") {
      view.current = { ...view.current, x: d.vx + (e.clientX - d.sx), y: d.vy + (e.clientY - d.sy) };
      applyView();
      return;
    }
    const p = toWorld(e);
    if (d.mode === "node") { d.moved = true; draftNode(d.id, p.x - d.ox, p.y - d.oy); return; }
    if (d.mode === "anno") { d.moved = true; draftNote(d.id, p.x - d.ox, p.y - d.oy); return; }
    if (d.mode === "wire") {
      const a = byId(d.from);
      if (!a) return;
      const r = rect(a, heights);
      const p0 = { x: r.x + r.w, y: r.y + r.h / 2 };
      const dd = Math.max(40, Math.abs(p.x - p0.x) * 0.45);
      setTempWire(`M${p0.x} ${p0.y} C${p0.x + dd} ${p0.y} ${p.x - dd} ${p.y} ${p.x} ${p.y}`);
    }
  };

  const endDrag = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    if (viewport.current) delete viewport.current.dataset.panning;
    try { viewport.current?.releasePointerCapture(e.pointerId); } catch { /* already gone */ }

    if (d.mode === "node" && d.moved) {
      const n = byId(d.id);
      if (!n) return;
      const cx = snap(n.cx), y = snap(n.y);
      draftNode(d.id, cx, y);
      void mutate({ op: "move", id: d.id, x: cx, y }).then((error) => {
        if (error) refusal(error);
        clearNodeDraft(d.id);
      });
      return;
    }
    if (d.mode === "anno" && d.moved) {
      const a = board.annos.find((x) => x.id === d.id);
      if (!a) return;
      const x = snap(a.x), y = snap(a.y);
      draftNote(d.id, x, y);
      /* a note that has never reached the harness has nothing to move yet —
         its position rides along when its text is committed */
      if (!arch?.annotations.some((n) => n.id === d.id)) return;
      void mutate({ op: "note", id: d.id, x, y }).then((error) => {
        if (error) refusal(error);
        clearNoteDraft(d.id);
      });
      return;
    }
    if (d.mode === "wire") {
      setTempWire(null);
      const el = document.elementFromPoint(e.clientX, e.clientY);
      const hit = el?.closest<HTMLElement>(".node");
      if (hit?.dataset.id && hit.dataset.id !== d.from) {
        void send({ op: "connect", src: d.from, dst: hit.dataset.id });
      }
    }
  };

  const onDoubleClick = (e: React.MouseEvent) => {
    const t = e.target as HTMLElement;
    const nodeEl = t.closest<HTMLElement>(".node");
    if (nodeEl) { e.stopPropagation(); setEditing({ t: "node", id: nodeEl.dataset.id! }); return; }
    const annoEl = t.closest<HTMLElement>(".anno");
    if (annoEl) { e.stopPropagation(); setEditing({ t: "anno", id: annoEl.dataset.id! }); return; }
    const p = toWorld(e);
    void addBoxAt(p.x, p.y);
  };

  /* wheel: trackpad scroll pans, pinch/ctrl zooms. Non-passive so the page
     itself never scrolls underneath the board. */
  useEffect(() => {
    const vp = viewport.current;
    if (!vp) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = vp.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) {
        zoomAt(e.clientX - r.left, e.clientY - r.top, view.current.k * Math.exp(-e.deltaY * 0.0018));
      } else {
        view.current = { ...view.current, x: view.current.x - e.deltaX, y: view.current.y - e.deltaY };
        applyView();
      }
    };
    vp.addEventListener("wheel", onWheel, { passive: false });
    return () => vp.removeEventListener("wheel", onWheel);
  }, [applyView, view, zoomAt]);

  /* ── keyboard ───────────────────────────────────────────────────────── */
  const nudge = useCallback((n: BoardNode, dx: number, dy: number) => {
    const cx = n.cx + dx, y = n.y + dy;
    draftNode(n.id, cx, y);
    void mutate({ op: "move", id: n.id, x: cx, y }).then((error) => {
      if (error) refusal(error);
      clearNodeDraft(n.id);
    });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "escape") { select(null); return; }
      if (k === "v") { setTool("select"); return; }
      if (k === "n") { setTool("node"); return; }
      if (k === "t") { setTool("note"); return; }
      if (k === "f") { frame(null, 70, 1); return; }

      const sel = getUi().selected;
      if (!sel) return;

      /* the keyboard equivalent of dragging — same round trip, so the harness
         hears about it the same way */
      const step = ({ arrowleft: [-1, 0], arrowright: [1, 0], arrowup: [0, -1], arrowdown: [0, 1] } as
        Record<string, [number, number]>)[k];
      if (step) {
        e.preventDefault();
        if (readOnly) return;
        const px = e.shiftKey ? 40 : 8;
        if (sel.t === "node") {
          const n = byId(sel.id);
          if (n) nudge(n, step[0] * px, step[1] * px);
        } else {
          const a = board.annos.find((x) => x.id === sel.id);
          if (!a) return;
          const x = a.x + step[0] * px, y = a.y + step[1] * px;
          draftNote(a.id, x, y);
          void mutate({ op: "note", id: a.id, x, y }).then((error) => {
            if (error) refusal(error);
            clearNoteDraft(a.id);
          });
        }
        return;
      }
      if (k === "e" && sel.t === "node") { e.preventDefault(); void runDeepen(sel.id); return; }
      if (e.key === "Backspace" || e.key === "Delete") { e.preventDefault(); void removeSelected(); }
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [frame, byId, board.annos, nudge, readOnly]);

  /* the composer's hint tracks what is selected */
  useEffect(() => {
    const sel = ui.selected;
    if (sel && sel.t === "node") {
      const n = board.nodes.find((x) => x.id === sel.id);
      setTip(n ? `Press T to pin a note to ${n.label}` : "");
    } else {
      setTip("Select a box first to pin a note to it");
    }
  }, [ui.selected, board.nodes, setTip]);

  const flashNonce = (id: string) => (ui.flash.ids.includes(id) ? ui.flash.nonce : 0);
  const empty = arch !== null && board.nodes.length === 0;

  return (
    <section className="board" aria-label="Architecture board" data-od-id="board">
      <div
        className="viewport"
        id="viewport"
        ref={viewport}
        data-tool={ui.tool}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={onDoubleClick}
      >
        <div className="world" id="world" ref={world}>
          <Lanes lanes={board.lanes} />
          <Wires paths={paths} temp={tempWire} />
          <div id="nodes">
            {board.nodes.map((n) => (
              <NodeCard
                key={n.id}
                node={n}
                selected={ui.selected?.t === "node" && ui.selected.id === n.id}
                flashNonce={flashNonce(n.id)}
                editing={ui.editing?.t === "node" && ui.editing.id === n.id}
                animate={!bornWith[n.id]}
                onAct={onAct}
                onCommitLabel={commitLabel}
                onEditCancelled={cancelEdit}
                onFocus={(id) => {
                  const sel = getUi().selected;
                  if (!sel || sel.t !== "node" || sel.id !== id) select({ t: "node", id });
                }}
                register={register}
              />
            ))}
          </div>
          <div id="annos">
            {board.annos.map((a) => (
              <Annotation
                key={a.id}
                anno={a}
                selected={ui.selected?.t === "anno" && ui.selected.id === a.id}
                editing={ui.editing?.t === "anno" && ui.editing.id === a.id}
                onCommit={commitAnno}
                register={registerAnno}
              />
            ))}
            {/* a note being typed for the first time is not in the harness yet */}
            {Object.entries(ui.noteDrafts)
              .filter(([id]) => !board.annos.some((a) => a.id === id))
              .map(([id, pos]) => (
                <Annotation
                  key={id}
                  anno={{ id, x: pos.x, y: pos.y, w: 190, text: "" }}
                  selected={ui.selected?.t === "anno" && ui.selected.id === id}
                  editing={ui.editing?.t === "anno" && ui.editing.id === id}
                  onCommit={commitAnno}
                  register={registerAnno}
                />
              ))}
          </div>
        </div>
      </div>

      <p className="hint" data-od-id="board-hint">
        Drag or <kbd>↑↓←→</kbd> to move · double-click to add a box · <kbd>E</kbd> deepens · <kbd>⌫</kbd> deletes
      </p>

      <div className="board-empty" id="board-empty" hidden={!empty} data-od-id="board-empty">
        <p className="be-title">Nothing on the board yet</p>
        <p className="be-body">
          Press <kbd>N</kbd> then click to drop a box — or just tell the architect what you are building and it will start drawing.
        </p>
      </div>

      <Dock tool={ui.tool} onPick={(t: Tool) => setTool(t)} />
      <Zoomer
        level={level}
        onIn={() => zoomAt((viewport.current?.clientWidth ?? 0) / 2, (viewport.current?.clientHeight ?? 0) / 2, view.current.k * 1.25)}
        onOut={() => zoomAt((viewport.current?.clientWidth ?? 0) / 2, (viewport.current?.clientHeight ?? 0) / 2, view.current.k / 1.25)}
        onFit={() => frame(null, 70, 1)}
        onTidy={() => void send({ op: "tidy" })}
        canTidy={!readOnly && board.nodes.some((n) => arch?.nodes[n.id]?.x != null)}
      />
    </section>
  );
}
