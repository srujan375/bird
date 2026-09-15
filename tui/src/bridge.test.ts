// Bridge wire-shape smoke tests — no real `bird serve`: BIRD_PYTHON points
// at a `cat` lookalike that echoes every stdin line back out on stdout, so
// the bridge's own JSON-lines parser hands us exactly what would have
// reached serve.py's StdioTransport. Follows queue.test.ts's pattern:
// check()/fails[]/exit 1.
//
// The load-bearing assertion is the interrupt message: Esc / first Ctrl+C
// in the TUI must put exactly {"type":"interrupt"} on the wire — the shape
// serve.py dispatches to Server.on_interrupt() (see tests/test_serve.py and
// tests/test_arch_e2e.py::test_interrupt_over_http for the backend side).
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Bridge, type ServerMessage } from "./bridge.ts";

const fails: string[] = [];
function check(cond: boolean, msg: string): void {
	if (!cond) fails.push(msg);
}

const dir = mkdtempSync(join(tmpdir(), "bird-bridge-test-"));
const fake = join(dir, "fake-python.sh");
writeFileSync(fake, "#!/bin/sh\nexec cat\n");
chmodSync(fake, 0o755);
process.env.BIRD_PYTHON = fake;

type Msg = ServerMessage & { type: string };
const seen: Msg[] = [];
const stderr: string[] = [];
let exited: number | null | undefined;

const bridge = new Bridge({
	repo: dir,
	onMessage: (m) => seen.push(m as Msg),
	onStderr: (l) => stderr.push(l),
	onExit: (c) => (exited = c),
});

bridge.userInput("hello");
bridge.command("/model x");
bridge.permission(3, true);
bridge.prompt(2, null);
bridge.interrupt();
bridge.sendMcpRefresh("fs");
bridge.sendMcpInstall("filesystem");
bridge.sendMcpRemove("old");

const EXPECTED = 8;
const deadline = Date.now() + 5000;
while (seen.length < EXPECTED && Date.now() < deadline) {
	await new Promise((r) => setTimeout(r, 10));
}
check(seen.length === EXPECTED, `all ${EXPECTED} sends echoed back (got ${seen.length})`);

const byType = new Map<string, Record<string, unknown>>();
for (const m of seen) byType.set(m.type, m as unknown as Record<string, unknown>);

/* ---------- interrupt: exactly {"type":"interrupt"} ---------- */
{
	const msg = byType.get("interrupt");
	check(!!msg, "interrupt message reached the wire");
	check(
		!!msg && Object.keys(msg).length === 1,
		`interrupt carries no extra fields (serve.py matches on type alone; got ${JSON.stringify(msg)})`,
	);
}

/* ---------- the other inbound shapes serve.py's StdioTransport expects ---------- */
{
	const m = byType.get("user_input");
	check(!!m && m.text === "hello", "user_input carries text");
}
{
	const m = byType.get("command");
	check(!!m && m.line === "/model x", "command carries line");
}
{
	const m = byType.get("permission_response");
	check(!!m && m.id === 3 && m.approved === true, "permission_response carries id + approved");
}
{
	const m = byType.get("prompt_response");
	check(!!m && m.id === 2 && m.value === null, "prompt_response carries id + null value (skipped)");
}
{
	const m = byType.get("mcp_catalog_refresh");
	check(!!m && m.query === "fs", "mcp_catalog_refresh carries query");
}
{
	const m = byType.get("mcp_install");
	check(!!m && m.name === "filesystem", "mcp_install carries name");
}
{
	const m = byType.get("mcp_remove");
	check(!!m && m.name === "old", "mcp_remove carries name");
}

/* ---------- clean shutdown: stdin close ends the echo server ---------- */
bridge.stop();
const deadline2 = Date.now() + 3000;
while (exited === undefined && Date.now() < deadline2) {
	await new Promise((r) => setTimeout(r, 10));
}
check(exited === 0, `echo server exited 0 on stdin close (got ${exited})`);
check(stderr.length === 0, `no stderr noise (got: ${stderr.join("; ")})`);

/* ---------- report ---------- */
if (fails.length) {
	console.error(`BRIDGE FAIL (${fails.length}):`);
	for (const f of fails) console.error(`  - ${f}`);
	process.exit(1);
}
console.log("BRIDGE OK");
process.exit(0);
