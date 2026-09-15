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
	// queued: the same geometry, two ink tiers. A queued item renders dim
	// (border t.dim, body t.muted, no fill, ◌ QUEUED i/n label); promoting it
	// to a real sent message is a flag flip, so the bubble's screen position
	// barely moves on flush — no scroll jump.
	private queued = false;
	private queueIndex = 0;
	private queueTotal = 0;
	private selected = false;
	// QUEUED = parked in the TUI, nothing has seen it. SENDING = already on
	// its way to serve, waiting for the running step to pick it up. Two very
	// different promises to the user, so they must not share a word.
	private queueLabel = "QUEUED";

	constructor(private text: string) {}

	/** Edit-in-place: swap the body text of a queued item (geometry recomputed
	 *  on the next render; the id/label/selection state is untouched). */
	setText(text: string): void {
		this.text = text;
	}

	/** Render as a queued (not yet sent) item: `◌ QUEUED i/n`, dim border,
	 *  muted body, no fill. Selection lights only the border (t.fg). */
	setQueued(index: number, total: number, selected = false, label = "QUEUED"): void {
		this.queued = true;
		this.queueIndex = index;
		this.queueTotal = total;
		this.selected = selected;
		this.queueLabel = label;
	}

	/** Promote to a real sent message: accent border + accentSoft fill + YOU. */
	promote(): void {
		this.queued = false;
		this.selected = false;
	}

	isQueued(): boolean {
		return this.queued;
	}

	render(width: number): string[] {
		// design: .msg max-width 88%, right-aligned accent-bordered bubble
		const maxBox = Math.max(20, Math.floor(width * 0.88));
		const wrapped = wrapTextWithAnsi(this.text, maxBox - 4);
		const contentW = Math.max(...wrapped.map(visibleWidth), 3);
		const boxW = Math.min(maxBox, contentW + 4);
		if (this.queued) {
			const border = this.selected ? t.fg : t.dim;
			const box = roundedBox(wrapped.map((l) => t.muted(l)), { width: boxW, border });
			const indent = " ".repeat(Math.max(0, width - boxW - 1));
			const labelText = `◌ ${this.queueLabel} ${this.queueIndex}/${this.queueTotal}`;
			const label =
				" ".repeat(Math.max(0, width - visibleWidth(labelText) - 2)) +
				t.muted.bold(labelText) +
				(this.selected ? t.muted(" · selected") : "");
			return [label, ...box.map((l) => indent + l)];
		}
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
	/** Esc pressed while the spinner is up — main.ts wires this to an
	 *  interrupt. A property (not a method) so the assignment in main.ts
	 *  type-checks and stays optional. */
	onAbort?: () => void;
	private frame = 0;
	private timer: ReturnType<typeof setInterval> | null = null;

	// Render-only: the spinner is never a focus target, so it owns no input
	// handling — Esc-to-interrupt lives in main.ts's global input listener,
	// which runs before the focused component. Focus stays on the editor.

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
	| { kind: "edit" | "write" | "delete"; file: string; lines: DiffLine[] }
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
		s.kind === "bash" || s.kind === "write" || s.kind === "delete" ||
		s.kind === "read_outside_repo" || s.kind === "mcp"
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
					: this.spec.kind === "delete"
						? "✓ Delete approved"
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
				: this.spec.kind === "delete"
					? "Delete file?"
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
		} else if (this.spec.kind === "mcp") {
			body.push(t.muted.bold(truncateToWidth(`${this.spec.server} · ${this.spec.tool}`, inner)));
			if (this.spec.args) body.push(t.muted(truncateToWidth(this.spec.args, inner)));
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

/** The rows whose value or label contains `filter`, case-insensitively.
 *  pi-tui's SelectList.setFilter is a PREFIX match on the value, and every
 *  model spec starts with its provider ("ollama:", "openrouter:") — so typing
 *  "qwen" there could never find ollama:qwen3.8. The pickers filter with this
 *  instead and rebuild their list from the survivors. */
export function filterPickerItems(items: SelectItem[], filter: string): SelectItem[] {
	const needle = filter.trim().toLowerCase();
	if (!needle) return items;
	return items.filter(
		(i) => i.value.toLowerCase().includes(needle) || i.label.toLowerCase().includes(needle),
	);
}

export interface ModelListEntry {
	spec: string;
	source: string;
	context_window: number | null;
	// the stored thinking level (off/low/medium/high/max) or null = auto
	think_mode?: string | null;
}

export class ModelPicker implements Component {
	invalidate(): void {}
	onDone?: (spec: string | null) => void;
	private items: SelectItem[];
	private matches: SelectItem[];
	private list: SelectList;
	private filter = "";

	/** `title` names the harness the pick is for ("Select model for design");
	 *  `aliasLabel` is the alias it lands on, shown on the entry that alias
	 *  already points at (default / architect / designer). */
	constructor(
		models: ModelListEntry[],
		current: string | null,
		defaultSpec: string | null,
		private title = "Select model",
		aliasLabel = "default",
	) {
		const items: SelectItem[] = models.map((m) => ({
			value: m.spec,
			label: (m.spec === current ? "● " : "  ") + m.spec,
			description:
				m.source +
				(m.context_window ? ` · ${Math.round(m.context_window / 1024)}k ctx` : "") +
				(m.think_mode ? ` · think: ${m.think_mode}` : "") +
				(m.spec === defaultSpec ? ` · ${aliasLabel}` : ""),
		}));
		this.items = items;
		this.matches = items;
		this.list = this.makeList(items);
	}

	/** A fresh list over `items` — SelectList cannot swap its rows, and its
	 *  selection resets on every filter change anyway. */
	private makeList(items: SelectItem[]): SelectList {
		const list = new SelectList(items, 10, {
			selectedPrefix: (s) => t.accentBold(s),
			selectedText: (s) => t.accentBold(s),
			description: (s) => t.muted(s),
			scrollInfo: (s) => t.dim(s),
			noMatch: (s) => t.muted(s),
		});
		list.onSelect = (item) => this.onDone?.(item.value);
		list.onCancel = () => this.onDone?.(null);
		return list;
	}

	private applyFilter(): void {
		this.matches = filterPickerItems(this.items, this.filter);
		this.list = this.makeList(this.matches);
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.backspace)) {
			this.filter = this.filter.slice(0, -1);
			this.applyFilter();
			return;
		}
		// single printable char → filter; everything else (arrows, ⏎, esc) → list
		if (data.length === 1 && data >= " " && data !== "\x7f") {
			this.filter += data;
			this.applyFilter();
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
			t.muted(this.filter ? `  filter: ${this.filter}` : "  type to filter · ⏎ select · esc cancel");
		if (!this.matches.length) return [truncateToWidth(head, width), t.muted(`    no model matches "${this.filter}"`)];
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
	private items: SelectItem[];
	private matches: SelectItem[];
	private list: SelectList;
	private filter = "";

	constructor(sessions: SessionListEntry[], current: string) {
		const items: SelectItem[] = sessions.map((s) => ({
			value: s.id,
			label: (s.id === current ? "● " : "  ") + s.name,
			description: s.id + (s.last_event ? ` · ${s.last_event}` : ""),
		}));
		this.items = items;
		this.matches = items;
		this.list = this.makeList(items);
	}

	/** A fresh list over `items` — SelectList cannot swap its rows, and its
	 *  selection resets on every filter change anyway. */
	private makeList(items: SelectItem[]): SelectList {
		const list = new SelectList(items, 10, {
			selectedPrefix: (s) => t.accentBold(s),
			selectedText: (s) => t.accentBold(s),
			description: (s) => t.muted(s),
			scrollInfo: (s) => t.dim(s),
			noMatch: (s) => t.muted(s),
		});
		list.onSelect = (item) => this.onDone?.(item.value);
		list.onCancel = () => this.onDone?.(null);
		return list;
	}

	private applyFilter(): void {
		this.matches = filterPickerItems(this.items, this.filter);
		this.list = this.makeList(this.matches);
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.backspace)) {
			this.filter = this.filter.slice(0, -1);
			this.applyFilter();
			return;
		}
		// single printable char → filter; everything else (arrows, ⏎, esc) → list
		if (data.length === 1 && data >= " " && data !== "\x7f") {
			this.filter += data;
			this.applyFilter();
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
		if (!this.matches.length) return [truncateToWidth(head, width), t.muted(`    no session matches "${this.filter}"`)];
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

	/** `title` and `escLabel` let the /model walk reuse this as its thinking
	 *  step, where esc means "keep the model's stored level", not cancel. */
	constructor(
		modes: string[],
		current: string | null,
		private title = "Select thinking mode",
		private escLabel = "esc cancel",
	) {
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
			t.fg.bold(this.title) +
			t.muted(`  ⏎ select · ${this.escLabel}`);
		return [truncateToWidth(head, width), ...this.list.render(Math.max(20, width - 2)).map((l) => "  " + l)];
	}
}

/* ---------- MCP catalog ---------- */

/** The TUI store, ported from the approved prototype (mcp-catalog-tui.html)
 *  and mirroring src/bird/mcp/catalog.py (the REPL's engine) view for view:
 *
 *    browse   one surface, no mode switch — the `>` query line is always
 *             live. Empty query = groups Connected → Available → Not
 *             installable; any keystroke filters into one flat list that
 *             keeps each row's status glyph. esc clears the query before
 *             it closes.
 *    detail   ⏎ on a row: full launch command (never truncated — wrapped at
 *             argument boundaries with `\` and a 4-col hanging indent), env
 *             as $VAR with a set/unset dot (values never read), repository,
 *             and for unsupported servers "Why bird can't install this" plus
 *             the manual mcp.json snippet.
 *    confirm  i: the card. Confirmation is the literal key y (not ⏎, not
 *             space) after a short arming delay; states that auto-approve
 *             does not apply.
 *    result   ✓ connected + tool count + sample names, or ✗ why + fixes +
 *             last server log lines + "kept in mcp.json"; r retry, x remove.
 *
 *  Degraded: unreachable-no-cache (error card, r retry, c show connected),
 *  stale cache (browsable, install disabled, header shows cache age), empty
 *  registry, no matches. The data comes from serve's mcp_catalog message;
 *  the writes go back as install/remove/test and return as mcp_result. */

export interface McpConnectedEntry {
	name: string;
	source?: string;
	disabled?: boolean;
	connected?: boolean;
	tools?: number;
	command?: string;
	args?: string[];
	env?: string[];
	error?: string;
}

export interface McpRegistryEntry {
	name: string;
	description?: string;
	version?: string;
	installable?: boolean;
	reason?: string;
	human_reason?: string;
	command?: string;
	args?: string[];
	env?: string[];
	repo?: string;
	url?: string;
}

export interface McpCatalogData {
	query: string;
	connected: McpConnectedEntry[];
	entries: McpRegistryEntry[];
	total: number | null;
	cache_age: number | null;
	registry_error: string | null;
	/** serve's instant first frame: connected servers only, page on its way */
	fetching?: boolean;
}

export interface McpResultData {
	name: string;
	ok: boolean;
	installed?: boolean;
	removed?: boolean;
	tools?: string[];
	why?: string;
	fix?: string[];
	log?: string[];
}

type CatalogView = "browse" | "detail" | "confirm" | "result";
type Degraded = "ok" | "unreachable" | "stale" | "empty";
interface CatalogRow {
	kind: "head" | "item";
	title?: string;
	n?: number;
	entry?: McpRegistryEntry;
	conn?: McpConnectedEntry;
}
/** what the result card is waiting on / showing */
type Pending = "install" | "reconnect" | "remove" | null;

/** how long the confirm card must be on screen before y counts — a held
 *  key (or the stroke that opened the card) must never fall through */
const CATALOG_ARM_MS = 450;
/** stable frame height across views, so switching browse → detail → confirm
 *  doesn't make the transcript jump (the prototype pads to a fixed frame) */
const CATALOG_MIN_LINES = 22;
const CATALOG_PAGE_ROWS = 18;
/** typing filters the loaded page instantly; after this pause the same
 *  query also goes to the registry's search API, whose results replace
 *  the page (the registry holds far more than one page) */
const CATALOG_SEARCH_DEBOUNCE_MS = 350;

const kbd = (s: string): string => t.accentSoftBg(t.fg(` ${s} `));

function humanReason(e: McpRegistryEntry): string {
	if (e.human_reason) return e.human_reason;
	const kind = (e.reason ?? "").split(" — ")[0].trim().toLowerCase();
	if (kind === "docker" || kind.startsWith("oci")) return "needs Docker · bird launches local subprocesses only";
	if (kind === "remote") return "remote (SSE/HTTP) endpoint · bird only speaks stdio today";
	return e.reason || "not installable by bird";
}

function ellip(s: string, n: number): string {
	if (s.length <= n) return s;
	const cut = s.slice(0, n - 1);
	const sp = cut.lastIndexOf(" ");
	return (sp > n * 0.6 ? cut.slice(0, sp) : cut) + "…";
}
const padTo = (s: string, n: number): string => s + " ".repeat(Math.max(0, n - visibleWidth(s)));

/** wrap at argument boundaries; 4-col hanging indent; trailing `\` */
export function wrapCommand(command: string, args: string[], width: number): string[] {
	const words = [command, ...args].filter((w) => w.length > 0);
	const lines: string[] = [];
	let cur = "";
	for (const w of words) {
		const piece = (cur ? " " : "") + w;
		if (cur && cur.length + piece.length > width - 2) {
			lines.push(cur + " \\");
			cur = "    " + w;
		} else cur += piece;
	}
	lines.push(cur);
	return lines;
}
const paintVars = (s: string): string => s.replace(/\$[A-Z_][A-Z0-9_]*/g, (m) => t.accent(m));

export class McpCatalog implements Component {
	invalidate(): void {}
	onClose?: () => void;
	onInstall?: (name: string) => void;
	onRemove?: (name: string) => void;
	onTest?: (name: string) => void;
	onRefresh?: (query: string) => void;
	onOpen?: (url: string) => void;
	/** the component needs a repaint on its own clock (arming delay) */
	onChange?: () => void;

	private connected: McpConnectedEntry[] = [];
	private entries: McpRegistryEntry[] = [];
	private total = 0;
	private cacheAge: number | null = null;
	private registryError: string | null = null;
	private degraded: Degraded = "ok";
	private fetching = false;
	private searching = false;
	private searchTimer: ReturnType<typeof setTimeout> | null = null;
	/** the query the loaded entries answer (registry-side) */
	private loadedQuery = "";
	private showConnectedOffline = false;

	private view: CatalogView = "browse";
	private query = "";
	private cursor = 0;
	private target: McpRegistryEntry | null = null;
	private armedAt = 0;
	private armTimer: ReturnType<typeof setTimeout> | null = null;
	private pending: Pending = null;
	private result: McpResultData | null = null;

	constructor(
		data: McpCatalogData,
		private envSet: (name: string) => boolean,
	) {
		this.setData(data);
	}

	/** a fresh mcp_catalog (initial, retry, or search) replaces the data in
	 *  place — the view, cursor and query survive a refresh */
	setData(data: McpCatalogData): void {
		if (data.fetching && this.entries.length) {
			// a search's instant frame: keep the current list on screen and
			// just mark the header — the skeleton is for the empty first load
			this.searching = true;
			this.onChange?.();
			return;
		}
		this.fetching = !!data.fetching;
		this.searching = false;
		if (!data.fetching) this.loadedQuery = data.query ?? "";
		this.connected = (data.connected ?? []).filter((c) => c.name);
		const configError = (data.connected ?? []).find((c) => !c.name && c.error)?.error ?? null;
		this.entries = data.entries ?? [];
		this.total = typeof data.total === "number" ? data.total : this.entries.length;
		this.cacheAge = data.cache_age ?? null;
		this.registryError = data.registry_error ?? configError ?? null;
		if (this.fetching) this.degraded = "ok";
		else if (data.registry_error) this.degraded = this.entries.length ? "stale" : "unreachable";
		else if (!this.entries.length && !this.connected.length) this.degraded = "empty";
		else this.degraded = "ok";
		if (data.query && !this.query && data.fetching) this.query = data.query;
		this.cursor = Math.min(this.cursor, Math.max(0, this.items().length - 1));
		if (this.view === "detail" && this.target) {
			const fresh = this.entries.find((e) => e.name === this.target?.name);
			if (fresh) this.target = fresh;
		}
	}

	/** an mcp_result for the pending install / reconnect / remove */
	setResult(r: McpResultData): void {
		if (this.target && r.name !== this.target.name) return;
		this.result = r;
		if (r.removed) {
			this.connected = this.connected.filter((c) => c.name !== r.name);
		} else if (r.installed || this.pending === "reconnect") {
			const info: McpConnectedEntry = {
				name: r.name,
				source: "project",
				connected: r.ok,
				tools: r.tools?.length ?? 0,
				error: r.ok ? undefined : r.why,
			};
			const i = this.connected.findIndex((c) => c.name === r.name);
			if (i >= 0) this.connected[i] = { ...this.connected[i], ...info };
			else this.connected.push(info);
		}
		this.view = "result";
	}

	/* ---------- list model ---------- */

	private connFor(name: string): McpConnectedEntry | undefined {
		return this.connected.find((c) => c.name === name);
	}
	private visibleEntries(): McpRegistryEntry[] {
		const q = this.query.trim().toLowerCase();
		if (!q) return this.entries;
		return this.entries.filter((e) => `${e.name} ${e.description ?? ""}`.toLowerCase().includes(q));
	}
	private rows(): CatalogRow[] {
		const out: CatalogRow[] = [];
		if (this.query.trim()) {
			for (const e of this.visibleEntries()) out.push({ kind: "item", entry: e, conn: this.connFor(e.name) });
			return out;
		}
		const names = new Set(this.connected.map((c) => c.name));
		const connectedRows: CatalogRow[] = this.entries
			.filter((e) => names.has(e.name))
			.map((e) => ({ kind: "item", entry: e, conn: this.connFor(e.name) }));
		const seen = new Set(connectedRows.map((r) => r.entry!.name));
		for (const c of this.connected) {
			// configured servers missing from the registry page are local facts
			// — they belong in the Connected group regardless
			if (!seen.has(c.name))
				connectedRows.push({
					kind: "item",
					entry: { name: c.name, description: c.command ? [c.command, ...(c.args ?? [])].join(" ") : "", installable: false, reason: "configured locally", command: c.command, args: c.args, env: c.env },
					conn: c,
				});
		}
		if (connectedRows.length) out.push({ kind: "head", title: "Connected", n: connectedRows.length }, ...connectedRows);
		const available = this.entries.filter((e) => !names.has(e.name) && e.installable !== false);
		if (available.length) out.push({ kind: "head", title: "Available", n: available.length }, ...available.map((e) => ({ kind: "item" as const, entry: e })));
		const unsupported = this.entries.filter((e) => !names.has(e.name) && e.installable === false);
		if (unsupported.length)
			out.push({ kind: "head", title: "Not installable on this machine", n: unsupported.length }, ...unsupported.map((e) => ({ kind: "item" as const, entry: e })));
		return out;
	}
	private items(): CatalogRow[] {
		return this.rows().filter((r) => r.kind === "item");
	}
	private current(): CatalogRow | null {
		return this.items()[this.cursor] ?? null;
	}

	/* ---------- keys ---------- */

	handleInput(data: string): void {
		const key = this.keyName(data);
		if (key === null) return;
		switch (this.view) {
			case "browse":
				this.keyBrowse(key);
				break;
			case "detail":
				this.keyDetail(key);
				break;
			case "confirm":
				this.keyConfirm(key);
				break;
			case "result":
				this.keyResult(key);
				break;
		}
	}

	private keyName(data: string): string | null {
		if (matchesKey(data, Key.up) || matchesKey(data, "ctrl+p")) return "up";
		if (matchesKey(data, Key.down) || matchesKey(data, "ctrl+n")) return "down";
		if (matchesKey(data, Key.pageUp)) return "pgup";
		if (matchesKey(data, Key.pageDown)) return "pgdown";
		if (matchesKey(data, Key.enter)) return "enter";
		if (matchesKey(data, Key.escape)) return "esc";
		if (matchesKey(data, Key.backspace)) return "backspace";
		if (data.length === 1 && data >= " " && data !== "\x7f") return data;
		return null;
	}

	private moveCursor(delta: number): void {
		const n = this.items().length;
		this.cursor = Math.max(0, Math.min(n - 1, this.cursor + delta));
	}

	private openConfirm(e: McpRegistryEntry): void {
		this.target = e;
		this.view = "confirm";
		this.armedAt = Date.now();
		if (this.armTimer) clearTimeout(this.armTimer);
		// repaint once the y button arms so the card shows it is live
		this.armTimer = setTimeout(() => {
			this.armTimer = null;
			this.onChange?.();
		}, CATALOG_ARM_MS + 10);
	}

	private canInstall(row: CatalogRow | null): boolean {
		return !!row?.entry && row.entry.installable !== false && !row.conn && this.degraded !== "stale";
	}

	private keyBrowse(key: string): void {
		if (this.fetching) {
			if (key === "q" || key === "esc") this.onClose?.();
			return;
		}
		if (this.degraded === "unreachable" || this.degraded === "empty") {
			if (key === "r") this.refresh();
			else if (key === "c") this.showConnectedOffline = !this.showConnectedOffline;
			else if (key === "q" || key === "esc") this.onClose?.();
			return;
		}
		if (key === "down") this.moveCursor(1);
		else if (key === "up") this.moveCursor(-1);
		else if (key === "pgdown") this.moveCursor(10);
		else if (key === "pgup") this.moveCursor(-10);
		else if (key === "enter") {
			const cur = this.current();
			if (cur?.entry) {
				this.target = cur.entry;
				this.view = "detail";
			}
		} else if (key === "esc") {
			// esc clears the query before it closes the catalog
			if (this.query) {
				this.query = "";
				this.cursor = 0;
				this.scheduleSearch();
			} else this.onClose?.();
		} else if (key === "backspace") {
			this.query = this.query.slice(0, -1);
			this.cursor = 0;
			this.scheduleSearch();
		} else if (key.length === 1) {
			// single-key actions only while the query is empty; otherwise
			// letters are search input
			const cur = this.current();
			if (!this.query && key === "q") this.onClose?.();
			else if (!this.query && key === "j") this.moveCursor(1);
			else if (!this.query && key === "k") this.moveCursor(-1);
			else if (!this.query && key === "r") this.refresh();
			else if (!this.query && key === "i" && cur?.conn) this.reconnect(cur.entry!.name);
			else if (!this.query && key === "i" && this.canInstall(cur)) this.openConfirm(cur!.entry!);
			else if (!this.query && key === "x" && cur?.conn) this.remove(cur.entry!.name);
			else if (!this.query && key === "i") {
				/* not installable here: swallow */
			} else {
				this.query += key;
				this.cursor = 0;
				this.scheduleSearch();
			}
		}
	}

	/** debounce: the registry sees the query once typing pauses; a query
	 *  the loaded page already answers (same text, or cleared back to the
	 *  browse page) is not re-fetched */
	private scheduleSearch(): void {
		if (this.searchTimer) clearTimeout(this.searchTimer);
		this.searchTimer = setTimeout(() => {
			this.searchTimer = null;
			const q = this.query.trim();
			if (q === this.loadedQuery) return;
			this.searching = true;
			this.onRefresh?.(q);
			this.onChange?.();
		}, CATALOG_SEARCH_DEBOUNCE_MS);
	}

	private keyDetail(key: string): void {
		const e = this.target;
		if (!e) return void (this.view = "browse");
		const conn = this.connFor(e.name);
		if (key === "esc" || key === "backspace") this.view = "browse";
		else if (key === "q") this.onClose?.();
		else if (key === "i" && conn) this.reconnect(e.name);
		else if (key === "i" && e.installable !== false && this.degraded !== "stale") this.openConfirm(e);
		else if (key === "x" && conn) this.remove(e.name);
		else if (key === "o" && e.repo) this.onOpen?.(e.repo.startsWith("http") ? e.repo : `https://${e.repo}`);
	}

	private keyConfirm(key: string): void {
		if (key === "y") {
			if (Date.now() - this.armedAt < CATALOG_ARM_MS) return; // not armed yet
			const e = this.target!;
			this.pending = "install";
			this.result = null;
			this.view = "result";
			this.onInstall?.(e.name);
		} else if (key === "n" || key === "esc") this.view = "detail";
		else if (key === "q") this.onClose?.();
	}

	private keyResult(key: string): void {
		if (this.result === null) {
			// still waiting on serve — only leaving is allowed
			if (key === "esc" || key === "q") this.onClose?.();
			return;
		}
		if (key === "enter" || key === "esc") {
			// back to the top of the (regrouped) list, as the prototype does
			this.view = "browse";
			this.pending = null;
			this.result = null;
			this.cursor = 0;
		} else if (key === "q") this.onClose?.();
		else if (key === "r" && !this.result.ok && !this.result.removed && this.target) this.reconnect(this.target.name);
		else if (key === "x" && !this.result.ok && !this.result.removed && this.target) this.remove(this.target.name);
	}

	private refresh(): void {
		if (this.entries.length) this.searching = true;
		else this.fetching = true;
		this.onRefresh?.(this.query.trim());
	}
	private reconnect(name: string): void {
		this.target = this.entries.find((e) => e.name === name) ?? this.target ?? { name };
		if (this.target.name !== name) this.target = { name };
		this.pending = "reconnect";
		this.result = null;
		this.view = "result";
		this.onTest?.(name);
	}
	private remove(name: string): void {
		this.target = this.entries.find((e) => e.name === name) ?? { name };
		this.pending = "remove";
		this.result = null;
		this.view = "result";
		this.onRemove?.(name);
	}

	/* ---------- rendering ---------- */

	render(width: number): string[] {
		// one column short of the viewport: a line that fills the last cell
		// makes some terminals auto-wrap and shifts everything below it
		const W = Math.max(60, width - 2);
		let L: string[];
		switch (this.view) {
			case "browse":
				L = this.renderBrowse(W);
				break;
			case "detail":
				L = this.renderDetail(W);
				break;
			case "confirm":
				L = this.renderConfirm(W);
				break;
			default:
				L = this.renderResult(W);
		}
		return L.map((l) => truncateToWidth(l, width - 1));
	}

	private header(crumb: string, right = ""): string {
		return " " + t.badge(" MCP ") + " " + crumb + (right ? "  " + right : "");
	}

	private glyph(r: CatalogRow): string {
		if (r.conn) return r.conn.connected ? t.success("✓") : t.danger("✗");
		if (r.entry && r.entry.installable === false) return t.muted("–");
		return " ";
	}
	private status(r: CatalogRow): string {
		if (r.conn) {
			if (r.conn.disabled) return t.muted("disabled");
			return r.conn.connected ? t.success(`${r.conn.tools ?? 0} tools`) : t.danger("not connected");
		}
		const e = r.entry;
		if (!e) return "";
		if (e.installable === false) {
			const kind = (e.reason ?? "").split(" — ")[0].trim().toLowerCase();
			if (kind === "docker" || kind.startsWith("oci")) return t.muted("needs docker");
			if (kind === "remote") return t.muted("remote only");
			return t.muted("unsupported");
		}
		const unset = (e.env ?? []).find((v) => !this.envSet(v));
		if (unset) return t.accent(`needs $${unset}`);
		return t.muted(e.version ? `v${e.version}` : "");
	}

	private itemRow(r: CatalogRow, selected: boolean, W: number): string {
		const e = r.entry!;
		const nameW = Math.min(42, Math.max(24, Math.floor(W * 0.4)));
		const status = this.status(r);
		// the description takes whatever the fixed columns leave, so a long
		// status ("needs $GITHUB_PERSONAL_ACCESS_TOKEN") never pushes the row
		// past the frame
		const descW = Math.max(12, W - 8 - nameW - visibleWidth(status) - 1);
		const name = padTo(ellip(e.name, nameW), nameW);
		const desc = padTo(ellip(e.description ?? "", descW), descW);
		const inner = `${this.glyph(r)} ${selected ? t.fg.bold(name) : name} ${t.muted(desc)} ${status}`;
		const line = truncateToWidth(`  ${selected ? t.accent("▸") : " "} ${inner}`, W);
		return selected ? t.panelBg(padTo(line, W)) : line;
	}

	private foot(L: string[], W: number, pos: string, act: string): string[] {
		while (L.length < CATALOG_MIN_LINES) L.push("");
		L.push(t.dim("├" + "─".repeat(Math.max(0, W - 2)) + "┤"));
		const keys = `${kbd("↑↓")} move  ${kbd("⏎")} open  ${act}${kbd("esc")} back  ${kbd("q")} close`;
		L.push(pos ? padTo(keys, W - visibleWidth(pos) - 1) + t.muted(pos) : keys);
		return L;
	}

	private renderBrowse(W: number): string[] {
		const L: string[] = [];
		let note: string;
		if (this.fetching) note = t.muted("fetching…");
		else if (this.searching) note = t.accent(`searching the registry for “${this.query.trim()}”…`);
		else if (this.loadedQuery) note = t.muted(`registry results for “${this.loadedQuery}”`);
		else if (this.degraded === "stale" && this.cacheAge !== null) note = t.accent(`offline · ${humanCacheAge(this.cacheAge)}`);
		else if (this.cacheAge !== null && this.cacheAge > 60) note = t.muted(`registry.modelcontextprotocol.io · ${humanCacheAge(this.cacheAge)}`);
		else note = t.muted("registry.modelcontextprotocol.io");
		L.push(this.header(t.fg.bold("Server catalog"), note));
		L.push("");
		L.push(t.dim("─".repeat(W)));
		const hint = this.query ? "" : t.muted("type to search · esc to clear");
		L.push(`  ${t.accent(">")} ${this.query}${t.btnPrimary(" ")}${hint}`);
		L.push(t.dim("─".repeat(W)));

		if (this.fetching) {
			const n = this.connected.length;
			L.push("");
			L.push(`  ${t.muted(`Connected · ${n}`)}`);
			for (const c of this.connected) {
				const mark = c.connected ? t.success(`${c.tools ?? 0} tools`) : t.danger("not connected");
				L.push(`    ${c.connected ? t.success("✓") : t.danger("✗")} ${c.name}  ${mark}`);
			}
			if (!n) L.push(`    ${t.muted("none configured yet")}`);
			L.push("");
			L.push(`  ${t.muted("Available")}`);
			for (let i = 0; i < 6; i++) L.push(`    ${t.dim("▆".repeat(28 + ((i * 7) % 20)) + "   " + "▆".repeat(36 + ((i * 11) % 24)))}`);
			L.push("");
			L.push(`    ${t.muted(this.query ? `searching the registry for “${this.query}”…` : "Loading the registry…")}  ${t.muted("the list stays usable — connected servers are local.")}`);
			return this.foot(L, W, "", "");
		}
		if (this.degraded === "unreachable" || this.degraded === "empty") {
			L.push("");
			if (this.degraded === "unreachable") {
				L.push(`  ${t.danger("✗")} ${t.fg.bold("Couldn't reach the registry")}`);
				for (const l of wrapTextWithAnsi(t.muted(this.registryError ?? ""), Math.max(20, W - 6))) L.push(`    ${l}`);
				L.push(`    ${t.muted("Nothing cached yet, so there is nothing to browse offline.")}`);
			} else {
				L.push(`  ${t.fg.bold("The registry returned no servers.")}`);
				L.push(`    ${t.muted("This usually means a registry-side outage. Your configured servers are unaffected.")}`);
			}
			L.push("");
			L.push(`    ${kbd("r")} retry   ${kbd("c")} ${this.showConnectedOffline ? "hide" : "show"} connected servers (from mcp.json)   ${kbd("q")} close`);
			L.push("");
			const n = this.connected.length;
			L.push(`  ${t.dim(`Your ${n} configured server${n === 1 ? "" : "s"} ${n === 1 ? "is" : "are"} unaffected — the catalog is only for discovery.`)}`);
			if (this.showConnectedOffline && n) {
				L.push("");
				L.push(`  ${t.muted(`Connected · ${n}`)}`);
				for (const c of this.connected) {
					const mark = c.connected ? t.success(`${c.tools ?? 0} tools`) : t.danger("not connected");
					L.push(`    ${c.connected ? t.success("✓") : t.danger("✗")} ${c.name}  ${mark}${c.error ? t.muted(` · ${c.error}`) : ""}`);
				}
			}
			return this.foot(L, W, "", "");
		}

		const R = this.rows();
		if (!R.length) {
			L.push("");
			if (this.searching) {
				L.push(`  ${t.fg.bold(`Nothing on this page matches “${this.query}”`)} ${t.muted("· asking the registry…")}`);
			} else {
				L.push(`  ${t.fg.bold(`No servers match “${this.query}”`)}`);
				L.push(`    ${t.muted("Search covers name and description. Try a vendor name (“sentry”) or a capability (“sql”).")}`);
			}
			L.push("");
			L.push(`    ${kbd("esc")} clear search   ${kbd("q")} close`);
			return this.foot(L, W, "", "");
		}
		// its must be filtered from the SAME R objects — rows() builds fresh
		// row objects on every call, so items() (which re-runs rows()) would
		// never match by identity and the ▸ selection marker would never show
		const its = R.filter((r) => r.kind === "item");
		const curLine = R.findIndex((r) => r.kind === "item" && its.indexOf(r) === this.cursor);
		const maxRows = CATALOG_PAGE_ROWS;
		let scroll = 0;
		if (curLine >= maxRows) scroll = Math.min(curLine - maxRows + 1, Math.max(0, R.length - maxRows));
		for (const r of R.slice(scroll, scroll + maxRows)) {
			if (r.kind === "head") {
				L.push("");
				L.push(`  ${t.muted(`${r.title} · ${r.n}`)}`);
				continue;
			}
			L.push(this.itemRow(r, its.indexOf(r) === this.cursor, W));
		}
		if (scroll + maxRows < R.length) L.push(`    ${t.muted(`↓ ${R.length - scroll - maxRows} more on this page`)}`);
		else if (!this.query && this.total > this.entries.length)
			L.push(`    ${t.muted(`↓ end of page · ${this.total} total in the registry · type to search all of it`)}`);
		if (this.degraded === "stale") L.push(`    ${t.accent("offline — install is disabled until the registry is reachable · r to retry")}`);
		const pos = `${its.length ? this.cursor + 1 : 0}/${its.length}${this.query ? " matches" : this.total > this.entries.length ? ` shown · ${this.total} total` : " shown"}`;
		const cur = this.current();
		let act = "";
		if (cur?.conn) act = `${kbd("i")} reconnect  ${kbd("x")} remove  `;
		else if (this.canInstall(cur)) act = `${kbd("i")} install  `;
		else if (cur?.entry) act = `${kbd("⏎")} why?  `;
		return this.foot(L, W, pos, act);
	}

	private envLines(e: McpRegistryEntry): string[] {
		if (!e.env?.length) return [t.muted("none")];
		return e.env.map((v) =>
			this.envSet(v)
				? `${t.success("●")} $${v}  ${t.muted("set in your environment · value never shown")}`
				: `${t.accent("○")} $${v}  ${t.accent("not set")} ${t.muted("· server will start but likely fail to connect")}`,
		);
	}

	private renderDetail(W: number): string[] {
		const e = this.target;
		if (!e) return ["  (no server selected)"];
		const conn = this.connFor(e.name);
		const L: string[] = [];
		L.push(this.header(`${t.muted("catalog ›")} ${t.fg.bold(e.name)}`, e.version ? t.muted(`v${e.version}`) : ""));
		L.push("");
		for (const l of wrapTextWithAnsi(e.description ?? "", Math.max(20, W - 4))) L.push(`  ${l}`);
		L.push("");
		if (conn) {
			L.push(
				conn.connected
					? `  ${t.success("✓ connected")} · ${conn.tools ?? 0} tools exposed`
					: `  ${t.danger("✗ installed but not connected")}${conn.error ? ` · ${conn.error}` : ""}`,
			);
		} else if (e.installable === false) {
			L.push(`  ${t.muted("–")} ${t.fg.bold("Why bird can't install this")}`);
			L.push(`    ${humanReason(e)}`);
			if (e.reason) L.push(`    ${t.muted(`registry says: “${e.reason}”`)}`);
		} else L.push(`  ${t.muted("not installed")}`);
		L.push("");
		L.push(`  ${t.muted(e.installable === false ? "Registry launch spec (not runnable by bird)" : "Launch command")}`);
		const cmd = e.command || e.url || "";
		if (cmd) for (const l of wrapCommand(cmd, e.args ?? [], W - 8)) L.push(`    ${t.panelBg(padTo(" " + paintVars(l), W - 10))}`);
		else L.push(`    ${t.muted("(none listed)")}`);
		L.push("");
		L.push(`  ${t.muted("Required environment")}`);
		for (const l of this.envLines(e)) L.push(`    ${l}`);
		L.push("");
		L.push(`  ${t.muted("Repository")}`);
		L.push(e.repo ? `    ${e.repo}  ${t.muted("(o to open)")}` : `    ${t.muted("(none listed)")}`);
		if (e.installable === false && !conn) {
			L.push("");
			L.push(`  ${t.muted("Run it yourself · add to mcp.json when bird supports this transport, or use a stdio bridge:")}`);
			const short = e.name.split("/").pop() ?? e.name;
			const snippet = e.url ? `"${short}": { "url": "${e.url}" }` : `"${short}": { "command": "${e.command ?? ""}", "args": ${JSON.stringify(e.args ?? [])} }`;
			L.push(`    ${t.panelBg(` ${snippet} `)}`);
		}
		while (L.length < CATALOG_MIN_LINES) L.push("");
		L.push(t.dim("├" + "─".repeat(Math.max(0, W - 2)) + "┤"));
		let act: string;
		if (conn) act = `${kbd("i")} reconnect  ${kbd("x")} remove`;
		else if (e.installable !== false) act = this.degraded === "stale" ? t.muted("install disabled offline") : `${kbd("i")} install`;
		else act = `${kbd("o")} open repository`;
		L.push(`${act}  ${kbd("esc")} back to catalog`);
		return L;
	}

	private renderConfirm(W: number): string[] {
		const e = this.target;
		if (!e) return ["  (no server selected)"];
		const boxW = Math.min(84, W - 2);
		const armed = Date.now() - this.armedAt >= CATALOG_ARM_MS;
		const inner: string[] = [];
		inner.push(`${t.fg.bold(`Install ${e.name}`)} ${e.version ? t.muted(`v${e.version}`) : ""}`);
		inner.push(t.muted("This runs third-party code on this machine as your user."));
		inner.push("");
		inner.push(t.muted("bird will run"));
		for (const l of wrapCommand(e.command ?? "", e.args ?? [], boxW - 8)) inner.push(`  ${paintVars(l)}`);
		inner.push("");
		inner.push(t.muted("and pass these from your environment at launch"));
		for (const l of this.envLines(e)) inner.push(`  ${l}`);
		inner.push("");
		inner.push(`${t.muted("written to")}  .bird/mcp.json ${t.muted("(project scope)")}`);
		inner.push("");
		const y = armed ? t.btnPrimary(" y  install ") : t.accentSoftBg(t.muted(" y  install "));
		inner.push(`${y}  ${t.accentSoftBg(t.fg(" n  cancel "))}`);
		inner.push(t.muted("auto-approve does not apply here · press y explicitly"));
		const L: string[] = [this.header(`${t.muted(`catalog › ${e.name} ›`)} ${t.fg.bold("confirm")}`), ""];
		for (const c of roundedBox(inner, { width: boxW, border: t.dim })) L.push("  " + c);
		L.push("");
		L.push(`  ${t.muted("Environment values are expanded by the server process; bird never reads or stores them.")}`);
		while (L.length < CATALOG_MIN_LINES) L.push("");
		L.push(t.dim("├" + "─".repeat(Math.max(0, W - 2)) + "┤"));
		L.push(`${kbd("y")} install  ${kbd("n")} / ${kbd("esc")} cancel`);
		return L;
	}

	private renderResult(W: number): string[] {
		const e = this.target;
		const name = e?.name ?? "?";
		const r = this.result;
		const boxW = Math.min(84, W - 2);
		const inner: string[] = [];
		let crumb: string;
		let keys: string;
		if (r === null) {
			const verb = this.pending === "remove" ? "removing" : this.pending === "reconnect" ? "reconnecting to" : "installing";
			crumb = t.fg.bold(`${verb}…`);
			inner.push(`${t.accent("…")} ${t.fg.bold(`${verb} ${name}`)}`);
			inner.push(t.muted(this.pending === "remove" ? "updating mcp.json" : "starting the server and testing the connection"));
			keys = `${kbd("esc")} close`;
		} else if (r.removed || this.pending === "remove") {
			crumb = t.fg.bold(r.ok ? "removed" : "remove failed");
			inner.push(r.ok ? `${t.success("✓")} ${t.fg.bold(`${name} removed from mcp.json`)}` : `${t.danger("✗")} ${t.fg.bold(`couldn't remove ${name}`)}`);
			if (!r.ok) inner.push(`  ${r.why ?? "unknown error"}`);
			if (r.ok) inner.push(t.muted("Its tools are gone from the next message on."));
			keys = `${kbd("⏎")} back to catalog  ${kbd("q")} close`;
		} else if (r.ok) {
			crumb = t.fg.bold("connected");
			inner.push(`${t.success("✓")} ${t.fg.bold(`${name} connected`)}`);
			const tools = r.tools ?? [];
			const sample = tools.slice(0, 3);
			const more = tools.length - sample.length;
			inner.push(`  exposes ${t.fg.bold(`${tools.length} tool${tools.length === 1 ? "" : "s"}`)}${sample.length ? ` · ${sample.map((s) => t.muted(s)).join("  ")}${more > 0 ? t.muted(`  +${more}`) : ""}` : ""}`);
			inner.push("");
			inner.push(t.muted("Tools are available to the agent from your next message."));
			keys = `${kbd("⏎")} back to catalog  ${kbd("q")} close`;
		} else {
			crumb = t.fg.bold("connection failed");
			inner.push(`${t.danger("✗")} ${t.fg.bold(`${name} ${r.installed === false ? "could not be installed" : "installed, but didn't connect"}`)}`);
			for (const l of wrapTextWithAnsi(r.why ?? "unknown error", boxW - 6)) inner.push(`  ${l}`);
			const fixes = r.fix ?? [];
			if (fixes.length) {
				inner.push("");
				for (const f of fixes) inner.push(`  ${t.muted("→")} ${f}`);
			}
			const log = (r.log ?? []).slice(-5);
			if (log.length) {
				inner.push("");
				inner.push(t.muted("last lines from the server:"));
				for (const l of log) inner.push(`  ${t.muted(truncateToWidth(l, boxW - 6))}`);
			}
			inner.push("");
			inner.push(t.muted(r.installed === false ? "Nothing was written to mcp.json." : "Kept in mcp.json — it will retry on next launch."));
			keys = r.installed === false ? `${kbd("⏎")} back to catalog` : `${kbd("r")} retry  ${kbd("x")} remove  ${kbd("⏎")} back to catalog`;
		}
		const L: string[] = [this.header(`${t.muted(`catalog › ${name} ›`)} ${crumb}`), ""];
		for (const c of roundedBox(inner, { width: boxW, border: t.dim })) L.push("  " + c);
		while (L.length < CATALOG_MIN_LINES) L.push("");
		L.push(t.dim("├" + "─".repeat(Math.max(0, W - 2)) + "┤"));
		L.push(keys);
		return L;
	}
}

export function humanCacheAge(seconds: number): string {
	if (seconds < 90) return "cached just now";
	if (seconds < 90 * 60) return `cached ${Math.floor(seconds / 60)}m ago`;
	if (seconds < 36 * 3600) return `cached ${Math.floor(seconds / 3600)}h ago`;
	return `cached ${Math.floor(seconds / 86400)}d ago`;
}

/* ---------- chat bar ghost text ---------- */

/** Placeholder shown inside the empty chat bar. pi-tui's Editor has no
 *  placeholder support, so we paint one over the padding of its content row:
 *  the row is the reverse-video cursor block followed by blanks, and the ghost
 *  replaces those blanks. It is inert — the moment there is text it's gone, so
 *  it can never be mistaken for content or end up submitted. */
export class GhostEditor extends Editor {
	// painted prefix on the content row (e.g. `edit ◌2 ›` while editing a
	// queued item). The text lives in the editor; the prefix is paint, the
	// same trick as the ghost text — never part of the value.
	private prefix: string | null = null;

	constructor(
		tui: TUI,
		theme: EditorTheme,
		private ghost: string,
	) {
		super(tui, theme);
	}

	/** Swap the placeholder shown when the bar is empty — the queue states
	 *  ("queue a message…", "⏎ sends ◌1 · …", …) replace "/ for commands". */
	setGhost(ghost: string): void {
		this.ghost = ghost;
	}

	/** Paint a prefix before the cursor on the content row (null clears it). */
	setPrefix(prefix: string | null): void {
		this.prefix = prefix;
	}

	getPrefix(): string | null {
		return this.prefix;
	}

	render(width: number): string[] {
		const lines = super.render(width);
		// only on the standard 3-row box (top rule / content / bottom rule) —
		// anything else and we leave the editor's own output alone rather
		// than risk corrupting a frame
		if (lines.length < 3) return lines;
		// painted prefix (edit-in-place marker): painted BEFORE the text, the
		// same trick as the ghost — never part of the value. Truncate the
		// combined row so the prefix can never overflow the frame.
		if (this.prefix !== null) {
			const row = lines[1];
			const painted = t.accent(this.prefix) + row;
			lines[1] = visibleWidth(this.prefix) + visibleWidth(row) > width ? truncateToWidth(painted, width) : painted;
			return lines;
		}
		if (this.getText().length > 0) return lines;
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
	// "delete" is deliberately in NEITHER set. Removing a file is the one action
	// here with nothing to undo it, so it asks every time regardless of mode.
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

	// queue state for the left slot: null = show the default ⇧⇥ hint;
	// {n, held} = show the queue indicator instead. held means the queue
	// survived an interrupt/error and waits for an explicit ⏎.
	private queue: { n: number; held: boolean } | null = null;

	constructor(private model: string) {}

	/** Show queue state in the left slot instead of ⇧⇥ when non-empty. */
	setQueue(n: number, held: boolean): void {
		this.queue = n > 0 ? { n, held } : null;
	}

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
		// queue indicator replaces the ⇧⇥ hint while the queue is non-empty:
		// accent while a turn runs, muted once held (interrupt/error) so the
		// state change reads at a glance.
		const left = this.queue
			? " " +
				(this.queue.held
					? t.muted(`◌ ${this.queue.n} held · ⏎ send next`)
					: t.accentBold(`◌ ${this.queue.n} queued · ↑ edit`))
			: " " + t.muted("⇧⇥ to cycle");
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
	private items: SelectItem[];
	private matches: SelectItem[];
	private list: SelectList;
	private filter = "";

	constructor(
		private title: string,
		choices: PromptChoice[],
		current: string | null,
		private escLabel = "esc keep current",
	) {
		const items: SelectItem[] = choices.map((c) => ({
			value: c.value,
			label: (c.value === current ? "● " : "  ") + c.label,
			description: c.description ?? "",
		}));
		this.items = items;
		this.matches = items;
		this.list = this.makeList(items);
	}

	/** A fresh list over `items` — SelectList cannot swap its rows, and its
	 *  selection resets on every filter change anyway. */
	private makeList(items: SelectItem[]): SelectList {
		const list = new SelectList(items, 10, {
			selectedPrefix: (s) => t.accentBold(s),
			selectedText: (s) => t.accentBold(s),
			description: (s) => t.muted(s),
			scrollInfo: (s) => t.dim(s),
			noMatch: (s) => t.muted(s),
		});
		list.onSelect = (item) => this.onDone?.(item.value);
		list.onCancel = () => this.onDone?.(null);
		return list;
	}

	private applyFilter(): void {
		this.matches = filterPickerItems(this.items, this.filter);
		this.list = this.makeList(this.matches);
	}

	handleInput(data: string): void {
		if (matchesKey(data, Key.backspace)) {
			this.filter = this.filter.slice(0, -1);
			this.applyFilter();
			return;
		}
		if (data.length === 1 && data >= " " && data !== "\x7f") {
			this.filter += data;
			this.applyFilter();
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
			t.muted(this.filter ? `  filter: ${this.filter}` : `  type to filter · ⏎ select · ${this.escLabel}`);
		if (!this.matches.length) return [truncateToWidth(head, width), t.muted(`    no choice matches "${this.filter}"`)];
		return [truncateToWidth(head, width), ...this.list.render(Math.max(20, width - 2)).map((l) => "  " + l)];
	}
}
