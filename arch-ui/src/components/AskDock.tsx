import { openAsk, useChat } from "../board/chat";
import { useChatHost } from "../chat/host";
import { Picker } from "./Picker";

/**
 * The one question on the table, pinned above the composer.
 *
 * The picker used to sit inside the turn that raised it, and the thread
 * auto-scrolls as the architect keeps writing — so the question scrolled
 * away and the user scrolled back to answer. Here it is composer state: it
 * stays put whatever streams above it, and when answered it collapses into
 * the thread at the place it was asked, as the record. The thread and the
 * dock draw the same AskBlock; only one of them shows it live.
 */
export function AskDock() {
  useChat();  // re-render when the thread moves
  const host = useChatHost();
  const open = openAsk();
  if (!open) return null;
  const { host: turn, ask } = open;
  return (
    <div className="dockq" data-od-id="ask-dock">
      <div className="dq-head">
        <span className="dq-label">On the table</span>
        <button
          type="button"
          className="dq-where"
          onClick={() => {
            document.querySelector<HTMLElement>(`[data-turn="${turn}"]`)
              ?.scrollIntoView({ block: "center", behavior: "smooth" });
          }}
        >
          asked above
        </button>
      </div>
      <Picker
        host={turn}
        ask={ask}
        flush
        queued={ask.queued}
        onConfirm={(o) => host.sendAnswer(turn, ask.picker.id, o)}
      />
    </div>
  );
}
