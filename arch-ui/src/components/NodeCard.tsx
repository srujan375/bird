import { Fragment, memo, useCallback, useEffect, useRef, useState } from "react";
import { laneVars, W } from "../board/geometry";
import type { BoardNode } from "../board/types";
import { KINDS, ROWS_SHOWN } from "../board/vocab";
import { useArriving } from "../hooks/useArriving";
import { IconConnect, IconDeepen, IconDelete, IconRename } from "./icons";

export type NodeAct = "rename" | "deepen" | "connect" | "delete" | "fold";
/** which way Tab asked to go after a field was committed */
export type Nav = "next" | "prev" | null;

/**
 * A field id names one part of the card a person can type into:
 * `label` · `resp` · `tech` · `detail` (prose lines, multi-line) ·
 * `fact:<key>` · `item:<index>` · `item:new`.
 */
export type FieldId = string;

/** The order Tab walks a selected box's fields. */
export function fieldOrder(n: BoardNode): FieldId[] {
  if (n.group) return ["label"];
  const out: FieldId[] = ["label", "resp"];
  for (const [k] of n.facts) out.push("fact:" + k);
  out.push("tech");
  if (n.items.length) {
    n.items.forEach((_, i) => out.push("item:" + i));
    out.push("item:new");
  } else {
    out.push("detail");
  }
  return out;
}

/** One list row as text, and back. `k v — d`; the key is the leading short
 *  token when there is one. */
export function itemText(it: { k: string; v: string; d: string }): string {
  return (it.k ? it.k + " " : "") + it.v + (it.d ? " — " + it.d : "");
}
export function parseItem(text: string, defaultKey: string): { k: string; v: string; d: string } | null {
  const t = text.trim();
  if (!t) return null;
  const [head, ...rest] = t.split(/\s+[—–-]{1,2}\s+/);
  const d = rest.join(" — ").trim();
  const m = /^([A-Za-z][\w-]{0,7})\s+(\S.*)$/.exec(head.trim());
  if (m) return { k: m[1], v: m[2].trim(), d };
  return { k: defaultKey, v: head.trim(), d };
}

interface Props {
  node: BoardNode;
  selected: boolean;
  /** bumped whenever this box should halo; 0 means never */
  flashNonce: number;
  /** which field is being typed into, if any */
  editing: FieldId | null;
  /** false for a box that was already there when the page loaded */
  animate: boolean;
  onAct: (act: NodeAct, id: string) => void;
  onCommitField: (id: string, field: FieldId, text: string, nav: Nav) => void;
  onEditCancelled: (id: string, field: FieldId, nav: Nav) => void;
  onSetKind: (id: string, kind: string) => void;
  onFocus: (id: string) => void;
  register: (id: string, el: HTMLElement | null) => void;
}

function NodeCardImpl({
  node: n, selected, flashNonce, editing, animate,
  onAct, onCommitField, onEditCancelled, onSetKind, onFocus, register,
}: Props) {
  const arriving = useArriving(animate);
  const ref = useRef<HTMLDivElement | null>(null);
  const fields = useRef(new Map<FieldId, HTMLElement>());
  const bind = (f: FieldId) => (el: HTMLElement | null) => {
    if (el) fields.current.set(f, el);
    else fields.current.delete(f);
  };
  const [kindOpen, setKindOpen] = useState(false);
  useEffect(() => { if (!selected) setKindOpen(false); }, [selected]);

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

  /* Inline editing: the field goes live where it is. Enter commits a one-line
     field; the prose block is lines, so there Enter is a newline and ⌘/Ctrl
     Enter commits. Tab commits and asks for the next field, Shift-Tab the
     previous. Escape puts back what was there. */
  useEffect(() => {
    if (!editing) return;
    const el = fields.current.get(editing);
    if (!el) return;
    const multiline = editing === "detail";
    const before = el.dataset.empty ? "" : (el.innerText ?? "");
    if (el.dataset.empty) el.textContent = "";
    el.setAttribute("contenteditable", multiline ? "plaintext-only" : "true");
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);

    let cancelled = false;
    let nav: Nav = null;
    const finish = () => {
      el.removeAttribute("contenteditable");
      const after = (el.innerText ?? "").trim();
      if (cancelled || after === before) { el.textContent = before; onEditCancelled(n.id, editing, nav); }
      else onCommitField(n.id, editing, after, nav);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab") { e.preventDefault(); nav = e.shiftKey ? "prev" : "next"; el.blur(); return; }
      if (e.key === "Enter" && (!multiline || e.metaKey || e.ctrlKey)) { e.preventDefault(); el.blur(); }
      if (e.key === "Escape") { cancelled = true; el.blur(); }
    };
    el.addEventListener("blur", finish, { once: true });
    el.addEventListener("keydown", onKey);
    return () => {
      el.removeEventListener("blur", finish);
      el.removeEventListener("keydown", onKey);
    };
  }, [editing, n.id, onCommitField, onEditCancelled]);

  const open = Boolean(n.group && !n.group.folded);
  /* Selected, the card opens into fields: every section it could have,
     filled or not, framed and labelled, so there is always somewhere to click.
     Unselected, it shows what its depth says it shows. */
  const reveal = selected && !open && !n.out;
  const facts = reveal ? n.facts : n.facts.filter(([, v]) => v);
  const showResp = !open && ((n.depth !== "stub" && n.resp) || reveal || editing === "resp");
  const showFacts = !open && ((n.depth !== "stub" && (facts.length > 0 || n.tech)) || reveal || editing?.startsWith("fact:") || editing === "tech");
  const rows = n.items.length ? n.items : n.rows.map((v) => ({ k: "", v, d: "" }));
  const showList = !open && ((n.depth === "detailed" && rows.length > 0) || reveal || editing === "detail" || editing?.startsWith("item:"));
  const showFoot = !open && n.depth === "detailed" && n.derived.length > 0;
  const shown = reveal || editing?.startsWith("item:") ? rows : rows.slice(0, ROWS_SHOWN);
  const w = open ? n.group!.w : W[n.depth];
  const empty = (yes: boolean) => (yes ? { "data-empty": "1" } : {});
  /* a one-word value is a chip; anything longer is text */
  const chip = (v: string) => /^[\w.-]+$/.test(v);
  const keys = editing
    ? editing === "detail"
      ? [["↵", "new line"], ["⌘↵", "save"], ["tab", "next"], ["esc", "cancel"]]
      : [["↵", "save"], ["tab", "next"], ["esc", "cancel"]]
    : null;

  return (
    <div
      ref={setRef}
      className={"node" + (arriving ? " arriving" : "")}
      data-id={n.id}
      data-od-id={"node-" + n.id}
      data-depth={n.depth}
      data-lane={n.slot}
      data-lane-id={n.lane}
      {...(n.parent ? { "data-parent": n.parent } : {})}
      {...(n.group ? { "data-group": open ? "open" : "folded" } : {})}
      {...(n.out ? { "data-out": "1" } : {})}
      {...(selected ? { "data-sel": "1" } : {})}
      {...(reveal ? { "data-fields": "1" } : {})}
      {...(editing ? { "data-editing": editing } : {})}
      tabIndex={0}
      role="group"
      aria-roledescription="board box"
      aria-label={[
        n.label, n.kind, n.depth === "stub" ? "outline only" : n.depth, n.out ? "not taken" : "",
        n.group ? `${n.group.count} boxes inside, ${open ? "open" : "folded"}` : "",
      ].filter(Boolean).join(", ")}
      /* tabbing to a box is the keyboard equivalent of clicking it: it becomes
         the live one, so its toolbar and port become reachable in turn */
      onFocus={() => onFocus(n.id)}
      style={{
        width: w, left: Math.round(n.cx - w / 2), top: Math.round(n.y),
        ...(open ? { height: n.group!.h } : {}),
        ...laneVars(n.slot),
      }}
    >
      <div className="node-head">
        {n.group ? (
          /* the fold toggle: one click opens the container or shuts it; the
             harness never hears about it, it is how *this* reader is reading */
          <button
            className="fold"
            data-role="fold"
            title={open ? "Fold shut" : `Open — ${n.group.count} inside`}
            aria-label={open ? "Fold shut" : "Open"}
            aria-expanded={open}
            onClick={(e) => { e.stopPropagation(); onAct("fold", n.id); }}
          >
            {open ? "−" : "+"}
          </button>
        ) : null}
        <span className="node-label" data-role="label" data-field="label" data-tag="name" ref={bind("label")}>{n.label}</span>
        {n.group
          ? <span className="node-count mono">{n.group.count} inside</span>
          : (
            <span className="node-kind-wrap">
              <button
                className="node-kind mono"
                data-role="kind"
                title="Change kind"
                aria-label={`Kind: ${n.kind}. Click to change`}
                aria-haspopup="listbox"
                aria-expanded={kindOpen}
                onClick={(e) => { e.stopPropagation(); if (reveal) setKindOpen((v) => !v); }}
              >{n.kind}</button>
              {kindOpen ? (
                <ul className="kind-menu" role="listbox" data-role="kind" aria-label="Kind">
                  {KINDS.map((k) => (
                    <li
                      key={k}
                      role="option"
                      aria-selected={k === n.kind}
                      {...(k === n.kind ? { "data-on": "1" } : {})}
                      onClick={(e) => { e.stopPropagation(); setKindOpen(false); if (k !== n.kind) onSetKind(n.id, k); }}
                    >{k}</li>
                  ))}
                </ul>
              ) : null}
            </span>
          )}
      </div>

      {showResp ? (
        <p className="node-resp" data-field="resp" data-tag="does" ref={bind("resp")} {...empty(!n.resp)}>
          {n.resp || "what it is responsible for"}
        </p>
      ) : null}

      {showFacts ? (
        <dl className="kv">
          {facts.map(([k, v]) => (
            <Fragment key={k}>
              <dt>{k}</dt>
              <dd data-field={"fact:" + k} ref={bind("fact:" + k)} {...(v ? {} : { className: "empty", "data-empty": "1" })}>
                {v ? (chip(v) ? <span className="chip">{v}</span> : v) : "—"}
              </dd>
            </Fragment>
          ))}
          {n.tech || reveal || editing === "tech" ? (
            <Fragment>
              <dt>tech</dt>
              <dd className="wrap" data-field="tech" ref={bind("tech")} {...empty(!n.tech)}>{n.tech || "built on"}</dd>
            </Fragment>
          ) : null}
        </dl>
      ) : null}

      {showList ? (
        <div className="rows">
          <div className="rows-head">
            <span>{n.listName}</span>
            <span>{rows.length > shown.length ? `${shown.length} of ${rows.length}` : rows.length || ""}</span>
          </div>
          {n.items.length || (reveal && n.depth === "detailed" && !n.rows.length) ? (
            <ul>
              {shown.map((r, i) => (
                <li key={i} data-field={"item:" + i} ref={bind("item:" + i)}>
                  <span className="k">{r.k}</span>
                  <span className="v">{r.v}</span>
                  {r.d ? <span className="d">{r.d}</span> : null}
                </li>
              ))}
              {reveal ? (
                <li className="add" data-field="item:new" data-empty="1" ref={bind("item:new")}>
                  <span className="k"></span>
                  <span className="v">{`add a row — ${n.listName.replace(/s$/, "")}, then a note`}</span>
                </li>
              ) : null}
            </ul>
          ) : (
            /* prose lines are one editable block: what the architect wrote,
               one line each, until it is turned into rows */
            <div className="prose" data-field="detail" data-tag="inside" ref={bind("detail")} {...empty(!n.rows.length)}>
              {n.rows.length ? n.rows.join("\n") : "what is inside — one line each"}
            </div>
          )}
          {rows.length > shown.length ? <div className="more">+ {rows.length - shown.length} more</div> : null}
        </div>
      ) : null}

      {showFoot ? (
        <div className="derived">
          {n.derived.map((g) => (
            <span className="grp" key={g.side}>
              <b>{g.side}</b>
              {g.names.map((nm) => <i key={nm}>{nm}</i>)}
            </span>
          ))}
        </div>
      ) : null}

      {n.out ? null : (
        <button className="port" data-role="port" aria-label="Draw a connection from this box" />
      )}

      {keys ? (
        <div className="keys" aria-hidden="true">
          {keys.map(([k, what]) => <span key={k}><b>{k}</b> {what}</span>)}
        </div>
      ) : (
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
      )}
    </div>
  );
}

export const NodeCard = memo(NodeCardImpl);
