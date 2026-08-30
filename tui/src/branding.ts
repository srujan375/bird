// Bird TUI branding — banner wordmark + adaptive accent.
//
// Three pieces:
//   1. BANNER  — frozen ANSI-Shadow "BIRD" wordmark, two-layer coloured
//                (default-fg blocks + dim bevel), printed once at session
//                start into scrollback. Never re-rendered on resize/Ctrl-L.
//   2. ACCENT  — adaptive clay accent, resolved once at startup from
//                background (COLORFGBG → OSC 11 → unknown) × colour depth
//                (truecolor → 256 → 16 → none). NO_COLOR / non-TTY ⇒ plain.
//   3. INDICATOR — the model line under the chat input frame: ◆ + bold model
//                id + dim meta, degrading right-to-left and never wrapping.

/* ---------- banner ---------- */

// Frozen wordmark: 27 cols × 6 rows. Trailing spaces on rows 1 and 6 are part
// of the fixture — do not trim.
export const BANNER_ART = [
	"██████╗ ██╗██████╗ ██████╗ ",
	"██╔══██╗██║██╔══██╗██╔══██╗",
	"██████╔╝██║██████╔╝██║  ██║",
	"██╔══██╗██║██╔══██╗██║  ██║",
	"██████╔╝██║██║  ██║██████╔╝",
	"╚═════╝ ╚═╝╚═╝  ╚═╝╚═════╝ ",
] as const;

const BEVEL = new Set(["═", "║", "╔", "╗", "╚", "╝"]);
const GUTTER = "  ";
const RESET = "\x1b[0m";
const DIM = "\x1b[2m";
const DEFAULT_FG = "\x1b[39m";

/** Split a banner row into runs of like class (block vs bevel vs space) so we
 *  emit ONE escape per contiguous run, not per char. */
function classifyRow(row: string): { kind: "block" | "bevel" | "space"; text: string }[] {
	const runs: { kind: "block" | "bevel" | "space"; text: string }[] = [];
	for (const ch of row) {
		const kind = ch === "█" ? "block" : BEVEL.has(ch) ? "bevel" : "space";
		const last = runs[runs.length - 1];
		if (last && last.kind === kind) last.text += ch;
		else runs.push({ kind, text: ch });
	}
	return runs;
}

/**
 * Render the banner lockup as an array of lines ready to print:
 * blank line, dim meta line, blank line, 6 guttered art lines, blank line.
 * Two-layer colouring: U+2588 blocks get SGR 39 (terminal default fg),
 * box-drawing bevel chars get SGR 2 (dim). One escape per contiguous run;
 * every art line ends with a reset. With colour disabled the art is emitted
 * plain (zero escape bytes).
 */
export function renderBanner(meta: string, color: boolean): string[] {
	// meta sits ABOVE the wordmark, indented to the art's gutter so version and
	// working directory read as a caption to the title rather than a footer
	const lines: string[] = ["", GUTTER + (color ? DIM + meta + RESET : meta), ""];
	for (const row of BANNER_ART) {
		let line = GUTTER;
		if (!color) {
			line += row;
		} else {
			for (const run of classifyRow(row)) {
				if (run.kind === "space") {
					line += run.text;
				} else if (run.kind === "block") {
					line += DEFAULT_FG + run.text + RESET;
				} else {
					line += DIM + run.text + RESET;
				}
			}
		}
		// rows 1 and 6 end on a space run, which emits no escape of its own —
		// close them explicitly so nothing bleeds past the wordmark
		if (color && !line.endsWith(RESET)) line += RESET;
		lines.push(line);
	}
	lines.push("");
	return lines;
}

/** Escape-byte count of a rendered banner (≤440 required by the design). */
export function bannerEscapeBytes(lines: string[]): number {
	let n = 0;
	for (const l of lines) for (let i = 0; i < l.length; i++) if (l[i] === "\x1b") n++;
	return n;
}

/* ---------- accent colour ---------- */

export type Depth = "truecolor" | "256" | "16" | "none";
export type Background = "dark" | "light" | "unknown";

interface AccentSpec {
	truecolor: [number, number, number];
	c256: number;
	c16: number;
}

// The Claude Native clay accent (theme.ts `palette.accent`), re-cut per
// background luminance so it keeps contrast on light terminals. Same hue the
// badges, auto-accept mode and selection highlights already use — the model
// name reads as part of the palette instead of against it.
const ACCENT: Record<Background, AccentSpec> = {
	dark: { truecolor: [204, 122, 79], c256: 173, c16: 137 }, // #CC7A4F
	light: { truecolor: [154, 79, 38], c256: 130, c16: 94 }, // #9A4F26
	unknown: { truecolor: [193, 111, 66], c256: 173, c16: 137 }, // #C16F42
};

export interface AccentTheme {
	background: Background;
	depth: Depth;
	plain: boolean; // NO_COLOR or non-TTY: zero escapes anywhere
	sgr(code: string): string; // full sequence for a base code, e.g. "38;5;170"
	boldSgr(code: string): string; // bold variant ("1;" prepended)
	glyph: string; // ◆ normally, * when plain
}

/** Colour depth ladder from env, mirroring chalk's own detection order. */
export function detectDepth(env: NodeJS.ProcessEnv, isTTY: boolean): Depth {
	if (!isTTY || env.NO_COLOR) return "none";
	if (env.FORCE_COLOR === "0") return "none";
	if (env.COLORTERM === "truecolor" || env.COLORTERM === "24bit") return "truecolor";
	// CI / TERM fallbacks: 256 when TERM advertises it, else the 16-colour floor
	if (env.TERM && /256colou?r/.test(env.TERM)) return "256";
	if (env.TERM && env.TERM !== "dumb") return "16";
	return "none";
}

/** Background from COLORFGBG if present; never guesses dark on its own. */
export function detectBackgroundFromEnv(env: NodeJS.ProcessEnv): Background | null {
	const raw = env.COLORFGBG;
	if (!raw) return null;
	// format "fg;bg" (sometimes just "bg"); bg 0–6 or 8 is dark, 7/9–15 light
	const parts = raw.split(";");
	const bgStr = parts.length >= 2 ? parts[parts.length - 1] : parts[0];
	const bg = parseInt(bgStr, 10);
	if (Number.isNaN(bg)) return null;
	if (bg === 7 || bg >= 9) return "light";
	return "dark";
}

export function resolveAccent(opts: {
	env?: NodeJS.ProcessEnv;
	isTTY?: boolean;
	background?: Background; // pre-resolved (tests); skips OSC query
}): AccentTheme {
	const env = opts.env ?? process.env;
	const isTTY = opts.isTTY ?? process.stdout.isTTY === true;
	const background = opts.background ?? detectBackgroundFromEnv(env) ?? "unknown";
	const depth = detectDepth(env, isTTY);
	const plain = depth === "none";

	const mk = (code: string) => `\x1b[${code}m`;
	return {
		background,
		depth,
		plain,
		glyph: plain ? "*" : "◆",
		sgr: (base) => (plain ? "" : mk(base)),
		boldSgr: (base) => (plain ? "" : mk(`1;${base}`)),
	};
}

/** The accent SGR body for this theme's depth, e.g. "38;2;204;122;79". */
export function accentCode(theme: AccentTheme): string {
	const spec = ACCENT[theme.background];
	switch (theme.depth) {
		case "truecolor":
			return `38;2;${spec.truecolor.join(";")}`;
		case "256":
			return `38;5;${spec.c256}`;
		case "16":
			return `38;5;${spec.c16}`;
		default:
			return "";
	}
}

/**
 * The chat-bar's model-name segment: BOLD + clay accent. Plain themes
 * (NO_COLOR / non-TTY) return the bare id — zero escape bytes, bold dropped.
 * Used by HintLine for the name at the bottom-right of the chat bar; nothing
 * else in the bar changes.
 */
export function renderChatBarModelName(model: string, theme: AccentTheme): string {
	if (theme.plain) return model;
	return theme.boldSgr(accentCode(theme)) + model + RESET;
}

/* ---------- model indicator ---------- */

export interface IndicatorInput {
	modelId: string;
	ctxUsed: number | null; // tokens, e.g. 47_000
	ctxWindow: number | null; // e.g. 200_000
	switchHint?: string | null; // e.g. "⇧⇥ cycle mode"
}

function abbrevK(n: number): string {
	return n >= 1000 ? `${Math.round(n / 100) / 10}k` : String(n);
}

/**
 * Build the indicator line, aligned to the prompt caret column (4-space
 * indent). Degrades without ever wrapping:
 *   1. drop switch hint
 *   2. drop ctx counter
 *   3. truncate model id from the LEFT with ellipsis (family stays visible)
 */
export function renderIndicator(input: IndicatorInput, theme: AccentTheme, width: number): string {
	const accent = accentCode(theme);
	const glyph = theme.sgr(accent) + theme.glyph + (theme.plain ? "" : RESET);
	const name = (id: string) => theme.boldSgr(accent) + id + (theme.plain ? "" : RESET);
	const ctx =
		input.ctxUsed !== null && input.ctxWindow !== null
			? `ctx ${abbrevK(input.ctxUsed)}/${abbrevK(input.ctxWindow)}`
			: null;

	const segments: string[] = [];
	if (input.switchHint) segments.push(input.switchHint);
	if (ctx) segments.push(ctx);
	const metaSep = "  ·  ";

	// try full → drop hint → drop both; then left-truncate the id
	const attempts: { idWidth: number; meta: string }[] = [];
	const metaFull = segments.join(metaSep);
	attempts.push({ idWidth: Infinity, meta: metaFull });
	if (input.switchHint && ctx) attempts.push({ idWidth: Infinity, meta: ctx });
	attempts.push({ idWidth: Infinity, meta: "" });

	for (const a of attempts) {
		const fixed = 4 /*indent*/ + 2 /*◆ */ + a.meta.length + (a.meta ? metaSep.length : 0);
		if (fixed + visibleLen(input.modelId) <= width) {
			return assemble(name(input.modelId), a.meta, glyph, metaSep, theme);
		}
	}

	// must left-truncate the model id
	const avail = Math.max(1, width - (4 + 2));
	return assemble(name(leftTruncate(input.modelId, avail)), "", glyph, metaSep, theme);
}

function assemble(
	nameStyled: string,
	meta: string,
	glyph: string,
	sep: string,
	theme: AccentTheme,
): string {
	// the separator is part of the meta, so it dims with it rather than
	// sitting at full brightness between two dim runs
	const dimMeta = meta ? (theme.plain ? sep + meta : "\x1b[2m" + sep + meta + RESET) : "";
	return "    " + glyph + " " + nameStyled + dimMeta;
}

function visibleLen(s: string): number {
	// eslint-disable-next-line no-control-regex
	return s.replace(/\x1b\[[0-9;]*m/g, "").length;
}

/** Keep the RIGHT end (family suffix stays visible), prefix with ellipsis. */
export function leftTruncate(id: string, maxWidth: number): string {
	if (visibleLen(id) <= maxWidth) return id;
	const keep = Math.max(1, maxWidth - 1);
	return "…" + id.slice(id.length - keep);
}
