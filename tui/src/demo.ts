// Scripted demo turn matching the design canvas moments — kept for showing
// the UI without a venv/ollama (`npm start -- --demo`).
import type { Container, TUI } from "@mariozechner/pi-tui";
import { AssistantMessage, PermissionCard, type PermissionSpec } from "./components.ts";

const REPLY_TEXT =
	"Retried both calls. One succeeded, the other needs a longer timeout. I'll patch the config and confirm before running anything.";

const PERMIT_EDIT: PermissionSpec = {
	kind: "edit",
	file: "harness/config/timeouts.json",
	lines: [
		{ kind: "ctx", text: '  "network_fetch": {' },
		{ kind: "del", text: '-   "timeout_ms": 4000,' },
		{ kind: "add", text: '+   "timeout_ms": 12000,' },
		{ kind: "ctx", text: "  }," },
	],
};

interface DemoDeps {
	tui: TUI;
	chat: Container;
	thinking: { hide: () => void };
	addToChat: (...components: Parameters<Container["addChild"]>[0][]) => void;
	endTurn: () => void;
}

export function runDemoTurn({ tui, chat, thinking, addToChat, endTurn }: DemoDeps): void {
	setTimeout(() => {
		thinking.hide();
		const msg = new AssistantMessage("");
		chat.addChild(msg);
		let shown = 0;
		const timer = setInterval(() => {
			shown += 2 + Math.floor(Math.random() * 3);
			if (shown >= REPLY_TEXT.length) {
				clearInterval(timer);
				msg.setText(REPLY_TEXT, false);
				const card = new PermissionCard(PERMIT_EDIT);
				card.onResolve = () => endTurn();
				addToChat(card);
				tui.setFocus(card);
			} else {
				msg.setText(REPLY_TEXT.slice(0, shown), true);
			}
			tui.requestRender();
		}, 28);
	}, 1500);
}
