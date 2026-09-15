/**
 * The picker, as the harness sends it.
 *
 * One shape for every question in bird: the architect's parked questions here,
 * the designer's intake in the design workbench. The host half is
 * `src/bird/harnesses/picker.py`; the spec both halves implement is the Open
 * Design handover, `design-workbench/pickers/handover-01-option-cards.html`.
 *
 * Everything in this file is pure. The component renders it, the store holds
 * it, and neither has an opinion the payload does not carry.
 */

export interface PickerOption {
  value: string;
  label: string;
  /** the one-line consequence of taking this row — not a restatement of it */
  description?: string;
  disabled?: boolean;
  disabledReason?: string;
  /** the row the host would take. At most one, and it is not a selection. */
  rec?: boolean;
  /** opaque here: arch names the approach a pick keeps alive */
  favor?: string;
}

export interface PickerPayload {
  type: "picker";
  variant: "option-cards" | "compact-list";
  id: string;
  prompt: string;
  hint: string;
  /** the noun for the decision — what the answered row is labelled with */
  summaryLabel: string;
  confirm: { template: string; empty: string };
  /** the pre-checked row, when being wrong is cheap. Usually absent. */
  default?: string;
  options: PickerOption[];
  answered?: boolean;
  value?: string;
  label?: string;
  revised?: boolean;
}

export function optionAt(picker: PickerPayload, value: string): PickerOption | null {
  return picker.options.find((o) => o.value === value) ?? null;
}

/**
 * What the confirm button says right now.
 *
 * This is the detail that makes the button honest: the user reads what is
 * about to happen, not "Continue". With nothing selected it falls back to the
 * empty label and the button paints disabled — neutral grey, not a tinted
 * accent at low opacity, which reads as broken rather than pending.
 */
export function confirmLabel(picker: PickerPayload, value: string): string {
  const picked = value ? optionAt(picker, value) : null;
  if (!picked) return picker.confirm?.empty || "Confirm";
  return (picker.confirm?.template || "{v}").replace("{v}", picked.label);
}

/** The row a picker opens on. A default is pre-checked; a recommendation is
 *  a badge and nothing more — pre-committing someone to a row they have not
 *  read is not a recommendation, it is an answer. */
export function openingValue(picker: PickerPayload): string {
  const value = picker.default ?? "";
  return value && optionAt(picker, value) ? value : "";
}

/**
 * The radio group's name, derived from the picker's id.
 *
 * Never hard-coded: two pickers in one thread with the same `name` are one
 * group, and answering the second would silently un-answer the first.
 */
export function groupName(picker: PickerPayload): string {
  return `pick-${picker.id}`;
}

/** The compact list drops the descriptions and moves the mark to the right
 *  edge, so the eye scans a clean left column of labels. */
export function isTight(picker: PickerPayload): boolean {
  return picker.variant === "compact-list";
}

/** The nth row, for the digit shortcuts — skipping nothing, because the
 *  numbers on screen have to match the numbers on the keyboard. A disabled
 *  row keeps its number and refuses to be taken. */
export function rowAt(picker: PickerPayload, n: number): PickerOption | null {
  const o = picker.options[n - 1];
  return o && !o.disabled ? o : null;
}
