import { useEffect, useRef } from "react";
import type { BoardLine, Turn } from "../board/chat";
import { useChatHost } from "../chat/host";
import { Picker } from "./Picker";
import type { Attachment } from "../board/types";
import { useArriving } from "../hooks/useArriving";
import { Rich, RichLines } from "./Rich";
import { IconDoc } from "./icons";

const human = (n: number) =>
  n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB";

function Tick() {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.4"
         strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.5 8.5l3 3 6-6.5" />
    </svg>
  );
}

/** The whole of the model's internals, in one line you can ignore. */
function BoardLineRow({ line }: { line: BoardLine }) {
  const host = useChatHost();
  return (
    <button
      type="button"
      className="board-line"
      data-od-id="board-line"
      onClick={() => host.reveal(line.ids)}
    >
      <span className="dot" />
      <span>{line.text}</span>
      <span className="go">show me</span>
    </button>
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
  const host = useChatHost();
  const entering = useArriving();
  const cls = "turn" + (entering ? " entering" : "") + (turn.t === "you" ? " you" : "");

  if (turn.t === "thinking") {
    return (
      <div className={cls}>
        <div className="thinking"><i /><i /><i /></div>
      </div>
    );
  }
  if (turn.t === "progress") {
    /* the research turn: what the architect is doing, one line per kind of
       work, in place of the dots; kept afterwards as the first minute's record */
    const active = turn.steps.findIndex((s) => !s.done);
    return (
      <div className={cls} data-od-id="progress">
        <div className="progress">
          {turn.steps.map((s, i) => (
            <div
              key={s.step}
              className="pg-row"
              {...(s.done ? { "data-done": "1" } : {})}
              {...(!s.done && i === active ? { "data-active": "1" } : {})}
            >
              <span className="pg-mark">{s.done ? <Tick /> : !s.done && i === active ? <i /> : null}</span>
              <span>{s.text}</span>
            </div>
          ))}
          {active >= 0 ? <div className="pg-eta">about two minutes</div> : null}
        </div>
      </div>
    );
  }
  if (turn.t === "card") {
    return (
      <div className={cls}>
        <details className="card" data-od-id="card">
          <summary><span className="dot" /><span>{turn.title}</span><span className="go">read</span></summary>
          <div className="card-body">
            {turn.text.split(/\n{2,}/).map((para, i) => <p className="say" key={i}><RichLines text={para} /></p>)}
          </div>
        </details>
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
    <div className={cls} data-turn={turn.id}>
      {turn.lines.map((l, i) => <p className="say" key={i}><Rich text={l} /></p>)}
      {turn.line ? <BoardLineRow line={turn.line} /> : null}
      {/* Only the record lives here. The live question is the dock's, pinned
          above the composer, so it never scrolls away while the architect
          keeps writing. */}
      {turn.ask && turn.ask.spent ? (
        <Picker
          host={turn.id}
          ask={turn.ask}
          flush={turn.lines.length === 0}
          onConfirm={(o) => host.sendAnswer(turn.id, turn.ask!.picker.id, o)}
          /* Change is the host's to offer. Arch never does: a pick goes
             straight to the architect, and by the time you could press it
             the answer has been acted on. The design intake does, until the
             brief has gone out. */
          onChange={host.askChange?.allowed(turn.ask.picker.id)
            ? () => host.askChange!.reopen(turn.id, turn.ask!.picker.id)
            : undefined}
          lockedNote="sent"
        />
      ) : null}
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
