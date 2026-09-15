// MessageQueue state-machine smoke tests — no terminal, no rendering, just
// the pure decision logic in queue.ts. Follows branding.test.ts's pattern:
// check()/fails[]/exit 1.
import { MessageQueue, routeQueueKey, type QueueHost, type QueueKeyState } from "./queue.ts";

const fails: string[] = [];
function check(cond: boolean, msg: string): void {
	if (!cond) fails.push(msg);
}

/** A fake host: busy/cardUp are toggles, onChange counts re-syncs. */
class FakeHost implements QueueHost {
	busyFlag = false;
	setup = false;
	card = false;
	changes = 0;
	busy() {
		return this.busyFlag;
	}
	setupBusy() {
		return this.setup;
	}
	cardUp() {
		return this.card;
	}
	onChange() {
		this.changes++;
	}
}
function makeHost() {
	return new FakeHost();
}

/* ---------- submit-while-busy queues ---------- */
{
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	const d = q.submit("hello");
	check(d.action === "inject" && d.text === "hello", "busy submit injects into the running step");
	check(q.length === 1, "in-flight item tracked");
	check(q.list()[0].sent === true, "injected item is marked in-flight");
	check(h.changes === 1, "push notified host");
	const d2 = q.submit("second");
	check(d2.action === "inject" && q.length === 2, "second busy submit injects behind");
	check(q.list()[0].text === "hello" && q.list()[1].text === "second", "FIFO order preserved");
}

/* ---------- card up also queues ---------- */
{
	const h = makeHost();
	const q = new MessageQueue(h);
	h.card = true;
	check(q.submit("x").action === "queue", "card-up submit queues");
	check(q.list()[0].sent !== true, "a card-parked item is not in-flight");
	// setup turns no runner loop, so its input parks locally too
	const h2 = makeHost();
	const q2 = new MessageQueue(h2);
	h2.busyFlag = true;
	h2.setup = true;
	check(q2.submit("y").action === "queue", "setup submit queues instead of injecting");
	check(q2.list()[0].sent !== true, "a setup-parked item is not in-flight");
}

/* ---------- turn_end done flushes head ---------- */
{
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	h.setup = true;
	q.submit("a");
	q.submit("b");
	h.busyFlag = false;
	h.setup = false;
	const t = q.flushHead();
	check(t === "a", "flushHead returns head text");
	check(q.length === 1 && q.list()[0].text === "b", "head removed, rest intact");
	check(q.flushHead() === "b", "second flush drains queue");
	check(q.flushHead() === null, "empty queue flush is a no-op");
}

/* ---------- interrupted/error holds ---------- */
{
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	q.submit("a");
	q.submit("b");
	h.busyFlag = false;
	q.hold();
	check(q.isHeld, "hold sets the held flag");
	check(q.flushHead() === null, "held queue does not auto-flush");
	const d = q.submit("typed");
	check(d.action === "send" && d.text === "typed", "typing while held sends now (jumps queue)");
	check(q.length === 2 && q.isHeld, "queue stays held behind the jump");
	q.hold();
	check(q.list()[0].text === "a", "double hold is a no-op");
}

/* ---------- local commands never queue ---------- */
{
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	for (const cmd of ["/quit", "/exit", "/clear"]) {
		const d = q.submit(cmd);
		check(d.action === "local" && d.command === cmd.slice(1), `${cmd} is local even while busy`);
	}
	check(q.length === 0, "local commands never enqueue");
	check(q.submit("/model sonnet").action === "queue", "non-local /commands still queue while busy");
}

/* ---------- held + empty submit sends head ---------- */
{
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	q.submit("a");
	q.submit("b");
	h.busyFlag = false;
	q.hold();
	const d = q.submit("", { emptyBar: true });
	check(d.action === "send-head" && d.text === "a", "empty Enter releases the held head");
	check(!q.isHeld && q.length === 1, "held flag cleared, tail remains");
	const d2 = q.submit("", { emptyBar: true });
	check(d2.action === "none", "empty Enter with nothing held is a no-op");
	// held + empty bar + typed text: the typed text goes out, head stays held
	q.hold();
	const d3 = q.submit("new");
	check(d3.action === "send" && d3.text === "new", "typed text while held sends immediately");
	check(q.length === 1 && q.isHeld, "head still queued and held");
	// busy blocks the release
	h.busyFlag = true;
	q.hold();
	check(q.submit("", { emptyBar: true }).action === "none", "busy blocks the held release");
}

/* ---------- remove/edit flows ---------- */
{
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	q.submit("a");
	q.submit("b");
	q.submit("c");
	h.busyFlag = false;
	// ↑ selects tail, then head
	const sel = q.selectUp();
	check(sel !== null && q.item(sel)!.text === "c", "selectUp picks the tail first");
	const sel2 = q.selectUp();
	check(sel2 !== null && q.item(sel2)!.text === "b", "selectUp steps toward the head");
	check(q.selectDown() === sel, "selectDown steps back toward the tail");
	check(q.selectDown() === null, "selectDown past the tail exits selection");
	q.selectUp();
	// ⌫ removes the selected item
	const removed = q.removeSelected();
	check(removed === sel, "removeSelected returns the selected id");
	check(q.length === 2 && !q.list().some((i) => i.text === "c"), "selected item removed");
	// edit: lift, save, cancel
	q.selectUp();
	const it = q.beginEdit();
	check(it !== null && q.editingId === it!.id, "beginEdit marks the item as editing");
	const saved = q.saveEdit("edited");
	check(saved !== null && saved!.text === "edited" && q.editingId === null, "saveEdit replaces text in place");
	check(q.length === 2, "saveEdit does not add or drop items");
	q.selectUp();
	q.beginEdit();
	q.cancelEdit();
	check(q.editingId === null, "cancelEdit discards the edit state");
	check(q.item(saved!.id)!.text === "edited", "cancelEdit leaves the item untouched");
	// remove by id clears selection/editing references
	const id = q.list()[0].id;
	q.selectUp();
	q.beginEdit();
	q.remove(id);
	check(q.editingId === null, "remove clears the editing state of that id");
	check(q.selectedId !== null && q.selectedId !== id, "remove moves selection to a neighbor");
	// clear resets everything
	q.submit("z");
	q.hold();
	q.clear();
	check(q.length === 0 && !q.isHeld && q.selectedId === null, "clear wipes queue, held, selection");
}

/* ---------- slash commands are sends, not locals ---------- */
// Regression for the bare-/mcp "does nothing" bug: /mcp must leave the
// funnel as action "send" so main.ts's sendTurn() forwards it to
// bridge.command() — serve's _command("/mcp") is what emits mcp_catalog.
// Only /quit /exit /clear are UI-local; every other slash word is a send.
{
	const h = makeHost();
	const q = new MessageQueue(h);
	for (const cmd of ["/mcp", "/model", "/think", "/sessions", "/mcp search filesystem"]) {
		const d = q.submit(cmd);
		check(
			d.action === "send" && d.text === cmd,
			`${cmd} is a send (goes to bridge.command), not local/queued/dropped`,
		);
	}
	check(q.length === 0, "slash sends never enqueue when idle");
	// and while busy they queue like any other text, so the catalog request
	// fires after the in-flight turn instead of racing it
	h.busyFlag = true;
	const d = q.submit("/mcp");
	check(d.action === "queue" && q.length === 1, "/mcp while busy queues for the next flush");
	check(q.list()[0].sent !== true, "/mcp is never injected — it must go out as a command");
}

/* ---------- injection: confirm, reclaim, and the flush interlock ---------- */
{
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	q.submit("first");
	q.submit("second");
	// serve confirms them in order as the loop drains
	const c1 = q.confirmInjected();
	check(c1 !== null && c1!.text === "first", "confirmInjected retires the oldest in-flight item");
	check(q.length === 1, "confirmed item leaves the list");
	const c2 = q.confirmInjected();
	check(c2 !== null && c2!.text === "second", "second confirmation retires the second item");
	check(q.confirmInjected() === null, "confirming an empty list is a no-op");
}
{
	// a locally parked slash command ahead of an in-flight message must not be
	// retired in its place
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	q.submit("/mcp"); // parks locally
	q.submit("hello"); // injects
	const c = q.confirmInjected();
	check(c !== null && c!.text === "hello", "confirmInjected picks the in-flight item, not the head");
	check(q.length === 1 && q.list()[0].text === "/mcp", "the parked command survives the confirmation");
}
{
	// flushHead must not race the turn serve starts for an unconfirmed item
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	q.submit("in flight");
	h.busyFlag = false;
	check(q.flushHead() === null, "flushHead refuses while an injected item is unconfirmed");
	q.confirmInjected();
	check(q.length === 0, "nothing left to flush once confirmed");
}
{
	// interrupt hand-back: items come home, un-marked and held
	const h = makeHost();
	const q = new MessageQueue(h);
	h.busyFlag = true;
	q.submit("a");
	q.submit("b");
	q.reclaim();
	check(q.isHeld, "reclaim holds the queue");
	check(q.list().every((i) => !i.sent), "reclaim clears the in-flight mark");
	h.busyFlag = false;
	check(q.flushHead() === null, "a held queue still does not auto-flush");
	const d = q.submit("", { emptyBar: true });
	check(d.action === "send-head" && d.text === "a", "an explicit ⏎ releases the reclaimed head");
}

/* ---------- key routing (the ↑/⏎/⌫/esc flow) ---------- */
// Regression for the "press ↑ to edit does nothing" bug: the state machine
// implemented selectUp/beginEdit/saveEdit but nothing ever called them, so
// selectedId stayed null forever. routeQueueKey is the wiring, kept pure so
// it can be checked here instead of by pressing keys at a terminal.
{
	const base: QueueKeyState = {
		editorFocused: true,
		autocompleteOpen: false,
		barEmpty: true,
		selectedId: null,
		editingId: null,
		length: 2,
	};
	const s = (over: Partial<QueueKeyState>): QueueKeyState => ({ ...base, ...over });

	check(routeQueueKey("up", s({})) === "select-up", "↑ on an empty bar selects the tail");
	check(routeQueueKey("up", s({ length: 0 })) === null, "↑ with an empty queue is the editor's");
	check(routeQueueKey("up", s({ barEmpty: false })) === null, "↑ with text in the bar is the editor's");
	check(routeQueueKey("up", s({ autocompleteOpen: true })) === null, "the dropdown owns ↑");
	check(routeQueueKey("up", s({ editorFocused: false })) === null, "a card/picker owns every key");

	check(routeQueueKey("down", s({ selectedId: 1 })) === "select-down", "↓ steps toward the tail");
	check(routeQueueKey("down", s({})) === null, "↓ with nothing selected is the editor's");

	check(routeQueueKey("enter", s({ selectedId: 1 })) === "begin-edit", "⏎ on a selection lifts it for editing");
	check(routeQueueKey("enter", s({})) === null, "⏎ with no selection is a submit");
	check(routeQueueKey("enter", s({ selectedId: 1, barEmpty: false })) === null, "⏎ with text in the bar submits");
	check(
		routeQueueKey("enter", s({ selectedId: 1, autocompleteOpen: true })) === null,
		"the dropdown owns ⏎ (completion, not edit)",
	);

	check(routeQueueKey("backspace", s({ selectedId: 1 })) === "remove-selected", "⌫ on a selection removes it");
	check(routeQueueKey("backspace", s({})) === null, "⌫ with no selection is a character delete");

	check(routeQueueKey("escape", s({ selectedId: 1 })) === "clear-selection", "esc drops the selection");
	check(routeQueueKey("escape", s({})) === null, "esc with no selection falls through to the interrupt chain");

	// while editing, the bar holds the item's text: only esc leaves the edit
	const editing = s({ editingId: 1, selectedId: 1, barEmpty: false });
	check(routeQueueKey("escape", editing) === "cancel-edit", "esc while editing cancels the edit");
	check(routeQueueKey("up", editing) === null, "↑ while editing is the editor's cursor");
	check(routeQueueKey("enter", editing) === null, "⏎ while editing is the editor's submit (the save)");
	check(routeQueueKey("backspace", editing) === null, "⌫ while editing deletes a character");
	check(
		routeQueueKey("escape", s({ editingId: 1, autocompleteOpen: true })) === "cancel-edit",
		"esc while editing wins over the dropdown (abandoning the edit clears the bar)",
	);
}

/* ---------- the edit-save path end to end ---------- */
// main.ts's onSubmit routes ⏎ to saveEdit() while editingId is set, so the
// edited words go back into the item instead of out as a new turn.
{
	const h = makeHost();
	const q = new MessageQueue(h);
	// park it locally (a card is up) rather than injecting it: an injected
	// item is already with serve and is marked sent
	h.card = true;
	q.submit("original");
	h.card = false;
	q.selectUp();
	const lifted = q.beginEdit();
	check(lifted !== null && lifted!.text === "original", "beginEdit hands back the text to lift into the bar");
	const saved = q.saveEdit("edited words");
	check(saved !== null && saved!.text === "edited words", "the save replaces the item's text");
	check(q.length === 1 && q.list()[0].id === lifted!.id, "the save keeps the same item (id stable)");
	check(q.editingId === null, "the save leaves edit mode");
	// and the item is still a queued turn, not a sent one
	check(q.list()[0].sent !== true, "an edited item is still parked, not sent");
}

/* ---------- report ---------- */
if (fails.length) {
	console.error(`QUEUE FAIL (${fails.length}):`);
	for (const f of fails) console.error(`  - ${f}`);
	process.exit(1);
}
console.log("QUEUE OK");