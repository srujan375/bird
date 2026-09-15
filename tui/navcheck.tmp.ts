import { McpCatalog } from "./src/components.ts";
const entries = [
	{ name: "@modelcontextprotocol/server-filesystem", description: "d", version: "0.6.2", installable: true, env: [], command: "npx", args: [] },
	{ name: "@modelcontextprotocol/server-postgres", description: "d2", version: "0.6.2", installable: true, env: ["POSTGRES_URL"], command: "npx", args: [] },
];
const connected = [
	{ name: "@modelcontextprotocol/server-filesystem", source: "project", connected: true, tools: 12 },
	{ name: "@modelcontextprotocol/server-postgres", source: "project", connected: false, tools: 0, error: "x" },
];
const c = new McpCatalog({ query: "", connected, entries, total: 2, cache_age: null, registry_error: null }, () => false);
const strip = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, "");
console.log(JSON.stringify(c.render(100).map(strip)));
c.handleInput("\x1b[B");
console.log(JSON.stringify(c.render(100).map(strip)));