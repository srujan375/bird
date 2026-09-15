// Renders every McpCatalog view with the prototype's data (mcp-catalog-tui.html)
// so the port can be eyeballed against the design: `npx tsx src/mcp-catalog.smoke.ts`
import chalk from "chalk";
import { McpCatalog, wrapCommand, humanCacheAge } from "./components.ts";

// piped output makes chalk drop to level 0 (no escapes at all), which would
// defeat the panel-background assertion below — force truecolor
chalk.level = 3;

const entries = [
	{ name: "@modelcontextprotocol/server-filesystem", description: "Read, write and search files under allowed directories", version: "0.6.2", installable: true, env: [], repo: "github.com/modelcontextprotocol/servers", command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/srujan/Workspace"] },
	{ name: "@modelcontextprotocol/server-postgres", description: "Read-only SQL access and schema inspection for PostgreSQL", version: "0.6.2", installable: true, env: ["POSTGRES_URL"], repo: "github.com/modelcontextprotocol/servers", command: "npx", args: ["-y", "@modelcontextprotocol/server-postgres", "$POSTGRES_URL"] },
	{ name: "@modelcontextprotocol/server-github", description: "Issues, pull requests, file contents and search across GitHub repositories", version: "2025.4.8", installable: true, env: ["GITHUB_PERSONAL_ACCESS_TOKEN"], repo: "github.com/github/github-mcp-server", command: "npx", args: ["-y", "@modelcontextprotocol/server-github"] },
	{ name: "@sentry/mcp-server", description: "Query issues, events and release health from Sentry", version: "0.12.0", installable: true, env: ["SENTRY_AUTH_TOKEN", "SENTRY_ORG"], repo: "github.com/getsentry/sentry-mcp", command: "npx", args: ["-y", "@sentry/mcp-server@0.12.0", "--access-token=$SENTRY_AUTH_TOKEN", "--organization-slug=$SENTRY_ORG", "--host=sentry.io", "--sentry-dsn=https://examplePublicKey@o0.ingest.sentry.io/0"] },
	{ name: "@playwright/mcp", description: "Drive a browser: navigate, click, fill forms, read accessibility snapshots", version: "0.0.32", installable: true, env: [], repo: "github.com/microsoft/playwright-mcp", command: "npx", args: ["@playwright/mcp@latest", "--headless"] },
	{ name: "mcp/grafana", description: "Dashboards, datasources and alert rules from a Grafana instance", version: "0.4.0", installable: false, reason: "docker — unsupported", repo: "github.com/grafana/mcp-grafana", command: "docker", args: ["run", "--rm", "-i", "mcp/grafana"] },
	{ name: "linear-remote", description: "Issues, projects and cycles from Linear", version: "1.2.0", installable: false, reason: "remote — unsupported", repo: "linear.app/docs/mcp", url: "https://mcp.linear.app/sse" },
];
const connected = [
	{ name: "@modelcontextprotocol/server-filesystem", source: "project", connected: true, tools: 12 },
	{ name: "@modelcontextprotocol/server-postgres", source: "project", connected: false, tools: 0, error: "POSTGRES_URL is not set" },
];
const envSet = (v: string) => ["GITHUB_PERSONAL_ACCESS_TOKEN", "SENTRY_AUTH_TOKEN", "SENTRY_ORG"].includes(v);
const W = 100;
const show = (title: string, c: McpCatalog) => {
	console.log(`\n══════ ${title} ══════`);
	for (const l of c.render(W)) console.log(l);
};
const data = { query: "", connected, entries, total: 1204, cache_age: null, registry_error: null };

const c = new McpCatalog(data, envSet);
show("browse", c);
for (const ch of "sentry") c.handleInput(ch);
show("search 'sentry'", c);
c.handleInput("\x1b"); // esc clears query
c.handleInput("\x1b[B"); c.handleInput("\x1b[B"); c.handleInput("\x1b[B"); // down x3 -> sentry
c.handleInput("\r");
show("detail (sentry)", c);
c.handleInput("i");
show("confirm (not yet armed)", c);
await new Promise((r) => setTimeout(r, 500));
show("confirm (armed)", c);
let installed = "";
c.onInstall = (n) => (installed = n);
c.handleInput("y");
show("result (pending)", c);
c.setResult({ name: installed, ok: true, installed: true, tools: ["find_issues", "get_issue_details", "search_events", "a", "b"] });
show("result (ok)", c);
c.handleInput("\r"); // back to browse, cursor 0 = filesystem
c.handleInput("\x1b[B"); // cursor 1 = postgres (connected, failed)
let tested = "";
c.onTest = (n) => (tested = n);
c.handleInput("i"); // reconnect
if (tested !== "@modelcontextprotocol/server-postgres") throw new Error(`reconnect targeted ${tested}`);
c.setResult({ name: "@modelcontextprotocol/server-postgres", ok: false, installed: true, why: "$POSTGRES_URL is not set — the server started, then exited with code 1", fix: ["export POSTGRES_URL=… in your shell, then press r to retry"], log: ["Error: POSTGRES_URL environment variable is required", "    at start (index.js:42)"] });
show("result (failed)", c);
c.handleInput("\r");
for (const ch of "grafana") c.handleInput(ch);
c.handleInput("\r");
show("detail (unsupported)", c);
if (!c.render(W).some((l) => l.includes("Why bird can't install this"))) throw new Error("unsupported detail missing the why block");

show("unreachable", new McpCatalog({ ...data, entries: [], registry_error: "cannot reach the MCP registry (https://registry.modelcontextprotocol.io/v0.1): timed out" }, envSet));
show("stale cache", new McpCatalog({ ...data, cache_age: 3 * 3600 + 5, registry_error: "cannot reach the MCP registry: timed out" }, envSet));
const nomatch = new McpCatalog(data, envSet);
for (const ch of "zzz") nomatch.handleInput(ch);
show("no matches", nomatch);

/* ---------- regression: arrow keys move the cursor and the selection is
   visibly marked (the /mcp catalog "arrows don't navigate" bug) ---------- */
const nav = new McpCatalog(data, envSet);
const strip = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, "");
const selectedName = (c: McpCatalog) => {
	const row = c.render(W).find((l) => strip(l).includes("▸"));
	if (!row) throw new Error("no selected row (▸ marker) rendered");
	return strip(row).trim();
};
if (!selectedName(nav).includes("@modelcontextprotocol/server-filesystem")) throw new Error("cursor starts on row 0");
nav.handleInput("\x1b[B"); // down arrow
if (!selectedName(nav).includes("@modelcontextprotocol/server-postgres")) throw new Error("down arrow did not move the cursor");
nav.handleInput("j"); // vim-style down
if (!selectedName(nav).includes("@modelcontextprotocol/server-github")) throw new Error("j did not move the cursor");
nav.handleInput("\x1b[A"); // up arrow
if (!selectedName(nav).includes("@modelcontextprotocol/server-postgres")) throw new Error("up arrow did not move the cursor");
nav.handleInput("k"); // vim-style up
if (!selectedName(nav).includes("@modelcontextprotocol/server-filesystem")) throw new Error("k did not move the cursor");
// exactly one ▸ marker at a time — a second indicator would be ambiguous
if (nav.render(W).filter((l) => strip(l).includes("▸")).length !== 1) throw new Error("browse view must show exactly one selection marker");
// The selected row carries a background highlight. Matched as "any SGR
// background" rather than a literal \x1b[48: chalk emits 48;5;N at 256-colour
// depth but plain 40-47 at basic depth, and which one the suite sees depends on
// the terminal it happens to run in — the original assertion passed only where
// it was written.
const BG_ESCAPE = /\x1b\[(?:4[0-7]|10[0-7]|48;)/;
if (!nav.render(W).some((l) => strip(l).includes("▸") && BG_ESCAPE.test(l)))
	throw new Error("selected row lacks the background highlight");

console.log("\nwrapCommand:", wrapCommand("npx", ["-y", "@sentry/mcp-server@0.12.0", "--access-token=$SENTRY_AUTH_TOKEN", "--organization-slug=$SENTRY_ORG", "--host=sentry.io"], 60));
console.log("humanCacheAge:", humanCacheAge(30), humanCacheAge(1500), humanCacheAge(10800), humanCacheAge(200000));
console.log("\nMCP CATALOG SMOKE OK");
