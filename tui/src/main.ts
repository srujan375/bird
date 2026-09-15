import { spawn } from "node:child_process";
import {
	CombinedAutocompleteProvider,
	Container,
	Key,
	matchesKey,
	ProcessTerminal,
	Spacer,
	TUI,
} from "@mariozechner/pi-tui";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { Bridge, type ServerMessage } from "./bridge.ts";
import {
	AssistantMessage,
	ChoicePicker,
	DispatchBanner,
	GhostEditor,
	HeaderBar,
	HintLine,
	McpCatalog,
	ModelPicker,
	Notice,
	PermissionCard,
	type PermissionMode,
	type PermissionSpec,
	PromptInput,
	SessionPicker,
	Thinking,
	ThinkingTrace,
	ThinkPicker,
	UserMessage,
	autoApproves,
} from "./components.ts";
import { runDemoTurn } from "./demo.ts";
import { detectBackgroundFromEnv, renderBanner, resolveAccent } from "./branding.ts";
import { t } from "./theme.ts";
import { MessageQueue, routeQueueKey, type QueueKeyAction } from "./queue.ts";

/* ---------- args ---------- */

const argv = process.argv.slice(2);
function argValue(flag: string): string | undefined {
	const i = argv.indexOf(flag);
	return i >= 0 ? argv[i + 1] : undefined;
}
const DEMO = argv.includes("--demo");
const NO_KG = argv.includes("--no-kg");
const MODEL_ARG = argValue("--model");
const HARNESS_ARG = argValue("--harness");
const FROM_ARCH_ARG = argValue("--from-arch");
let repo = resolve(argValue("--repo") ?? process.cwd());
// `npm start` runs inside tui/ — the agent should work on the enclosing repo
if (basename(repo) === "tui" && existsSync(join(dirname(repo), "pyproject.toml"))) {
	repo = dirname(repo);
}

function tildify(p: string): string {
	const home = homedir();
	return p.startsWith(home) ? "~" + p.slice(home.length) : p;
}

// Mirrors bird's REPL commands (src/bird/repl.py). Skill names from the server
// are merged in at runtime when the "ready" message arrives (below).
const SLASH_COMMANDS = [
	{ name: "help", description: "list commands" },
	{ name: "model", description: "pick a harness, then its model and thinking level" },
	{ name: "think", description: "pick a thinking mode (off/low/medium/high/max)" },
	{ name: "kg", description: "knowledge graph status / build / query" },
	{ name: "mcp", description: "MCP server status / search / add" },
	{ name: "setup", description: "first-run walkthrough: keys, model pick, verify" },
	{ name: "doctor", description: "health check: one line per check" },
	{ name: "keys", description: "show provider keys · /keys set <NAME> to store one" },
	{ name: "tools", description: "list available tools" },
	{ name: "skills", description: "list available skills" },
	{ name: "compact", description: "compact conversation history" },
	{ name: "clear", description: "start a fresh conversation" },
	{ name: "reload", description: "respawn bird with latest code/skills (resume this session)" },
	{ name: "session", description: "show session info" },
	{ name: "sessions", description: "list all past sessions with names" },
	{ name: "continue", description: "resume a previous session" },
	{ name: "quit", description: "exit bird" },
];

/* ---------- UI scaffold ---------- */

const terminal = new ProcessTerminal();
const tui = new TUI(terminal);

// Accent colour, resolved synchronously at startup from COLORFGBG (when the
// terminal exports it) and otherwise assumed dark — theme.ts hard-codes a dark
// charcoal palette, so dark is the honest default rather than a guess.
//
// This deliberately does NOT probe the terminal with an OSC 11 background
// query. `process.stdout` on a TTY is a net.Socket, so attaching a 'data'
// listener to it puts it in flowing mode and it starts READING fd 1 — the same
// terminal device as fd 0. It then races process.stdin for keystrokes and wins,
// which left the TUI rendering fine but deaf to input. The reply arrives on
// stdin anyway, so listening on stdout could never have worked.
const accent = resolveAccent({ background: detectBackgroundFromEnv(process.env) ?? "dark" });

const header = new HeaderBar();
const hint = new HintLine(DEMO ? "demo" : "connecting…");
hint.setTheme(accent);
// The one place the model name is shown: bottom-right of the chat bar.
let currentModel = DEMO ? "demo" : "connecting…";
const chat = new Container();
// Queued bubbles live in their own region between the transcript and the bar,
// NOT in `chat`. Appending them to the transcript put them in the scrolling
// container, so every tool call / notice / streamed delta the turn produced
// after them pushed them further up until they scrolled off — the "pinned at
// the tail" comment was a promise nothing kept. Here they sit directly above
// the input bar and stay put however long the transcript grows.
const queueRegion = new Container();
const editor = new GhostEditor(
	tui,
	{
		borderColor: (s) => t.dim(s),
		selectList: {
			selectedPrefix: (s) => t.accentBold(s),
			selectedText: (s) => t.accentBold(s),
			description: (s) => t.muted(s),
			scrollInfo: (s) => t.dim(s),
			noMatch: (s) => t.muted(s),
		},
	},
	"/ for commands",
);
editor.setAutocompleteProvider(new CombinedAutocompleteProvider(SLASH_COMMANDS, repo));

tui.addChild(header);
tui.addChild(new Spacer(1));
tui.addChild(chat);
tui.addChild(new Spacer(1));
tui.addChild(queueRegion);
tui.addChild(editor);
tui.addChild(hint);

header.setBaseHarness(HARNESS_ARG ?? "code");

let busy = false;
// busy because the first-run setup walkthrough is running, not because a model
// run is. Setup turns no runner loop, so nothing would ever drain an injection
// — its input parks locally and setup_end flushes it the old way.
let setupBusy = false;
/** An interrupt was requested for the in-flight turn but its turn_end has
 *  not landed yet. While this is set, a second Ctrl+C exits instead of
 *  re-interrupting (Claude Code behavior). Cleared by endTurn(). */
let interruptRequested = false;
const thinking = new Thinking(tui);
let thinkingShown = false;

/* ---------- message queue (Claude Code style) ---------- */

// The state machine lives in queue.ts (testable without a terminal); main.ts
// owns the UI surfaces: the dimmed ◌ QUEUED bubbles in the queue region above
// the bar, the hint-line indicator and the editor's ghost/prefix paint. Every
// mutation funnels through renderQueue() so the surfaces can never drift from
// state.
const bubbles = new Map<number, UserMessage>();

/** Is a PermissionCard or picker currently mounted (i.e. the editor is NOT
 *  the focused component)? pi-tui exposes no getFocus(), so every setFocus
 *  call site keeps this flag in sync. */
let overlayUp = false;

function setFocus(component: Parameters<TUI["setFocus"]>[0]): void {
	overlayUp = component !== null && component !== editor;
	tui.setFocus(component);
	renderQueue();
}

/** Is a PermissionCard (or any non-editor component) currently focused? */
function cardUp(): boolean {
	return overlayUp;
}

/** Re-sync every queue surface with state: bubble labels/selection, the
 *  hint-line indicator, and the editor's ghost/prefix paint. */
function renderQueue(): void {
	const items = queue.list();
	const n = items.length;
	for (let i = 0; i < n; i++) {
		bubbles.get(items[i].id)?.setQueued(
			i + 1,
			n,
			items[i].id === queue.selectedId,
			items[i].sent ? "SENDING" : "QUEUED",
		);
	}
	hint.setQueue(n, queue.isHeld);
	// ghost text + painted prefix on the bar
	if (queue.editingId !== null) {
		const idx = queue.indexOf(queue.editingId);
		editor.setPrefix(idx >= 0 ? `edit ◌${idx + 1} › ` : null);
		editor.setGhost("/ for commands");
	} else {
		editor.setPrefix(null);
		if (cardUp()) editor.setGhost("answer the card first · y / n");
		else if (queue.selectedId !== null) {
			const idx = queue.indexOf(queue.selectedId);
			editor.setGhost(idx >= 0 ? `◌${idx + 1} selected · ⏎ edit · ⌫ remove · esc` : "/ for commands");
		} else if (queue.isHeld && !busy) editor.setGhost(`⏎ sends ◌1 · type to jump the queue`);
		else if (busy && !setupBusy && !cardUp()) editor.setGhost("⏎ sends now · lands at the next step");
		else if ((busy || cardUp()) && n > 0) editor.setGhost("queue a message…");
		else editor.setGhost("/ for commands");
	}
	tui.requestRender();
}

const queue = new MessageQueue({
	busy: () => busy,
	setupBusy: () => setupBusy,
	cardUp,
	onChange: renderQueue,
});

/** Push a queued turn: bubble in the queue region above the bar, surfaces
 *  re-synced. */
function queuePush(text: string): void {
	const bubble = new UserMessage(text);
	queueRegion.addChild(bubble);
	queueRegion.addChild(new Spacer(1));
	bubbles.set(queue.list().at(-1)!.id, bubble);
	renderQueue();
}

/** Remove a queued item's bubble from the queue region (state lives in
 *  queue.ts). */
function queueRemoveBubble(id: number): void {
	const bubble = bubbles.get(id);
	if (!bubble) return;
	const bi = queueRegion.children.indexOf(bubble);
	if (bi < 0) return;
	queueRegion.removeChild(bubble);
	// the spacer that followed it
	if (queueRegion.children[bi] instanceof Spacer) queueRegion.removeChild(queueRegion.children[bi]);
	bubbles.delete(id);
}

/** Send `text` as a real turn NOW: bubble in the live region, busy on,
 *  spinner up, per-turn stream guards armed.
 *
 *  A leading "/" makes it a COMMAND, not a turn: serve's _command() handles
 *  it (bare /model /think /mcp /sessions emit their pickers; /mcp search|add
 *  and the rest print command_output). Sending it as user_input instead
 *  starts a model turn whose prompt is the bare command word — the model
 *  replies conversationally (or errors), and the catalog/picker never
 *  appears. That was the bare-/mcp "does nothing" bug. */
function sendTurn(text: string): void {
	addToChat(new UserMessage(text));
	// fresh turn: a prior interrupt request no longer applies, so the first
	// Ctrl+C of THIS turn interrupts again instead of exiting
	interruptRequested = false;
	if (DEMO) {
		busy = true;
		streamedReply = false;
		streamedContent = false;
		showThinking();
		setFocus(editor);
		runDemoTurn({ tui, chat, thinking: { hide: hideThinking }, addToChat, endTurn });
		return;
	}
	if (text.startsWith("/")) {
		const cmd = text.slice(1).split(/\s+/)[0];
		// Only a /<skill> (a real model turn) and /setup (released by
		// setup_end) go busy: their replies end with a turn_end/setup_end.
		// Every other built-in answers with a picker, a catalog or a
		// command_output and never sends a turn_end — going busy for those
		// left the spinner up forever and queued all later input. That was
		// the "/mcp does nothing" bug in its second form.
		if (cmd === "setup" || skillNames.has(cmd)) {
			busy = true;
			streamedReply = false;
			streamedContent = false;
			showThinking();
		}
		setFocus(editor);
		bridge?.command(text);
		return;
	}
	busy = true;
	streamedReply = false;
	streamedContent = false;
	showThinking();
	setFocus(editor);
	bridge?.userInput(text);
}

/** Run a UI-local command immediately — never queued, even while busy. */
function runLocalCommand(cmd: string): void {
	if (cmd === "quit" || cmd === "exit") {
		if (bridge) bridge.command("/quit");
		else shutdown(0);
		return;
	}
	if (cmd === "clear") {
		chat.clear();
		queueRegion.clear();
		bubbles.clear();
		queue.clear();
	}
}

/** THE single submission funnel — editor Enter, turn_end flush and edit-save
 *  all land here, so there is no race window between a turn ending and the
 *  user submitting: one function reads busy/card/held and decides.
 *
 *  1. local commands (/quit /clear) run immediately, never queued — even busy
 *  2. busy || card up → queue it
 *  3. held && !busy && empty submit → send the head of the queue
 *  4. else → send as a normal turn now */
function submit(text: string, opts?: { emptyBar?: boolean }): void {
	const d = queue.submit(text, opts);
	switch (d.action) {
		case "local":
			runLocalCommand(d.command!);
			return;
		case "queue":
			queuePush(d.text!);
			return;
		case "inject":
			// straight out to serve, which parks it for the running step. The
			// bubble goes up dim ("SENDING") and promotes in place when the
			// loop confirms with user_injected — it never moves on screen.
			queuePush(d.text!);
			bridge?.userInput(d.text!);
			return;
		case "send":
		case "send-head":
			sendTurn(d.text!);
			return;
	}
}

/** Flush the head of the queue as the next turn (turn_end done/reply,
 *  setup_end). No-op when the queue is empty or held. */
function flushQueueHead(): void {
	const text = queue.flushHead();
	if (text === null) return;
	// drop the bubble of whichever id just left the state machine
	for (const id of bubbles.keys()) {
		if (!queue.item(id)) {
			queueRemoveBubble(id);
			break;
		}
	}
	sendTurn(text);
}

/** Hold the queue: interrupted/error turns leave it waiting for an explicit
 *  ⏎ rather than auto-firing the next turn. */
function holdQueue(): void {
	queue.hold();
}

/** Apply a routed queue key. The decision (which key means what, and when it
 *  belongs to the editor instead) is routeQueueKey's; this is the effect. */
function applyQueueKey(action: QueueKeyAction): void {
	switch (action) {
		case "select-up":
			queue.selectUp();
			break;
		case "select-down":
			queue.selectDown();
			break;
		case "begin-edit": {
			// lift the item's text into the bar — the ghost text promises it
			const it = queue.beginEdit();
			if (it) editor.setText(it.text);
			break;
		}
		case "remove-selected":
			queue.removeSelected();
			break;
		case "cancel-edit":
			queue.cancelEdit();
			editor.setText("");
			break;
		case "clear-selection":
			queue.clearSelection();
			break;
	}
	tui.requestRender();
}

// the sub-harness the lead is currently running, if any. The backend emits
// `dispatch` when `code`/`architect` starts and closes it with a tool_result
// carrying details.harness.
let dispatch: DispatchBanner | null = null;

function endDispatch(ok: boolean, summary: string): void {
	if (!dispatch) return;
	dispatch.finish(ok, summary);
	dispatch = null;
	header.setActiveHarness(null);
	tui.requestRender();
}

function addToChat(...components: Parameters<Container["addChild"]>[0][]): void {
	hideThinking();
	for (const c of components) {
		chat.addChild(c);
		chat.addChild(new Spacer(1));
	}
	if (busy) showThinking();
	tui.requestRender();
}

function showThinking(): void {
	if (!thinkingShown) {
		chat.addChild(thinking);
		thinking.start();
		thinkingShown = true;
	}
}

function hideThinking(): void {
	if (thinkingShown) {
		thinking.stop();
		chat.removeChild(thinking);
		thinkingShown = false;
	}
}

function endTurn(): void {
	busy = false;
	interruptRequested = false;
	hideThinking();
	// a turn can end while a catalog/picker/card is up (e.g. /mcp opened
	// mid-turn): focus belongs to the overlay, not the editor — restoring
	// it here left the catalog rendered but key-dead
	if (!cardUp()) setFocus(editor);
	tui.requestRender();
}

function setModel(model: string): void {
	// No transcript notice here: the chat bar is the single place the model is
	// named, and this fired on the first `ready` too, showing it twice.
	currentModel = model;
	hint.setModel(model);
	tui.requestRender();
}

function setThinkMode(mode: string | null): void {
	hint.setThinkMode(mode);
	tui.requestRender();
}

/* ---------- bridge wiring ---------- */

function shortToolLabel(name: string, argsJson: string): string {
	try {
		const args = JSON.parse(argsJson || "{}");
		const detail = args.command ?? args.path ?? args.question ?? args.summary ?? "";
		const label = `${name} ${String(detail)}`.trim().replace(/\s+/g, " ");
		return label.length > 100 ? label.slice(0, 100) + "…" : label;
	} catch {
		return name;
	}
}

let bridge: Bridge | null = null;

/* Skill names from the server's `ready`. A `/<skill>` is not a UI command —
   the server runs it as a model turn — so the editor has to know which slash
   words start a turn and go busy for those. */
let skillNames = new Set<string>();
/** the open MCP catalog, if any — mcp_catalog refreshes and mcp_result land in it */
let activeCatalog: McpCatalog | null = null;

/* streaming state: assistant text arrives token-by-token via assistant_delta,
   then the "assistant" harness event finalizes it. When the finalized message
   had no tool calls it IS the reply, so turn_end must not re-add it. */
let streamMsg: AssistantMessage | null = null;
let streamText = "";
let streamedReply = false;
/* True once any assistant content was streamed AND finalized into a visible
   AssistantMessage this turn — whether or not it was the reply (a `done` turn
   streams the summary text alongside the done tool call, so turn_end must
   not re-add it). Reset per turn in editor.onSubmit. */
let streamedContent = false;

/* reasoning-trace state: thinking models stream reasoning via thinking_delta
   in one or more contiguous segments (Ollama may interleave reasoning after
   content). Each open segment is its own ThinkingTrace block above the
   answer; the first assistant_delta closes the current segment (collapses
   it, does NOT discard — a later thinking_delta opens a fresh one), and the
   complete "thinking" harness event / turn_end closes whatever remains. */
let thinkingTrace: ThinkingTrace | null = null;

function finalizeStream(content: string | null, cursor = false): void {
	if (!streamMsg) return;
	streamMsg.setText((content ?? streamText).trim(), cursor);
	streamMsg = null;
	streamText = "";
	tui.requestRender();
}

/** Close the current reasoning segment (collapse to last-N lines). A later
 *  thinking_delta opens a fresh one, so interleaved reasoning is never lost. */
function closeThinkingSegment(): void {
	if (thinkingTrace) {
		thinkingTrace.close();
		thinkingTrace = null;
		tui.requestRender();
	}
}

/** Close any open reasoning segment as interrupted (dim, no fake done badge). */
function finalizeThinkingInterrupted(): void {
	if (thinkingTrace) {
		thinkingTrace.closeInterrupted();
		thinkingTrace = null;
		tui.requestRender();
	}
}

function onMessage(msg: ServerMessage & { type: string; [k: string]: unknown }): void {
	switch (msg.type) {
		case "ready": {
			setModel(msg.model);
			// the initial thinking mode (friendly label or null) so the hint
			// line shows it from the first frame, same as the model name
			setThinkMode(msg.think_mode ?? null);
			// ready is the session boundary: zero the token readout and re-seed
			// it from the payload when the server resurrects a session's spend
			// (a /reload respawn resumes, so the cumulative count is right from
			// the first frame; a fresh connect carries none → shows nothing).
			if (msg.input_tokens || msg.output_tokens) {
				hint.setTokens(msg.input_tokens ?? 0, msg.output_tokens ?? 0);
			} else {
				hint.clearTokens();
			}
			// /reload respawns bird serve: the fresh process has no memory of the
			// approval mode, so reset our own to normal. Silently keeping
			// full-auto across a code-reload respawn would be the one accidental-
			// persistence path in this design.
			if (hint.getMode() !== "normal") {
				hint.resetMode();
				addToChat(new Notice("approval mode reset — ⇧⇥ to re-enable", "muted"));
			}
			// kg state belongs in the chat bar, not the transcript — and the
			// "connected · session <id>" line said nothing the chrome doesn't.
			hint.setKg(msg.kg ? (msg.kg_ready ? "ready" : "building") : "off");
			// Merge skill names from the server into the autocomplete dropdown
			// so /<skill-name> appears alongside built-in /commands. The
			// provider's command list is private, so we rebuild the provider
			// with built-ins + skills and swap it onto the editor.
			// Built-ins win on a name clash — the server reserves them the same
			// way, so a skill named "model" never starts a turn and must never
			// leave the UI waiting for a turn_end that isn't coming.
			skillNames = new Set(
				(msg.skills ?? []).map((s) => s.name).filter((n) => !SLASH_COMMANDS.some((c) => c.name === n)),
			);
			if (msg.skills?.length) {
				const skillCmds = msg.skills.map((s) => ({
					name: s.name,
					description: s.description || `[${s.source} skill]`,
				}));
				editor.setAutocompleteProvider(new CombinedAutocompleteProvider([...SLASH_COMMANDS, ...skillCmds], repo));
			}
			break;
		}
		case "state":
			setModel((msg as unknown as { model: string }).model);
			// a /think <mode> (or /continue resume) reports the new mode; keep
			// the hint line in sync. Absent on older servers → leave it alone.
			if (typeof msg.think_mode !== "undefined") setThinkMode(msg.think_mode ?? null);
			break;
		case "harness_event": {
			const { event, data } = msg;
			if (event === "thinking_delta") {
				// a reasoning chunk arrived. If no segment is open (content
				// already streaming, or the first thought of the turn) open a
				// NEW ThinkingTrace block above the answer; if one is open,
				// append to it. Segments, so interleaved reasoning after
				// content is never dropped.
				if (!thinkingTrace) {
					thinkingTrace = new ThinkingTrace();
					addToChat(thinkingTrace);
				}
				thinkingTrace.append((data.text as string) ?? "");
				tui.requestRender();
			} else if (event === "thinking") {
				// the complete reasoning trace (recorder-bound, covers the
				// non-streaming path too): close whatever segment is open.
				closeThinkingSegment();
			} else if (event === "assistant_delta") {
				// the first content delta closes the current reasoning segment
				// (collapses it, does NOT discard — a later thinking_delta
				// opens a fresh one). Done before creating the answer block so
				// the trace stays above it.
				if (thinkingTrace) closeThinkingSegment();
				if (!streamMsg) {
					streamMsg = new AssistantMessage("");
					streamText = "";
					addToChat(streamMsg);
				}
				streamText += (data.text as string) ?? "";
				streamMsg.setText(streamText, true);
				tui.requestRender();
			} else if (event === "assistant") {
				const calls = (data.tool_calls as { name: string; arguments_json: string }[]) ?? [];
				const content = (data.content as string) ?? "";
				if (streamMsg) {
					streamedReply = calls.length === 0;
					// the streamed text is now finalized into a visible
					// AssistantMessage; a `done` turn carries the same text as
					// the done tool's summary, so turn_end must not re-add it
					if (content.trim()) streamedContent = true;
					finalizeStream(content);
				} else if (calls.length && content.trim()) {
					addToChat(new Notice(content.trim()));
				}
				// a gutter while a sub-harness is running, so its tool calls read as
				// nested under the dispatch block rather than as the lead's own
				const gutter = dispatch ? "│ " : "";
				for (const c of calls)
					addToChat(new Notice(`${gutter}› ${shortToolLabel(c.name, c.arguments_json)}`, "accent"));
			} else if (event === "dispatch") {
				// the lead just handed off — frame the sub-session as its own block
				// and light up the header so it is obvious the lead is not driving
				const sub = (data.harness as string) ?? "code";
				dispatch = new DispatchBanner(sub, (data.task as string) ?? "", Boolean(data.seeded));
				header.setActiveHarness(sub);
				addToChat(dispatch);
			} else if (event === "dispatch_status") {
				dispatch?.setStatus((data.message as string) ?? "");
				tui.requestRender();
			} else if (event === "tool_result" && (data.details as { harness?: string })?.harness) {
				const d = data.details as { harness?: string; status?: string; turns?: number; phase?: string };
				const summary = d.phase
					? `architecture ${d.phase}`
					: `${d.status ?? "finished"}${d.turns ? ` · ${d.turns} turns` : ""}`;
				endDispatch(!data.is_error, summary);
			} else if (event === "tool_result" && data.is_error) {
				addToChat(new Notice(`✕ ${data.name} failed`, "danger"));
			} else if (event === "attachment_saved") {
				// the image was copied out of its temp path before anything could
				// reap it; say so, since the model will cite the copy's path
				const kb = Math.max(1, Math.round(Number(data.size ?? 0) / 1024));
				addToChat(new Notice(`📎 saved ${data.path} (${kb} KB)`, "accent"));
			} else if (event === "attachment_failed") {
				addToChat(new Notice(`⚠ could not save attachment: ${data.error}`, "danger"));
			} else if (event === "user_injected") {
				// the running step appended it to the transcript: the message
				// is real now, so the bubble stops being a promise. It has to
				// MOVE — the queue region is not the transcript, so promoting
				// it in place would leave a sent message sitting above the bar
				// forever. Same text, same geometry, so the jump is invisible.
				const it = queue.confirmInjected();
				if (it) {
					const bubble = bubbles.get(it.id);
					if (bubble) {
						queueRemoveBubble(it.id);
						bubble.promote();
						addToChat(bubble);
					}
					tui.requestRender();
				}
			} else if (event === "kg_ready_notice") {
				hint.setKg("ready");
			} else if (event === "wire_retry") {
				// The provider went quiet and the wire adapter is retrying. This is
				// otherwise invisible — it happens between "asked" and "answered", so
				// the spinner just sits there and a stalled provider reads as a hung
				// bird. Gutter it like a tool call so a retry inside a dispatch reads
				// as the sub-session's.
				const gutter = dispatch ? "│ " : "";
				addToChat(
					new Notice(
						`${gutter}⟳ provider did not respond — retry ${data.attempt}/${data.max_attempts} in ${data.delay}s`,
						"accent",
					),
				);
			} else if (event === "bash_rejected") {
				addToChat(new Notice(`✕ bash rejected: ${data.reason}`, "danger"));
			}
			break;
		}
		case "permission_request": {
			const spec: PermissionSpec =
				msg.kind === "bash"
					? { kind: "bash", cmd: msg.cmd }
					: msg.kind === "read_outside_repo"
						? { kind: "read_outside_repo", tool: msg.tool, path: msg.path }
						: { kind: msg.kind, file: msg.file, lines: msg.lines };
			// auto-approve mode: skip the card and approve immediately, like
			// Claude Code's "auto-accept edits" (Shift+Tab). auto_edits covers
			// edit/write/read_outside_repo; full_auto additionally covers bash.
			// Unknown payload kinds ALWAYS show a card regardless of mode, so a
			// newer server's new payload shape can never be mass-approved.
			const mode = hint.getMode();
			if (autoApproves(mode, String(msg.kind))) {
				bridge?.permission(msg.id, true);
				const label =
					msg.kind === "bash"
						? `✓ ⚠ FULL AUTO ran bash: ${msg.cmd}`
						: msg.kind === "read_outside_repo"
							? `✓ auto-approved ${msg.kind} ${msg.path ?? ""}`
							: `✓ auto-approved ${msg.kind}`;
				addToChat(new Notice(label, mode === "full_auto" ? "danger" : "success"));
				break;
			}
			const card = new PermissionCard(spec);
			card.onResolve = (r) => {
				bridge?.permission(msg.id, r === "approved");
				// focus returns to the editor — the spinner is render-only
				// now, never a focus target
				setFocus(editor);
				tui.requestRender();
			};
			addToChat(card);
			setFocus(card);
			break;
		}
		case "turn_end": {
			const { status, summary } = msg;
			// Cumulative session spend from the server (which folds in its own
			// runner plus sub-harness dispatches). Absent only when talking to
			// a server older than these fields — keep the last known count in
			// that case rather than flashing a misleading 0.
			if (typeof msg.input_tokens === "number" || typeof msg.output_tokens === "number") {
				hint.setTokens(msg.input_tokens ?? 0, msg.output_tokens ?? 0);
			}
			finalizeStream(null); // drop the cursor if a stream was cut short
			// close any reasoning segment still open: an interrupted/error turn
			// marks it interrupted (dim, no fake done badge); a normal end just
			// collapses it. The complete "thinking" event usually closed it
			// already, but this is the backstop for the non-streaming path and
			// for a turn that ended mid-thought.
			if (status === "interrupted" || status === "error") {
				finalizeThinkingInterrupted();
			} else {
				closeThinkingSegment();
			}
			// a turn can end with a dispatch still open (interrupt, or the sub-session
			// died before returning a result) — never leave the header claiming a
			// sub-harness is driving when nothing is
			endDispatch(status === "done" || status === "reply", `ended (${status})`);
			if (status === "reply" || status === "done") {
				// skip the closing AssistantMessage when its text was already
				// streamed this turn: a `reply` finalized the reply, and a
				// `done` finalized the summary text the model streamed alongside
				// the done tool call (the done tool's output is that same text).
				// streamedReply covers reply; streamedContent covers done.
				if ((status === "reply" && !streamedReply) || (status === "done" && !streamedContent)) {
					addToChat(new AssistantMessage((status === "done" ? "✓ " : "") + summary));
				}
			} else if (status === "interrupted") {
				addToChat(new Notice("✕ interrupted"));
			} else {
				addToChat(
					new Notice(`⚠ ${status}: ${summary}`, "danger"),
					new Notice("(conversation kept; rephrase or /clear to reset)"),
				);
			}
			endTurn();
			// queue policy per busy-clearing event: done/reply means the
			// server is ready for the next turn — flush the head; an
			// interrupted/error turn means the user just stopped something,
			// so auto-firing the next turn would be a footgun — hold it.
			if (status === "reply" || status === "done") flushQueueHead();
			else holdQueue();
			break;
		}
		case "harness_list": {
			// step one of the /model walk: which harness the pick is for. The
			// answer is "/model <harness>", which brings back model_list.
			const choices = msg.harnesses.map((h) => ({
				value: h.name,
				label: h.name,
				description:
					`${h.model ?? "(unset)"} · think: ${h.think_mode ?? "auto"} · ${h.alias}` +
					(h.shared_with.length ? ` (shared with ${h.shared_with.join(", ")})` : ""),
			}));
			const picker = new ChoicePicker("Select harness", choices, msg.current, "esc cancel");
			picker.onDone = (name) => {
				chat.removeChild(picker);
				setFocus(editor);
				if (name) bridge?.command(`/model ${name}`);
				tui.requestRender();
			};
			chat.addChild(picker);
			chat.addChild(new Spacer(1));
			setFocus(picker);
			tui.requestRender();
			break;
		}
		case "model_list": {
			for (const n of msg.notes ?? []) addToChat(new Notice(n));
			const harness = msg.harness;
			const picker = new ModelPicker(
				msg.models,
				msg.current,
				msg.default,
				harness ? `Select model for ${harness}` : "Select model",
				msg.alias ?? "default",
			);
			picker.onDone = (spec) => {
				chat.removeChild(picker);
				setFocus(editor);
				tui.requestRender();
				if (!spec) return;
				if (!harness) {
					bridge?.command(`/model ${spec}`);
					return;
				}
				// step three: the thinking level. No round-trip — the server
				// sent each model's stored level and the modes its provider
				// accepts. esc keeps the stored level (`keep`, so the server
				// does not carry the running model's level across instead).
				const provider = spec.split(":")[0];
				const modes = msg.think_modes?.[provider] ?? ["off", "low", "medium", "high", "max"];
				const stored = msg.models.find((m) => m.spec === spec)?.think_mode ?? null;
				const think = new ThinkPicker(modes, stored, `Thinking level for ${spec}`, "esc keep current");
				think.onDone = (mode) => {
					chat.removeChild(think);
					setFocus(editor);
					bridge?.command(`/model ${harness} ${spec} ${mode ?? "keep"}`);
					tui.requestRender();
				};
				chat.addChild(think);
				chat.addChild(new Spacer(1));
				setFocus(think);
				tui.requestRender();
			};
			chat.addChild(picker);
			chat.addChild(new Spacer(1));
			setFocus(picker);
			tui.requestRender();
			break;
		}
		case "session_list": {
			const picker = new SessionPicker(msg.sessions, msg.current);
			picker.onDone = (id) => {
				chat.removeChild(picker);
				chat.removeChild(pickerSpacer);
				setFocus(editor);
				if (id) bridge?.command(`/continue ${id}`);
				tui.requestRender();
			};
			const pickerSpacer = new Spacer(1);
			chat.addChild(picker);
			chat.addChild(pickerSpacer);
			setFocus(picker);
			tui.requestRender();
			break;
		}
		case "think_list": {
			const picker = new ThinkPicker(msg.modes, msg.current);
			picker.onDone = (mode) => {
				chat.removeChild(picker);
				setFocus(editor);
				if (mode) bridge?.command(`/think ${mode}`);
				tui.requestRender();
			};
			chat.addChild(picker);
			chat.addChild(new Spacer(1));
			setFocus(picker);
			tui.requestRender();
			break;
		}
		case "mcp_catalog": {
			// bare /mcp (or a refresh / search from the open catalog): serve
			// already fetched everything on its worker thread — connected
			// servers, the registry page, degraded-state markers. The
			// catalog owns the whole flow (browse › detail › confirm ›
			// result); writes go back as install / remove / test and their
			// outcome returns as mcp_result, routed into the same instance.
			if (activeCatalog) {
				activeCatalog.setData(msg);
				tui.requestRender();
				break;
			}
			const catalog = new McpCatalog(msg, (v) => !!process.env[v]);
			activeCatalog = catalog;
			const closeCatalog = () => {
				if (activeCatalog === catalog) activeCatalog = null;
				chat.removeChild(catalog);
				setFocus(editor);
				tui.requestRender();
			};
			catalog.onClose = closeCatalog;
			catalog.onChange = () => tui.requestRender();
			catalog.onInstall = (name) => bridge?.sendMcpInstall(name);
			catalog.onRemove = (name) => bridge?.sendMcpRemove(name);
			catalog.onTest = (name) => bridge?.sendMcpTest(name);
			catalog.onRefresh = (query) => bridge?.sendMcpRefresh(query);
			catalog.onOpen = (url) => {
				try {
					const opener = process.platform === "darwin" ? "open" : process.platform === "win32" ? "start" : "xdg-open";
					spawn(opener, [url], { detached: true, stdio: "ignore" }).unref();
				} catch {
					addToChat(new Notice(`open ${url}`, "accent"));
				}
			};
			chat.addChild(catalog);
			chat.addChild(new Spacer(1));
			// through the wrapper (not tui.setFocus directly): overlayUp must
			// be true while the catalog is up, or endTurn() steals focus back
			// to the editor on the next turn_end — leaving the catalog
			// rendered but key-dead (arrows land in the chat bar)
			setFocus(catalog);
			tui.requestRender();
			break;
		}
		case "command_output":
			if (msg.text) addToChat(new Notice(msg.text));
			break;
		case "mcp_result": {
			if (activeCatalog) {
				activeCatalog.setResult(msg);
				tui.requestRender();
				break;
			}
			if (msg.removed !== undefined) {
				addToChat(
					msg.ok
						? new Notice(`✓ removed ${msg.name}`, "accent")
						: new Notice(`⚠ could not remove ${msg.name}: ${msg.why ?? "unknown error"}`, "danger"),
				);
				break;
			}
			if (msg.ok) {
				const n = msg.tools?.length ?? 0;
				addToChat(new Notice(`✓ ${msg.name} installed · connected · ${n} tool${n === 1 ? "" : "s"}`, "accent"));
			} else {
				const lines = [`⚠ ${msg.name}: ${msg.why ?? "install failed"}`];
				if (msg.installed) lines.push("  written to mcp.json but the connection test failed");
				for (const f of msg.fix ?? []) lines.push(`  ${f}`);
				for (const l of (msg.log ?? []).slice(-5)) lines.push(`  │ ${l}`);
				addToChat(new Notice(lines.join("\n"), "danger"));
			}
			break;
		}
		case "setup_start":
			setupBusy = true;
			// first launch: serve runs the walkthrough before it is ready; the
			// prompts below arrive next, then setup_end and finally ready
			addToChat(new Notice("first run — setting up", "accent"));
			break;
		case "setup_end":
			busy = false;
			setupBusy = false;
			setFocus(editor);
			// setup is a turn-like busy: flush the head like a done turn
			flushQueueHead();
			break;
		case "prompt_request": {
			const id = Number(msg.id);
			const answer = (value: string | null) => {
				bridge?.prompt(id, value);
				setFocus(editor);
				tui.requestRender();
			};
			if (Array.isArray(msg.choices)) {
				const picker = new ChoicePicker(String(msg.prompt), msg.choices, msg.current ?? null);
				picker.onDone = (v) => {
					chat.removeChild(picker);
					answer(v);
				};
				chat.addChild(picker);
				setFocus(picker);
			} else {
				const box = new PromptInput(String(msg.prompt), Boolean(msg.secret), String(msg.default ?? ""));
				box.onDone = (v) => {
					chat.removeChild(box);
					// keep the question in scrollback; never the secret itself
					addToChat(new Notice(`${msg.prompt}: ${!v ? "skipped" : msg.secret ? "••••" : v}`));
					answer(v);
				};
				chat.addChild(box);
				setFocus(box);
			}
			tui.requestRender();
			break;
		}
		case "input_pending":
			// ack only — the bubble went up when we sent it. Nothing to do.
			break;
		case "input_unsent":
			// the turn died before the loop reached these. They are still on
			// screen as dim bubbles; un-mark them as in-flight and hold, so an
			// explicit ⏎ decides whether they go out at all.
			queue.reclaim();
			break;
		case "reload": {
			// serve asked us to respawn it fresh from disk, resuming this
			// session's transcript so code/skill/tool changes take effect
			// without a new terminal session.
			const rid = msg.run_id;
			addToChat(new Notice("↻ reloading bird — respawning serve with latest code/skills…", "accent"));
			busy = false;
			hideThinking();
			// reload respawns the server process: nothing should auto-send
			// into a fresh serve — hold the queue (bubbles stay, no flush)
			holdQueue();
			bridge?.restart(rid);
			break;
		}
		case "error":
			addToChat(new Notice(`⚠ ${msg.message}`, "danger"));
			break;
		case "bye":
			shutdown(0);
			break;
	}
}

function shutdown(code: number): void {
	bridge?.stop();
	tui.stop();
	process.exit(code);
}

if (!DEMO) {
	bridge = new Bridge({
		repo,
		model: MODEL_ARG,
		noKg: NO_KG,
		harness: HARNESS_ARG,
		fromArch: FROM_ARCH_ARG,
		onMessage,
		onStderr: (line) => addToChat(new Notice(line, "danger")),
		onExit: (code) => {
			if (code !== 0) {
				hideThinking();
				addToChat(new Notice(`bird serve exited (code ${code}) — is the venv installed and ollama running?`, "danger"));
				busy = false;
				// the bridge died: hold the queue so nothing auto-sends into
				// a dead process — the user sees the held state and can retry
				holdQueue();
				tui.requestRender();
			} else {
				shutdown(0);
			}
		},
	});
}

/* ---------- input ---------- */

/** Ask the server to cancel the in-flight turn — the single interrupt path,
 *  shared by thinking.onAbort and the global Esc/Ctrl+C listener below.
 *  Sets interruptRequested so a second Ctrl+C exits instead (Claude Code
 *  behavior); endTurn()/sendTurn() clear it. */
function requestInterrupt(): void {
	if (DEMO) return; // demo handles its own abort
	interruptRequested = true;
	bridge?.interrupt();
	addToChat(new Notice("interrupt requested — cancelling the in-flight request"));
}

thinking.onAbort = requestInterrupt;

editor.onSubmit = (text) => {
	// An edit-save is not a turn: the bar was lifted from a queued item, so
	// ⏎ writes the text back into that item and stops. Without this the save
	// fell through to submit() and the edited words went out as a NEW turn
	// while the original stayed queued — the item was never actually edited.
	if (queue.editingId !== null) {
		queue.saveEdit(text);
		return;
	}
	// THE single submission path: local commands, queueing while busy, the
	// held-queue release and the plain send all funnel through submit().
	// The editor has already cleared itself by the time this runs; an
	// empty-bar Enter (held-queue release) is signalled via emptyBar.
	const wasEmpty = text.trim() === "";
	submit(text, { emptyBar: wasEmpty });
};

setFocus(editor);

/* ---------- branding: banner ---------- */

// The wordmark prints ONCE at session start into scrollback. It is plain
// output, not a Component, so resize and Ctrl-L never re-render it.
// The one place the version and working directory appear.
for (const line of renderBanner(`bird v0.1.0 · ${tildify(repo)}`, !accent.plain)) console.log(line);

tui.addInputListener((data) => {
	if (matchesKey(data, Key.ctrl("c"))) {
		// Claude Code behavior: the FIRST Ctrl+C while a turn is running
		// interrupts it; a second one (or any Ctrl+C while idle or on an
		// overlay) exits the TUI. interruptRequested is cleared by
		// endTurn()/sendTurn(), so "second" resets once the turn is gone.
		if (busy && !interruptRequested && editor.focused) {
			requestInterrupt();
			return { consume: true };
		}
		shutdown(0);
		return { consume: true };
	}
	if (matchesKey(data, Key.escape)) {
		// Esc's other jobs come first: exit a queue edit/selection (the
		// ghost text promises "◌1 selected · ⏎ edit · ⌫ remove · esc").
		// Both are reachable now that ↑/⏎/⌫ are wired below — before that
		// selectedId was permanently null and these branches were dead.
		if (queue.editingId !== null) {
			queue.cancelEdit();
			// the lifted text is still in the bar; abandoning the edit must
			// take it back out, or the next ⏎ submits it as a new turn
			editor.setText("");
			tui.requestRender();
			return { consume: true };
		}
		if (queue.selectedId !== null) {
			queue.clearSelection();
			return { consume: true };
		}
		// Interrupt the running turn — but only when the editor is the
		// focused component: a PermissionCard/picker/catalog/prompt up
		// (cardUp) consumes Esc itself (deny/cancel/close/skip), and an
		// open autocomplete dropdown means "close the dropdown", which the
		// editor's own handleInput does. The Thinking spinner is
		// render-only and never focused, so this global branch is what
		// actually invokes thinking.onAbort's logic.
		if (busy && editor.focused && !editor.isShowingAutocomplete()) {
			requestInterrupt();
			return { consume: true };
		}
		// Last rung of the design's esc chain: idle with text → clear the bar.
		if (!busy && editor.focused && !editor.isShowingAutocomplete() && editor.getText().trim()) {
			editor.setText("");
			tui.requestRender();
			return { consume: true };
		}
		return;
	}
	// Queue keys (↑ select · ↓ step · ⏎ edit · ⌫ remove · esc deselect). The
	// routing decision is pure and lives in queue.ts (routeQueueKey) so it can
	// be smoke-tested; this only maps the raw bytes to a key id and applies the
	// action. Esc is handled above (it has the interrupt chain to fall through
	// to), so it is not routed here.
	const queueKey = matchesKey(data, Key.up)
		? "up"
		: matchesKey(data, Key.down)
			? "down"
			: matchesKey(data, Key.enter)
				? "enter"
				: matchesKey(data, Key.backspace)
					? "backspace"
					: null;
	if (queueKey !== null) {
		const action = routeQueueKey(queueKey, {
			editorFocused: editor.focused,
			autocompleteOpen: editor.isShowingAutocomplete(),
			barEmpty: editor.getText().trim() === "",
			selectedId: queue.selectedId,
			editingId: queue.editingId,
			length: queue.length,
		});
		if (action !== null) {
			applyQueueKey(action);
			return { consume: true };
		}
	}
	// While the autocomplete dropdown is open, Enter should complete the
	// selected item (like Tab) instead of submitting. The Editor's own
	// handleInput treats Enter on a slash-command completion as "apply then
	// fall through to submit", which submits the partial `/re` before the
	// user can continue typing their prompt. Intercept Enter here — before
	// the editor sees it — and route it through the Tab-completion path,
	// which applies the completion and returns without submitting. The user
	// then types their prompt and presses Enter again (with the dropdown
	// closed) to submit.
	if (
		editor.focused &&
		editor.isShowingAutocomplete() &&
		matchesKey(data, Key.enter)
	) {
		editor.handleInput("\t");
		tui.requestRender();
		return { consume: true };
	}
	if (matchesKey(data, Key.shift("tab"))) {
		// Shift+Tab cycles the approval mode (Claude Code / pi style): normal →
		// auto_edits → full_auto → normal. Works in any focus state because it's
		// a global listener that runs before the focused component. Returns
		// consume:true so the editor never sees the chord.
		const mode = hint.cycleMode();
		const notice =
			mode === "full_auto"
				? "⇧⇥ ⚠ FULL AUTO — edits AND bash run without asking"
				: mode === "auto_edits"
					? "⇧⇥ auto-accept edits ON — edits applied without asking"
					: "⇧⇥ ask everything — each edit and bash asks";
		addToChat(new Notice(notice, mode === "full_auto" ? "danger" : mode === "auto_edits" ? "accent" : "muted"));
		tui.requestRender();
		return { consume: true };
	}
});

// pi-tui renders from a timer callback, so an exception thrown by any
// component's render() unwinds straight out of the event loop and takes the
// process — and the session — with it. A bad frame is not worth losing a
// conversation over: surface it in the transcript and keep running. Kept last
// so it can't mask a startup failure, which should still exit loudly.
process.on("uncaughtException", (err: Error) => {
	try {
		addToChat(new Notice(`⚠ internal error: ${err?.message ?? err}`, "danger"));
		tui.requestRender();
	} catch {
		// the TUI itself is wedged — fall back to dying visibly rather than
		// spinning in a broken render loop
		tui.stop();
		console.error(err);
		process.exit(1);
	}
});

tui.start();
