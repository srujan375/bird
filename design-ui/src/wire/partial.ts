/**
 * Reading a tool call's arguments while they are still being written.
 *
 * The model calls `design_create` with the whole artboard as one JSON string
 * value. It arrives over the wire a few characters at a time, so the page
 * cannot JSON.parse it until the last brace — which is exactly when it is no
 * longer interesting. This module decodes the `html` value as far as it has
 * been written, escape by escape, so a frame can be drawing the document
 * while the designer is still typing it.
 *
 * Everything here is pure and incremental: nothing is re-decoded, so a
 * 60 KB document costs 60 KB of work however many pieces it comes in.
 */

export interface Decoded {
  /** what was decoded this time */
  text: string;
  /** how far into the source the decoder got — resume from here */
  used: number;
  /** the string's closing quote was reached */
  closed: boolean;
}

const SIMPLE: Record<string, string> = {
  '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t",
};

/** Decode a JSON string value from `from` (just past its opening quote) as
 *  far as the source goes. An escape cut off by the end of the source is left
 *  for next time rather than guessed at. */
export function decodeStringPrefix(src: string, from: number): Decoded {
  let out = "";
  let i = from;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    if (c === '"') return { text: out, used: i + 1, closed: true };
    if (c !== "\\") { out += c; i++; continue; }
    if (i + 1 >= n) break; // a lone backslash: the escape has not arrived
    const e = src[i + 1];
    if (e === "u") {
      if (i + 6 > n) break; // \uXXXX cut short
      const code = parseInt(src.slice(i + 2, i + 6), 16);
      if (Number.isNaN(code)) { out += src.slice(i, i + 6); i += 6; continue; }
      /* a surrogate pair arrives as two \u escapes; emitting each half as
         its own code unit concatenates back into the right character */
      out += String.fromCharCode(code);
      i += 6;
      continue;
    }
    out += SIMPLE[e] ?? e;
    i += 2;
  }
  return { text: out, used: i, closed: false };
}

const HTML_KEY = /"html"\s*:\s*"/;

/** A complete string field, wherever it sits in the arguments. Only whole
 *  values count: half a title is not a title. */
function field(src: string, key: string): string | null {
  const re = new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`);
  const m = re.exec(src);
  if (!m) return null;
  try { return JSON.parse('"' + m[1] + '"') as string; } catch { return null; }
}

/** The arguments of one tool call, fed a piece at a time. */
export class ToolArgs {
  args = "";
  /** the html value decoded so far */
  html = "";
  /** its closing quote has been written — the document is complete */
  htmlClosed = false;
  title: string | null = null;
  artboard: string | null = null;
  private htmlAt = -1;
  private pos = 0;

  /** Take the next piece. Returns what it added to `html`, if anything. */
  feed(piece: string): string {
    this.args += piece;
    if (this.htmlAt < 0) {
      const m = HTML_KEY.exec(this.args);
      if (!m) { this.scan(); return ""; }
      this.htmlAt = m.index + m[0].length;
      this.pos = this.htmlAt;
    }
    let added = "";
    if (!this.htmlClosed) {
      const d = decodeStringPrefix(this.args, this.pos);
      this.pos = d.used;
      this.html += d.text;
      this.htmlClosed = d.closed;
      added = d.text;
    }
    this.scan();
    return added;
  }

  /** The other fields, read off the parts of the arguments that are not the
   *  document: before the html value, and after it once it has closed. The
   *  document itself is never searched — it is the bulk of the bytes and a
   *  title cannot be inside it. */
  private scan(): void {
    if (this.title !== null && this.artboard !== null) return;
    const src = this.htmlAt < 0 ? this.args
      : this.htmlClosed ? this.args.slice(0, this.htmlAt) + this.args.slice(this.pos)
      : this.args.slice(0, this.htmlAt);
    if (this.title === null) this.title = field(src, "title");
    if (this.artboard === null) this.artboard = field(src, "artboard");
  }
}

/** The artboard id the harness will give a title — `_slug` in state.py. A
 *  collision gets `-2`, `-3`… there, which the page cannot predict, so this
 *  is a first guess and the caller keeps a fallback. */
export function slug(title: string): string {
  let out = "";
  for (const ch of title.toLowerCase()) {
    if (/[\p{L}\p{N}]/u.test(ch)) out += ch;
    else if (out && !out.endsWith("-")) out += "-";
  }
  out = out.replace(/^-+|-+$/g, "");
  return out || "artboard";
}
