/**
 * An offer: a question with the answers already written.
 *
 * It rides the same `permission_request` wire as the two gates, because it is
 * the same thing mechanically — the harness blocks a tool call until a person
 * answers. It is a different thing to *use*, though, so it looks different:
 * no summary, no concern list, no "request changes". A question and some
 * buttons.
 *
 * The reason it exists at all is cost. The facts that decide how much
 * infrastructure a design needs — how many users, how much consistency — are
 * exactly the ones a user will not volunteer, and asking for them in prose
 * means asking someone to stop and compose a paragraph. Most people won't, the
 * architect guesses, and it guesses big. A tap is a low enough bar that the
 * question actually gets answered.
 *
 * "I don't know" is a first-class answer and is deliberately not styled as a
 * refusal — a user who doesn't know the number yet is the common case, and the
 * harness's job then is to say what it is assuming instead.
 */
import { respondToGate } from "../store/session";
import type { PermissionEvent } from "../types";

export function Offer({ req, onRespond }: {
  req: PermissionEvent;
  onRespond: () => void;
}) {
  const options = req.options ?? [];
  return (
    <div className="gate offer">
      <h4>{req.question}</h4>
      <div className="offer-options">
        {options.map((opt) => (
          <button
            key={opt}
            className="offer-option"
            onClick={() => { respondToGate(true, opt); onRespond(); }}
          >
            {opt}
          </button>
        ))}
      </div>
      <div className="buttons">
        <button
          className="ghost"
          onClick={() => { respondToGate(false, ""); onRespond(); }}
        >
          I don't know yet
        </button>
      </div>
      <p className="faint" style={{ marginTop: 6, marginBottom: 0 }}>
        Or type your own answer in the composer.
      </p>
    </div>
  );
}
