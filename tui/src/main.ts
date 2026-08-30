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
	{ name: "model", description: "pick from available models (sets default)" },
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
tui.addChild(editor);
tui.addChild(hint);

header.setBaseHarness(HARNESS_ARG ?? "code");

let busy = false;
const thinking = new Thinking(tui);
let thinkingShown = false;

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
	hideThinking();
	tui.setFocus(editor);
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
			} else if (event === "kg_ready_notice") {
				hint.setKg("ready");
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
				tui.setFocus(thinkingShown ? thinking : editor);
				tui.requestRender();
			};
			addToChat(card);
			tui.setFocus(card);
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
			break;
		}
		case "model_list": {
			for (const n of msg.notes ?? []) addToChat(new Notice(n));
			const picker = new ModelPicker(msg.models, msg.current, msg.default);
			picker.onDone = (spec) => {
				chat.removeChild(picker);
				tui.setFocus(editor);
				if (spec) bridge?.command(`/model ${spec}`);
				tui.requestRender();
			};
			chat.addChild(picker);
			chat.addChild(new Spacer(1));
			tui.setFocus(picker);
			tui.requestRender();
			break;
		}
		case "session_list": {
			const picker = new SessionPicker(msg.sessions, msg.current);
			picker.onDone = (id) => {
				chat.removeChild(picker);
				chat.removeChild(pickerSpacer);
				tui.setFocus(editor);
				if (id) bridge?.command(`/continue ${id}`);
				tui.requestRender();
			};
			const pickerSpacer = new Spacer(1);
			chat.addChild(picker);
			chat.addChild(pickerSpacer);
			tui.setFocus(picker);
			tui.requestRender();
			break;
		}
		case "think_list": {
			const picker = new ThinkPicker(msg.modes, msg.current);
			picker.onDone = (mode) => {
				chat.removeChild(picker);
				tui.setFocus(editor);
				if (mode) bridge?.command(`/think ${mode}`);
				tui.requestRender();
			};
			chat.addChild(picker);
			chat.addChild(new Spacer(1));
			tui.setFocus(picker);
			tui.requestRender();
			break;
		}
		case "command_output":
			if (msg.text) addToChat(new Notice(msg.text));
			break;
		case "setup_start":
			// first launch: serve runs the walkthrough before it is ready; the
			// prompts below arrive next, then setup_end and finally ready
			addToChat(new Notice("first run — setting up", "accent"));
			break;
		case "setup_end":
			busy = false;
			tui.setFocus(editor);
			tui.requestRender();
			break;
		case "prompt_request": {
			const id = Number(msg.id);
			const answer = (value: string | null) => {
				bridge?.prompt(id, value);
				tui.setFocus(editor);
				tui.requestRender();
			};
			if (Array.isArray(msg.choices)) {
				const picker = new ChoicePicker(String(msg.prompt), msg.choices, msg.current ?? null);
				picker.onDone = (v) => {
					chat.removeChild(picker);
					answer(v);
				};
				chat.addChild(picker);
				tui.setFocus(picker);
			} else {
				const box = new PromptInput(String(msg.prompt), Boolean(msg.secret), String(msg.default ?? ""));
				box.onDone = (v) => {
					chat.removeChild(box);
					// keep the question in scrollback; never the secret itself
					addToChat(new Notice(`${msg.prompt}: ${!v ? "skipped" : msg.secret ? "••••" : v}`));
					answer(v);
				};
				chat.addChild(box);
				tui.setFocus(box);
			}
			tui.requestRender();
			break;
		}
		case "reload": {
			// serve asked us to respawn it fresh from disk, resuming this
			// session's transcript so code/skill/tool changes take effect
			// without a new terminal session.
			const rid = msg.run_id;
			addToChat(new Notice("↻ reloading bird — respawning serve with latest code/skills…", "accent"));
			busy = false;
			hideThinking();
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
				tui.requestRender();
			} else {
				shutdown(0);
			}
		},
	});
}

/* ---------- input ---------- */

thinking.onAbort = () => {
	if (DEMO) return; // demo handles its own abort
	bridge?.interrupt();
	addToChat(new Notice("interrupt requested — cancelling the in-flight request"));
};

editor.onSubmit = (text) => {
	const trimmed = text.trim();
	if (!trimmed) return;
	if (busy) return;
	editor.setText("");

	if (trimmed.startsWith("/")) {
		const cmd = trimmed.slice(1).split(/\s+/)[0];
		if (cmd === "quit" || cmd === "exit") {
			if (bridge) bridge.command("/quit");
			else shutdown(0);
			return;
		}
		if (cmd === "clear") chat.clear();
		if (DEMO) {
			addToChat(new Notice(`/${cmd} needs the live harness — run without --demo.`));
			return;
		}
		// a /<skill> starts a real turn server-side: echo it, go busy and arm
		// the per-turn stream guards exactly as typed input does, so the
		// spinner, the interrupt key and the turn_end dedup all work for it
		if (cmd === "setup") {
			busy = true; // released by setup_end
		}
		if (skillNames.has(cmd)) {
			addToChat(new UserMessage(trimmed));
			busy = true;
			streamedReply = false;
			streamedContent = false;
			showThinking();
			tui.setFocus(thinking);
		}
		bridge?.command(trimmed);
		return;
	}

	addToChat(new UserMessage(trimmed));
	busy = true;
	streamedReply = false;
	streamedContent = false;
	showThinking();
	tui.setFocus(thinking);
	if (DEMO) {
		runDemoTurn({ tui, chat, thinking: { hide: hideThinking }, addToChat, endTurn });
	} else {
		bridge?.userInput(trimmed);
	}
};

tui.setFocus(editor);

/* ---------- branding: banner ---------- */

// The wordmark prints ONCE at session start into scrollback. It is plain
// output, not a Component, so resize and Ctrl-L never re-render it.
// The one place the version and working directory appear.
for (const line of renderBanner(`bird v0.1.0 · ${tildify(repo)}`, !accent.plain)) console.log(line);

tui.addInputListener((data) => {
	if (matchesKey(data, Key.ctrl("c"))) shutdown(0);
	// Shift+Tab cycles the approval mode (Claude Code / pi style): normal →
	// auto_edits → full_auto → normal. Works in any focus state because it's a
	// global listener that runs before the focused component. Returns
	// consume:true so the editor never sees the chord.
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
