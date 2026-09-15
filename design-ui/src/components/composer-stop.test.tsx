import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import { ChatHostProvider, type ChatHost } from "@arch/chat/host";
import { Composer } from "@arch/components/Composer";

/* The composer foot carries ONE button that morphs between the two verbs:
 * Send when idle, Stop while a turn runs. The failure this shape prevents:
 * a separate Stop appearing beside Send mid-turn caught the habitual
 * double-send click aimed where Send was and killed the turn that just
 * started. Morphing in place keeps the same slot under the cursor, so a
 * second click there is always deliberate. renderToString runs the hooks
 * but no effects, which is all a presence assertion needs. */

const base: ChatHost = {
  who: "Message the designer",
  placeholder: "Describe a change…",
  pendingEdits: 0,
  subjects: () => [],
  sendInput: () => {},
  sendBoard: () => {},
  sendAnswer: () => {},
  reveal: () => {},
};

const render = (host: ChatHost) =>
  renderToString(
    <ChatHostProvider host={host}>
      <Composer tip="" disabled={false} reason="" />
    </ChatHostProvider>,
  );

const stopButton = (html: string) =>
  /<button[^>]*data-od-id="stop-turn"[^>]*>/.exec(html)?.[0] ?? "";

const submitSend = (html: string) =>
  /<button[^>]*type="submit"[^>]*>/.exec(html)?.[0] ?? "";

describe("the composer's Send/Stop morph", () => {
  it("morphs to Stop while a turn runs and a stop verb is wired", () => {
    const html = render({ ...base, running: true, stop: () => {} });
    expect(stopButton(html)).toContain('type="button"');
    /* no Send twin beside it — the second control under the cursor was the
       whole bug; the morph replaces Send in its slot instead */
    expect(submitSend(html)).toBe("");
  });

  it("is Send again when idle — same slot, no stop sibling", () => {
    const html = render(base);
    expect(stopButton(html)).toBe("");
    expect(submitSend(html)).toContain('id="send"');
  });

  it("keeps a plain Send mid-turn when the page wires no stop verb", () => {
    /* a rail beside a board with no interrupt route — arch-ui's page today —
       must not grow a button that does nothing */
    const html = render({ ...base, running: true });
    expect(stopButton(html)).toBe("");
    expect(submitSend(html)).toContain('id="send"');
  });

  it("never renders both verbs at once, in any state", () => {
    for (const host of [base, { ...base, running: true }, { ...base, running: true, stop: () => {} }]) {
      const html = render(host);
      expect(Boolean(stopButton(html))).not.toBe(Boolean(submitSend(html)));
    }
  });

  it("the Stop morph is never disabled — a wedged turn must stay stoppable", () => {
    /* Send disables on an empty draft; Stop must not inherit that, or the
       one state the control exists for is the one it refuses */
    const html = render({ ...base, running: true, stop: () => {} });
    expect(stopButton(html)).not.toContain("disabled");
  });

  it("is a plain button, not a submit — stopping must not send the draft", () => {
    const html = render({ ...base, running: true, stop: () => {} });
    expect(stopButton(html)).toContain('type="button"');
  });

  it("leaves the composer open while a turn runs — mid-turn input is the nudge path", () => {
    /* the server injects mid-turn input at the safe seam, so the textarea
       must stay live while running; killing it would kill the only way to
       steer a long-thinking model without killing its turn */
    const html = render({ ...base, running: true, stop: () => {} });
    expect(html).not.toMatch(/<textarea[^>]*disabled/);
  });
});