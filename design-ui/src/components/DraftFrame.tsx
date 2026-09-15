import { useEffect, useRef } from "react";
import { draftHtml, subscribe, type Draft } from "../wire/drafts";

export const FRAME_W = 1280;
export const FRAME_H = 900;

interface Props {
  draft: Draft;
  /** the artboard being rebuilt, if this draft is one */
  title: string;
  x: number;
  w: number;
  h: number;
  /** the active theme's tokens, so the draft is drawn in the palette the
   *  finished artboard will wear */
  themeCss: string;
  onSize: (key: string, w: number, h: number) => void;
  /** the draft's call has run and its artboard is loading in this same slot:
   *  stay as the picture underneath until that frame has painted, with no
   *  name row or chip of your own — the artboard's are on top */
  poster?: boolean;
}

/** An artboard while the designer is still writing it.
 *
 *  The document is streamed into the frame the way a page arrives over a slow
 *  connection: `document.write` piece by piece, so the browser's own parser
 *  renders what there is as it comes and never reloads what it has. That
 *  takes a same-origin frame, so unlike a finished artboard's frame this one
 *  may not run scripts; the finished one, with the bridge inside it, replaces
 *  this the moment the call runs. */
export function DraftFrame({ draft, title, x, w, h, themeCss, onSize, poster = false }: Props) {
  const ref = useRef<HTMLIFrameElement | null>(null);
  const sizeRef = useRef(onSize);
  sizeRef.current = onSize;

  useEffect(() => {
    const el = ref.current;
    const doc = el?.contentDocument;
    if (!el || !doc) return;
    doc.open();
    /* the tokens ride as an adopted sheet rather than written markup: a
       <style> before the model's own <!doctype> would drop the frame into
       quirks mode, and the theme is not part of the document */
    const win = el.contentWindow as (Window & { CSSStyleSheet?: typeof CSSStyleSheet }) | null;
    if (themeCss && win?.CSSStyleSheet) {
      try {
        const sheet = new win.CSSStyleSheet();
        sheet.replaceSync(themeCss);
        doc.adoptedStyleSheets = [sheet];
      } catch { /* an older engine: the draft draws unthemed */ }
    }
    doc.write(draftHtml(draft.key));
    const un = subscribe(draft.key, (piece) => doc.write(piece));
    const measure = () => {
      const de = doc.documentElement, b = doc.body;
      if (!de) return;
      const bottom = b ? Math.max(b.scrollHeight, b.getBoundingClientRect().bottom) : 0;
      sizeRef.current(draft.key, FRAME_W, Math.max(FRAME_H, Math.ceil(Math.max(bottom, de.scrollHeight))));
    };
    const timer = setInterval(measure, 300);
    return () => {
      un();
      clearInterval(timer);
      try { doc.close(); } catch { /* already closed */ }
    };
  }, [draft.key, themeCss]);

  useEffect(() => {
    if (!draft.closed) return;
    try { ref.current?.contentDocument?.close(); } catch { /* already closed */ }
  }, [draft.closed]);

  return (
    <div className={"frame on drafting" + (poster ? " poster" : "")} style={{ transform: `translate(${x}px, 0px)`, width: w }}
         data-draft={draft.key} data-od-id={(poster ? "poster-" : "draft-") + draft.key} aria-hidden={poster || undefined}>
      {poster ? null : (
        <>
          <span className="frame-name stay" aria-live="polite">
            <span className="nm">{title}</span>
            <span className="v mono">{draft.closed ? "written" : "writing"}</span>
          </span>
          <span className="busy stay">{draft.artboard ? "editing" : "writing"}</span>
        </>
      )}
      <iframe
        ref={ref}
        title={title}
        sandbox="allow-same-origin"
        style={{ width: w, height: h }}
      />
      {/* nothing in a draft is there to click yet; the shield keeps the
          board's wheel and pinch on the board */}
      <div className="shield" aria-hidden="true" />
    </div>
  );
}
