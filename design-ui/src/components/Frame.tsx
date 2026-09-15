import { useEffect, useRef, useState } from "react";
import { markFrameLoading, markFrameReady, postToFrame, registerFrame, unregisterFrame } from "../board/frames";
import { adoptSelfApplied, getUi, setUi } from "../board/ui";
import { RichLines } from "@arch/components/Rich";
import { critiqueNotes } from "../wire/session";
import type { WireArtboard, WireCritique } from "../wire/types";

interface Props {
  ab: WireArtboard;
  /** the current version's document, as the harness holds it */
  html: string;
  /** bumped to force a reload from the harness's copy */
  reload: number;
  x: number;
  w: number;
  h: number;
  focused: boolean;
  chosen: boolean;
  dim: boolean;
  /** what the designer is doing to this one right now — "editing",
   *  "reading", "reviewing" — or null when its hands are elsewhere */
  busy: string | null;
  lit: boolean;
  /** the critic's latest note on this artboard, if it has one */
  critique: WireCritique | null;
  /** a draft stood in this slot and has just handed over: the iframe stays
   *  see-through until its document has loaded, so the poster underneath
   *  shows instead of a white card */
  posterBehind: boolean;
  /** present only when deletion is allowed (not read-only, no turn running):
   *  fires on the CONFIRMING click of the card's two-step delete control */
  onDelete?: () => void;
  onFocus: () => void;
}

/** One artboard on the board: a named frame holding the document in a
 *  sandboxed iframe, with editor-bridge.js inside it answering the page. */
export function Frame({ ab, html, reload, x, w, h, focused, chosen, dim, busy, lit, critique, posterBehind, onDelete, onFocus }: Props) {
  const ref = useRef<HTMLIFrameElement | null>(null);
  const loaded = useRef<string | null>(null);
  const reloaded = useRef(reload);
  const [shown, setShown] = useState(false);
  const [critOpen, setCritOpen] = useState(false);

  /* the delete control's two-step confirm: the first click arms it, the
     second fires. The arm decays after a moment — a stray click must not
     take a card away, and the page has no modal to fall back on. */
  const [armed, setArmed] = useState(false);
  const armTimer = useRef<number | null>(null);
  const canDelete = onDelete !== undefined;
  useEffect(() => {
    // a turn started (or the session closed): the control is gone, so a
    // stale armed state must not survive its return
    if (!canDelete) setArmed(false);
    return () => {
      if (armTimer.current !== null) { clearTimeout(armTimer.current); armTimer.current = null; }
    };
  }, [canDelete]);

  const onDeleteClick = () => {
    if (!canDelete) return;
    if (armed) {
      setArmed(false);
      if (armTimer.current !== null) { clearTimeout(armTimer.current); armTimer.current = null; }
      onDelete();
      return;
    }
    setArmed(true);
    armTimer.current = window.setTimeout(() => { setArmed(false); armTimer.current = null; }, 3000);
  };

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    registerFrame(ab.id, el);
    return () => unregisterFrame(ab.id);
  }, [ab.id]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const forced = reload !== reloaded.current;
    reloaded.current = reload;
    if (loaded.current === html && !forced) return;
    if (!forced && adoptSelfApplied(ab.id, ab.current)) {
      /* this page made that version, in this very frame — take the harness's
         copy as the new baseline without reloading on top of the user */
      loaded.current = html;
      return;
    }
    loaded.current = html;
    /* the element's box and styles are about to be stale; the selector is
       kept so the load handler can put the selection back if it still holds */
    if (getUi().selection?.artboard === ab.id) setUi({ selection: null });
    /* nothing posted to the frame arrives until the new document has loaded:
       a capture asked for on this same push waits for the load event */
    markFrameLoading(ab.id);
    setShown(false);
    el.srcdoc = html;
  }, [ab.id, ab.current, html, reload]);

  /* the critique is about a version; a new one closes the old note */
  useEffect(() => { setCritOpen(false); }, [critique?.version]);

  const onLoad = () => {
    if (loaded.current === null) return; // about:blank, before any document
    markFrameReady(ab.id);
    setShown(true);
    postToFrame(ab.id, { type: "size" });
    const ui = getUi();
    if (ui.focus === ab.id) {
      postToFrame(ab.id, { type: "getTree" });
      /* quiet: after a reload the path may be gone, and that is not worth saying */
      if (ui.lastSelector) postToFrame(ab.id, { type: "select", selector: ui.lastSelector, quiet: true });
    }
  };

  const notes = critique ? critiqueNotes(critique.text) : 0;
  const stale = critique !== null && critique.version !== ab.current;
  const cls = "frame" + (focused ? " on" : "") + (chosen ? " chosen" : "") + (dim ? " dim" : "") + (lit ? " lit" : "")
    + (posterBehind && !shown ? " handing-over" : "");
  return (
    <div className={cls} style={{ transform: `translate(${x}px, 0px)`, width: w }} data-artboard={ab.id}
         data-od-id={"frame-" + ab.id}>
      <button type="button" className="frame-name stay" onClick={onFocus}
              title={focused ? "This artboard is in focus" : "Work in this artboard"}>
        <span className="nm">{ab.title || ab.id}</span>
        <span className="v mono">{ab.current}</span>
        {chosen ? <span className="tick mono">✓ chosen</span> : null}
      </button>
      <span className="frame-tools stay">
        {critique ? (
          <button type="button" className={"frame-crit" + (critOpen ? " open" : "") + (stale ? " stale" : "")}
                  aria-expanded={critOpen} aria-controls={"crit-" + ab.id}
                  title={stale ? `The critic's note on ${critique.version} — this artboard has moved on since`
                               : `The critic's note on ${critique.version}: ${notes} ${notes === 1 ? "item" : "items"}`}
                  data-od-id={"critique-" + ab.id}
                  onClick={() => setCritOpen((o) => !o)}>
            critique · {critique.version}{notes > 1 ? ` · ${notes}` : ""}
          </button>
        ) : null}
        {canDelete ? (
          <button type="button" className={"frame-del" + (armed ? " armed" : "")}
                  title={armed ? "Click again to delete — this cannot be undone"
                               : "Delete this artboard"}
                  data-od-id={"delete-" + ab.id}
                  onClick={onDeleteClick}>
            {armed ? "Delete?" : "✕"}
          </button>
        ) : null}
      </span>
      {busy ? <span className="busy stay">{busy}</span> : null}
      {critique && critOpen ? (
        <div className="crit-card stay" id={"crit-" + ab.id} role="note" data-od-id={"critique-card-" + ab.id}>
          <div className="crit-head">
            <span className="lbl">critique · {critique.version}{critique.kind === "polish" ? " · polish" : ""}</span>
            <button type="button" className="crit-x" aria-label="Close the critique" onClick={() => setCritOpen(false)}>✕</button>
          </div>
          <div className="crit-body"><RichLines text={critique.text} /></div>
        </div>
      ) : null}
      <iframe
        ref={ref}
        title={ab.title || ab.id}
        sandbox="allow-scripts"
        style={{ width: w, height: h }}
        onLoad={onLoad}
      />
    </div>
  );
}
