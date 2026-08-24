import { memo, useCallback, useEffect, useRef } from "react";
import type { Anno } from "../board/types";
import { useArriving } from "../hooks/useArriving";

interface Props {
  anno: Anno;
  selected: boolean;
  editing: boolean;
  onCommit: (id: string, text: string) => void;
  register: (id: string, el: HTMLElement | null) => void;
}

function AnnotationImpl({ anno: a, selected, editing, onCommit, register }: Props) {
  const arriving = useArriving();
  const ref = useRef<HTMLDivElement | null>(null);

  /* Stable — see NodeCard. */
  const setRef = useCallback((el: HTMLDivElement | null) => {
    ref.current = el;
    register(a.id, el);
  }, [a.id, register]);

  /* Text is written straight into the element while editing, so React must not
     also own it — the value is committed on blur instead. */
  useEffect(() => {
    const el = ref.current;
    if (el && el.getAttribute("contenteditable") !== "true" && el.textContent !== a.text) {
      el.textContent = a.text;
    }
  }, [a.text]);

  useEffect(() => {
    if (!editing) return;
    const el = ref.current;
    if (!el) return;
    el.setAttribute("contenteditable", "true");
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);

    const finish = () => {
      el.removeAttribute("contenteditable");
      onCommit(a.id, (el.textContent ?? "").trim());
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") el.blur(); };
    el.addEventListener("blur", finish, { once: true });
    el.addEventListener("keydown", onKey);
    return () => {
      el.removeEventListener("blur", finish);
      el.removeEventListener("keydown", onKey);
    };
  }, [editing, a.id, onCommit]);

  return (
    <div
      ref={setRef}
      className={"anno" + (arriving ? " arriving" : "")}
      data-id={a.id}
      data-od-id={"note-" + a.id}
      {...(selected ? { "data-sel": "1" } : {})}
      style={{ left: Math.round(a.x), top: Math.round(a.y), width: a.w || 180 }}
    />
  );
}

export const Annotation = memo(AnnotationImpl);
