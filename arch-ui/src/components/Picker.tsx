import { useEffect, useRef } from "react";
import type { AskBlock } from "../board/chat";
import { selectAsk } from "../board/chat";
import {
  confirmLabel,
  groupName,
  isTight,
  optionAt,
  type PickerOption,
} from "../board/picker";
import { Rich } from "./Rich";

/**
 * The picker: one question, a few rows, and a confirm that names the act.
 *
 * A port of the Open Design component in
 * `design-workbench/pickers/handover-01-option-cards.html`, and it keeps that
 * spec's two load-bearing decisions:
 *
 *  - **The control is a real radio.** Arrow keys, Space, Tab-into-the-checked-
 *    row and every screen reader behaviour come from the input, not from a
 *    roving tabindex we would have to maintain. The `.opt-mark` is a visual
 *    echo, `aria-hidden`, and carries nothing.
 *  - **Selecting is not answering.** The confirm button is the answer, and it
 *    is labelled with what is about to happen — never "Continue". With
 *    nothing selected it is disabled and neutral grey, because a tinted
 *    button at low opacity reads as broken rather than pending.
 *
 * The background moves between states; the text never does.
 */

function Tick() {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.4"
         strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.5 8.5l3 3 6-6.5" />
    </svg>
  );
}

/** What is left once the picker collapses: the noun for the decision, the
 *  value, and — while it is still the user's to change — a way back. */
function Answered({ label, value, onChange, note }: {
  label: string; value: string; onChange?: () => void; note: string;
}) {
  const change = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    /* preventScroll on purpose: scrollIntoView here yanks the thread out from
       under the reader and breaks the embedded preview. */
    change.current?.focus({ preventScroll: true });
  }, []);
  return (
    <div className={"answered is-in" + (onChange ? "" : " is-locked")} data-answered
         data-od-id="ask-answered">
      <span className="a-tick" aria-hidden="true"><Tick /></span>
      <span className="a-body">
        <span className="a-label">{label}</span>
        <span className="a-value">{value}</span>
      </span>
      {onChange
        ? <button ref={change} type="button" className="link-btn" data-change onClick={onChange}>
            Change
          </button>
        : <span className="locked-note">{note}</span>}
    </div>
  );
}

interface Props {
  /** the turn holding the question, so a selection lands on the right one */
  host: number;
  ask: AskBlock;
  /** the answer goes out: the host decides where to */
  onConfirm: (o: PickerOption) => void;
  /** offered only while the answer is still the user's to take back. Absent
   *  once it has been acted on — editing it then rewrites history rather than
   *  changing the future. */
  onChange?: () => void;
  /** why it can no longer be changed, in the user's terms */
  lockedNote?: string;
  /** the question is the whole turn: no text above it to sit under */
  flush?: boolean;
  /** picked while the architect was still writing: rows lock, and the foot
   *  says the answer goes when the turn ends */
  queued?: boolean;
}

export function Picker({ host, ask, onConfirm, onChange, lockedNote, flush, queued }: Props) {
  const picker = ask.picker;
  const tight = isTight(picker);
  const selected = ask.selected;
  const chosen = selected ? optionAt(picker, selected) : null;

  if (ask.spent && ask.pickedLabel) {
    return (
      <div className="ask" data-od-id="ask" style={flush ? { marginTop: 0 } : undefined}>
        <Answered
          label={picker.summaryLabel || picker.prompt}
          value={ask.pickedLabel}
          onChange={onChange}
          note={lockedNote || "sent"}
        />
      </div>
    );
  }

  return (
    <div className="ask" data-od-id="ask" style={flush ? { marginTop: 0 } : undefined}>
      <div className={"picker" + (ask.spent ? " is-spent" : "")} data-picker={picker.variant}>
        <p className="q-text"><Rich text={picker.prompt} /></p>
        <p className="q-hint">{picker.hint}</p>
        <div className="opts" role="radiogroup" aria-label={picker.summaryLabel || picker.prompt}>
          {picker.options.map((o, i) => (
            <label
              key={o.value}
              className={"opt" + (tight ? " is-tight" : "")}
              data-od-id={"opt-" + i}
            >
              <input
                className="ctl"
                type="radio"
                name={groupName(picker)}
                value={o.value}
                data-label={o.label}
                checked={selected === o.value}
                disabled={o.disabled || ask.spent || queued}
                title={o.disabled ? o.disabledReason : undefined}
                onChange={() => selectAsk(host, o.value)}
              />
              <span className="opt-mark" aria-hidden="true"><i /></span>
              {tight ? (
                <span className="opt-label">{o.label}</span>
              ) : (
                <span className="opt-body">
                  <span className="opt-label">
                    {o.label}
                    {o.rec ? <span className="badge">recommended</span> : null}
                  </span>
                  {o.description ? <span className="opt-desc">{o.description}</span> : null}
                </span>
              )}
              {tight && o.rec ? <span className="badge">recommended</span> : null}
            </label>
          ))}
        </div>
        {queued ? (
          <div className="queued" data-od-id="ask-queued">
            <span className="thinking"><i /><i /><i /></span>
            <span>Sending when the architect finishes</span>
          </div>
        ) : ask.spent ? null : (
          <div className="picker-foot">
            <span className="counter">
              {picker.options.slice(0, 9).map((_, i) => <kbd key={i}>{i + 1}</kbd>)}
              {" or just type an answer"}
            </span>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              data-confirm
              data-od-id="ask-confirm"
              disabled={!chosen}
              onClick={() => { if (chosen) onConfirm(chosen); }}
            >
              {confirmLabel(picker, selected)}
            </button>
          </div>
        )}
      </div>
      {ask.spent ? (
        <p className="ask-foot spent" data-od-id="ask-foot">
          answered in a message — no row was taken as-is
        </p>
      ) : null}
    </div>
  );
}
