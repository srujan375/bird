// Picker filter tests — headless, following queue.test.ts's pattern
// (check()/fails[]/exit 1). The bug these guard: pi-tui's SelectList filters
// by PREFIX of the value, and every model spec starts with its provider, so
// typing "qwen" into the model picker found nothing — a locally pulled
// qwen3.8 looked missing from /model.
import { ChoicePicker, filterPickerItems, ModelPicker, SessionPicker } from "./components.ts";

const fails: string[] = [];
function check(cond: boolean, msg: string): void {
	if (!cond) fails.push(msg);
}
const plain = (lines: string[]) => lines.join("\n").replace(/\x1b\[[0-9;]*m/g, "");
const count = (s: string, sub: string) => s.split(sub).length - 1;
function type(p: { handleInput(d: string): void }, text: string): void {
	for (const ch of text) p.handleInput(ch);
}
const BACKSPACE = "\x7f";
const ENTER = "\r";
const ESC = "\x1b";

const models = [
	{ spec: "ollama:ornith", source: "configured", context_window: 262144 },
	{ spec: "openrouter:qwen/qwen3.8-2.4t-a95b", source: "configured", context_window: 900000 },
	{ spec: "ollama:qwen3.8:27b-mlx", source: "ollama", context_window: null },
	{ spec: "ollama:qwen3.8", source: "ollama", context_window: null },
];

// --- the matcher itself ---
const items = models.map((m) => ({ value: m.spec, label: "  " + m.spec }));
check(filterPickerItems(items, "").length === 4, "empty filter keeps every row");
check(filterPickerItems(items, "  ").length === 4, "whitespace-only filter keeps every row");
check(
	filterPickerItems(items, "qwen")
		.map((i) => i.value)
		.join(",") === "openrouter:qwen/qwen3.8-2.4t-a95b,ollama:qwen3.8:27b-mlx,ollama:qwen3.8",
	'"qwen" matches inside the spec, keeping list order',
);
check(filterPickerItems(items, "QWEN3.8:").length === 1, "case-insensitive, punctuation literal");
check(filterPickerItems(items, "ollama:qwen3.8").length === 2, "a provider-prefixed filter still works");
check(filterPickerItems(items, "zzz").length === 0, "gibberish matches nothing");

// --- the model picker, end to end through keystrokes and render ---
let picked: string | null | undefined;
const mp = new ModelPicker(models, "ollama:ornith", "ollama:ornith", "Select model for code", "default");
mp.onDone = (spec) => (picked = spec);

let out = plain(mp.render(120));
check(out.includes("ollama:ornith") && out.includes("ollama:qwen3.8:27b-mlx"), "unfiltered: every row shows");

type(mp, "qwen");
out = plain(mp.render(120));
check(out.includes("filter: qwen"), "the header echoes the filter");
check(!out.includes("ornith"), '"qwen" hides ornith');
check(out.includes("ollama:qwen3.8:27b-mlx"), '"qwen" shows the local 27b-mlx tag');
check(count(out, "ollama:qwen3.8") === 2, '"qwen" shows both local qwen3.8 tags');

type(mp, "zzz");
out = plain(mp.render(120));
check(out.includes('no model matches "qwenzzz"'), "no match says so in the picker's own words");
check(!out.includes("No matching commands"), "the library's command-palette wording is gone");

type(mp, BACKSPACE + BACKSPACE + BACKSPACE + "3.8:27b");
out = plain(mp.render(120));
check(count(out, "ollama:qwen3.8") === 1 && out.includes("27b-mlx"), "backspace re-widens, then narrows to one row");
mp.handleInput(ENTER);
check(picked === "ollama:qwen3.8:27b-mlx", `enter picks the surviving row (got ${String(picked)})`);

picked = undefined;
type(mp, "zzz");
mp.handleInput(ENTER);
check(picked === undefined, "enter on an empty list picks nothing");
mp.handleInput(ESC);
check(picked === null, "esc still cancels");

// --- the session picker: same filter, on id or name ---
const sessions = [
	{ id: "a1b2c3", name: "fix the picker", last_event: "2m ago" },
	{ id: "d4e5f6", name: "kg rebuild", last_event: "" },
];
const sp = new SessionPicker(sessions, "a1b2c3");
type(sp, "rebuild");
out = plain(sp.render(120));
check(out.includes("kg rebuild") && !out.includes("fix the picker"), "sessions filter by name substring");
type(sp, BACKSPACE.repeat(7) + "d4e5");
out = plain(sp.render(120));
check(out.includes("kg rebuild") && !out.includes("fix the picker"), "sessions filter by id substring too");
type(sp, "zzz");
out = plain(sp.render(120));
check(out.includes('no session matches "d4e5zzz"'), "session picker has its own no-match line");

// --- the harness step (ChoicePicker): same filter ---
const cp = new ChoicePicker(
	"Select harness",
	[
		{ value: "code", label: "code", description: "ollama:glm-5.3:cloud" },
		{ value: "arch", label: "arch", description: "ollama:kimi-k3:cloud" },
	],
	"code",
);
type(cp, "rch");
out = plain(cp.render(120));
check(out.includes("arch") && !out.includes("glm-5.3"), "harness picker matches inside the name");
type(cp, "q");
out = plain(cp.render(120));
check(out.includes('no choice matches "rchq"'), "harness picker has its own no-match line");

if (fails.length) {
	console.error("\nPICKERS FAIL:");
	for (const f of fails) console.error("  - " + f);
	process.exit(1);
}
console.log("PICKERS OK");
