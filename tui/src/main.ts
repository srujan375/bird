import {
	CombinedAutocompleteProvider,
	Container,
	Editor,
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
	DispatchBanner,
	HeaderBar,
	HintLine,
	ModelPicker,
	Notice,
	PermissionCard,
	type PermissionSpec,
	SessionPicker,
	Thinking,
	UserMessage,
} from "./components.ts";
import { runDemoTurn } from "./demo.ts";
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

// Mirrors mha's REPL commands (src/mha/repl.py). Skill names from the server
// are merged in at runtime when the "ready" message arrives (below).
const SLASH_COMMANDS = [
	{ name: "help", description: "list commands" },
	{ name: "model", description: "pick from available models (sets default)" },
	{ name: "kg", description: "knowledge graph status / build / query" },
	{ name: "tools", description: "list available tools" },
	{ name: "skills", description: "list available skills" },
	{ name: "compact", description: "compact conversation history" },
	{ name: "clear", description: "start a fresh conversation" },
	{ name: "reload", description: "respawn mha with latest code/skills (resume this session)" },
	{ name: "session", description: "show session info" },
	{ name: "sessions", description: "list all past sessions with names" },
	{ name: "continue", description: "resume a previous session" },
	{ name: "quit", description: "exit mha" },
];

/* ---------- UI scaffold ---------- */

const terminal = new ProcessTerminal();
const tui = new TUI(terminal);

const header = new HeaderBar(tildify(repo), DEMO ? "demo" : "connecting…");
const hint = new HintLine(DEMO ? "demo" : "connecting…");
const chat = new Container();
const editor = new Editor(tui, {
	borderColor: (s) => t.dim(s),
	selectList: {
		selectedPrefix: (s) => t.accentBold(s),
		selectedText: (s) => t.accentBold(s),
		description: (s) => t.muted(s),
		scrollInfo: (s) => t.dim(s),
		noMatch: (s) => t.muted(s),
	},
});
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
	header.setModel(model);
	hint.setModel(model);
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

/* streaming state: assistant text arrives token-by-token via assistant_delta,
   then the "assistant" harness event finalizes it. When the finalized message
   had no tool calls it IS the reply, so turn_end must not re-add it. */
let streamMsg: AssistantMessage | null = null;
let streamText = "";
let streamedReply = false;

function finalizeStream(content: string | null, cursor = false): void {
	if (!streamMsg) return;
	streamMsg.setText((content ?? streamText).trim(), cursor);
	streamMsg = null;
	streamText = "";
	tui.requestRender();
}

function onMessage(msg: ServerMessage & { type: string; [k: string]: unknown }): void {
	switch (msg.type) {
		case "ready": {
			setModel(msg.model);
			const kgNote = msg.kg ? (msg.kg_ready ? "kg ready" : "kg building in background") : "kg off";
			addToChat(new Notice(`connected · session ${msg.run_id} · ${kgNote}`));
			// Merge skill names from the server into the autocomplete dropdown
			// so /<skill-name> appears alongside built-in /commands. The
			// provider's command list is private, so we rebuild the provider
			// with built-ins + skills and swap it onto the editor.
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
			break;
		case "harness_event": {
			const { event, data } = msg;
			if (event === "assistant_delta") {
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
			} else if (event === "kg_ready_notice") {
				addToChat(new Notice("✓ knowledge graph ready — kg_query is live", "success"));
			} else if (event === "bash_rejected") {
				addToChat(new Notice(`✕ bash rejected: ${data.reason}`, "danger"));
			}
			break;
		}
		case "permission_request": {
			const spec: PermissionSpec =
				msg.kind === "bash" ? { kind: "bash", cmd: msg.cmd } : { kind: msg.kind, file: msg.file, lines: msg.lines };
			// auto-approve mode: skip the card and approve immediately, like
			// Claude Code's "auto-accept edits" (Shift+Tab). Scoped to edit/write
			// on purpose — bash is gated too now, and it can write anywhere, so
			// auto-accepting *edits* must not silently auto-accept a shell that
			// does the same thing unobserved.
			if (hint.getAutoApprove() && (msg.kind === "edit" || msg.kind === "write")) {
				bridge?.permission(msg.id, true);
				addToChat(new Notice(`✓ auto-approved ${msg.kind}`, "success"));
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
			finalizeStream(null); // drop the cursor if a stream was cut short
			// a turn can end with a dispatch still open (interrupt, or the sub-session
			// died before returning a result) — never leave the header claiming a
			// sub-harness is driving when nothing is
			endDispatch(status === "done" || status === "reply", `ended (${status})`);
			if (status === "reply" || status === "done") {
				if (status !== "reply" || !streamedReply) {
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
		case "command_output":
			if (msg.text) addToChat(new Notice(msg.text));
			break;
		case "reload": {
			// serve asked us to respawn it fresh from disk, resuming this
			// session's transcript so code/skill/tool changes take effect
			// without a new terminal session.
			const rid = msg.run_id;
			addToChat(new Notice("↻ reloading mha — respawning serve with latest code/skills…", "accent"));
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
				addToChat(new Notice(`mha serve exited (code ${code}) — is the venv installed and ollama running?`, "danger"));
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
	addToChat(new Notice("interrupt requested — takes effect at the next harness step"));
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
		bridge?.command(trimmed);
		return;
	}

	addToChat(new UserMessage(trimmed));
	busy = true;
	streamedReply = false;
	showThinking();
	tui.setFocus(thinking);
	if (DEMO) {
		runDemoTurn({ tui, chat, thinking: { hide: hideThinking }, addToChat, endTurn });
	} else {
		bridge?.userInput(trimmed);
	}
};

tui.setFocus(editor);

tui.addInputListener((data) => {
	if (matchesKey(data, Key.ctrl("c"))) shutdown(0);
	// Shift+Tab toggles auto-approve mode (Claude Code / pi style). Works in any
	// focus state because it's a global listener that runs before the focused
	// component. Returns consume:true so the editor never sees the chord.
	if (matchesKey(data, Key.shift("tab"))) {
		hint.setAutoApprove(!hint.getAutoApprove());
		addToChat(new Notice(hint.getAutoApprove() ? "⇧⇥ auto-approve ON — edits applied without asking" : "⇧⇥ auto-approve OFF — each edit asks", hint.getAutoApprove() ? "accent" : "muted"));
		tui.requestRender();
		return { consume: true };
	}
});

tui.start();
