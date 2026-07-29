// Spawns `ox serve` and speaks its JSON-lines protocol over stdio.
import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

export interface DiffLine {
	kind: "ctx" | "add" | "del";
	text: string;
}

export type ServerMessage =
	| { type: "ready"; model: string; kg: boolean; kg_ready: boolean; run_id: string; repo: string; skills: { name: string; description: string; source: string }[] }
	| { type: "harness_event"; event: string; data: Record<string, unknown> }
	| ({ type: "permission_request"; id: number } & (
			| { kind: "bash"; cmd: string }
			| { kind: "edit" | "write"; file: string; lines: DiffLine[] }
			| { kind: "read_outside_repo"; tool: string; path: string }
	  ))
	| { type: "state"; model: string }
	| {
			type: "model_list";
			current: string;
			default: string | null;
			models: { spec: string; source: string; context_window: number | null }[];
			notes: string[];
	  }
	| {
			type: "session_list";
			current: string;
			sessions: { id: string; name: string; last_event: string }[];
	  }
	| { type: "turn_end"; status: string; summary: string; turns: number }
	| { type: "command_output"; text: string }
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
	if (process.env.OX_PYTHON) return process.env.OX_PYTHON;
	// the ox source tree's venv (where `pip install -e .` put ox) beats the
	// target repo's venv, which usually doesn't have ox installed
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
		const args = ["-m", "ox", "serve", "--repo", this.opts.repo];
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
				this.opts.onStderr(`failed to restart ox serve: ${err.message}`);
				return;
			}
			this.opts.onStderr(`failed to start ox serve: ${err.message}`);
			this.opts.onExit(1);
		});
		return proc;
	}

	private restarting = false;

	/** Respawn `ox serve` fresh from disk, resuming `runId`'s transcript.
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

	interrupt(): void {
		this.send({ type: "interrupt" });
	}

	stop(): void {
		this.proc.stdin.end();
		setTimeout(() => this.proc.kill(), 1500).unref();
	}
}
