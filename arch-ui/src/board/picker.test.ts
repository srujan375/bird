import { describe, expect, it, beforeEach } from "vitest";
import {
  ask,
  block,
  getChat,
  hasAsk,
  openAsk,
  resetChat,
  selectAsk,
  spendAsk,
} from "./chat";
import { confirmLabel, groupName, isTight, openingValue, rowAt, type PickerPayload } from "./picker";

const PICKER: PickerPayload = {
  type: "picker",
  variant: "option-cards",
  id: "q1",
  prompt: "Where does the queue live?",
  hint: "Pick one",
  summaryLabel: "Queue home",
  confirm: { template: "Go with {v}", empty: "Pick an answer" },
  options: [
    { value: "In-process", label: "In-process", description: "One box. Loses queued work on a crash." },
    { value: "Redis", label: "Redis", description: "One more thing to run.", rec: true },
    { value: "SQS", label: "SQS", disabled: true, disabledReason: "no AWS account on this project" },
  ],
};

describe("the confirm button", () => {
  it("says what is about to happen", () => {
    expect(confirmLabel(PICKER, "Redis")).toBe("Go with Redis");
  });

  it("falls back to the empty label with nothing selected", () => {
    expect(confirmLabel(PICKER, "")).toBe("Pick an answer");
  });

  it("does not promise a row that was never offered", () => {
    expect(confirmLabel(PICKER, "Kafka")).toBe("Pick an answer");
  });
});

describe("what the picker opens on", () => {
  it("is nothing when the harness named no default", () => {
    expect(openingValue(PICKER)).toBe("");
  });

  it("is the default when it named one", () => {
    expect(openingValue({ ...PICKER, default: "Redis" })).toBe("Redis");
  });

  it("ignores a default that is not on the list", () => {
    expect(openingValue({ ...PICKER, default: "Kafka" })).toBe("");
  });

  it("is never the recommendation — a badge is not an answer", () => {
    expect(openingValue(PICKER)).not.toBe("Redis");
  });
});

describe("the group name", () => {
  it("comes from the picker's id, so two questions never share a group", () => {
    expect(groupName(PICKER)).toBe("pick-q1");
    expect(groupName({ ...PICKER, id: "q2" })).not.toBe(groupName(PICKER));
  });
});

describe("the variant", () => {
  it("keeps descriptions on cards and drops them on the list", () => {
    expect(isTight(PICKER)).toBe(false);
    expect(isTight({ ...PICKER, variant: "compact-list" })).toBe(true);
  });
});

describe("the digit shortcuts", () => {
  it("count the rows on screen, from one", () => {
    expect(rowAt(PICKER, 1)?.value).toBe("In-process");
    expect(rowAt(PICKER, 2)?.value).toBe("Redis");
  });

  it("refuse a disabled row rather than skipping its number", () => {
    expect(rowAt(PICKER, 3)).toBeNull();
  });

  it("refuse a number that is not on screen", () => {
    expect(rowAt(PICKER, 0)).toBeNull();
    expect(rowAt(PICKER, 9)).toBeNull();
  });
});

describe("the question in the thread", () => {
  beforeEach(() => resetChat());

  it("goes under the turn that raised it, unselected", () => {
    const host = block();
    ask(host, PICKER);
    const open = openAsk();
    expect(open?.host).toBe(host);
    expect(open?.ask.picker.id).toBe("q1");
    expect(open?.ask.selected).toBe("");
    expect(getChat().pendingAsk).toBe("q1");
  });

  it("is asked once, however many times the harness pushes its state", () => {
    ask(block(), PICKER);
    expect(hasAsk("q1")).toBe(true);
    expect(hasAsk("q2")).toBe(false);
  });

  it("moves its checked row without answering anything", () => {
    const host = block();
    ask(host, PICKER);
    selectAsk(host, "Redis");
    expect(openAsk()?.ask.selected).toBe("Redis");
    expect(getChat().pendingAsk).toBe("q1");
  });

  it("is off the table once a row is confirmed", () => {
    const host = block();
    ask(host, PICKER);
    spendAsk(host, "Redis");
    expect(openAsk()).toBeNull();
    expect(getChat().pendingAsk).toBeNull();
  });

  it("cannot be re-selected after it is answered", () => {
    const host = block();
    ask(host, PICKER);
    spendAsk(host, "Redis");
    selectAsk(host, "In-process");
    const turn = getChat().turns.find((t) => t.id === host);
    expect(turn?.t === "say" && turn.ask?.pickedLabel).toBe("Redis");
  });
});
