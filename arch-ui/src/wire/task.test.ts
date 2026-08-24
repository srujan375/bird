import { describe, expect, it } from "vitest";
import { BOARD_EDIT_PREFIX, FOCUS_PREFIX, splitTask } from "./task";

/**
 * The harness assembles a prompt out of context plus what the user typed. This
 * takes it apart again. Getting it wrong does not just lose a label — it puts
 * the prompt's own scaffolding inside the user's message, so the transcript
 * claims they said something they never said.
 */

const focus = [
  FOCUS_PREFIX,
  "- Search index",
  "  id: idx · kind: store · depth: detailed · approach: Pool",
  "  owns: the vector index",
].join("\n");

const drew = [BOARD_EDIT_PREFIX, '- drew a box "Rate limiter"', "- drew a wire api -> pg"].join("\n");

describe("splitTask", () => {
  it("leaves a plain message alone", () => {
    expect(splitTask("why this one?")).toEqual({ drew: [], about: [], typed: "why this one?" });
  });

  it("takes the boxes a message pointed at out of the words", () => {
    const { about, typed } = splitTask(`${focus}\n\nwhy this one?`);
    expect(about).toEqual(["Search index"]);
    expect(typed).toBe("why this one?");
  });

  it("ignores the indented detail under a bullet", () => {
    /* that detail is for the architect; showing it in the chat would put a
       block of prompt where a message should be */
    expect(splitTask(focus).about).toEqual(["Search index"]);
  });

  it("carries what was drawn and what was pointed at together", () => {
    const { drew: d, about, typed } = splitTask(`${drew}\n\n${focus}\n\nand backpressure?`);
    expect(d).toEqual(['drew a box "Rate limiter"', "drew a wire api -> pg"]);
    expect(about).toEqual(["Search index"]);
    expect(typed).toBe("and backpressure?");
  });

  it("does not care which order the context blocks arrive in", () => {
    const a = splitTask(`${drew}\n\n${focus}\n\nhm`);
    const b = splitTask(`${focus}\n\n${drew}\n\nhm`);
    expect(b).toEqual(a);
  });

  it("keeps the blank lines inside what was typed", () => {
    const { typed } = splitTask(`${focus}\n\nfirst\n\nsecond`);
    expect(typed).toBe("first\n\nsecond");
  });

  it("survives a message that is only context", () => {
    expect(splitTask(drew)).toEqual({
      drew: ['drew a box "Rate limiter"', "drew a wire api -> pg"],
      about: [],
      typed: "",
    });
  });
});
