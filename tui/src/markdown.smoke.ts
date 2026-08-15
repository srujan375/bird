// Manual smoke check: render a kitchen-sink markdown sample at terminal
// width with chalk forced to plain output, so spacing is eyeballable/testable
// without a tty. Prints the rendered lines with a leading "|" marker.
import chalk from "chalk";
import { renderInline, renderMarkdown } from "./markdown.ts";
import { visibleWidth } from "@mariozechner/pi-tui";

chalk.level = 0; // chalk calls become identity; every theme function emits plain text

const W = 40;
const sample = `
# Title

First paragraph line one
with a continuation.


Second paragraph after **multiple** blanks.

- item one
- item two
  with a longer continuation line that will surely wrap at forty columns wide
- item three

---

before table text

| a | bbb |
| - | --- |
| 1 | 222 |

> a blockquote line
> with a second line

\`\`\`ts
const x = 1;
\`\`\`

last line

1. first
2. second
`;

const out = renderMarkdown(sample, W);

// eyeball: print with explicit line boundaries
for (const l of out) console.log("|" + l + "|" + (l === "" ? "<blank>" : ""));

// assertions
const bad: string[] = [];
out.forEach((l, idx) => {
	if (visibleWidth(l) > W) bad.push(`line ${idx} wider than ${W}: ${JSON.stringify(l)}`);
});
if (out[0] === "") bad.push("leading blank line");
if (out[out.length - 1] === "") bad.push("trailing blank line");
for (let i = 1; i < out.length; i++) {
	if (out[i] === "" && out[i - 1] === "") bad.push(`consecutive blanks at ${i - 1}/${i}`);
}

// consecutive same-kind list items stay tight; para between para and list
const paraPara = renderMarkdown("para A\n\npara B", W);
if (!(paraPara.length === 3 && paraPara[1] === "")) bad.push("para/para spacing: " + JSON.stringify(paraPara));

const lists = renderMarkdown("- a\n- b\n\ntext\n\n- c\n- d\n\n1. one\n2. two", W);
const items = lists.map((l) => l.trim());
if (!(items[0] === "• a" && items[1] === "• b")) bad.push("ul group not tight");
if (items[2] !== "" || items[3] !== "text") bad.push("missing blank before text after list");
if (items[4] !== "" || items[5] !== "• c" || items[6] !== "• d") bad.push("missing blank before next ul group");

// header spacing
const hdr = renderMarkdown("intro\n\n## H\n\nafter", W);
if (!(hdr.length === 5 && hdr[1] === "" && hdr[2] === "H" && hdr[3] === "")) bad.push("header spacing: " + JSON.stringify(hdr));
// no leading blank even when the first output would be a block preceded by "before"
const first = renderMarkdown("## H", W);
if (first[0] !== "H") bad.push("leading cell not header: " + JSON.stringify(first));

// blockquote composed of multiple paragraphs inside gets internal spacing
const bq = renderMarkdown("> p1\n>\n> p2", W);
if (!(bq.length === 3 && bq[1].trim() === "│")) bad.push("blockquote internal spacing: " + JSON.stringify(bq));

// empty input / whitespace-only inputs
if (renderMarkdown("", W).length !== 0) bad.push("empty input not empty output");
if (renderMarkdown("\n\n\n", W).length !== 0) bad.push("blank-only input not empty output");
if (renderMarkdown("   ", W).length !== 0) bad.push("space-only input not empty output");

// inline still single-line
const inl = renderInline("a **b** `c` [d](e) f");
if (inl.includes("\n")) bad.push("inline rendered newline");

// focus rule: a fenced block starting at line 0 leaves no leading blank;
// a code block followed by nothing leaves no trailing blank
const codeOnly = renderMarkdown("```js\nx\n```", W);
if (codeOnly[0] === "" || codeOnly[codeOnly.length - 1] === "") bad.push("code-only leading/trailing blank");

if (bad.length) {
	console.error("\nSMOKE FAIL:");
	for (const b of bad) console.error("  - " + b);
	process.exit(1);
}
console.log("\nSMOKE OK");
