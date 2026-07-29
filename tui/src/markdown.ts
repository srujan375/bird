// Lightweight markdown → terminal renderer.
//
// Block-level elements are processed line-by-line with a small state machine;
// inline formatting (code spans, bold, italic, links) is applied per text run
// via `renderInline`. Output is an array of ANSI-styled lines sized to the
// given available width.
import chalk from "chalk";
import { truncateToWidth, visibleWidth, wrapTextWithAnsi } from "@mariozechner/pi-tui";
import { palette, t } from "./theme.ts";

/* ---------- inline formatting ---------- */

// Null char placeholders for code spans — they cannot appear in markdown text,
// so they make safe delimiters while bold/italic run over the string.
const CODE_PLACEHOLDER = "\x00";

/** Render inline markdown (code spans, bold, italic, links) to a single
 *  ANSI-styled string. Code spans are extracted first so `*`/`_` inside them
 *  are never re-processed. */
export function renderInline(text: string): string {
	// 1. Extract code spans first, replace with placeholders.
	const codes: string[] = [];
	let work = text.replace(/`([^`]+)`/g, (_m, code: string) => {
		const styled = chalk.bgHex(palette.panel).hex(palette.accent)(" " + code + " ");
		codes.push(styled);
		return CODE_PLACEHOLDER + (codes.length - 1) + CODE_PLACEHOLDER;
	});

	// 2. Bold: **text** or __text__
	work = work.replace(/\*\*([^*]+)\*\*/g, (_m, inner: string) => t.fg.bold(inner));
	work = work.replace(/__([^_]+)__/g, (_m, inner: string) => t.fg.bold(inner));

	// 3. Italic: *text* or _text_ — lookbehind/ahead to avoid matching ** / __
	work = work.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, (_m, inner: string) => t.fg.italic(inner));
	work = work.replace(/(?<!_)_([^_]+)_(?!_)/g, (_m, inner: string) => t.fg.italic(inner));

	// 4. Links: [text](url) → underlined label, URL dropped (compact terminal)
	work = work.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, label: string, _url: string) => chalk.underline(label));

	// 5. Restore code spans last.
	work = work.replace(
		new RegExp(`${CODE_PLACEHOLDER}(\\d+)${CODE_PLACEHOLDER}`, "g"),
		(_m, idx: string) => codes[Number(idx)] ?? "",
	);

	return work;
}

/* ---------- block-level helpers ---------- */

function renderCodeBlock(lines: string[], width: number): string[] {
	const inner = Math.max(4, width - 4); // box borders + 2 padding
	const b = t.dim;
	const out: string[] = [];
	out.push(b("╭" + "─".repeat(width - 2) + "╮"));
	for (const line of lines) {
		const clipped = visibleWidth(line) > inner ? truncateToWidth(line, inner) : line;
		const padRight = " ".repeat(Math.max(0, inner - visibleWidth(clipped)));
		const content = " " + t.muted(clipped) + padRight + " ";
		out.push(b("│") + content + b("│"));
	}
	out.push(b("╰" + "─".repeat(width - 2) + "╯"));
	return out;
}

function renderTable(rows: string[][], width: number): string[] {
	// rows[0] = header, rows[1] = separator (already validated), rows[2:] = data
	const cols = rows[0].length;
	// measure column widths from content (visibleWidth strips ANSI we add later)
	const widths = new Array(cols).fill(0);
	for (const row of rows) {
		for (let i = 0; i < cols; i++) {
			const cell = (row[i] ?? "").trim();
			widths[i] = Math.max(widths[i], visibleWidth(cell));
		}
	}

	// shrink to fit available width: borders (cols+1) + padding (2*cols)
	const overhead = cols + 1 + cols * 2;
	let total = overhead + widths.reduce((a, b) => a + b, 0);
	while (total > width && widths.some((w) => w > 1)) {
		// shrink the widest column by one
		let maxIdx = 0;
		for (let i = 1; i < cols; i++) if (widths[i] > widths[maxIdx]) maxIdx = i;
		widths[maxIdx]--;
		total--;
	}

	const pad = (cell: string, w: number) => {
		const s = cell.trim();
		const vis = visibleWidth(s);
		const clipped = vis > w ? truncateToWidth(s, w) : s;
		return clipped + " ".repeat(Math.max(0, w - visibleWidth(clipped)));
	};

	const top = "╭" + widths.map((w) => "─".repeat(w + 2)).join("┬") + "╮";
	const mid = "├" + widths.map((w) => "─".repeat(w + 2)).join("┼") + "┤";
	const bot = "╰" + widths.map((w) => "─".repeat(w + 2)).join("┴") + "╯";

	const fmtRow = (row: string[], paint: (s: string) => string) =>
		"│" + row.map((c, i) => " " + paint(pad(c, widths[i])) + " ").join("│") + "│";

	const out: string[] = [t.dim(top)];
	out.push(fmtRow(rows[0], (s) => t.fg.bold(s)));
	out.push(t.dim(mid));
	for (let r = 2; r < rows.length; r++) {
		out.push(fmtRow(rows[r], (s) => t.fg(s)));
	}
	out.push(t.dim(bot));
	return out;
}

function renderListItem(
	marker: string,
	content: string,
	width: number,
	indent: number,
): string[] {
	// marker is already styled; indent = visible width of marker + leading space
	const avail = Math.max(2, width - indent);
	const wrapped = wrapTextWithAnsi(renderInline(content), avail);
	if (wrapped.length === 0) return [marker + " "];
	const out = [marker + " " + wrapped[0]];
	const cont = " ".repeat(indent);
	for (let i = 1; i < wrapped.length; i++) out.push(cont + wrapped[i]);
	return out;
}

/* ---------- main entry ---------- */

/** Render markdown `text` to an array of ANSI-styled lines fitting `width`. */
export function renderMarkdown(text: string, width: number): string[] {
	const lines = text.split("\n");
	const out: string[] = [];
	let i = 0;

	// paragraph accumulator
	let para: string[] = [];
	const flushPara = () => {
		if (para.length === 0) return;
		const joined = para.join(" ");
		para = [];
		const wrapped = wrapTextWithAnsi(renderInline(joined), width);
		for (const l of wrapped) out.push(l);
	};

	while (i < lines.length) {
		const raw = lines[i];
		const line = raw.replace(/\s+$/, ""); // trim trailing whitespace

		// fenced code block
		const fence = line.match(/^```(.*)$/);
		if (fence) {
			flushPara();
			const code: string[] = [];
			i++;
			while (i < lines.length && !/^```/.test(lines[i])) {
				code.push(lines[i]);
				i++;
			}
			i++; // skip closing fence
			out.push(...renderCodeBlock(code, width));
			continue;
		}

		// horizontal rule
		if (/^(\s*(-{3,}|\*{3,}|_{3,})\s*)$/.test(line)) {
			flushPara();
			out.push(t.dim("─".repeat(width)));
			i++;
			continue;
		}

		// header
		const h = line.match(/^(#{1,6})\s+(.*)$/);
		if (h) {
			flushPara();
			const level = h[1].length;
			const paint = level <= 2 ? t.accentBold : t.fg.bold;
			const text2 = renderInline(h[2]);
			out.push(paint(text2));
			i++;
			continue;
		}

		// table: a line starting with `|`, and the next line is a separator
		if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
			flushPara();
			const tableRows: string[][] = [];
			// header
			tableRows.push(splitTableRow(line));
			i++; // separator
			tableRows.push(splitTableRow(lines[i]));
			i++;
			while (i < lines.length && /^\s*\|/.test(lines[i])) {
				tableRows.push(splitTableRow(lines[i]));
				i++;
			}
			out.push(...renderTable(tableRows, width));
			continue;
		}

		// blockquote
		const bq = line.match(/^>\s?(.*)$/);
		if (bq) {
			flushPara();
			const quoted: string[] = [];
			while (i < lines.length) {
				const m = lines[i].match(/^>\s?(.*)$/);
				if (!m) break;
				quoted.push(m[1]);
				i++;
			}
			const inner = renderMarkdown(quoted.join("\n"), Math.max(4, width - 2));
			for (const l of inner) out.push(t.accent("│ ") + l);
			continue;
		}

		// unordered list
		const ul = line.match(/^\s*[-*+]\s+(.*)$/);
		if (ul) {
			flushPara();
			const marker = t.accent("•");
			const indent = 2; // "• " visible width
			out.push(...renderListItem(marker, ul[1], width, indent));
			i++;
			// continuation: indented lines (no list marker) belong to this item
			while (i < lines.length && /^\s+/.test(lines[i]) && !/^\s*[-*+]\s+/.test(lines[i]) && !/^\s*\d+\.\s+/.test(lines[i])) {
				const cont = lines[i].replace(/^\s+/, "");
				if (cont === "") {
					i++;
					continue;
				}
				const avail = Math.max(2, width - indent);
				const wrapped = wrapTextWithAnsi(renderInline(cont), avail);
				for (const l of wrapped) out.push(" ".repeat(indent) + l);
				i++;
			}
			continue;
		}

		// ordered list
		const ol = line.match(/^\s*(\d+)\.\s+(.*)$/);
		if (ol) {
			flushPara();
			const num = ol[1] + ".";
			const marker = t.accent(num);
			const indent = num.length + 1;
			out.push(...renderListItem(marker, ol[2], width, indent));
			i++;
			while (i < lines.length && /^\s+/.test(lines[i]) && !/^\s*[-*+]\s+/.test(lines[i]) && !/^\s*\d+\.\s+/.test(lines[i])) {
				const cont = lines[i].replace(/^\s+/, "");
				if (cont === "") {
					i++;
					continue;
				}
				const avail = Math.max(2, width - indent);
				const wrapped = wrapTextWithAnsi(renderInline(cont), avail);
				for (const l of wrapped) out.push(" ".repeat(indent) + l);
				i++;
			}
			continue;
		}

		// empty line
		if (line.trim() === "") {
			flushPara();
			i++;
			continue;
		}

		// paragraph text
		para.push(line.trim());
		i++;
	}

	flushPara();
	return out;
}

function splitTableRow(line: string): string[] {
	const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
	return trimmed.split("|");
}