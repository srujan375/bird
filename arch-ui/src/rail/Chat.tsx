/**
 * The transcript and composer, handover §3.
 *
 * The complaint this rebuild answers: you could not find your own message in
 * the column, and you could not tell what the agent had actually *done* without
 * opening the session log. So:
 *
 *   - Your message is a full-width block with an accent rail and a YOU badge.
 *     It does not right-align and it does not cap at 88% width — the two things
 *     that made the thing you wrote read as an aside in your own conversation.
 *   - Every actor is named. The critic is a different model reviewing in the
 *     background and gets its own colour, because a background objection that
 *     reads as the architect changing its mind is worse than no attribution.
 *   - Tool calls are rows you can read: name, argument, status.
 *   - Turn dividers carry the model and the context depth, because compaction
 *     fires at 90% of the window and you should see that coming.
 */
import { useEffect, useRef, useState } from "react";
import { interrupt, sendInput, useSession } from "../store/session";
import { Markdown } from "./Markdown";
import type { TranscriptItem } from "../types";

/** §3: a tool row shows itself for this long, then folds away. Long enough to
 *  read in passing, short enough that a 12-call turn does not bury the prose. */
const TOOL_OPEN_MS = 2400;
/** §3: auto-scroll only while you are already near the bottom, so reading back
 *  through a long turn is not fought by the stream. */
const STICK_PX = 220;

const clock = (at: number) =>
  new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const tokens = (n: number) =>
  n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);

function ToolRow({ item }: { item: Extract<TranscriptItem, { t: "tool" }> }) {
  // §2: arrives open, auto-collapses after 2400ms, clicking pins it open.
  // A failure keeps the row, turns the status red and does NOT auto-collapse —
  // the one case where the output still matters after it landed.
  const failed = item.status === "error";
  const [open, setOpen] = useState(failed || Date.now() - item.at < TOOL_OPEN_MS);
  const [pinned, setPinned] = useState(false);
  useEffect(() => {
    if (pinned || failed || !open) return;
    const t = setTimeout(() => setOpen(false),
                         Math.max(0, TOOL_OPEN_MS - (Date.now() - item.at)));
    return () => clearTimeout(t);
  }, [pinned, failed, open, item.at]);
  useEffect(() => { if (failed) setOpen(true); }, [failed]);

  const lines = item.lines ?? [];
  const shown = lines.slice(0, 6);
  const more = lines.length - shown.length;

  // A native <details> per §7: keyboard-operable and announced without wiring.
  return (
    <details
      className="tool"
      data-status={item.status}
      open={open}
      onToggle={(e) => {
        const next = (e.currentTarget as HTMLDetailsElement).open;
        setOpen(next);
        if (next) setPinned(true); // opened by hand: stop the auto-collapse
      }}
    >
      <summary className="tool-summary">
        <span className="tw">‣</span>
        <span className="mono tool-name">{item.name}</span>
        <span className="mono tool-arg">{item.arg}</span>
        <span className="spacer" />
        <span className="tool-status">
          {item.status === "running" ? "…" : failed ? "failed" : "ok"}
        </span>
      </summary>
      {shown.length > 0 && (
        <div className="tool-body mono">
          {shown.map((l, i) => <div key={i}>{l}</div>)}
          {more > 0 && <div className="faint">+{more} more</div>}
        </div>
      )}
    </details>
  );
}

function TurnDivider({ item }: { item: Extract<TranscriptItem, { t: "turn" }> }) {
  const window = useSession((s) => s.ready?.context_window);
  const cost = [
    item.model,
    // no window means an older server: print what we know rather than a
    // fraction with an invented denominator
    item.inTokens ? (window ? `${tokens(item.inTokens)} / ${tokens(window)}` : tokens(item.inTokens)) : "",
    item.tools ? `${item.tools} tool${item.tools === 1 ? "" : "s"}` : "",
  ].filter(Boolean).join(" · ");

  return (
    <div className="turn-divider" data-status={item.status}>
      <span className="mono turn-id">
        {item.n ? `turn ${item.n} · ` : ""}{item.status}
      </span>
      <span className="rule" />
      <span className="mono turn-cost">{cost}</span>
    </div>
  );
}

function Message({ item, judge }: { item: TranscriptItem; judge?: string | null }) {
  switch (item.t) {
    case "user":
      return (
        <div className="msg user">
          <div className="who">
            <span className="who-badge">you</span>
            <span className="mono tiny faint">{clock(item.at)}</span>
          </div>
          <div className="mtext">{item.text}</div>
        </div>
      );
    case "agent":
      return (
        <div className="msg agent" data-critic={item.who === "critic"}>
          <div className="who">
            <span className="who-name">
              {item.who}{item.who === "critic" && judge ? ` · ${judge}` : ""}
            </span>
            <span className="mono tiny faint">{clock(item.at)}</span>
          </div>
          <Markdown text={item.text} className="mtext md" />
        </div>
      );
    case "tool":
      return <ToolRow item={item} />;
    case "turn":
      return (
        <>
          <TurnDivider item={item} />
          {item.message && <div className="msg notice" data-err>{item.message}</div>}
        </>
      );
    default:
      return <div className="msg notice" data-err={item.err}>{item.text}</div>;
  }
}

export function Chat({ draft, setDraft }: { draft: string; setDraft: (v: string) => void }) {
  const transcript = useSession((s) => s.transcript);
  const stream = useSession((s) => s.stream);
  const running = useSession((s) => s.running);
  const permission = useSession((s) => s.permission);
  const conn = useSession((s) => s.conn);
  const finalized = useSession((s) => s.finalized);
  const artifacts = useSession((s) => s.artifacts);
  const model = useSession((s) => s.ready?.model);
  const judge = useSession((s) => s.ready?.judge_model);
  const window_ = useSession((s) => s.ready?.context_window);
  const used = useSession((s) => s.turnTokens);
  /** §4: how full the window is. Null when the server did not say how big it
   *  is — a bar with an invented denominator is worse than no bar. */
  const ctx = window_ && used ? used / window_ : null;

  const logRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  /**
   * Whether to follow the stream, updated only when the *user* scrolls.
   *
   * This was measured after every render, which is the one moment it cannot be
   * measured: by then the new message is already in the DOM, so the gap below
   * the viewport is always large, so it always concluded you had scrolled away
   * and stopped following. The result was that your own message landed in the
   * transcript and the column never moved to show it — it read as the message
   * being swallowed.
   *
   * A scroll listener only fires when someone actually scrolls, which is the
   * only event that should change this.
   */
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const onScroll = () => {
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const el = logRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [transcript, stream, permission]);

  const submit = () => {
    if (!draft.trim()) return;
    sendInput(draft);
    setDraft("");
    // You just acted: follow the stream again no matter where you had scrolled
    // to. Sending a message and not being shown it is never what you wanted.
    stick.current = true;
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  return (
    <div className="chat">
      <div className="log" ref={logRef} role="log" aria-live="polite" aria-label="transcript">
        {transcript.length === 0 && !stream && (
          <div className="empty-note">
            The first turn is already running — the agent's opening take will appear here.
          </div>
        )}
        {transcript.map((item, i) => <Message key={i} item={item} judge={judge} />)}

        {stream !== null && stream !== "" && (
          <div className="msg agent">
            <div className="who"><span className="who-name">architect</span></div>
            <Markdown text={stream} className="mtext md" trailing={<span className="cursor" />} />
          </div>
        )}
        {/* §8: never at the same time as the streaming cursor */}
        {running && stream === "" && (
          <div className="msg notice thinking">
            <i /><i /><i /><span>thinking…</span>
          </div>
        )}
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
          </div>
        </div>
      ) : (
        <div className="composer">
          <textarea
            value={draft}
            placeholder={
              permission?.kind === "offer" ? "…or type your own answer" :
              permission ? "Reply to request changes…" :
              conn === "disconnected" ? "reconnecting…" :
              "Talk to the architect… (⏎ to send, ⇧⏎ for a newline)"
            }
            /* §2: the connection pill left the top bar — this is where the
               session ending is reported now, at the moment you try to act */
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
            <span className="mono tiny faint">{model ?? "—"}</span>
            {ctx !== null && (
              <span
                className="ctx-bar"
                data-level={ctx > 0.9 ? "hot" : ctx > 0.75 ? "warm" : "ok"}
                title={`${Math.round(ctx * 100)}% of the context window — compaction fires at 90%`}
              >
                <i style={{ width: `${Math.min(100, ctx * 100)}%` }} />
              </span>
            )}
            <span className="spacer" />
            {running && <button onClick={interrupt}>Interrupt</button>}
            <button className="primary" onClick={submit}
                    disabled={!draft.trim() || conn === "disconnected"}>
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
