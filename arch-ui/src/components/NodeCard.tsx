import { memo, useCallback, useEffect, useRef } from "react";
import { laneVars, W } from "../board/geometry";
import type { BoardNode } from "../board/types";
import { useArriving } from "../hooks/useArriving";
import { IconConnect, IconDeepen, IconDelete, IconRename } from "./icons";

export type NodeAct = "rename" | "deepen" | "connect" | "delete";

interface Props {
  node: BoardNode;
  selected: boolean;
  /** bumped whenever this box should halo; 0 means never */
  flashNonce: number;
  editing: boolean;
  /** false for a box that was already there when the page loaded */
  animate: boolean;
  onAct: (act: NodeAct, id: string) => void;
  onCommitLabel: (id: string, label: string) => void;
  onEditCancelled: (id: string) => void;
  onFocus: (id: string) => void;
  register: (id: string, el: HTMLElement | null) => void;
}

function NodeCardImpl({
  node: n, selected, flashNonce, editing, animate,
  onAct, onCommitLabel, onEditCancelled, onFocus, register,
}: Props) {
  const arriving = useArriving(animate);
  const ref = useRef<HTMLDivElement | null>(null);
  const labelRef = useRef<HTMLSpanElement | null>(null);

  /* Stable, or React detaches and re-attaches on every render — and the height
     observer's detach path writes state, which turns that into a loop. */
  const setRef = useCallback((el: HTMLDivElement | null) => {
    ref.current = el;
    register(n.id, el);
  }, [n.id, register]);

  /* The halo is a CSS animation, and re-adding a class in the same commit will
     not restart one — the reflow between remove and add is what does. That is
     inherently imperative, so it stays imperative. */
  useEffect(() => {
    const el = ref.current;
    if (!el || !flashNonce) return;
    el.classList.remove("just", "grew");
    void el.offsetWidth;
    el.classList.add("just", "grew");
    const t = setTimeout(() => el.classList.remove("grew"), 900);
    return () => clearTimeout(t);
  }, [flashNonce]);

  /* Inline rename, the same contenteditable dance as the prototype. */
  useEffect(() => {
    if (!editing) return;
    const span = labelRef.current;
    if (!span) return;
    const before = span.textContent ?? "";
    span.setAttribute("contenteditable", "true");
    span.focus();
    const range = document.createRange();
    range.selectNodeContents(span);
    const sel = getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);

    let cancelled = false;
    const finish = () => {
      span.removeAttribute("contenteditable");
      const after = (span.textContent ?? "").trim() || before;
      span.textContent = after;
      if (cancelled || after === before) onEditCancelled(n.id);
      else onCommitLabel(n.id, after);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Enter") { e.preventDefault(); span.blur(); }
      if (e.key === "Escape") { cancelled = true; span.textContent = before; span.blur(); }
    };
    span.addEventListener("blur", finish, { once: true });
    span.addEventListener("keydown", onKey);
    return () => {
      span.removeEventListener("blur", finish);
      span.removeEventListener("keydown", onKey);
    };
  }, [editing, n.id, onCommitLabel, onEditCancelled]);

  const showResp = n.depth !== "stub" && n.resp;
  const showDeep = n.depth === "detailed" && (n.tech || n.rows.length);
  const w = W[n.depth];

  return (
    <div
      ref={setRef}
      className={"node" + (arriving ? " arriving" : "")}
      data-id={n.id}
      data-od-id={"node-" + n.id}
      data-depth={n.depth}
      data-lane={n.slot}
      data-lane-id={n.lane}
      {...(n.out ? { "data-out": "1" } : {})}
      {...(selected ? { "data-sel": "1" } : {})}
      tabIndex={0}
      role="group"
      aria-roledescription="board box"
      aria-label={[n.label, n.kind, n.depth === "stub" ? "outline only" : n.depth, n.out ? "not taken" : ""]
        .filter(Boolean).join(", ")}
      /* tabbing to a box is the keyboard equivalent of clicking it: it becomes
         the live one, so its toolbar and port become reachable in turn */
      onFocus={() => onFocus(n.id)}
      style={{ width: w, left: Math.round(n.cx - w / 2), top: Math.round(n.y), ...laneVars(n.slot) }}
    >
      <div className="node-head">
        <span className="node-label" data-role="label" ref={labelRef}>{n.label}</span>
        <span className="node-kind mono">{n.kind}</span>
      </div>

      {showResp ? <p className="node-resp">{n.resp}</p> : null}

      {showDeep ? (
        <div className="node-deep">
          {n.tech ? <div className="node-tech">{n.tech}</div> : null}
          {n.rows.length ? (
            <ul className="node-rows">
              {n.rows.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          ) : null}
        </div>
      ) : null}

      {n.out ? null : (
        <button className="port" data-role="port" aria-label="Draw a connection from this box" />
      )}

      <div className="node-bar" data-role="bar">
        <button data-act="rename" title="Rename" aria-label="Rename"
                onClick={() => onAct("rename", n.id)}><IconRename /></button>
        <button data-act="deepen" title="Deepen" aria-label="Deepen"
                onClick={() => onAct("deepen", n.id)}><IconDeepen /></button>
        <button data-act="connect" title="Connect" aria-label="Connect"
                onClick={() => onAct("connect", n.id)}><IconConnect /></button>
        <span className="sep" />
        <button data-act="delete" title="Delete" aria-label="Delete"
                onClick={() => onAct("delete", n.id)}><IconDelete /></button>
      </div>
    </div>
  );
}

export const NodeCard = memo(NodeCardImpl);
