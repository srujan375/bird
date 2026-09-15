import { useEffect, useRef, useState } from "react";
import { IconChat } from "@arch/components/icons";
import type { Status } from "../wire/types";

interface Props {
  status: Status | "";
  /** the brief the session opened with — the bar's title is its first clause */
  brief: string;
  sub: string;
  chatOpen: boolean;
  unread: boolean;
  canUndo: boolean;
  canFinalize: boolean;
  /** the artboard Finalize would record, named on the button so the click
   *  that closes the session says what it closes it on */
  finalizeTitle: string;
  onUndo: () => void;
  onFinalize: () => void;
  /** set only in the showcase phase: Back ends it and returns the board */
  onBack?: () => void;
  onToggleChat: () => void;
}

/** The bird mark: the same stroke the arch board wears. */
function Mark() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="var(--accent)"
         strokeWidth="1.4" strokeLinejoin="round" aria-hidden="true">
      <path d="M2.4 8.6c2.6.3 4.6-.6 6-2.6 1.2-1.7 2.9-2.2 5-1.5-.4 4.6-2.7 7.2-6.8 7.7-1.8.2-3.2-.4-4.2-1.9z" />
      <path d="M2 13.2c1.4-2.4 3.1-4 5.1-4.9" strokeLinecap="round" />
    </svg>
  );
}

const STATUS_WORD: Record<string, string> = {
  asking: "asking", generating: "generating", ready: "ready",
  showcase: "showcase", finalized: "finalized",
};

/** The brief's first clause, short enough for a title: up to the first
 *  sentence end, dash or colon, then trimmed to a line. */
export function briefTitle(brief: string, max = 60): string {
  const one = brief.replace(/\s+/g, " ").trim();
  if (!one) return "Design session";
  const cut = /^(.+?)(?:[.!?](?=\s|$)|\s[—–-]\s|:\s|\n)/.exec(one);
  let head = (cut ? cut[1] : one).trim();
  if (head.length > max) {
    head = head.slice(0, max);
    const sp = head.lastIndexOf(" ");
    head = (sp > max * 0.6 ? head.slice(0, sp) : head).replace(/[\s,;:]+$/, "") + "…";
  }
  return head;
}

const shortTitle = (t: string, max = 22) => (t.length > max ? t.slice(0, max - 1).trimEnd() + "…" : t);

export function AppBar({
  status, brief, sub, chatOpen, unread, canUndo, canFinalize, finalizeTitle, onUndo, onFinalize, onBack, onToggleChat,
}: Props) {
  /* Finalize closes the session and cannot be undone; delete got a two-step
     arm for a far smaller consequence. First click arms it, the second
     fires, and the arm decays so a stray click never finalizes anything. */
  const [armed, setArmed] = useState(false);
  const armTimer = useRef<number | null>(null);
  useEffect(() => {
    if (!canFinalize) setArmed(false);
    return () => { if (armTimer.current !== null) { clearTimeout(armTimer.current); armTimer.current = null; } };
  }, [canFinalize, finalizeTitle]);
  const onFinalizeClick = () => {
    if (!canFinalize) return;
    if (armed) {
      setArmed(false);
      if (armTimer.current !== null) { clearTimeout(armTimer.current); armTimer.current = null; }
      onFinalize();
      return;
    }
    setArmed(true);
    armTimer.current = window.setTimeout(() => { setArmed(false); armTimer.current = null; }, 3000);
  };

  const title = briefTitle(brief);
  const name = finalizeTitle ? shortTitle(finalizeTitle) : "";
  return (
    <header className="appbar" data-od-id="appbar">
      <div className="brand">
        <Mark />
        <h1 className="goal" id="goal" title={brief.trim() || "Design session"} data-od-id="session-goal">{title}</h1>
      </div>
      <span className="pill" id="status" data-status={status || ""}>{STATUS_WORD[status] ?? "…"}</span>
      <span className="sub" id="sub" aria-live="polite">{sub}</span>
      <span className="spacer" />
      {onBack ? (
        <button className="bar-btn" id="btn-back" type="button"
                title="End the showcase phase and return the board"
                data-od-id="back" onClick={onBack}>Back</button>
      ) : null}
      <button className="bar-btn" id="btn-undo" type="button" disabled={!canUndo}
              title="Step the artboard back one version — yours or the designer's ⌘Z"
              data-od-id="undo" onClick={onUndo}>Undo</button>
      <button className={"bar-btn finalize" + (armed ? " armed" : "")} id="btn-finalize" type="button"
              disabled={!canFinalize}
              title={!canFinalize ? "Finalize needs an artboard in focus and nothing being written"
                : armed ? `Click again to record ${finalizeTitle} as the design and close the session`
                : `Record ${finalizeTitle} as the design and close the session — this cannot be undone`}
              data-od-id="finalize" onClick={onFinalizeClick}>
        {armed ? `Finalize ${name}?` : name ? `Finalize ${name}` : "Finalize"}
      </button>
      <button
        className="bar-btn chat-toggle"
        id="btn-chat"
        type="button"
        aria-expanded={chatOpen}
        aria-controls="chat"
        title={(chatOpen ? "Hide" : "Show") + " the conversation ⌘\\"}
        data-od-id="toggle-chat"
        {...(unread ? { "data-unread": "1" } : {})}
        onClick={onToggleChat}
      >
        <IconChat />
        <span>Chat</span>
        <i className="unread" aria-hidden="true" />
      </button>
    </header>
  );
}
