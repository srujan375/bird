// Scratch harness: render the chrome the way a session shows it, so the
// model name and repo path can be eyeballed for duplication.
import { HeaderBar, HintLine } from "./components.ts";
import { renderBanner, resolveAccent } from "./branding.ts";

const theme = resolveAccent({ background: "dark", env: { COLORTERM: "truecolor" }, isTTY: true });
const W = 110;

for (const line of renderBanner("bird v0.1.0 · ~/Workspace/Personal/bird", true)) console.log(line);

for (const [base, active] of [["code", null], ["lead", null], ["lead", "arch"]] as const) {
	const head = new HeaderBar();
	head.setBaseHarness(base);
	head.setActiveHarness(active);
	console.log(`  -- base=${base} active=${active ?? "-"} --`);
	for (const l of head.render(W)) console.log(l);
}

console.log("  ╭" + "─".repeat(W - 4) + "╮");
console.log("  │ > " + " ".repeat(W - 9) + "│");
console.log("  ╰" + "─".repeat(W - 4) + "╯");

const hint = new HintLine("qwen3:32b");
hint.setTheme(theme);
hint.setTokens(12400, 3100);
hint.setThinkMode("high");
hint.setKg("ready");
for (const l of hint.render(W)) console.log(l);
