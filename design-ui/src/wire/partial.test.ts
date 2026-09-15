import { describe, expect, it } from "vitest";
import { decodeStringPrefix, slug, ToolArgs } from "./partial";

describe("decodeStringPrefix", () => {
  it("decodes plain text up to the end of what has arrived", () => {
    expect(decodeStringPrefix('"abc', 1)).toEqual({ text: "abc", used: 4, closed: false });
  });

  it("stops at the closing quote and says so", () => {
    expect(decodeStringPrefix('"abc", "x"', 1)).toEqual({ text: "abc", used: 5, closed: true });
  });

  it("decodes the simple escapes", () => {
    const d = decodeStringPrefix('\\n\\t\\"\\\\\\/', 0);
    expect(d.text).toBe('\n\t"\\/');
    expect(d.closed).toBe(false);
  });

  it("decodes \\u escapes, surrogate pairs included", () => {
    expect(decodeStringPrefix("\\u00e9\\ud83d\\ude00", 0).text).toBe("é😀");
  });

  it("holds back an escape the wire has cut short", () => {
    expect(decodeStringPrefix("ab\\", 0)).toEqual({ text: "ab", used: 2, closed: false });
    expect(decodeStringPrefix("ab\\u12", 0)).toEqual({ text: "ab", used: 2, closed: false });
    /* and picks it up once the rest is there */
    expect(decodeStringPrefix("ab\\u1234", 2)).toEqual({ text: "ሴ", used: 8, closed: false });
  });
});

describe("ToolArgs", () => {
  const feedAll = (t: ToolArgs, pieces: string[]) => pieces.map((p) => t.feed(p));

  it("finds the html value and decodes it piece by piece", () => {
    const t = new ToolArgs();
    const added = feedAll(t, ['{"title": "Hero', ' — dark", "ht', 'ml": "<!doctype ht', 'ml>\\n<h1>Hi</h1>"', "}"]);
    expect(added).toEqual(["", "", "<!doctype ht", "ml>\n<h1>Hi</h1>", ""]);
    expect(t.html).toBe("<!doctype html>\n<h1>Hi</h1>");
    expect(t.htmlClosed).toBe(true);
    expect(t.title).toBe("Hero — dark");
    expect(t.artboard).toBeNull();
  });

  it("reads fields that come after the document", () => {
    const t = new ToolArgs();
    feedAll(t, ['{"html": "<p>x</p>", "artb', 'oard": "hero-dark", "title": "Hero"}']);
    expect(t.html).toBe("<p>x</p>");
    expect(t.artboard).toBe("hero-dark");
    expect(t.title).toBe("Hero");
  });

  it("never mistakes half a title for a title", () => {
    const t = new ToolArgs();
    t.feed('{"title": "Hero');
    expect(t.title).toBeNull();
    t.feed(' — warm"');
    expect(t.title).toBe("Hero — warm");
  });

  it("survives an escape split across pieces", () => {
    const t = new ToolArgs();
    const added = feedAll(t, ['{"html": "a\\', 'nb\\u00', 'e9c"}']);
    expect(added).toEqual(["a", "\nb", "éc"]);
    expect(t.html).toBe("a\nbéc");
  });

  it("keeps quotes inside the document", () => {
    const t = new ToolArgs();
    feedAll(t, ['{"html": "<div class=\\"hero\\">', 'x</div>"}']);
    expect(t.html).toBe('<div class="hero">x</div>');
    expect(t.htmlClosed).toBe(true);
  });

  it("adds nothing once the document has closed", () => {
    const t = new ToolArgs();
    feedAll(t, ['{"html": "<p>x</p>"', ', "title": "T"}']);
    expect(t.feed("")).toBe("");
    expect(t.html).toBe("<p>x</p>");
  });
});

describe("slug", () => {
  it("mirrors state.py's _slug", () => {
    expect(slug("Hero — dark")).toBe("hero-dark");
    expect(slug("  Sign-up / step 2 ")).toBe("sign-up-step-2");
    expect(slug("!!!")).toBe("artboard");
  });
});
