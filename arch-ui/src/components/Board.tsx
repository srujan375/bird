import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toBoard } from "../board/adapter";
import { bounds as calcBounds, rect, snap, STEP, wirePaths } from "../board/geometry";
import type { BoardNode, NodeField, Tool } from "../board/types";
import {
  clearNodeDraft, clearNoteDraft, draftNode, draftNote, flash, getUi,
  nextLocalId, select, setEditing, setFolded, setHot, setTool, useUi,
} from "../board/ui";
import { setViewApi } from "../board/viewApi";
import { mutate, refusal, useSession } from "../wire/session";
import { useNodeHeights } from "../hooks/useNodeHeights";
import { useView } from "../hooks/useView";
import { Annotation } from "./Annotation";
import { Dock } from "./Dock";
import { Lanes } from "./Lanes";
import { fieldOrder, itemText, NodeCard, parseItem, type Nav, type NodeAct } from "./NodeCard";
import { KIND_LIST } from "../board/vocab";
import { Wires } from "./Wires";
import { Zoomer } from "./Zoomer";

type Drag =
  | { mode: "pan"; sx: number; sy: number; vx: number; vy: number }
  | { mode: "node"; id: string; ox: number; oy: number; moved: boolean; cx0: number; y0: number }
  | { mode: "anno"; id: string; ox: number; oy: number; moved: boolean }
  | { mode: "wire"; from: string };

const EMPTY = { lanes: [], nodes: [], wires: [], annos: [] };

/** The key a new list row wears when the person typed none — mirrors
 *  KIND_LIST in state.py. */
const DEFAULT_KEY: Record<string, string> = {
  service: "op", api: "GET", store: "tbl", queue: "msg", ui: "scr", llm: "fn", external: "call", infra: "res",
};
const defaultKey = (kind: string) => DEFAULT_KEY[kind] ?? "";

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
    const view = arch ? toBoard(arch, heights, ui.folded) : EMPTY;
    if (!Object.keys(ui.drafts).length && !Object.keys(ui.noteDrafts).length) return view;
    return {
      ...view,
      nodes: view.nodes.map((n) => (ui.drafts[n.id] ? { ...n, ...ui.drafts[n.id] } : n)),
      annos: view.annos.map((a) => (ui.noteDrafts[a.id] ? { ...a, ...ui.noteDrafts[a.id] } : a)),
    };
  }, [arch, heights, ui.drafts, ui.noteDrafts, ui.folded]);

  const index = useMemo(() => new Map(board.nodes.map((n) => [n.id, n])), [board.nodes]);
  const byId = useCallback((id: string) => index.get(id), [index]);

  /* What the wires light up for: the box under the pointer and the one
     selected. Everything not touching them steps back, so "what is this
     connected to" is answered by pointing at it. */
  const hot = useMemo(() => {
    const ids = new Set<string>();
    if (ui.hot && index.has(ui.hot)) ids.add(ui.hot);
    if (ui.selected?.t === "node" && index.has(ui.selected.id)) ids.add(ui.selected.id);
    return ids;
  }, [ui.hot, ui.selected, index]);

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
    setTimeout(() => setEditing({ t: "node", id, field: "label" }), 30);
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

  /** Every box somebody has placed by hand inside `id`, however deep. */
  const placedInside = (id: string) => {
    if (!arch) return [];
    const out: typeof arch.nodes[string][] = [];
    const walk = (pid: string) => {
      for (const n of Object.values(arch.nodes)) {
        if (n.parent !== pid || n.id === pid) continue;
        if (n.x !== null && n.y !== null) out.push(n);
        walk(n.id);
      }
    };
    walk(id);
    return out;
  };

  const toggleFold = (id: string) => {
    const n = byId(id);
    if (!n?.group) return;
    setFolded(id, !n.group.folded);
  };

  const runDeepen = async (id: string) => {
    const n = byId(id);
    if (!n || readOnly) return;
    // a container's detail is what is inside it
    if (n.group) { toggleFold(id); return; }
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
    if (act === "fold") { toggleFold(id); return; }
    if (act === "rename") setEditing({ t: "node", id, field: "label" });
    if (act === "deepen") void runDeepen(id);
    if (act === "delete") { select({ t: "node", id }); void removeSelected(); }
    if (act === "connect") {
      select({ t: "node", id });
      setTip("Drag the dot on the right edge onto another box");
    }
  };

  /* Tab walks the fields of a selected box; committing one hands the next
     one the caret. */
  const advance = useCallback((id: string, field: NodeField, nav: Nav) => {
    if (!nav) { setEditing(null); return; }
    const n = byId(id);
    if (!n) { setEditing(null); return; }
    const order = fieldOrder(n);
    const i = order.indexOf(field);
    const next = order[(i + (nav === "next" ? 1 : -1) + order.length) % order.length];
    setEditing({ t: "node", id, field: next });
  }, [byId]);

  /* What the card calls a field and what the harness calls it. Filling a
     part of a box the depth was hiding deepens the box to show it — the two
     are the same statement about how much of the design is known. */
  const commitField = useCallback(async (id: string, field: NodeField, text: string, nav: Nav) => {
    advance(id, field, nav);
    const n = byId(id);
    if (!n) return;
    const payload: Record<string, unknown> = { op: "node", id };
    const deepenTo = (d: "sketch" | "detailed") => {
      if (n.depth === "stub" || (d === "detailed" && n.depth === "sketch")) payload.depth = d;
    };
    if (field === "label") {
      if (!text || text === n.label) return;
      payload.label = text;
    } else if (field === "resp") {
      payload.responsibility = text;
      if (text) deepenTo("sketch");
    } else if (field === "tech") {
      payload.tech = text;
      if (text) deepenTo("sketch");
    } else if (field === "detail") {
      payload.detail = text;
      if (text) deepenTo("detailed");
    } else if (field.startsWith("fact:")) {
      payload.facts = { [field.slice(5)]: text === "—" ? "" : text };
      if (text && text !== "—") deepenTo("sketch");
    } else if (field.startsWith("item:")) {
      const key = KIND_LIST[n.kind] ? (n.items[0]?.k || defaultKey(n.kind)) : "";
      const items = n.items.map((it) => ({ ...it }));
      const parsed = parseItem(text, key);
      if (field === "item:new") {
        if (!parsed) return;
        items.push(parsed);
      } else {
        const i = Number(field.slice(5));
        if (!parsed) items.splice(i, 1);        // an emptied row is a removed row
        else items[i] = parsed;
        if (parsed && itemText(parsed) === itemText(n.items[i] ?? { k: "", v: "", d: "" })) return;
      }
      payload.items = items;
      if (items.length) deepenTo("detailed");
    } else {
      return;
    }
    await mutate(payload).then((e) => e && refusal(e));
    if (payload.depth) flash([id]);
  }, [byId, advance]);

  const setKind = useCallback(async (id: string, kind: string) => {
    if (readOnly) return;
    await mutate({ op: "node", id, kind }).then((e) => e && refusal(e));
  }, [readOnly]);

  const cancelEdit = useCallback((id: string, field: NodeField, nav: Nav) => advance(id, field, nav), [advance]);

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
    if (t.closest('[data-role="bar"]') || t.closest('[data-role="fold"]') || t.closest('[data-role="kind"]')) return;
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
      /* on a box that is already selected, a click on one of its fields is a
         request to type there — the card has opened into fields for exactly
         this. The click is consumed; a drag has to start from the card's
         own ground. */
      const sel = getUi().selected;
      const fieldEl = t.closest<HTMLElement>("[data-field]");
      if (fieldEl && sel?.t === "node" && sel.id === n.id && !readOnly && !n.out) {
        e.preventDefault();
        setEditing({ t: "node", id: n.id, field: fieldEl.dataset.field! });
        return;
      }
      e.preventDefault();
      select({ t: "node", id: n.id });
      drag.current = { mode: "node", id: n.id, ox: p.x - n.cx, oy: p.y - n.y, moved: false, cx0: n.cx, y0: n.y };
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
      /* a container carries its hand-placed members with it; the laid-out
         ones follow on their own, because they are placed relative to it */
      const dx = cx - d.cx0, dy = y - d.y0;
      for (const m of placedInside(d.id)) {
        const mx = m.x! + dx, my = m.y! + dy;
        draftNode(m.id, mx, my);
        void mutate({ op: "move", id: m.id, x: mx, y: my }).then((error) => {
          if (error) refusal(error);
          clearNodeDraft(m.id);
        });
      }
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
    if (nodeEl) {
      e.stopPropagation();
      if (readOnly) return;
      const field = (t.closest<HTMLElement>("[data-field]")?.dataset.field as NodeField | undefined) ?? "label";
      setEditing({ t: "node", id: nodeEl.dataset.id!, field });
      return;
    }
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
      if (k === " " && sel.t === "node") { e.preventDefault(); toggleFold(sel.id); return; }

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
          <Wires paths={paths} temp={tempWire} hot={hot} />
          <div
            id="nodes"
            onPointerOver={(e) => setHot((e.target as HTMLElement).closest<HTMLElement>(".node")?.dataset.id ?? null)}
            onPointerLeave={() => setHot(null)}
          >
            {board.nodes.map((n) => (
              <NodeCard
                key={n.id}
                node={n}
                selected={ui.selected?.t === "node" && ui.selected.id === n.id}
                flashNonce={flashNonce(n.id)}
                editing={ui.editing?.t === "node" && ui.editing.id === n.id ? (ui.editing.field ?? "label") : null}
                animate={!bornWith[n.id]}
                onAct={onAct}
                onCommitField={commitField}
                onEditCancelled={cancelEdit}
                onSetKind={setKind}
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
        Drag or <kbd>↑↓←→</kbd> to move · select a box, then click any part of it to edit · double-click empty space to add one · <kbd>E</kbd> deepens · <kbd>space</kbd> folds a container · <kbd>⌫</kbd> deletes
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
