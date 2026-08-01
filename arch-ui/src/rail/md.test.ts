import { describe, expect, it } from "vitest";
import { parseInline, parseMarkdown, safeHref } from "./md";

/** Flatten a tree back to its text, so a test can assert content without
 *  spelling out every node. */
function text(n: unknown): string {
  const node = n as { t?: string; v?: string; code?: string; kids?: unknown[]; items?: unknown[][]; head?: unknown; rows?: unknown };
  if (Array.isArray(n)) return n.map(text).join("");
  if (node.v !== undefined) return node.v;
  if (node.code !== undefined) return node.code;
  if (node.kids) return text(node.kids);
  if (node.items) return node.items.map((b) => text(b)).join("|");
  return "";
}

describe("parseInline", () => {
  it("reads bold, italic and code", () => {
    const out = parseInline("a **b** c *d* e `f`");
    expect(out.map((n) => n.t)).toEqual(["text", "strong", "text", "em", "text", "code"]);
    expect(text(out)).toBe("a b c d e f");
  });

  it("leaves markup inside a code span alone", () => {
    const out = parseInline("call `arr[*]` now");
    expect(out[1]).toEqual({ t: "code", v: "arr[*]" });
    expect(out.some((n) => n.t === "em")).toBe(false);
  });

  it("does not italicise mid-word underscores", () => {
    const out = parseInline("run_id and snake_case_name");
    expect(out).toEqual([{ t: "text", v: "run_id and snake_case_name" }]);
  });

  it("nests emphasis", () => {
    const [node] = parseInline("**bold with `code` in it**");
    expect(node.t).toBe("strong");
    expect(text(node)).toBe("bold with code in it");
  });

  it("reads links and bare urls", () => {
    expect(parseInline("[docs](https://x.dev/a)")).toEqual([
      { t: "link", href: "https://x.dev/a", kids: [{ t: "text", v: "docs" }] },
    ]);
    expect(parseInline("see https://x.dev now")[1]).toMatchObject({ t: "link", href: "https://x.dev" });
  });

  it("keeps an unfinished pair literal — a half-streamed message", () => {
    expect(parseInline("this is **not closed")).toEqual([{ t: "text", v: "this is **not closed" }]);
  });
});

describe("safeHref", () => {
  it("passes http, mailto and in-page links", () => {
    expect(safeHref("https://x.dev")).toBe("https://x.dev");
    expect(safeHref("mailto:a@b.c")).toBe("mailto:a@b.c");
    expect(safeHref("#anchor")).toBe("#anchor");
  });

  it("refuses anything that could execute", () => {
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("  JavaScript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,<script>")).toBeNull();
  });
});

describe("parseMarkdown", () => {
  it("splits paragraphs on blank lines", () => {
    const out = parseMarkdown("one\nstill one\n\ntwo");
    expect(out.map((b) => b.t)).toEqual(["p", "p"]);
    expect(text(out[0])).toBe("one\nstill one");
  });

  it("reads headings without the hashes", () => {
    const [h] = parseMarkdown("## Boundaries");
    expect(h).toMatchObject({ t: "h", level: 2 });
    expect(text(h)).toBe("Boundaries");
  });

  it("keeps a fenced block verbatim", () => {
    const [b] = parseMarkdown("```py\nx = 1\n\n  y = *2*\n```");
    expect(b).toEqual({ t: "code", lang: "py", code: "x = 1\n\n  y = *2*" });
  });

  it("closes an unterminated fence at the end of the text", () => {
    const [b] = parseMarkdown("```\nhalf a block");
    expect(b).toEqual({ t: "code", lang: "", code: "half a block" });
  });

  it("reads a bullet list", () => {
    const [list] = parseMarkdown("- one\n- two\n- three");
    expect(list).toMatchObject({ t: "list", ordered: false });
    expect(text(list)).toBe("one|two|three");
  });

  it("reads an ordered list and keeps its start", () => {
    const [list] = parseMarkdown("3. c\n4. d");
    expect(list).toMatchObject({ t: "list", ordered: true, start: 3 });
  });

  it("nests an indented list inside its item", () => {
    const [list] = parseMarkdown("- outer\n  - inner\n- next");
    if (list.t !== "list") throw new Error("expected a list");
    expect(list.items).toHaveLength(2);
    expect(list.items[0].map((b) => b.t)).toEqual(["p", "list"]);
    expect(text(list.items[0][1])).toBe("inner");
  });

  it("takes a lazy continuation line as part of the item", () => {
    const [list] = parseMarkdown("- a claim that\nwrapped in the source");
    if (list.t !== "list") throw new Error("expected a list");
    expect(list.items).toHaveLength(1);
    expect(text(list.items[0])).toBe("a claim that\nwrapped in the source");
  });

  it("does not read a rule as a bullet", () => {
    expect(parseMarkdown("---").map((b) => b.t)).toEqual(["hr"]);
    expect(parseMarkdown("- - -").map((b) => b.t)).toEqual(["hr"]);
  });

  it("reads a blockquote as blocks", () => {
    const [q] = parseMarkdown("> quoted **hard**\n> - a bullet");
    if (q.t !== "quote") throw new Error("expected a quote");
    expect(q.kids.map((b) => b.t)).toEqual(["p", "list"]);
  });

  it("reads a table", () => {
    const [t] = parseMarkdown("| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |");
    if (t.t !== "table") throw new Error("expected a table");
    expect(t.head.map(text)).toEqual(["a", "b"]);
    expect(t.rows).toHaveLength(2);
    expect(t.rows[1].map(text)).toEqual(["3", "4"]);
  });

  it("returns nothing for empty text", () => {
    expect(parseMarkdown("")).toEqual([]);
    expect(parseMarkdown("\n\n  \n")).toEqual([]);
  });

  it("carries plain prose through untouched", () => {
    const out = parseMarkdown("Just a sentence about the auth service.");
    expect(out).toHaveLength(1);
    expect(text(out[0])).toBe("Just a sentence about the auth service.");
  });
});
