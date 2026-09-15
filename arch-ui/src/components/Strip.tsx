import { useState } from "react";
import { flash } from "../board/ui";
import { frame } from "../board/viewApi";
import { useSession } from "../wire/session";

/**
 * The activity strip: what the scribe is drawing, beside the tool dock.
 *
 * With a scribe on, the board fills in a step behind the conversation. The
 * strip is how the user tells "still drawing" from "done" without reading
 * the thread, and it keeps the last three things drawn as lines they can
 * click to frame. It replaces the "board ·" lines the thread used to carry.
 */
export function Strip() {
  const { scribe, scribeOn } = useSession();
  const [open, setOpen] = useState(false);
  if (!scribeOn || !scribe) return null;
  const recent = scribe.recent ?? [];
  const last = recent.length ? recent[recent.length - 1] : null;
  const label =
    scribe.state === "error" ? (scribe.error || "Couldn’t draw the last change. The architect will redo it.")
    : scribe.state === "drawing" ? "Drawing"
    : "Up to date";
  return (
    <>
      {open && recent.length ? (
        <div className="strip-recent" data-od-id="strip-recent">
          {recent.map((r, i) => (
            <button
              key={i}
              type="button"
              className="board-line"
              onClick={() => { if (r.ids.length) { frame(r.ids, 120, 1.15); flash(r.ids); } }}
            >
              <span className="dot" /><span>{r.text}</span><span className="go">show me</span>
            </button>
          ))}
        </div>
      ) : null}
      <button
        type="button"
        className="strip"
        data-state={scribe.state}
        data-od-id="activity-strip"
        title={recent.length ? "The last three things drawn" : undefined}
        onClick={() => setOpen((v) => !v && recent.length > 0)}
      >
        <span className="dot" />
        <span>
          {label}
          {scribe.state === "drawing" && scribe.queued > 0
            ? <span className="n"> · {scribe.queued} queued</span>
            : null}
        </span>
        {last && scribe.state !== "error" ? <><span className="sep" /><span className="last">{last.text}</span></> : null}
      </button>
    </>
  );
}
