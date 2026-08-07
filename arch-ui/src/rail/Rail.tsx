/**
 * The rail, handover §2 — two columns, both always open.
 *
 * It used to be five tabs. Four of them were views of the same design, and the
 * tab bar meant every one of them was invisible four fifths of the time. They
 * are gone rather than moved around:
 *
 *   Concerns   → sticky notes on the canvas, beside what they are against (§5)
 *   Decisions  → the finalize gate and the handoff bundle (§9)
 *   Questions  → the same, plus a badge on the node they hang off
 *   Flows      → the left column here, because it is the one panel that is
 *                genuinely useful *while* you are typing (§4)
 *
 * What is left is the conversation and the sequence, side by side.
 */
import { FlowsColumn } from "./Flows";
import { Chat } from "./Chat";
import { Offer } from "./Offer";
import { RailGrip } from "./RailGrip";
import { useSession } from "../store/session";

export function Rail({ draft, setDraft }: {
  /** The composer's text, owned by App: a gate ruling spends it as its reason,
   *  and the gate is now a sheet over the canvas rather than a note in here. */
  draft: string;
  setDraft: (s: string) => void;
}) {
  const permission = useSession((s) => s.permission);

  return (
    <aside className="rail">
      <RailGrip />
      <div className="rail-split">
        <div className="rail-col flows-col"><FlowsColumn /></div>
        <div className="rail-col chat-col">
          {/* An offer is a question, not a ruling, so it stays with the
              conversation it belongs to rather than taking over the canvas the
              way the finalize sheet does. */}
          {permission?.kind === "offer" && (
            <Offer req={permission} onRespond={() => setDraft("")} />
          )}
          <Chat draft={draft} setDraft={setDraft} />
        </div>
      </div>
    </aside>
  );
}
