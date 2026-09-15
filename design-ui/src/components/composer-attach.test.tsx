import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import { ChatHostProvider, type ChatHost } from "@arch/chat/host";
import { Composer } from "@arch/components/Composer";
import { composeMessageText } from "@arch/chat/attach";

/* Pasted and dropped screenshots used to go nowhere: the composer staged a
 * chip and the message said files were attached, but no bytes ever moved.
 * The upload verb closes that — on send each staged file is uploaded and the
 * returned reference (a path the server's ingest flow recognizes) rides the
 * message text, where the designer's design_look can be pointed at it.
 * renderToString runs the hooks but no effects, which is all a presence
 * assertion needs; the send-time composition is a pure function tested
 * directly, so the chip → reference path needs no DOM. */

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

const png = () => new File(["x"], "shot.png", { type: "image/png" });

describe("the composer's attachment upload", () => {
  it("renders the tray and drop target with the upload verb wired", () => {
    const html = renderToString(
      <ChatHostProvider host={{ ...base, upload: async () => null }}>
        <Composer tip="" disabled={false} reason="" />
      </ChatHostProvider>,
    );
    expect(html).toContain('data-od-id="attachment-tray"');
    expect(html).toContain("drop to attach");
  });

  it("a staged image's reference rides the message text", async () => {
    const body = await composeMessageText(
      "look at this", [png()], async () => ".bird/sessions/t/attachments/shot.png");
    expect(body).toBe("look at this\n\n.bird/sessions/t/attachments/shot.png");
  });

  it("an attachment-only message is the references alone", async () => {
    const body = await composeMessageText("", [png()], async () => "attachments/shot.png");
    expect(body).toBe("attachments/shot.png");
  });

  it("a failed upload is said honestly, not pretended delivered", async () => {
    const body = await composeMessageText("", [png()], async () => null);
    expect(body).toContain("could not be uploaded");
    expect(body).toContain("shot.png");
  });

  it("without the upload verb the old note is unchanged", async () => {
    expect(await composeMessageText("", [png()], null))
      .toBe("[the user attached 1 file(s): shot.png]");
    expect(await composeMessageText("hello", [png()], null)).toBe("hello");
  });
});