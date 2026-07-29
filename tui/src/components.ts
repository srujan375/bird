import {
	type Component,
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
import { palette, SPINNER, SPINNER_MS, t } from "./theme.ts";

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

export class HeaderBar implements Component {
	invalidate(): void {}
	// which harness this session started as, and which sub-harness (if any) is
	// running right now — the lead dispatches `code`/`architect` mid-turn, and
	// without this the output of a sub-session is indistinguishable from the
	// lead's own
	private base = "code";
	private active: string | null = null;
	constructor(
		private cwd: string,
		private model: string,
	) {}

	setModel(model: string): void {
		this.model = model;
	}

	setCwd(cwd: string): void {
		this.cwd = cwd;
	}

	setBaseHarness(name: string): void {
		this.base = name;
	}

	setActiveHarness(name: string | null): void {
		this.active = name;
	}

	render(width: number): string[] {
		const path = t.muted(this.cwd);
		const dispatched = this.active !== null && this.active !== this.base;
		const chain = dispatched
			? `${harnessLabel(this.base)} ▸ ${harnessLabel(this.active as string)}`
			: harnessLabel(this.base);
		// lit badge only while a sub-harness holds the wheel; the resting state
		// stays dim so "something else is driving" reads at a glance
		const hb = dispatched ? t.badge(` ${chain} `) : t.dim(chain);
		const badge = hb + "  " + t.badge(` ${this.model.toUpperCase()} `);
		const left = ` ${path}`;
		const gap = width - visibleWidth(left) - visibleWidth(badge) - 1;
		let line: string;
		if (gap < 1) {
			line = truncateToWidth(left, width);
		} else {
			line = left + " ".repeat(gap) + badge + " ";
		}
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
	invalidate(): void {}
	private cursor = false;

	constructor(private text: string) {}

	setText(text: string, cursor = false): void {
		this.text = text;
		this.cursor = cursor;
	}

	render(width: number): string[] {
		const bodyW = Math.max(20, Math.floor(width * 0.88));
		if (this.cursor) {
			// streaming: raw text with cursor — don't render partial markdown
			const body = this.text + t.accent("▎");
			const wrapped = wrapTextWithAnsi(body, bodyW);
			return [" " + t.muted.bold("AGENT"), ...wrapped.map((l) => " " + t.fg(l))];
		}
		// finalized: render markdown (renderer applies its own colors per element)
		const lines = renderMarkdown(this.text, bodyW);
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

/* ---------- permission cards ---------- */

export interface DiffLine {
	kind: "ctx" | "add" | "del";
	text: string;
}

export type PermissionSpec =
	| { kind: "bash"; cmd: string }
	| { kind: "edit" | "write"; file: string; lines: DiffLine[] }
	| { kind: "read_outside_repo"; tool: string; path: string };

export type Resolution = "approved" | "denied";

// The spec arrives as JSON off the wire, so its fields are whatever the server
// sent — a `read_outside_repo` payload has no `file`, and an older TUI reading
// it as an edit passed `undefined` into truncateToWidth, which throws. A throw
// inside render() escapes through pi-tui's render timer and kills the whole
// process, losing the session. Normalizing once here means no payload shape,
// present or future, can turn a permission prompt into a crash.
function normalizeSpec(spec: PermissionSpec): PermissionSpec {
	const s = spec as Partial<Record<string, unknown>> & { kind?: string };
	const kind = s.kind === "bash" || s.kind === "write" || s.kind === "read_outside_repo" ? s.kind : "edit";
	if (kind === "bash") return { kind, cmd: String(s.cmd ?? "") };
	if (kind === "read_outside_repo")
		return { kind, tool: String(s.tool ?? "read"), path: String(s.path ?? "?") };
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

/* ---------- prompt hint line ---------- */

export class HintLine implements Component {
	invalidate(): void {}
	private autoApprove = false;

	constructor(private model: string) {}

	setModel(model: string): void {
		this.model = model;
	}

	setAutoApprove(on: boolean): void {
		this.autoApprove = on;
	}

	getAutoApprove(): boolean {
		return this.autoApprove;
	}

	render(width: number): string[] {
		const left = " " + t.muted("⏎ send · ⇧⏎ newline · / commands · ⇧⇥ auto-approve");
		const mode = this.autoApprove ? t.accentBold("auto-approve on") : t.muted("auto-approve off");
		const right = mode + "  " + t.muted(this.model) + " ";
		const gap = width - visibleWidth(left) - visibleWidth(right);
		if (gap < 1) return [truncateToWidth(left, width)];
		return [left + " ".repeat(gap) + right];
	}
}
