// Spawns `mha serve` and speaks its JSON-lines protocol over stdio.
import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

export interface DiffLine {
	kind: "ctx" | "add" | "del";
	text: string;
}

export type ServerMessage =
	| { type: "ready"; model: string; kg: boolean; kg_ready: boolean; run_id: string; repo: string }
	| { type: "harness_event"; event: string; data: Record<string, unknown> }
	| ({ type: "permission_request"; id: number } & (
			| { kind: "bash"; cmd: string }
			| { kind: "edit" | "write"; file: string; lines: DiffLine[] }
	  ))
	| { type: "state"; model: string }
	| {
			type: "model_list";
			current: string;
			default: string | null;
			models: { spec: string; source: string; context_window: number | null }[];
			notes: string[];
	  }
	| { type: "turn_end"; status: string; summary: string; turns: number }
	| { type: "command_output"; text: string }
	| { type: "error"; message: string }
	| { type: "bye" };

export interface BridgeOptions {
	repo: string;
	model?: string;
	noKg?: boolean;
	onMessage: (msg: ServerMessage) => void;
	onStderr: (line: string) => void;
	onExit: (code: number | null) => void;
}

function findPython(repo: string): string {
	if (process.env.MHA_PYTHON) return process.env.MHA_PYTHON;
	// the mha source tree's venv (where `pip install -e .` put mha) beats the
	// target repo's venv, which usually doesn't have mha installed
	const mhaRoot = join(import.meta.dirname, "..", "..");
	for (const root of [mhaRoot, repo]) {
		const venv = join(root, ".venv", "bin", "python");
		if (existsSync(venv)) return venv;
	}
	return "python3";
}

export class Bridge {
	private proc: ChildProcessWithoutNullStreams;
	private buffer = "";

	constructor(opts: BridgeOptions) {
		const args = ["-m", "mha", "serve", "--repo", opts.repo];
		if (opts.model) args.push("--model", opts.model);
		if (opts.noKg) args.push("--no-kg");
		this.proc = spawn(findPython(opts.repo), args, {
			cwd: opts.repo,
			env: process.env,
		});
		this.proc.stdout.setEncoding("utf-8");
		this.proc.stdout.on("data", (chunk: string) => {
			this.buffer += chunk;
			let nl: number;
			while ((nl = this.buffer.indexOf("\n")) >= 0) {
				const line = this.buffer.slice(0, nl).trim();
				this.buffer = this.buffer.slice(nl + 1);
				if (!line) continue;
				try {
					opts.onMessage(JSON.parse(line) as ServerMessage);
				} catch {
					opts.onStderr(line);
				}
			}
		});
		this.proc.stderr.setEncoding("utf-8");
		this.proc.stderr.on("data", (chunk: string) => {
			for (const line of chunk.split("\n")) if (line.trim()) opts.onStderr(line.trim());
		});
		this.proc.on("exit", (code) => opts.onExit(code));
		this.proc.on("error", (err) => {
			opts.onStderr(`failed to start mha serve: ${err.message}`);
			opts.onExit(1);
		});
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
