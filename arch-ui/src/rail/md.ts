/**
 * Markdown → a small block/inline tree.
 *
 * The architect writes markdown; showing it raw shows the user asterisks. This
 * is the same line-by-line state machine the TUI renderer uses (tui/src/markdown.ts),
 * except it emits data instead of ANSI so React can render it — and so the
 * parse can be tested without a DOM.
 *
 * Deliberately not CommonMark: no reference links, no setext headings, no
 * indented code blocks. It covers what a model actually writes in a chat turn,
 * and anything it does not understand survives as the literal text it was —
 * which is the only failure mode worth having while a message is still
 * streaming in half-written.
 */

export type Inline =
  | { t: "text"; v: string }
  | { t: "code"; v: string }
  | { t: "strong"; kids: Inline[] }
  | { t: "em"; kids: Inline[] }
  | { t: "del"; kids: Inline[] }
  | { t: "link"; href: string; kids: Inline[] };

export type Block =
  | { t: "p"; kids: Inline[] }
  | { t: "h"; level: number; kids: Inline[] }
  | { t: "code"; lang: string; code: string }
  | { t: "list"; ordered: boolean; start: number; items: Block[][] }
  | { t: "quote"; kids: Block[] }
  | { t: "table"; head: Inline[][]; rows: Inline[][][] }
  | { t: "hr" };

/* ---------- inline ---------- */

// One pass, alternation ordered so the greedy pairs (**, __, ~~) win over the
// single-character ones. Code spans come first: nothing inside them is markup.
const INLINE =
  /(?<ticks>`+)(?<code>[\s\S]*?)\k<ticks>|\*\*(?<strong>[\s\S]+?)\*\*|__(?<strong2>[\s\S]+?)__|~~(?<del>[\s\S]+?)~~|(?<![\w*])\*(?<em>[^*\n]+?)\*(?![\w*])|(?<![\w_])_(?<em2>[^_\n]+?)_(?![\w_])|\[(?<label>[^\]\n]*)\]\((?<href>[^()\s]*)(?:\s+"[^"\n]*")?\)|(?<auto>https?:\/\/[^\s<>()]+)/g;

/** Split a run of text into inline nodes. Emphasis nests; code never does. */
export function parseInline(src: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  // a fresh matcher per call: emphasis recurses, and a shared `lastIndex`
  // would be rewound by the inner parse and re-match the outer text forever
  const re = new RegExp(INLINE.source, INLINE.flags);
  for (let m = re.exec(src); m; m = re.exec(src)) {
    const g = m.groups!;
    if (m.index > last) out.push({ t: "text", v: src.slice(last, m.index) });
    last = m.index + m[0].length;

    if (g.code !== undefined) out.push({ t: "code", v: g.code.trim() });
    else if (g.strong !== undefined) out.push({ t: "strong", kids: parseInline(g.strong) });
    else if (g.strong2 !== undefined) out.push({ t: "strong", kids: parseInline(g.strong2) });
    else if (g.del !== undefined) out.push({ t: "del", kids: parseInline(g.del) });
    else if (g.em !== undefined) out.push({ t: "em", kids: parseInline(g.em) });
    else if (g.em2 !== undefined) out.push({ t: "em", kids: parseInline(g.em2) });
    else if (g.label !== undefined) out.push({ t: "link", href: g.href ?? "", kids: parseInline(g.label) });
    else if (g.auto !== undefined) out.push({ t: "link", href: g.auto, kids: [{ t: "text", v: g.auto }] });
  }
  if (last < src.length) out.push({ t: "text", v: src.slice(last) });
  return out;
}

/**
 * Links come from a model, so the scheme is checked rather than trusted —
 * `javascript:` in an href is a script the transcript would happily run.
 */
export function safeHref(href: string): string | null {
  const h = href.trim();
  if (/^(https?:|mailto:)/i.test(h)) return h;
  if (/^[#/]/.test(h)) return h;
  return null;
}

/* ---------- blocks ---------- */

const MARKER = /^(\s*)([-*+]|(\d+)[.)])(\s+)(.*)$/;
const leadingWs = (s: string) => s.length - s.trimStart().length;

function nextNonBlank(lines: string[], i: number): number {
  while (i < lines.length && lines[i].trim() === "") i++;
  return i;
}

function splitRow(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

export function parseMarkdown(src: string): Block[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const out: Block[] = [];
  let para: string[] = [];

  const flush = () => {
    if (para.length === 0) return;
    const text = para.join("\n");
    para = [];
    out.push({ t: "p", kids: parseInline(text) });
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i].replace(/\s+$/, "");

    // fenced code — an unterminated fence still renders, which is what a
    // half-streamed message looks like for a second or two
    const fence = line.match(/^\s{0,3}(`{3,}|~{3,})\s*(\S*)/);
    if (fence) {
      flush();
      const close = new RegExp(`^\\s{0,3}${fence[1][0] === "`" ? "`" : "~"}{${fence[1].length},}\\s*$`);
      const code: string[] = [];
      i++;
      while (i < lines.length && !close.test(lines[i])) code.push(lines[i++]);
      i++;
      out.push({ t: "code", lang: fence[2] ?? "", code: code.join("\n") });
      continue;
    }

    if (line.trim() === "") { flush(); i++; continue; }

    // rule — before lists, so `- - -` is not read as a bullet
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      out.push({ t: "hr" });
      i++;
      continue;
    }

    const h = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
    if (h) {
      flush();
      out.push({ t: "h", level: h[1].length, kids: parseInline(h[2].replace(/\s+#+\s*$/, "")) });
      i++;
      continue;
    }

    // table: a pipe row whose successor is a separator row
    if (
      /^\s*\|/.test(line) &&
      i + 1 < lines.length &&
      /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) &&
      lines[i + 1].includes("-")
    ) {
      flush();
      const head = splitRow(line).map(parseInline);
      i += 2;
      const rows: Inline[][][] = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(splitRow(lines[i++]).map(parseInline));
      out.push({ t: "table", head, rows });
      continue;
    }

    const bq = line.match(/^\s{0,3}>\s?(.*)$/);
    if (bq) {
      flush();
      const quoted: string[] = [];
      while (i < lines.length) {
        const m = lines[i].match(/^\s{0,3}>\s?(.*)$/);
        if (!m) break;
        quoted.push(m[1]);
        i++;
      }
      out.push({ t: "quote", kids: parseMarkdown(quoted.join("\n")) });
      continue;
    }

    const li = line.match(MARKER);
    if (li) {
      flush();
      const indent = li[1].length;
      const ordered = li[3] !== undefined;
      const items: Block[][] = [];

      while (i < lines.length) {
        const m = lines[i].match(MARKER);
        if (!m || m[1].length !== indent || (m[3] !== undefined) !== ordered) break;
        const content = m[1].length + m[2].length + m[4].length; // where the text starts
        const buf = [m[5]];
        i++;

        // continuation: anything indented past the marker, plus lazy paragraph
        // lines that just kept typing on the next line
        while (i < lines.length) {
          const nxt = lines[i];
          if (nxt.trim() === "") {
            const k = nextNonBlank(lines, i);
            if (k < lines.length && leadingWs(lines[k]) > indent) { buf.push(""); i++; continue; }
            break;
          }
          const ws = leadingWs(nxt);
          if (ws > indent) { buf.push(nxt.slice(Math.min(ws, content))); i++; continue; }
          if (!MARKER.test(nxt) && !/^\s{0,3}(#{1,6}\s|>|```|~~~)/.test(nxt)) { buf.push(nxt.trim()); i++; continue; }
          break;
        }
        items.push(parseMarkdown(buf.join("\n")));
      }

      out.push({ t: "list", ordered, start: ordered ? Number(li[3]) : 1, items });
      continue;
    }

    para.push(line.trim());
    i++;
  }

  flush();
  return out;
}
