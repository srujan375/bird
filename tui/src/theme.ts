// Claude Native direction — tokens ported from the design canvas
// ([data-theme="native"]): charcoal instead of black, clay instead of green,
// one mono face throughout (the terminal's own). Rounded corners are the real
// ╭╮╰╯ glyphs the design annotated as Textual `border: round`. No shadows —
// terminals cannot cast one.
import chalk from "chalk";

export const palette = {
	bg: "#1c1917",
	panel: "#221e1b",
	border: "#38322c",
	fg: "#ece6de",
	muted: "#9c9188",
	accent: "#cc7a4f",
	// rgba(204,122,79,.16) pre-composited over bg
	accentSoft: "#382920",
	success: "#8fb573",
	danger: "#e0796a",
	// color-mix(success/danger 12%, transparent) pre-composited over panel
	diffAddBg: "#2f3026",
	diffDelBg: "#392924",
} as const;

export const t = {
	fg: chalk.hex(palette.fg),
	muted: chalk.hex(palette.muted),
	dim: chalk.hex(palette.border),
	accent: chalk.hex(palette.accent),
	accentBold: chalk.hex(palette.accent).bold,
	success: chalk.hex(palette.success),
	danger: chalk.hex(palette.danger),
	panelBg: chalk.bgHex(palette.panel),
	accentSoftBg: chalk.bgHex(palette.accentSoft),
	// primary button: clay fill, charcoal text (design: bg accent / color t-bg)
	btnPrimary: chalk.bgHex(palette.accent).hex(palette.bg).bold,
	diffAdd: chalk.bgHex(palette.diffAddBg).hex(palette.success),
	diffDel: chalk.bgHex(palette.diffDelBg).hex(palette.danger),
	diffCtx: chalk.hex(palette.muted),
	badge: chalk.bgHex(palette.accentSoft).hex(palette.accent).bold,
};

// Claude Native spinner — discrete dot frames at a slow tick (design: 180ms),
// the way a real character-grid spinner works.
export const SPINNER = [".", "..", "...", "....", ".....", "....", "...", ".."];
export const SPINNER_MS = 180;
