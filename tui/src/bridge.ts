// Spawns `bird serve` and speaks its JSON-lines protocol over stdio.
import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

export interface DiffLine {
	kind: "ctx" | "add" | "del";
	text: string;
}

export type ServerMessage =
	| { type: "ready"; model: string; kg: boolean; kg_ready: boolean; run_id: string; repo: string; skills: { name: string; description: string; source: string }[]; input_tokens?: number; output_tokens?: number; think_mode?: string | null }
	| { type: "harness_event"; event: string; data: Record<string, unknown> }
	| ({ type: "permission_request"; id: number } & (
			| { kind: "bash"; cmd: string }
			| { kind: "edit" | "write" | "delete"; file: string; lines: DiffLine[] }
			| { kind: "read_outside_repo"; tool: string; path: string }
	  ))
	| { type: "state"; model: string; think_mode?: string | null }
	| {
			type: "harness_list";
			current: string;
			harnesses: { name: string; alias: string; model: string | null; think_mode: string | null; shared_with: string[] }[];
	  }
	| {
			type: "model_list";
			// the /model walk: harness + alias name the pick lands on, and
			// what the thinking step needs — each entry's stored level and
			// the modes per provider (OpenRouter has no "max")
			harness?: string;
			alias?: string;
			current: string | null;
			default: string | null;
			models: { spec: string; source: string; context_window: number | null; think_mode?: string | null }[];
			notes: string[];
			think_modes?: Record<string, string[]>;
	  }
	| {
			type: "session_list";
			current: string;
			sessions: { id: string; name: string; last_event: string }[];
	  }
	| { type: "think_list"; current: string | null; modes: string[] }
	| {
			type: "mcp_catalog";
			query: string;
			connected: { name: string; source?: string; disabled?: boolean; connected?: boolean; tools?: number; command?: string; args?: string[]; env?: string[]; error?: string }[];
			entries: { name: string; description?: string; version?: string; installable?: boolean; reason?: string; human_reason?: string; command?: string; args?: string[]; env?: string[]; repo?: string; url?: string }[];
			total: number | null;
			cache_age: number | null;
			registry_error: string | null;
			fetching?: boolean;
	  }
	| {
			type: "mcp_result";
			name: string;
			ok: boolean;
			installed?: boolean;
			removed?: boolean;
			tools?: string[];
			why?: string;
			fix?: string[];
			log?: string[];
	  }
	| { type: "turn_end"; status: string; summary: string; turns: number; input_tokens?: number; output_tokens?: number }
	| { type: "command_output"; text: string }
	| { type: "setup_start" }
	| { type: "setup_end" }
	| {
			type: "prompt_request";
			id: number;
			prompt: string;
			secret?: boolean;
			default?: string;
			choices?: { value: string; label: string; description?: string }[];
			current?: string | null;
	  }
	// serve parked mid-turn input for the running step to pick up (ack only —
	// the TUI already drew the bubble when it sent it)
	| { type: "input_pending"; text: string }
	// serve handed mid-turn input back: the turn was interrupted or errored
	// before the loop ever showed it to the model
	| { type: "input_unsent"; texts: string[] }
	| { type: "reload"; run_id: string }
	| { type: "error"; message: string }
	| { type: "bye" };

export interface BridgeOptions {
	repo: string;
	model?: string;
	noKg?: boolean;
	harness?: string;
	fromArch?: string;
	onMessage: (msg: ServerMessage) => void;
	onStderr: (line: string) => void;
	onExit: (code: number | null) => void;
}

function findPython(repo: string): string {
	if (process.env.BIRD_PYTHON) return process.env.BIRD_PYTHON;
	// the bird source tree's venv (where `pip install -e .` put bird) beats the
	// target repo's venv, which usually doesn't have bird installed
	const oxRoot = join(import.meta.dirname, "..", "..");
	for (const root of [oxRoot, repo]) {
		const venv = join(root, ".venv", "bin", "python");
		if (existsSync(venv)) return venv;
	}
	return "python3";
}

export class Bridge {
	private proc: ChildProcessWithoutNullStreams;
	private buffer = "";
	private opts: BridgeOptions;

	constructor(opts: BridgeOptions) {
		this.opts = opts;
		this.proc = this.spawn();
	}

	private serveArgs(resume?: string): string[] {
		const args = ["-m", "bird", "serve", "--repo", this.opts.repo];
		if (this.opts.model) args.push("--model", this.opts.model);
		if (this.opts.noKg) args.push("--no-kg");
		if (this.opts.harness) args.push("--harness", this.opts.harness);
		if (this.opts.fromArch) args.push("--from-arch", this.opts.fromArch);
		if (resume) args.push("--resume", resume);
		return args;
	}

	private spawn(resume?: string): ChildProcessWithoutNullStreams {
		const proc = spawn(findPython(this.opts.repo), this.serveArgs(resume), {
			cwd: this.opts.repo,
			env: process.env,
		});
		proc.stdout.setEncoding("utf-8");
		proc.stdout.on("data", (chunk: string) => {
			this.buffer += chunk;
			let nl: number;
			while ((nl = this.buffer.indexOf("\n")) >= 0) {
				const line = this.buffer.slice(0, nl).trim();
				this.buffer = this.buffer.slice(nl + 1);
				if (!line) continue;
				try {
					this.opts.onMessage(JSON.parse(line) as ServerMessage);
				} catch {
					this.opts.onStderr(line);
				}
			}
		});
		proc.stderr.setEncoding("utf-8");
		proc.stderr.on("data", (chunk: string) => {
			for (const line of chunk.split("\n")) if (line.trim()) this.opts.onStderr(line.trim());
		});
		// During a restart we manage the lifecycle ourselves (see restart()),
		// so suppress the default onExit handler that would shut the TUI down.
		proc.on("exit", (code) => {
			if (this.restarting) return;
			this.opts.onExit(code);
		});
		proc.on("error", (err) => {
			if (this.restarting) {
				this.opts.onStderr(`failed to restart bird serve: ${err.message}`);
				return;
			}
			this.opts.onStderr(`failed to start bird serve: ${err.message}`);
			this.opts.onExit(1);
		});
		return proc;
	}

	private restarting = false;

	/** Respawn `bird serve` fresh from disk, resuming `runId`'s transcript.
	 * Used by /reload so code/skill/tool changes take effect without a new
	 * terminal session. The old process is killed; the new one reuses the
	 * same onMessage/onStderr handlers. */
	restart(runId: string): void {
		this.restarting = true;
		try {
			this.buffer = "";
			// close stdin so the old process drains and exits; then kill to be sure
			this.proc.stdin.end();
			try {
				this.proc.kill("SIGTERM");
			} catch {
				/* already dead */
			}
			this.proc = this.spawn(runId);
		} finally {
			this.restarting = false;
		}
	}

	private send(obj: Record<string, unknown>): void {
		this.proc.stdin.write(JSON.stringify(obj) + "\n");
	}

	userInput(text: string): void {
		this.send({ type: "user_input", text });
	}

	command(line: string): void {
		this.send({ type: "command", line });
	}

	permission(id: number, approved: boolean): void {
		this.send({ type: "permission_response", id, approved });
	}

	/** Answer a prompt_request (setup walkthrough: key entry, model pick).
	 * null means the user cancelled/skipped. */
	prompt(id: number, value: string | null): void {
		this.send({ type: "prompt_response", id, value });
	}

	interrupt(): void {
		this.send({ type: "interrupt" });
	}

	/** Ask serve to re-fetch the MCP catalog (retry after an error, or a
	 *  new search from the catalog view). */
	/** re-test a configured server (the catalog's reconnect / retry) */
	sendMcpTest(name: string): void {
		this.send({ type: "mcp_test", name });
	}

	sendMcpRefresh(query: string): void {
		this.send({ type: "mcp_catalog_refresh", query });
	}

	/** Install a registry server by name (serve shows the command and env
	 *  vars and asks for an explicit 'y' before writing anything). */
	sendMcpInstall(name: string): void {
		this.send({ type: "mcp_install", name });
	}

	/** Remove a configured server by name (the catalog's 'x' key). */
	sendMcpRemove(name: string): void {
		this.send({ type: "mcp_remove", name });
	}

	stop(): void {
		this.proc.stdin.end();
		setTimeout(() => this.proc.kill(), 1500).unref();
	}
}
