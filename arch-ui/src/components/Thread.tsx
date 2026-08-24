import { useEffect, useRef } from "react";
import { frame } from "../board/viewApi";
import { flash } from "../board/ui";
import type { AskBlock, BoardLine, Turn } from "../board/chat";
import type { Attachment } from "../board/types";
import { useArriving } from "../hooks/useArriving";
import { Rich, RichLines } from "./Rich";
import { IconDoc } from "./icons";

const human = (n: number) =>
  n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB";

/** The whole of the model's internals, in one line you can ignore. */
function BoardLineRow({ line }: { line: BoardLine }) {
  return (
    <button
      type="button"
      className="board-line"
      data-od-id="board-line"
      onClick={() => { frame(line.ids, 120, 1.15); flash(line.ids); }}
    >
      <span className="dot" />
      <span>{line.text}</span>
      <span className="go">show me</span>
    </button>
  );
}

function Ask({ ask, flush }: { ask: AskBlock; flush: boolean }) {
  return (
    <div className="ask" data-od-id="ask" style={flush ? { marginTop: 0 } : undefined}>
      <q><Rich text={ask.question} /></q>
      <div className={"chips" + (ask.spent ? " spent" : "")}>
        {ask.opts.map((o, i) => (
          <button
            key={i}
            type="button"
            className="chip"
            data-od-id={"reply-chip-" + i}
            {...(o.rec ? { "data-rec": "1" } : {})}
            {...(ask.spent && ask.pickedLabel === o.label ? { "data-picked": "1" } : {})}
            onClick={() => { if (!ask.spent) ask.onPick(o); }}
          >
            {o.label}
            {o.rec ? <span className="rec">recommended</span> : null}
          </button>
        ))}
      </div>
    </div>
  );
}

/** What the turn keeps: thumbnails you can open, files you can read the name of. */
function Files({ files, onOpen }: { files: Attachment[]; onOpen: (a: Attachment) => void }) {
  if (!files.length) return null;
  return (
    <div className="files">
      {files.map((a) =>
        a.img ? (
          <button key={a.id} type="button" className="shot" data-od-id={"sent-" + a.id}
                  aria-label={"Open " + a.name} onClick={() => onOpen(a)}>
            <img src={a.url ?? ""} alt={a.name} />
          </button>
        ) : (
          <span key={a.id} className="doc" data-od-id={"sent-" + a.id}>
            <IconDoc />
            <span className="nm">{a.name}</span>
            <span className="sz">{human(a.size)}</span>
          </span>
        ),
      )}
    </div>
  );
}

function TurnBlock({ turn, onOpen }: { turn: Turn; onOpen: (a: Attachment) => void }) {
  const entering = useArriving();
  const cls = "turn" + (entering ? " entering" : "") + (turn.t === "you" ? " you" : "");

  if (turn.t === "thinking") {
    return (
      <div className={cls}>
        <div className="thinking"><i /><i /><i /></div>
      </div>
    );
  }
  if (turn.t === "quiet") {
    return (
      <div className={cls} style={{ marginBottom: 14 }}>
        <span className="board-line"><span className="dot" /><span>{turn.text}</span></span>
      </div>
    );
  }
  if (turn.t === "you") {
    return (
      <div className={cls}>
        <span className="who">{turn.via ? `you · ${turn.via}` : "you"}</span>
        {turn.text ? <p className="say"><RichLines text={turn.text} /></p> : null}
        {turn.about?.length ? (
          <p className="about" data-od-id="turn-about">
            about <b>{turn.about.join(", ")}</b>
          </p>
        ) : null}
        {turn.drew?.length ? (
          <ul className="drew">
            {turn.drew.map((d, i) => <li key={i}>{d}</li>)}
          </ul>
        ) : null}
        <Files files={turn.files ?? []} onOpen={onOpen} />
      </div>
    );
  }
  return (
    <div className={cls}>
      {turn.lines.map((l, i) => <p className="say" key={i}><Rich text={l} /></p>)}
      {turn.line ? <BoardLineRow line={turn.line} /> : null}
      {turn.ask ? <Ask ask={turn.ask} flush={turn.lines.length === 0} /> : null}
    </div>
  );
}

export function Thread({ turns, onOpen }: { turns: Turn[]; onOpen: (a: Attachment) => void }) {
  const el = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const node = el.current;
    if (!node) return;
    const raf = requestAnimationFrame(() => { node.scrollTop = node.scrollHeight; });
    return () => cancelAnimationFrame(raf);
  }, [turns]);

  return (
    <div className="thread" id="thread" role="log" aria-live="polite" aria-label="conversation" ref={el}>
      {turns.map((t) => <TurnBlock key={t.id} turn={t} onOpen={onOpen} />)}
    </div>
  );
}
