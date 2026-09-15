import { useEffect, useRef } from "react";
import { markFrameLoading, markFrameReady, postToFrame, registerFrame, unregisterFrame } from "../board/frames";
import { useSession } from "../wire/session";

/** The showcase phase's view: one full-viewport render of the elevated
 *  artboard while status is "showcase". The canvas is gone — this is the
 *  finished result, animated, with the editor bridge still inside so the
 *  critique loop's capture and click-to-select keep working mid-polish.
 *  Chrome-free by design: Back/Undo/Finalize stay in the app bar. */
export function Showcase() {
  const session = useSession();
  const aid = session.showcaseArtboard ?? "";
  const ref = useRef<HTMLIFrameElement | null>(null);
  const loaded = useRef<string | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !aid) return;
    /* registered under the elevated artboard's id: the capture round trip
       and click-to-select route here exactly as they do on the board */
    registerFrame(aid, el);
    return () => unregisterFrame(aid);
  }, [aid]);

  useEffect(() => {
    const el = ref.current;
    if (!el || !aid) return;
    const html = session.html[aid] ?? "";
    if (!html || loaded.current === html) return;
    loaded.current = html;
    /* a polish lands as a new document; the capture that judges it must
       wait for the load, same as on the board */
    markFrameLoading(aid);
    el.srcdoc = html;
  }, [aid, session.html]);

  const onLoad = () => {
    if (loaded.current === null) return; // about:blank, before any document
    markFrameReady(aid);
    /* the bridge intercepts wheel/pinch to pan the board; a full-fledged
       page scrolls like one, so tell it to stand down. Click-to-select
       stays — pointing still matters mid-polish. */
    postToFrame(aid, { type: "showcase" });
  };

  return (
    <section className="showcase" id="showcase" data-od-id="showcase">
      <iframe ref={ref} title="Showcase" sandbox="allow-scripts" onLoad={onLoad} />
    </section>
  );
}
