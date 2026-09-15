// The message queue state machine (Claude Code style), extracted from
// main.ts so it can be smoke-tested without a terminal (queue.test.ts).
//
// The single source of truth for everything queue-shaped: the FIFO of pending
// user turns, the held flag (interrupt/error), and the keyboard
// selection/edit-in-place state. main.ts owns the UI surfaces (bubbles, hint
// line, ghost text) and feeds every mutation back through onChange, so the
// surfaces can never drift from state.
//
// The submission decision lives here too: ONE funnel that reads busy/card/
// held and decides local/queue/inject/send, called by editor Enter, turn_end
// flush and edit-save alike — the no-race requirement is satisfied by
// construction on the single-threaded event loop.
//
// Two parking spots, not one. A message typed while a RUN is turning is
// shipped to serve immediately ("inject") and the runner appends it at its
// next step boundary; the item lingers here only so its bubble can be tracked
// until `user_injected` confirms it landed. A message typed while nothing is
// turning — setup, a mounted card, a slash command that has to go out as a
// command rather than a turn — is parked locally ("queue") and flushed the
// old way, because there is no loop running that would ever drain it.

export interface QueueItem {
	id: number;
	text: string;
	/** already shipped to serve, waiting for the running step to pick it up
	 *  (renders SENDING, not QUEUED). Absent/false = parked locally, nothing
	 *  outside this process has seen it. */
	sent?: boolean;
}

export interface QueueDecision {
	/** "local": a UI-local command (/quit /clear) — run immediately, never
	 *  queued, even while busy. "queue": park the text locally as a queued
	 *  turn. "inject": ship it to serve NOW for the running step to pick up.
	 *  "send": fire it as a real turn NOW. "send-head": the queue was held
	 *  and an explicit ⏎ (or a turn_end flush) releases the head — the caller
	 *  sends head.text, and `queued` carries what to do with the submitted
	 *  text ("queue" it behind the head, or nothing when the bar was empty).
	 *  "none": nothing to do (empty bar, no held queue). */
	action: "local" | "queue" | "inject" | "send" | "send-head" | "none";
	/** the command name for action === "local" ("quit" | "clear") */
	command?: string;
	/** the text to send for action === "send" | "send-head" | "inject" */
	text?: string;
	/** for "send-head": what to do with the submitted text after the head
	 *  goes out ("queue" = park it behind the head; undefined = bar was
	 *  empty, nothing more to do) */
	then?: "queue";
}

export interface QueueHost {
	/** is a turn running? */
	busy(): boolean;
	/** is the busy state the first-run setup walkthrough rather than a model
	 *  run? Setup turns no runner loop, so nothing would drain an injection —
	 *  its input parks locally and setup_end flushes it. */
	setupBusy(): boolean;
	/** is a PermissionCard or picker mounted (editor not the focused
	 *  component)? While a card is up, nothing queues — the user must answer
	 *  the card first. */
	cardUp(): boolean;
	/** called after every mutation so the UI surfaces re-sync */
	onChange(): void;
}

export class MessageQueue {
	private items: QueueItem[] = [];
	private held = false;
	private selected: number | null = null;
	private editing: number | null = null;
	private seq = 0;

	constructor(private host: QueueHost) {}

	get length(): number {
		return this.items.length;
	}
	get isHeld(): boolean {
		return this.held;
	}
	get selectedId(): number | null {
		return this.selected;
	}
	get editingId(): number | null {
		return this.editing;
	}
	/** snapshot for rendering/tests */
	list(): QueueItem[] {
		return this.items.map(({ id, text, sent }) => ({ id, text, sent }));
	}
	/** 1-based index of an id, or -1 */
	indexOf(id: number): number {
		return this.items.findIndex((q) => q.id === id);
	}
	item(id: number): QueueItem | undefined {
		return this.items.find((q) => q.id === id);
	}

	/** THE single submission funnel. Editor Enter, turn_end flush and
	 *  edit-save all land here, so there is no race window between a turn
	 *  ending and the user submitting: one function reads busy/card/held
	 *  and decides.
	 *
	 *  1. local commands (/quit /clear) run immediately, never queued —
	 *     even while busy
	 *  2. setup || card up || a slash command → park it locally
	 *  3. busy → inject it into the running step
	 *  4. held && !busy && empty bar → send the head of the queue
	 *  5. else → send as a normal turn now */
	submit(text: string, opts?: { emptyBar?: boolean }): QueueDecision {
		const trimmed = text.trim();

		// local commands bypass the queue entirely — even mid-turn
		if (trimmed.startsWith("/")) {
			const cmd = trimmed.slice(1).split(/\s+/)[0];
			if (cmd === "quit" || cmd === "exit" || cmd === "clear") {
				return { action: "local", command: cmd };
			}
		}

		if (opts?.emptyBar) {
			// empty-bar Enter: only meaningful when the queue is held —
			// release the head. Otherwise it is a no-op.
			if (this.held && !this.host.busy() && !this.host.cardUp() && this.items.length > 0) {
				const head = this.shift();
				this.held = false;
				this.host.onChange();
				return { action: "send-head", text: head.text };
			}
			return { action: "none" };
		}
		if (!trimmed) return { action: "none" };

		// A slash command is not a turn: main.ts routes it to bridge.command(),
		// and serve's _command refuses while a worker is alive. Injecting it
		// would hand the model the bare word "/mcp" as a prompt instead of
		// opening the catalog — the bare-/mcp bug in a third form. Park it.
		const isSlash = trimmed.startsWith("/");
		if (this.host.setupBusy() || this.host.cardUp() || (isSlash && this.host.busy())) {
			this.push(trimmed);
			return { action: "queue", text: trimmed };
		}
		if (this.host.busy()) {
			// a run IS turning: ship it now and let the loop drain it at its
			// next step. It stays in `items` only until user_injected lands.
			this.push(trimmed, true);
			return { action: "inject", text: trimmed };
		}
		if (this.held && this.items.length > 0) {
			// typing while held jumps the queue: the new text goes out NOW
			// and the queue stays held behind it
			return { action: "send", text: trimmed };
		}
		return { action: "send", text: trimmed };
	}

	/** Flush the head as the next turn (turn_end done/reply, setup_end).
	 *  No-op when the queue is empty, held, or something else owns the
	 *  screen (busy/card). Returns the head's text, or null. */
	flushHead(): string | null {
		if (this.items.length === 0 || this.held || this.host.busy() || this.host.cardUp()) return null;
		// An injected item is already with serve, which starts its own turn for
		// anything the loop did not reach. Flushing while one is outstanding
		// would race that turn and send the same words twice — wait for the
		// user_injected that retires it.
		if (this.items.some((q) => q.sent)) return null;
		const head = this.shift();
		this.host.onChange();
		return head.text;
	}

	/** serve confirmed an injected message reached the model (`user_injected`).
	 *  Drop it from the in-flight list; main.ts promotes its bubble in place,
	 *  so the bubble never moves on screen — it just stops being dim. */
	confirmInjected(): QueueItem | null {
		// the FIRST in-flight item, not the head: a locally parked slash
		// command can sit ahead of it and must not be retired in its place
		const idx = this.items.findIndex((q) => q.sent);
		if (idx < 0) return null;
		const [it] = this.items.splice(idx, 1);
		if (this.selected === it.id) this.selected = this.items[idx]?.id ?? this.items[idx - 1]?.id ?? null;
		if (this.editing === it.id) this.editing = null;
		this.host.onChange();
		return it;
	}

	/** serve handed back input it never showed the model (`input_unsent`,
	 *  after an interrupt or a failed turn). The items are still here — clear
	 *  the in-flight mark so a later flush can send them, and hold the queue
	 *  so nothing auto-fires into the wreckage the user just stopped. */
	reclaim(): void {
		for (const it of this.items) it.sent = false;
		this.hold();
		this.host.onChange();
	}

	/** Hold the queue: interrupted/error turns leave it waiting for an
	 *  explicit ⏎ rather than auto-firing the next turn. */
	hold(): void {
		if (this.items.length === 0 || this.held) return;
		this.held = true;
		this.host.onChange();
	}

	/** Everything that clears the queue wholesale (/clear, session reset). */
	clear(): void {
		this.items = [];
		this.held = false;
		this.selected = null;
		this.editing = null;
		this.host.onChange();
	}

	/** ↑ from the bar: select the last item (or step toward the head).
	 *  Returns the newly selected id, or null when the queue is empty. */
	selectUp(): number | null {
		if (this.items.length === 0) return null;
		const cur = this.selected === null ? -1 : this.indexOf(this.selected);
		const next = cur <= 0 ? this.items.length - 1 : cur - 1;
		this.selected = this.items[next].id;
		this.host.onChange();
		return this.selected;
	}

	/** ↓ from the bar: step toward the tail, exiting selection past it. */
	selectDown(): number | null {
		if (this.items.length === 0) return null;
		if (this.selected === null) return null;
		const cur = this.indexOf(this.selected);
		if (cur < 0 || cur >= this.items.length - 1) {
			this.selected = null;
			this.host.onChange();
			return null;
		}
		this.selected = this.items[cur + 1].id;
		this.host.onChange();
		return this.selected;
	}

	/** Exit selection mode (Esc). */
	clearSelection(): void {
		if (this.selected === null) return;
		this.selected = null;
		this.host.onChange();
	}

	/** ⌫ on an empty bar with a selection: remove the selected item.
	 *  Returns the removed id, or null. */
	removeSelected(): number | null {
		if (this.selected === null) return null;
		const id = this.selected;
		this.remove(id);
		return id;
	}

	/** Remove an item by id (⌫, or the edit flow's discard). */
	remove(id: number): void {
		const idx = this.indexOf(id);
		if (idx < 0) return;
		this.items.splice(idx, 1);
		if (this.selected === id) this.selected = this.items[idx]?.id ?? this.items[idx - 1]?.id ?? null;
		if (this.editing === id) this.editing = null;
		this.host.onChange();
	}

	/** ⏎ on a selection: lift the item's text into the bar for editing.
	 *  Returns the item, or null. */
	beginEdit(): QueueItem | null {
		if (this.selected === null) return null;
		this.editing = this.selected;
		this.host.onChange();
		return this.item(this.editing) ?? null;
	}

	/** Edit-save: replace the edited item's text in place (the id and so
	 *  the label/selection state stay stable). Returns the item, or null. */
	saveEdit(text: string): QueueItem | null {
		if (this.editing === null) return null;
		const it = this.item(this.editing);
		if (!it) {
			this.editing = null;
			this.host.onChange();
			return null;
		}
		it.text = text.trim();
		this.editing = null;
		this.host.onChange();
		return it;
	}

	/** Esc while editing: discard the edit, keep the item untouched. */
	cancelEdit(): void {
		if (this.editing === null) return;
		this.editing = null;
		this.host.onChange();
	}

	private push(text: string, sent = false): QueueItem {
		const it = { id: ++this.seq, text, sent };
		this.items.push(it);
		this.host.onChange();
		return it;
	}

	private shift(): QueueItem {
		const head = this.items.shift()!;
		if (this.selected === head.id) this.selected = this.items[0]?.id ?? null;
		if (this.editing === head.id) this.editing = null;
		return head;
	}
}

/* ---------- key routing ---------- */

/** What a queue key should do, or null when the key belongs to the editor. */
export type QueueKeyAction =
	| "select-up"
	| "select-down"
	| "begin-edit"
	| "remove-selected"
	| "cancel-edit"
	| "clear-selection";

export interface QueueKeyState {
	/** the editor is the focused component (no card/picker/catalog up) */
	editorFocused: boolean;
	/** the editor's autocomplete dropdown is open — it owns ↑/↓/⏎ itself */
	autocompleteOpen: boolean;
	/** the bar holds no text */
	barEmpty: boolean;
	selectedId: number | null;
	editingId: number | null;
	length: number;
}

/** Decide what a key means for the queue, given the editor's state.
 *
 *  Pure and terminal-free so the routing can be smoke-tested (queue.test.ts)
 *  — the whole selection/edit flow was unreachable for want of this wiring,
 *  and a routing bug is invisible until someone presses the key.
 *
 *  `key` is a pi-tui key id ("up" | "down" | "enter" | "backspace" |
 *  "escape"); main.ts maps the raw bytes to it. */
export function routeQueueKey(key: string, s: QueueKeyState): QueueKeyAction | null {
	// A card/picker/catalog owns every key while it is up: the editor is not
	// focused, so nothing here may steal its y/n/⏎/esc.
	if (!s.editorFocused) return null;

	// While an item is lifted into the bar the text IS the bar: ↑/↓/⏎/⌫ are
	// the editor's (cursor, submit, delete). Only Esc leaves the edit — and it
	// must win over the dropdown check below, because abandoning the edit
	// clears the bar, which closes the dropdown anyway.
	if (s.editingId !== null) return key === "escape" ? "cancel-edit" : null;

	// The dropdown owns ↑/↓ (list navigation) and ⏎ (complete) while it is
	// open; stealing them would make the completion list unusable.
	if (s.autocompleteOpen) return null;

	// ↑/↓ only when the bar is empty: with text in it the editor's own ↑/↓
	// (prompt history, cursor movement) is what the user means. A selection
	// never puts text in the bar, so the whole select→edit→remove flow stays
	// reachable.
	if (key === "up") return s.barEmpty && s.length > 0 ? "select-up" : null;
	if (key === "down") return s.barEmpty && s.selectedId !== null ? "select-down" : null;
	// ⏎/⌫ act on a selection only from an empty bar — otherwise ⏎ is a submit
	// and ⌫ is a character delete.
	if (key === "enter") return s.barEmpty && s.selectedId !== null ? "begin-edit" : null;
	if (key === "backspace") return s.barEmpty && s.selectedId !== null ? "remove-selected" : null;
	if (key === "escape") return s.selectedId !== null ? "clear-selection" : null;
	return null;
}