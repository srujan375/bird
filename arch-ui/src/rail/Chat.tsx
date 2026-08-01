/**
 * Transcript + composer.
 *
 * The draft belongs to the rail, not to this panel: whatever is in the composer
 * is also the reason that travels with a gate ruling, and the gate outlives a
 * tab switch. See ./Gate.
 */
import { useEffect, useRef } from "react";
import { interrupt, sendInput, useSession } from "../store/session";
import { Markdown } from "./Markdown";

export function Chat({ draft, setDraft }: { draft: string; setDraft: (v: string) => void }) {
  const transcript = useSession((s) => s.transcript);
  const stream = useSession((s) => s.stream);
  const running = useSession((s) => s.running);
  const permission = useSession((s) => s.permission);
  const conn = useSession((s) => s.conn);
  const finalized = useSession((s) => s.finalized);
  const artifacts = useSession((s) => s.artifacts);

  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [transcript, stream, permission]);

  const submit = () => {
    if (!draft.trim()) return;
    sendInput(draft);
    setDraft("");
  };

  return (
    <div className="rail-body chat">
      <div className="log" ref={logRef}>
        {transcript.length === 0 && !stream && (
          <div className="empty-note">
            The first turn is already running — the agent's opening take will appear here.
          </div>
        )}
        {transcript.map((item, i) => {
          if (item.t === "user") return <div key={i} className="msg user">{item.text}</div>;
          if (item.t === "agent")
            return <Markdown key={i} text={item.text} className="msg agent md" />;
          if (item.t === "notice")
            return <div key={i} className="msg notice" data-err={item.err}>{item.text}</div>;
          return (
            <div key={i} className="msg turn">
              turn {item.status}{item.message ? ` — ${item.message}` : ""}
            </div>
          );
        })}
        {stream !== null && stream !== "" && (
          <Markdown
            text={stream}
            className="msg agent md"
            trailing={<span className="cursor" />}
          />
        )}
        {running && stream === "" && <div className="msg notice">thinking…</div>}
      </div>

      {finalized ? (
        <div className="composer">
          <div className="faint" style={{ fontSize: 11.5, lineHeight: 1.55 }}>
            <b>Architecture finalized.</b> The handoff bundle is written; the session is over
            but the design stays here to read.
            {artifacts.length > 0 && (
              <div className="mono" style={{ marginTop: 4 }}>{artifacts.join("\n")}</div>
            )}
            <div style={{ marginTop: 6 }}>Next step: <code className="mono">bird code</code></div>
            <div style={{ marginTop: 6 }}>Close the tab when you are done — the server is
              only still up so you can read this.</div>
          </div>
        </div>
      ) : (
        <div className="composer">
          <textarea
            value={draft}
            placeholder={
              permission ? "Reply to request changes…" :
              conn === "disconnected" ? "Disconnected." :
              "Talk to the architect… (⏎ to send, ⇧⏎ for a newline)"
            }
            disabled={conn === "disconnected"}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="actions">
            {running && <button onClick={interrupt}>Interrupt</button>}
            <span className="spacer" />
            <button className="primary" onClick={submit} disabled={!draft.trim() || conn === "disconnected"}>
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
