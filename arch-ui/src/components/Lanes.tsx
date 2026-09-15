import { memo } from "react";
import { laneVars } from "../board/geometry";
import type { Lane } from "../board/types";

function LanesImpl({ lanes }: { lanes: Lane[] }) {
  return (
    <div id="lanes">
      {lanes.map((l) => (
        <div
          key={l.k}
          className="lane"
          data-k={l.slot}
          data-lane-id={l.k}
          {...(l.out ? { "data-out": "1" } : {})}
          data-od-id={"lane-" + l.k}
          style={{ left: l.x, top: l.y, width: l.w, height: l.h, ...laneVars(l.slot) }}
        >
          <div className="lane-label">
            <b title={l.name}>{l.name}</b>
            {/* the note is clamped to keep the lane a lane rather than a
                paragraph; `title` is where the rest of it stays readable */}
            <span className="lane-note" title={l.note}>{l.note}</span>
            {l.taken ? <span className="taken">taken</span> : null}
          </div>
          {l.evidence ? (
            <div className="lane-evidence" data-od-id={"evidence-" + l.k}>
              <span className="k">seen at</span>
              {l.evidence.who ? <span className="who">{l.evidence.who}</span> : null}
              {l.evidence.scale ? <span>{l.evidence.who ? "· " : ""}{l.evidence.scale}</span> : null}
              {l.evidence.sources?.length ? (
                <a className="src" href={l.evidence.sources[0]} target="_blank" rel="noreferrer"
                   title={l.evidence.sources.join("\n")}>
                  {l.evidence.sources.length} source{l.evidence.sources.length > 1 ? "s" : ""}
                </a>
              ) : null}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export const Lanes = memo(LanesImpl);
