// Branding tests — golden/byte-exact assertions on emitted bytes, following
// the repo's smoke-test pattern (tsx script, exit 1 on failure).
import {
	BANNER_ART,
	bannerEscapeBytes,
	accentCode,
	detectBackgroundFromEnv,
	detectDepth,
	leftTruncate,
	renderBanner,
	renderChatBarModelName,
	renderIndicator,
	resolveAccent,
	type AccentTheme,
} from "./branding.ts";
import { GhostEditor, HeaderBar, HintLine, harnessStates } from "./components.ts";
import { ProcessTerminal, TUI } from "@mariozechner/pi-tui";

const fails: string[] = [];
function check(cond: boolean, msg: string): void {
	if (!cond) fails.push(msg);
}

/* ---------- banner: byte-exact fixture ---------- */

check(BANNER_ART.length === 6, "banner must be 6 rows");
for (const row of BANNER_ART) check([...row].length === 27, `row not 27 cols: ${JSON.stringify(row)}`);
check(BANNER_ART[0].endsWith(" "), "row 1 keeps its trailing space");
check(BANNER_ART[5].endsWith(" "), "row 6 keeps its trailing space");

const theme = resolveAccent({ background: "dark", env: { COLORTERM: "truecolor" }, isTTY: true });
const lines = renderBanner("meta", true);

// lockup shape: blank, dim meta (gutter-aligned), blank, 6 art lines, blank.
// Version + working directory caption the wordmark from ABOVE it.
const ART0 = 3; // index of the first art row
check(lines.length === 10, `lockup line count ${lines.length}`);
check(lines[0] === "", "blank line above");
check(lines[1] === "  \x1b[2mmeta\x1b[0m", "dim meta caption sits above the art, gutter-aligned");
check(lines[2] === "", "blank between caption and art");
check(lines[9] === "", "blank line below art");
for (let i = ART0; i < ART0 + 6; i++) check(lines[i].startsWith("  "), `art line ${i} has 2-space gutter`);

// every coloured art line ends with reset
for (let i = ART0; i < ART0 + 6; i++) check(lines[i].endsWith("\x1b[0m"), `art line ${i} ends with reset`);

// two-layer colouring: blocks get SGR 39, bevels SGR 2
check(lines[ART0].includes("\x1b[39m█"), "blocks use default-fg SGR 39");
check(lines[ART0].includes("\x1b[2m╗"), "bevels use dim SGR 2");
check(lines[ART0 + 5].includes("\x1b[2m╚═════╝"), "a bevel run is one escape, not one per char");

// one escape per contiguous run: ≤440 escape bytes total
const esc = bannerEscapeBytes(lines);
check(esc <= 440, `escape bytes ${esc} > 440`);
check(esc >= 60, `escape bytes suspiciously low: ${esc}`);

// plain mode: zero escape bytes anywhere
const plainLines = renderBanner("meta", false);
check(bannerEscapeBytes(plainLines) === 0, "plain banner emits zero escapes");
check(plainLines[1] === "  meta", "plain caption keeps the gutter, drops escapes");
check(plainLines[ART0] === "  " + BANNER_ART[0], "plain art is verbatim");

/* ---------- accent ladder ---------- */

// dark bg per depth
const mk = (env: NodeJS.ProcessEnv, bg: "dark" | "light" | "unknown") =>
	accentCode(resolveAccent({ background: bg, env, isTTY: true }));
check(mk({ COLORTERM: "truecolor" }, "dark") === "38;2;204;122;79", "dark truecolor");
check(mk({ TERM: "xterm-256color" }, "dark") === "38;5;173", "dark 256");
check(mk({ TERM: "xterm" }, "dark") === "38;5;137", "dark 16 uses nearest index");
check(accentCode(resolveAccent({ background: "dark", env: {}, isTTY: false })) === "", "non-TTY → none");

check(mk({ COLORTERM: "truecolor" }, "light") === "38;2;154;79;38", "light truecolor");
check(mk({ TERM: "xterm-256color" }, "light") === "38;5;130", "light 256");
check(mk({ TERM: "xterm-256color" }, "unknown") === "38;5;173", "unknown 256");
check(mk({ COLORTERM: "truecolor" }, "unknown") === "38;2;193;111;66", "unknown truecolor");

// bold variants prepend "1;"
const tDark = resolveAccent({ background: "dark", env: { COLORTERM: "truecolor" }, isTTY: true });
check(tDark.boldSgr("38;5;170") === "\x1b[1;38;5;170m", "bold prepends 1;");
check(tDark.sgr("38;5;170") === "\x1b[38;5;170m", "regular sgr");

// NO_COLOR / non-TTY: glyph becomes *, no escapes
const tPlain = resolveAccent({ background: "dark", env: { NO_COLOR: "1" }, isTTY: true });
check(tPlain.plain && tPlain.glyph === "*", "NO_COLOR → plain, * glyph");
const tNoTty = resolveAccent({ background: "dark", env: {}, isTTY: false });
check(tNoTty.depth === "none" && tNoTty.glyph === "*", "non-TTY → plain");

/* ---------- background detection ---------- */

check(detectBackgroundFromEnv({ COLORFGBG: "15;0" }) === "dark", "COLORFGBG 15;0 → dark");
check(detectBackgroundFromEnv({ COLORFGBG: "0;15" }) === "light", "COLORFGBG 0;15 → light");
check(detectBackgroundFromEnv({}) === null, "no COLORFGBG → null (never guess)");

/* ---------- indicator ---------- */

const strip = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, "");

const ind = (width: number, over?: Partial<Parameters<typeof renderIndicator>[0]>) =>
	renderIndicator(
		{ modelId: "qwen3:32b", ctxUsed: 47000, ctxWindow: 200000, switchHint: "⇧⇥ cycle mode", ...over },
		tDark,
		width,
	);

// full line at generous width
const wide = ind(120);
check(wide.startsWith("    "), "indicator aligned to caret column (4 spaces)");
check(wide.includes("\x1b[1;38;2;204;122;79mqwen3:32b"), "bold accent model id");
check(wide.includes("ctx 47k/200k"), "ctx meta present");
check(wide.includes("⇧⇥ cycle mode"), "switch hint present");
check(wide.includes("\x1b[2m  ·  ⇧⇥ cycle mode"), "meta dims from the separator on");
check(strip(wide).includes("qwen3:32b  ·  ⇧⇥ cycle mode  ·  ctx 47k/200k"), "meta order");

// never wraps: visible width always fits
for (const w of [120, 60, 40, 30, 20, 12]) check(visibleW(ind(w)) <= w, `width ${w}: wraps`);

function visibleW(s: string): number {
	return strip(s).length;
}

// step-down order: hint dropped first, then ctx
check(!ind(40).includes("cycle mode") && ind(40).includes("ctx 47k/200k"), "narrow: hint dropped first");
check(!ind(28).includes("ctx") && !ind(28).includes("cycle mode"), "narrower: ctx dropped too");
check(ind(28).includes("qwen3:32b"), "model id intact after meta drops");

// then left-truncation with ellipsis, family suffix stays visible
const tiny = strip(ind(10));
check(tiny.startsWith("    ◆ …"), "left ellipsis on truncation");
check(tiny.endsWith("32b"), "family tail stays visible");

// plain mode: ◆ → *, no escapes
const pInd = renderIndicator(
	{ modelId: "qwen3:32b", ctxUsed: 47000, ctxWindow: 200000 },
	resolveAccent({ background: "dark", env: { NO_COLOR: "1" }, isTTY: true }),
	80,
);
check(pInd.includes("* qwen3:32b"), "plain glyph is *");
check(!pInd.includes("\x1b"), "plain indicator has zero escapes");

// leftTruncate unit
check(leftTruncate("abcdefghij", 5) === "…ghij", "leftTruncate keeps right end");
check(leftTruncate("abc", 5) === "abc", "leftTruncate no-op when it fits");

/* ---------- chat-bar model-name highlight ---------- */

// byte-exact styled segment per background × depth
check(
	renderChatBarModelName("qwen3:32b", tDark) === "\x1b[1;38;2;204;122;79mqwen3:32b\x1b[0m",
	"chat-bar model name: bold + dark truecolor accent",
);
const tLight = resolveAccent({ background: "light", env: { COLORTERM: "truecolor" }, isTTY: true });
check(
	renderChatBarModelName("m", tLight) === "\x1b[1;38;2;154;79;38mm\x1b[0m",
	"light truecolor variant",
);
const tUnknown = resolveAccent({ background: "unknown", env: { TERM: "xterm-256color" }, isTTY: true });
check(renderChatBarModelName("m", tUnknown) === "\x1b[1;38;5;173mm\x1b[0m", "unknown 256 → index 173");
const t16 = resolveAccent({ background: "light", env: { TERM: "xterm" }, isTTY: true });
check(renderChatBarModelName("m", t16) === "\x1b[1;38;5;94mm\x1b[0m", "light 16-colour ladder");

// NO_COLOR / non-TTY: bare id, zero escapes, bold dropped
check(renderChatBarModelName("qwen3:32b", tPlain) === "qwen3:32b", "NO_COLOR → plain id");
check(!renderChatBarModelName("qwen3:32b", tNoTty).includes("\x1b"), "non-TTY → no escapes");

/* ---------- HintLine integration: only the model name changes ---------- */

const bar = (theme: AccentTheme, model = "qwen3:32b") => {
	const h = new HintLine(model);
	h.setTheme(theme);
	return h.render(120)[0];
};

const styledBar = bar(tDark);
check(styledBar.includes("\x1b[1;38;2;204;122;79mqwen3:32b\x1b[0m"), "chat-bar carries bold+accent model name");
check(styledBar.includes("⇧⇥ to cycle"), "the one remaining hint is unchanged");
check(styledBar.includes("ask everything"), "mode segment unchanged");
// the ONLY escape sequences are the model-name pair — rest of bar untouched
const otherEscs = styledBar.replace("\x1b[1;38;2;204;122;79mqwen3:32b\x1b[0m", "");
check(!otherEscs.includes("\x1b"), "rest of the chat-bar has no new escapes");

// plain theme: whole bar escape-free
check(!bar(tPlain).includes("\x1b"), "plain theme: chat-bar emits zero escapes");

/* ---------- the model name appears exactly ONCE in the chrome ---------- */

// The header bar used to carry a `MODEL` badge and the indicator line under
// the editor a third copy; the name now lives only in the chat bar.
const head = new HeaderBar();
head.setBaseHarness("code");
const headLine = strip(head.render(120).join("\n"));
check(!/qwen3/i.test(headLine), "header bar carries no model name");
check(!headLine.includes("/"), "header bar carries no repo path — it captions the banner now");
check(headLine.includes("CODE"), "header bar still shows the harness");

/* ---------- harness strip: all three listed, active highlighted ---------- */

// every harness is always on the strip, whichever one you launched
for (const base of ["code", "arch", "lead"]) {
	const names = harnessStates(base, null).map((h) => h.name);
	check(names.join(",") === "code,arch,lead", `base ${base}: all three listed in order`);
	const lit = harnessStates(base, null).filter((h) => h.state === "active");
	check(lit.length === 1 && lit[0].name === base, `base ${base}: exactly the base is lit`);
}
const stripLine = strip(head.render(120).join("\n"));
for (const label of ["CODE", "ARCHITECT", "LEAD"])
	check(stripLine.includes(label), `header bar lists ${label}`);

// lead dispatches code: code executes (lit), lead is the waiting dispatcher
const dispatched = harnessStates("lead", "code");
check(dispatched.find((h) => h.name === "code")?.state === "active", "dispatched sub-harness is active");
check(dispatched.find((h) => h.name === "lead")?.state === "dispatcher", "lead stays half-lit as dispatcher");
check(dispatched.find((h) => h.name === "arch")?.state === "idle", "untouched harness is idle");
check(dispatched.filter((h) => h.state === "active").length === 1, "exactly one harness is ever lit");

// an unknown harness still gets a slot rather than vanishing
const unknown = harnessStates("lead", "reviewer");
check(unknown.some((h) => h.name === "reviewer" && h.state === "active"), "unknown harness still shown");

// narrow terminal: the strip never overflows its width
const narrow = new HeaderBar();
narrow.setBaseHarness("lead");
narrow.setActiveHarness("code");
for (const w of [120, 60, 30, 16, 10]) {
	const row = strip(narrow.render(w)[0]);
	check(row.length <= w, `header width ${w}: overflows (${row.length})`);
	check(row.includes("CODE"), `header width ${w}: the running harness survives`);
}

const barOnce = strip(bar(tDark));
check(barOnce.split("qwen3:32b").length - 1 === 1, "chat bar names the model exactly once");

// the model name is the LAST segment to go as the terminal narrows — it used
// to vanish wholesale the moment the full line stopped fitting
const loaded = (w: number) => {
	const h = new HintLine("qwen3:32b");
	h.setTheme(tDark);
	h.setTokens(12400, 3100);
	h.setThinkMode("high");
	h.setKg("ready");
	return strip(h.render(w)[0]);
};
for (const w of [200, 140, 120, 110, 100, 80, 60, 40, 20]) {
	check(loaded(w).includes("qwen3:32b"), `width ${w}: model name survives`);
	check(loaded(w).length <= w, `width ${w}: hint line overflows (${loaded(w).length})`);
}
// segments shed in importance order as width drops
check(loaded(200).includes("↑12.4k") && loaded(200).includes("kg:ready"), "wide: everything shown");
const narrowest = [200, 140, 120, 110, 100, 90, 80, 70, 60].find((w) => !loaded(w).includes("↑12.4k"));
check(narrowest !== undefined, "tokens drop at some width");
check(loaded(narrowest as number).includes("ask everything"), "mode outlives tokens");
check(loaded(narrowest as number).includes("qwen3:32b"), "model outlives tokens");

/* ---------- chat bar ghost text ---------- */

const ghostText = "/ for commands";
const ed = new GhostEditor(
	new TUI(new ProcessTerminal()),
	{ borderColor: (x: string) => x, selectList: {} as never },
	ghostText,
);

const rows = (w: number) => ed.render(w);
const vis = (r: string) => strip(r).length;

// empty chat bar shows the placeholder...
ed.setText("");
check(strip(rows(60)[1]).includes(ghostText), "empty chat bar shows the ghost text");
check(rows(60)[1].includes("\x1b[7m"), "the cursor block survives the overpaint");

// ...and it vanishes the instant there is content, so it can never be mistaken
// for text or end up submitted
ed.setText("hello");
check(!strip(rows(60)[1]).includes(ghostText), "ghost text gone once there is text");
check(strip(rows(60)[1]).startsWith("hello"), "typed text is untouched");
check(ed.getText() === "hello", "ghost text is not part of the value");
ed.setText("");
check(ed.getText() === "", "empty editor stays empty despite the ghost");

// never overflows, at any width; below its own length it simply drops out
// rather than corrupting the frame
for (const w of [80, 60, 40, 32, 24, 12]) {
	const r = rows(w);
	check(r.length === 3, `ghost width ${w}: row count unchanged (${r.length})`);
	for (const line of r) check(vis(line) <= w, `ghost width ${w}: row overflows (${vis(line)})`);
}
// shown wherever it fits; below that it drops out whole rather than
// rendering a half-sentence
const fits = ghostText.length + 2;
check(strip(rows(fits)[1]).includes(ghostText), `ghost shown at its minimum width (${fits})`);
check(!strip(rows(fits - 1)[1]).includes("commands"), "ghost dropped, not truncated, when too narrow");

/* ---------- the hint line carries no keybinding instructions ---------- */

// ⏎ send / / commands / ⇧⏎ newline all moved out: the ghost text covers "/",
// and send/newline are conventional enough not to need saying. The approval
// chord stays — nothing else in the UI hints that it exists.
const hintText = strip(bar(tDark));
for (const gone of ["⏎ send", "/ commands", "newline"])
	check(!hintText.includes(gone), `hint line no longer instructs "${gone}"`);
check(hintText.includes("⇧⇥ to cycle"), "the approval-mode chord is the one hint kept");

if (fails.length) {
	console.error("\nBRANDING FAIL:");
	for (const f of fails) console.error("  - " + f);
	process.exit(1);
}
console.log(`BRANDING OK (${esc} escape bytes in banner)`);
