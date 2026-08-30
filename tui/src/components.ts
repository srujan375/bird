import {
	type Component,
	Editor,
	type EditorTheme,
	Input,
	Key,
	matchesKey,
	type SelectItem,
	SelectList,
	truncateToWidth,
	type TUI,
	visibleWidth,
	wrapTextWithAnsi,
} from "@mariozechner/pi-tui";
import { renderMarkdown } from "./markdown.ts";
import {
	renderChatBarModelName,
	renderIndicator,
	resolveAccent,
	type AccentTheme,
	type IndicatorInput,
} from "./branding.ts";
import { palette, SPINNER, SPINNER_MS, t } from "./theme.ts";

/* ---------- messages ---------- */

// While streaming, re-parse markdown at most this often; deltas arrive far
// faster than frames render, so this keeps long messages cheap. Finalization
// always re-parses regardless of the throttle.
const STREAM_PARSE_MS = 50;

/* ---------- rounded box helper (Textual `border: round` glyphs) ---------- */

interface BoxOpts {
	width: number; // total box width including borders
	border: (s: string) => string;
	pad?: (s: string) => string; // applied to the padded content line (for bg fills)
}

export function roundedBox(lines: string[], opts: BoxOpts): string[] {
	const inner = opts.width - 2;
	const b = opts.border;
	const out: string[] = [];
	out.push(b("╭" + "─".repeat(inner) + "╮"));
	for (const line of lines) {
		const vis = visibleWidth(line);
		const clipped = vis > inner - 2 ? truncateToWidth(line, inner - 2) : line;
		const padRight = " ".repeat(Math.max(0, inner - 2 - visibleWidth(clipped)));
		let content = " " + clipped + padRight + " ";
		if (opts.pad) content = opts.pad(content);
		out.push(b("│") + content + b("│"));
	}
	out.push(b("╰" + "─".repeat(inner) + "╯"));
	return out;
}

export function boxDivider(width: number, border: (s: string) => string): string {
	return border("├" + "─".repeat(width - 2) + "┤");
}

/* ---------- header bar ---------- */

export const HARNESS_LABEL: Record<string, string> = {
	code: "CODE",
	arch: "ARCHITECT",
	lead: "LEAD",
};

export function harnessLabel(name: string): string {
	return HARNESS_LABEL[name] ?? name.toUpperCase();
}

// Display order for the harness strip. All three are always listed so the set
// is discoverable from the UI rather than only from `--harness`.
export const HARNESS_ORDER = ["code", "arch", "lead"] as const;

export type HarnessState =
	| "active" // holding the wheel right now
	| "dispatcher" // the session's own harness, waiting on a sub-harness it dispatched
	| "idle";

/** Which harnesses to show and how to weight each. Pure, so the weighting is
 *  testable without a colour-capable terminal. */
export function harnessStates(base: string, active: string | null): { name: string; state: HarnessState }[] {
	const current = active ?? base;
	const names: string[] = [...HARNESS_ORDER];
	// a harness the build doesn't know about still gets a slot rather than
	// silently vanishing from the strip
	for (const n of [base, current]) if (n && !names.includes(n)) names.push(n);
	return names.map((name) => ({
		name,
		state: name === current ? "active" : name === base ? "dispatcher" : "idle",
	}));
}

export class HeaderBar implements Component {
	invalidate(): void {}
	// which harness this session started as, and which sub-harness (if any) is
	// running right now — the lead dispatches `code`/`architect` mid-turn, and
	// without this the output of a sub-session is indistinguishable from the
	// lead's own
	private base = "code";
	private active: string | null = null;
	// Deliberately bare otherwise: the model name lives only in the chat bar
	// (HintLine), and the repo path only in the startup banner.
	setBaseHarness(name: string): void {
		this.base = name;
	}

	setActiveHarness(name: string | null): void {
		this.active = name;
	}

	render(width: number): string[] {
		// All three harnesses, with the one actually executing lit. When a lead
		// has dispatched a sub-harness the lead stays half-lit, so the chain the
		// old `LEAD ▸ CODE` notation carried is still readable at a glance.
		const segs = harnessStates(this.base, this.active).map(({ name, state }) => {
			const label = harnessLabel(name);
			if (state === "active") return t.badge(` ${label} `);
			if (state === "dispatcher") return t.muted(label);
			return t.dim(label);
		});
		let bar = segs.join("  ");
		// too narrow for the full strip: keep the lit one, which is the only
		// segment that answers "what is running right now"
		if (visibleWidth(bar) + 1 > width) {
			const current = this.active ?? this.base;
			bar = t.badge(` ${harnessLabel(current)} `);
		}
		const pad = Math.max(0, width - visibleWidth(bar) - 1);
		const line = pad > 0 ? " ".repeat(pad) + bar + " " : truncateToWidth(bar, width);
		const fill = " ".repeat(Math.max(0, width - visibleWidth(line)));
		return [t.panelBg(line + fill), t.dim("─".repeat(width))];
	}
}

/* ---------- messages ---------- */

export class UserMessage implements Component {
	invalidate(): void {}
	constructor(private text: string) {}

	render(width: number): string[] {
		// design: .msg max-width 88%, right-aligned accent-bordered bubble
		const maxBox = Math.max(20, Math.floor(width * 0.88));
		const wrapped = wrapTextWithAnsi(this.text, maxBox - 4);
		const contentW = Math.max(...wrapped.map(visibleWidth), 3);
		const boxW = Math.min(maxBox, contentW + 4);
		const box = roundedBox(
			wrapped.map((l) => t.fg(l)),
			{ width: boxW, border: t.accent, pad: t.accentSoftBg },
		);
		const indent = " ".repeat(Math.max(0, width - boxW - 1));
		const label = " ".repeat(Math.max(0, width - 4)) + t.muted.bold("YOU");
		return [label, ...box.map((l) => indent + l)];
	}
}

export class AssistantMessage implements Component {
	// Markdown is rendered identically while streaming and after finalization,
	// so the line count never changes at finalize and the scroll position
	// stays put. The cursor is appended to the last rendered line (not fed
	// through the parser) so partial/unclosed markdown still parses cleanly.
	private cursor = false;
	private cachedLines: string[] | null = null;
	private lastParse = 0;

	constructor(private text: string) {}

	invalidate(): void {
		// width or theme changed upstream — drop the memo so the next frame re-renders
		this.cachedLines = null;
	}

	setText(text: string, cursor = false): void {
		this.text = text;
		this.cursor = cursor;
		if (!cursor) this.lastParse = 0; // finalization must always re-parse exactly
	}

	render(width: number): string[] {
		const bodyW = Math.max(20, Math.floor(width * 0.88));
		if (this.cursor && this.cachedLines !== null && Date.now() - this.lastParse < STREAM_PARSE_MS) {
			return [" " + t.muted.bold("AGENT"), ...this.cachedLines.map((l) => " " + l)];
		}
		this.lastParse = Date.now();
		const lines = renderMarkdown(this.text, bodyW);
		if (this.cursor) {
			if (lines.length > 0) lines[lines.length - 1] += t.accent("▎");
			else lines.push(t.accent("▎"));
		}
		this.cachedLines = lines;
		return [" " + t.muted.bold("AGENT"), ...lines.map((l) => " " + l)];
	}
}

export type NoticeStyle = "muted" | "danger" | "success" | "accent";

export class Notice implements Component {
	invalidate(): void {}
	constructor(
		private text: string,
		private style: NoticeStyle = "muted",
	) {}

	render(width: number): string[] {
		const paint =
			this.style === "danger" ? t.danger : this.style === "success" ? t.success : this.style === "accent" ? t.accent : t.muted;
		return wrapTextWithAnsi(this.text, width - 2).map((l) => " " + paint(l));
	}
}

/* ---------- thinking spinner ---------- */

export class Thinking implements Component {
	invalidate(): void {}
	private frame = 0;
	private timer: ReturnType<typeof setInterval> | null = null;
	onAbort?: () => void;

	constructor(private tui: TUI) {}

	start(): void {
		this.timer = setInterval(() => {
			this.frame = (this.frame + 1) % SPINNER.length;
			this.tui.requestRender();
		}, SPINNER_MS);
	}

	stop(): void {
		if (this.timer) clearInterval(this.timer);
		this.timer = null;
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.escape)) this.onAbort?.();
	}

	render(width: number): string[] {
		const glyph = SPINNER[this.frame].padEnd(5);
		const line = ` ${t.accentBold(glyph)} ${t.muted("Thinking")} ${t.dim("· esc to interrupt")}`;
		return [truncateToWidth(line, width)];
	}
}

/* ---------- reasoning trace (Ollama thinking models) ---------- */

// How many lines of a closed reasoning segment stay visible before the rest
// is elided. A thinking trace is a scratchpad, not an artifact — once the
// segment closes we keep the tail (the part nearest the answer) and fold the
// rest into a one-line "(+M lines elided)" marker.
const THINKING_KEEP_LINES = 4;

/** One contiguous reasoning segment streamed live from a thinking model.
 *  Dimmed throughout to read as distinct from the assistant's answer. While
 *  open it streams plain text (no markdown — it's a scratchpad) with a cursor;
 *  on close it collapses to the last N lines + "(+M lines elided)". A turn
 *  may open several of these if a model interleaves reasoning after content
 *  (docs-sanctioned); each is its own segment. */
export class ThinkingTrace implements Component {
	invalidate(): void {}
	private text = "";
	private open = true;
	private interrupted = false;

	constructor() {}

	/** Append a live reasoning chunk. Only meaningful while the segment is open. */
	append(chunk: string): void {
		if (!this.open) return;
		this.text += chunk;
	}

	/** Close the segment: stop streaming, collapse to the kept tail. */
	close(): void {
		this.open = false;
	}

	/** Close the segment as interrupted (dim, no fake done badge). */
	closeInterrupted(): void {
		this.open = false;
		this.interrupted = true;
	}

	isOpen(): boolean {
		return this.open;
	}

	render(width: number): string[] {
		const bodyW = Math.max(20, Math.floor(width * 0.88));
		const label = this.interrupted
			? t.dim("✕ reasoning (interrupted)")
			: this.open
				? t.dim("reasoning")
				: t.dim("reasoning");
		const lines: string[] = [" " + label];

		if (this.open) {
			// streaming: raw text with a cursor, no markdown
			const body = this.text + t.dim("▎");
			for (const l of wrapTextWithAnsi(body, bodyW)) {
				lines.push(" " + t.muted(l));
			}
			return lines;
		}

		// closed: collapse to the last N lines + an elision marker
		const wrapped = wrapTextWithAnsi(this.text, bodyW);
		if (wrapped.length <= THINKING_KEEP_LINES) {
			for (const l of wrapped) lines.push(" " + t.muted(l));
		} else {
			const elided = wrapped.length - THINKING_KEEP_LINES;
			lines.push(" " + t.dim(`(+${elided} lines elided)`));
			for (const l of wrapped.slice(-THINKING_KEEP_LINES)) {
				lines.push(" " + t.muted(l));
			}
		}
		return lines;
	}
}

/* ---------- permission cards ---------- */

export interface DiffLine {
	kind: "ctx" | "add" | "del";
	text: string;
}

export type PermissionSpec =
	| { kind: "bash"; cmd: string }
	| { kind: "edit" | "write"; file: string; lines: DiffLine[] }
	| { kind: "read_outside_repo"; tool: string; path: string }
	| { kind: "mcp"; server: string; tool: string; args: string };

export type Resolution = "approved" | "denied";

// The spec arrives as JSON off the wire, so its fields are whatever the server
// sent — a `read_outside_repo` payload has no `file`, and an older TUI reading
// it as an edit passed `undefined` into truncateToWidth, which throws. A throw
// inside render() escapes through pi-tui's render timer and kills the whole
// process, losing the session. Normalizing once here means no payload shape,
// present or future, can turn a permission prompt into a crash.
function normalizeSpec(spec: PermissionSpec): PermissionSpec {
	const s = spec as Partial<Record<string, unknown>> & { kind?: string };
	const kind =
		s.kind === "bash" || s.kind === "write" || s.kind === "read_outside_repo" || s.kind === "mcp"
			? s.kind
			: "edit";
	if (kind === "bash") return { kind, cmd: String(s.cmd ?? "") };
	if (kind === "read_outside_repo")
		return { kind, tool: String(s.tool ?? "read"), path: String(s.path ?? "?") };
	if (kind === "mcp")
		return { kind, server: String(s.server ?? "?"), tool: String(s.tool ?? "?"), args: String(s.args ?? "{}") };
	const raw = Array.isArray(s.lines) ? (s.lines as unknown[]) : [];
	const lines: DiffLine[] = raw.map((l) => {
		const d = (l ?? {}) as Partial<DiffLine>;
		return { kind: d.kind === "add" || d.kind === "del" ? d.kind : "ctx", text: String(d.text ?? "") };
	});
	return { kind, file: String(s.file ?? "?"), lines };
}

export class PermissionCard implements Component {
	invalidate(): void {}
	resolved: Resolution | null = null;
	onResolve?: (r: Resolution) => void;
	private spec: PermissionSpec;

	constructor(spec: PermissionSpec) {
		this.spec = normalizeSpec(spec);
	}

	handleInput(data: string): void {
		if (this.resolved) return;
		if (matchesKey(data, "y") || matchesKey(data, Key.enter)) this.resolve("approved");
		else if (matchesKey(data, "n") || matchesKey(data, Key.escape)) this.resolve("denied");
	}

	private resolve(r: Resolution): void {
		this.resolved = r;
		this.onResolve?.(r);
	}

	render(width: number): string[] {
		const boxW = Math.min(width - 2, 96);
		const inner = boxW - 4;
		const b = t.dim;

		if (this.resolved === "approved") {
			const msg =
				this.spec.kind === "bash"
					? "✓ Approved for this session"
					: this.spec.kind === "write"
						? "✓ Write approved"
						: this.spec.kind === "read_outside_repo"
							? "✓ Read approved"
							: this.spec.kind === "mcp"
								? "✓ MCP call approved"
								: "✓ Edit approved";
			return roundedBox([t.success.bold(msg)], { width: boxW, border: b, pad: t.panelBg }).map((l) => " " + l);
		}
		if (this.resolved === "denied") {
			return roundedBox([t.danger.bold("✕ Denied")], { width: boxW, border: b, pad: t.panelBg }).map((l) => " " + l);
		}

		const question =
			this.spec.kind === "bash"
				? "Run bash command?"
				: this.spec.kind === "write"
					? "Write file?"
					: this.spec.kind === "read_outside_repo"
						? "Read file outside repo?"
						: "Edit file?";
		const head = t.accent("●") + " " + t.fg.bold(question);

		// every field below is guaranteed a string by normalizeSpec()
		const body: string[] = [];
		if (this.spec.kind === "bash") {
			body.push(t.accent("› ") + t.fg(truncateToWidth(this.spec.cmd, inner - 2)));
		} else if (this.spec.kind === "read_outside_repo") {
			body.push(t.muted.bold(truncateToWidth(this.spec.path, inner)));
		} else {
			body.push(t.muted.bold(truncateToWidth(this.spec.file, inner)));
			for (const l of this.spec.lines) {
				const style = l.kind === "add" ? t.diffAdd : l.kind === "del" ? t.diffDel : t.diffCtx;
				const padded = l.text + " ".repeat(Math.max(0, inner - visibleWidth(l.text)));
				body.push(style(truncateToWidth(padded, inner)));
			}
		}

		const allowLabel =
			this.spec.kind === "bash"
				? "Allow this session"
				: this.spec.kind === "write"
					? "Allow write"
					: this.spec.kind === "read_outside_repo"
						? "Allow read"
						: "Allow edit";
		const buttons =
			t.btnPrimary(` ${allowLabel} `) + t.btnPrimary.dim(`[Y]`) + t.btnPrimary(" ") + "  " + t.fg(" Deny ") + t.muted("[N]");

		const lines = [head, boxDividerInner(inner), ...body, boxDividerInner(inner), buttons];
		return roundedBoxWithDividers(lines, { width: boxW, border: b, pad: t.panelBg }).map((l) => " " + l);
	}
}

// divider sentinel: rendered as ├───┤ instead of a padded content row
const DIVIDER = " DIVIDER ";
function boxDividerInner(_inner: number): string {
	return DIVIDER;
}

function roundedBoxWithDividers(lines: string[], opts: BoxOpts): string[] {
	const inner = opts.width - 2;
	const out: string[] = [];
	const b = opts.border;
	out.push(b("╭" + "─".repeat(inner) + "╮"));
	for (const line of lines) {
		if (line === DIVIDER) {
			out.push(b("├" + "─".repeat(inner) + "┤"));
			continue;
		}
		const vis = visibleWidth(line);
		const clipped = vis > inner - 2 ? truncateToWidth(line, inner - 2) : line;
		const padRight = " ".repeat(Math.max(0, inner - 2 - visibleWidth(clipped)));
		let content = " " + clipped + padRight + " ";
		if (opts.pad) content = opts.pad(content);
		out.push(b("│") + content + b("│"));
	}
	out.push(b("╰" + "─".repeat(inner) + "╯"));
	return out;
}

/* ---------- sub-harness dispatch ---------- */

/** The lead handing off to `code`/`architect`. Rendered as its own framed
 *  block so a sub-session's work is visibly not the lead's, with a live
 *  status line that resolves to the sub-session's verdict. */
export class DispatchBanner implements Component {
	invalidate(): void {}
	private status = "starting…";
	private done: { ok: boolean; summary: string } | null = null;

	constructor(
		private harness: string,
		private task: string,
		private seeded = false,
	) {}

	setStatus(text: string): void {
		this.status = text;
	}

	finish(ok: boolean, summary: string): void {
		this.done = { ok, summary };
	}

	isSettled(): boolean {
		return this.done !== null;
	}

	render(width: number): string[] {
		const boxW = Math.min(width - 1, 76);
		const inner = boxW - 2;
		const b = (s: string) => (this.done === null ? t.accent(s) : t.dim(s));
		const head = t.accentBold(`▸ ${harnessLabel(this.harness)}`) + t.muted("  sub-session");
		const body = [t.fg(this.task)];
		if (this.seeded) body.push(t.muted("seeded from the finalized architecture"));
		const foot = this.done
			? this.done.ok
				? t.success(`✓ ${this.done.summary}`)
				: t.danger(`✕ ${this.done.summary}`)
			: t.muted(`${this.status}`);
		const lines = [head, boxDividerInner(inner), ...body, boxDividerInner(inner), foot];
		return roundedBoxWithDividers(lines, { width: boxW, border: b, pad: t.panelBg }).map((l) => " " + l);
	}
}

/* ---------- model picker ---------- */

export interface ModelListEntry {
	spec: string;
	source: string;
	context_window: number | null;
}

export class ModelPicker implements Component {
	invalidate(): void {}
	onDone?: (spec: string | null) => void;
	private list: SelectList;
	private filter = "";

	constructor(models: ModelListEntry[], current: string, defaultSpec: string | null) {
		const items: SelectItem[] = models.map((m) => ({
			value: m.spec,
			label: (m.spec === current ? "● " : "  ") + m.spec,
			description:
				m.source +
				(m.context_window ? ` · ${Math.round(m.context_window / 1024)}k ctx` : "") +
				(m.spec === defaultSpec ? " · default" : ""),
		}));
		this.list = new SelectList(items, 10, {
			selectedPrefix: (s) => t.accentBold(s),
			selectedText: (s) => t.accentBold(s),
			description: (s) => t.muted(s),
			scrollInfo: (s) => t.dim(s),
			noMatch: (s) => t.muted(s),
		});
		this.list.onSelect = (item) => this.onDone?.(item.value);
		this.list.onCancel = () => this.onDone?.(null);
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.backspace)) {
			this.filter = this.filter.slice(0, -1);
			this.list.setFilter(this.filter);
			return;
		}
		// single printable char → filter; everything else (arrows, ⏎, esc) → list
		if (data.length === 1 && data >= " " && data !== "\x7f") {
			this.filter += data;
			this.list.setFilter(this.filter);
			return;
		}
		this.list.handleInput(data);
	}

	render(width: number): string[] {
		const head =
			" " +
			t.accent("●") +
			" " +
			t.fg.bold("Select model") +
			t.muted(this.filter ? `  filter: ${this.filter}` : "  type to filter · ⏎ select · esc cancel");
		return [truncateToWidth(head, width), ...this.list.render(Math.max(20, width - 2)).map((l) => "  " + l)];
	}
}

/* ---------- session picker ---------- */

export interface SessionListEntry {
	id: string;
	name: string;
	last_event: string;
}

export class SessionPicker implements Component {
	invalidate(): void {}
	onDone?: (id: string | null) => void;
	private list: SelectList;
	private filter = "";

	constructor(sessions: SessionListEntry[], current: string) {
		const items: SelectItem[] = sessions.map((s) => ({
			value: s.id,
			label: (s.id === current ? "● " : "  ") + s.name,
			description: s.id + (s.last_event ? ` · ${s.last_event}` : ""),
		}));
		this.list = new SelectList(items, 10, {
			selectedPrefix: (s) => t.accentBold(s),
			selectedText: (s) => t.accentBold(s),
			description: (s) => t.muted(s),
			scrollInfo: (s) => t.dim(s),
			noMatch: (s) => t.muted(s),
		});
		this.list.onSelect = (item) => this.onDone?.(item.value);
		this.list.onCancel = () => this.onDone?.(null);
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.backspace)) {
			this.filter = this.filter.slice(0, -1);
			this.list.setFilter(this.filter);
			return;
		}
		// single printable char → filter; everything else (arrows, ⏎, esc) → list
		if (data.length === 1 && data >= " " && data !== "\x7f") {
			this.filter += data;
			this.list.setFilter(this.filter);
			return;
		}
		this.list.handleInput(data);
	}

	render(width: number): string[] {
		const head =
			" " +
			t.accent("●") +
			" " +
			t.fg.bold("Resume session") +
			t.muted(this.filter ? `  filter: ${this.filter}` : "  type to filter · ⏎ resume · esc cancel");
		return [truncateToWidth(head, width), ...this.list.render(Math.max(20, width - 2)).map((l) => "  " + l)];
	}
}

/* ---------- thinking-mode picker ---------- */

/** The thinking-mode picker — mirrors ModelPicker but simpler: a flat
 *  list of mode labels (off/low/medium/high/max) with the active one marked.
 *  Arrow keys to navigate, Enter to select, Esc to cancel. No numbers.
 *  Renders whatever modes the server's think_list message sends, so the list
 *  is already filtered for the current model's provider (e.g. OpenRouter has
 *  no "max"). */
export class ThinkPicker implements Component {
	invalidate(): void {}
	onDone?: (mode: string | null) => void;
	private list: SelectList;

	constructor(modes: string[], current: string | null) {
		const items: SelectItem[] = modes.map((m) => ({
			value: m,
			label: (m === current ? "● " : "  ") + m,
			description: m === "off" ? "thinking disabled" : "",
		}));
		this.list = new SelectList(items, 10, {
			selectedPrefix: (s) => t.accentBold(s),
			selectedText: (s) => t.accentBold(s),
			description: (s) => t.muted(s),
			scrollInfo: (s) => t.dim(s),
			noMatch: (s) => t.muted(s),
		});
		this.list.onSelect = (item) => this.onDone?.(item.value);
		this.list.onCancel = () => this.onDone?.(null);
	}

	handleInput(data: string): void {
		this.list.handleInput(data);
	}

	render(width: number): string[] {
		const head =
			" " +
			t.accent("●") +
			" " +
			t.fg.bold("Select thinking mode") +
			t.muted("  ⏎ select · esc cancel");
		return [truncateToWidth(head, width), ...this.list.render(Math.max(20, width - 2)).map((l) => "  " + l)];
	}
}

/* ---------- chat bar ghost text ---------- */

/** Placeholder shown inside the empty chat bar. pi-tui's Editor has no
 *  placeholder support, so we paint one over the padding of its content row:
 *  the row is the reverse-video cursor block followed by blanks, and the ghost
 *  replaces those blanks. It is inert — the moment there is text it's gone, so
 *  it can never be mistaken for content or end up submitted. */
export class GhostEditor extends Editor {
	constructor(
		tui: TUI,
		theme: EditorTheme,
		private ghost: string,
	) {
		super(tui, theme);
	}

	render(width: number): string[] {
		const lines = super.render(width);
		// only when genuinely empty, and only on the standard 3-row box
		// (top rule / content / bottom rule) — anything else and we leave the
		// editor's own output alone rather than risk corrupting a frame
		if (lines.length < 3 || this.getText().length > 0) return lines;
		const row = lines[1];
		// drop the trailing blanks; escapes (the cursor block) survive because
		// the run ends in a reset, not whitespace
		const head = row.replace(/ +$/, "");
		const used = visibleWidth(head);
		if (used + visibleWidth(this.ghost) + 1 > width) return lines;
		lines[1] = head + t.dim(this.ghost) + " ".repeat(width - used - visibleWidth(this.ghost));
		return lines;
	}
}

/* ---------- prompt hint line ---------- */

// Plain (escape-free) accent theme used until the startup background query
// resolves — and equivalent to what NO_COLOR / non-TTY resolve to.
const PLAIN_THEME = resolveAccent({ background: "unknown", env: { NO_COLOR: "1" }, isTTY: true });

// The three-state approval mode — the TS mirror of src/bird/permissions.py's
// PermissionMode contract (the only intentional logic duplication, ~6 lines).
// The TUI and the console broker implement identical semantics against one
// reading of the truth.
export type PermissionMode = "normal" | "auto_edits" | "full_auto";

// Shift+Tab cycle order.
const NEXT_MODE: Record<PermissionMode, PermissionMode> = {
	normal: "auto_edits",
	auto_edits: "full_auto",
	full_auto: "normal",
};

// The payload kinds each mode auto-answers without showing the card. "offer"
// is NEVER covered: an offer's answer IS the feedback string, so an
// auto-approved offer with no feedback is a corrupted answer.
const AUTO_MODES: Record<PermissionMode, ReadonlySet<string>> = {
	normal: new Set(),
	auto_edits: new Set(["edit", "write", "read_outside_repo"]),
	full_auto: new Set(["edit", "write", "read_outside_repo", "bash"]),
};

export function autoApproves(mode: PermissionMode, kind: string): boolean {
	return AUTO_MODES[mode].has(kind);
}

export class HintLine implements Component {
	invalidate(): void {}
	private mode: PermissionMode = "normal";
	// session-cumulative token spend, as reported by the server in turn_end.
	// null until the first turn ends (nothing spent → nothing to show).
	private tokens: { in: number; out: number } | null = null;
	// the friendly Ollama thinking-mode label (off/low/medium/high/max) or
	// null when no mode is set (Ollama's auto/default behavior). Shown next to
	// the model name so the active reasoning effort is visible at a glance.
	private thinkMode: string | null = null;
	// knowledge-graph state ("ready" / "building" / "off"), or null before the
	// server has said. Lives here rather than in the transcript so it stays
	// visible instead of scrolling away.
	private kg: string | null = null;
	// resolved accent theme (set once the startup OSC 11 query
	// lands); until then the model name renders plain.
	private theme: AccentTheme = PLAIN_THEME;

	constructor(private model: string) {}

	/** Swap the resolved accent theme once the startup background query lands. */
	setTheme(theme: AccentTheme): void {
		this.theme = theme;
	}

	setModel(model: string): void {
		this.model = model;
	}

	setThinkMode(mode: string | null): void {
		this.thinkMode = mode;
	}

	setKg(status: string | null): void {
		this.kg = status;
	}

	setTokens(input: number, output: number): void {
		this.tokens = { in: input, out: output };
	}

	clearTokens(): void {
		this.tokens = null;
	}

	// Shift+Tab cycles normal → auto_edits → full_auto → normal. Returns the
	// new mode so the caller can post the right entry notice.
	cycleMode(): PermissionMode {
		this.mode = NEXT_MODE[this.mode];
		return this.mode;
	}

	getMode(): PermissionMode {
		return this.mode;
	}

	// /reload respawns bird serve: the fresh process has no memory of the mode,
	// so the TUI resets its own mode to normal on "ready" — silently keeping
	// full-auto across a code-reload respawn would be the one accidental-
	// persistence path in this design.
	resetMode(): void {
		this.mode = "normal";
	}

	render(width: number): string[] {
		const left = " " + t.muted("⇧⇥ to cycle");
		const mode =
			this.mode === "full_auto"
				? t.danger.bold("⚠ FULL AUTO")
				: this.mode === "auto_edits"
					? t.accentBold("auto-accept edits")
					: t.muted("ask everything");
		const tok = this.tokens
			? t.dim(`↑${abbrevTokens(this.tokens.in)} ↓${abbrevTokens(this.tokens.out)}  `)
			: "";
		// kg state and thinking mode sit next to the model name; each is absent
		// when unset (Ollama's default/auto behavior) so the line stays uncluttered
		const think = this.thinkMode ? t.dim("· think:") + t.muted(this.thinkMode) + "  " : "";
		const kg = this.kg ? t.dim("· kg:") + t.muted(this.kg) + "  " : "";
		const name = renderChatBarModelName(this.model, this.theme);

		// Degrade right-to-left by IMPORTANCE, not all-or-nothing: the model name
		// is the last thing to go, so a narrow terminal never costs you the one
		// place the model is named. Tokens drop first, then kg, then think mode,
		// then the approval mode, then the keybinding hints on the left.
		const rights = [
			tok + mode + "  " + kg + think + name + " ",
			mode + "  " + kg + think + name + " ",
			mode + "  " + think + name + " ",
			mode + "  " + name + " ",
			name + " ",
		];
		for (const l of [left, ""]) {
			for (const right of rights) {
				const gap = width - visibleWidth(l) - visibleWidth(right);
				if (gap >= 1) return [l + " ".repeat(gap) + right];
			}
		}
		// narrower than the model name itself — keep its tail, which is the part
		// that identifies the model (family suffix), same rule as the indicator
		return [truncateToWidth(name, width)];
	}
}

/** Same 12.4k abbreviation the arch UI uses for token counts ("12.4k / 40k"). */
function abbrevTokens(n: number): string {
	return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/* ---------- model indicator (currently unmounted) ---------- */

/** One line directly below the chat input frame, aligned to the prompt caret
 *  column: accent ◆ + accent BOLD model id + dim meta ("ctx 47k/200k" etc.).
 *  Never wraps — as width shrinks the meta segments drop right-to-left
 *  (switch hint first, then ctx counter), then the model id truncates from
 *  the LEFT with an ellipsis so the family stays visible. All layout logic
 *  lives in branding.ts (pure, testable); this component only holds state.
 *
 *  NOT mounted: the model name is shown once, in the chat bar. Kept because
 *  the ctx counter here has no other home if it's ever wired up. */
export class ModelIndicator implements Component {
	invalidate(): void {}
	private ctxUsed: number | null = null;
	private ctxWindow: number | null = null;
	private switchHint: string | null = null;
	// set for one render when the model switches mid-session: a 300ms
	// reverse-video flash of this line, plus a permanent transcript rule.
	private flashUntil = 0;

	constructor(
		private modelId: string,
		private theme: AccentTheme,
	) {}

	setModel(model: string): void {
		if (model !== this.modelId) {
			this.modelId = model;
			this.flashUntil = Date.now() + 300;
		}
	}

	setContext(used: number | null, window_: number | null): void {
		this.ctxUsed = used;
		this.ctxWindow = window_;
	}

	/** Swap the resolved accent theme once the startup OSC 11 query lands. */
	setTheme(theme: AccentTheme): void {
		this.theme = theme;
	}

	setSwitchHint(hint: string | null): void {
		this.switchHint = hint;
	}

	isFlashing(): boolean {
		return Date.now() < this.flashUntil;
	}

	input(): IndicatorInput {
		return {
			modelId: this.modelId,
			ctxUsed: this.ctxUsed,
			ctxWindow: this.ctxWindow,
			switchHint: this.switchHint,
		};
	}

	render(width: number): string[] {
		let line = renderIndicator(this.input(), this.theme, width);
		if (this.isFlashing() && !this.theme.plain) {
			line = "\x1b[7m" + line + "\x1b[27m";
		}
		return [line];
	}
}


/* ---------- setup prompts (prompt_request from serve) ---------- */

/** A single question from the setup walkthrough: free text, or a secret
 * rendered as dots so a key never lands in the scrollback. ⏎ answers,
 * esc skips (null). */
export class PromptInput implements Component {
	invalidate(): void {}
	onDone?: (value: string | null) => void;
	private input = new Input();

	constructor(
		private prompt: string,
		private secret: boolean,
		defaultValue = "",
	) {
		this.input.setValue(defaultValue);
		this.input.onSubmit = (v) => this.onDone?.(v);
		this.input.onEscape = () => this.onDone?.(null);
	}

	handleInput(data: string): void {
		this.input.handleInput(data);
	}

	render(width: number): string[] {
		const head =
			" " +
			t.accent("●") +
			" " +
			t.fg.bold(this.prompt) +
			t.muted(this.secret ? "  (hidden) ⏎ save · esc skip" : "  ⏎ ok · esc skip");
		const body = this.secret
			? ["  " + t.fg("•".repeat(this.input.getValue().length)) + t.accent("▏")]
			: this.input.render(Math.max(20, width - 2)).map((l) => "  " + l);
		return [truncateToWidth(head, width), ...body];
	}
}

export interface PromptChoice {
	value: string;
	label: string;
	description?: string;
}

/** A pick-one question from the walkthrough (the default model). */
export class ChoicePicker implements Component {
	invalidate(): void {}
	onDone?: (value: string | null) => void;
	private list: SelectList;
	private filter = "";

	constructor(
		private title: string,
		choices: PromptChoice[],
		current: string | null,
	) {
		const items: SelectItem[] = choices.map((c) => ({
			value: c.value,
			label: (c.value === current ? "● " : "  ") + c.label,
			description: c.description ?? "",
		}));
		this.list = new SelectList(items, 10, {
			selectedPrefix: (s) => t.accentBold(s),
			selectedText: (s) => t.accentBold(s),
			description: (s) => t.muted(s),
			scrollInfo: (s) => t.dim(s),
			noMatch: (s) => t.muted(s),
		});
		this.list.onSelect = (item) => this.onDone?.(item.value);
		this.list.onCancel = () => this.onDone?.(null);
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.backspace)) {
			this.filter = this.filter.slice(0, -1);
			this.list.setFilter(this.filter);
			return;
		}
		if (data.length === 1 && data >= " " && data !== "\x7f") {
			this.filter += data;
			this.list.setFilter(this.filter);
			return;
		}
		this.list.handleInput(data);
	}

	render(width: number): string[] {
		const head =
			" " +
			t.accent("●") +
			" " +
			t.fg.bold(this.title) +
			t.muted(this.filter ? `  filter: ${this.filter}` : "  type to filter · ⏎ select · esc keep current");
		return [truncateToWidth(head, width), ...this.list.render(Math.max(20, width - 2)).map((l) => "  " + l)];
	}
}
