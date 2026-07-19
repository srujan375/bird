"""mha — CLI. v1: `mha code "task"` one-shot, or `mha` for interactive mode."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from .activity import attach_printer
from .context.kg import KG
from .harness.runner import Runner
from .harness.session import SessionRecorder, new_run_id
from .llm.ollama import Ollama, OllamaError
from .llm.registry import Registry
from .llm.wire.openai_compat import OpenAICompatClient
from .tools import ToolContext, code_harness_tools
from .skills import load_skills


def _add_common(p) -> None:
    p.add_argument("--repo", default=".", help="repository root (default: cwd)")
    p.add_argument("--model", default="default", help="model alias or provider:model spec")
    p.add_argument("--no-kg", action="store_true", help="control arm: no kg_query tool")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--models-json", default=None, help="path to a models.json override")
    p.add_argument(
        "--resume",
        default=None,
        metavar="RUN_ID",
        help="resume a previous session by run-id (loads its transcript + model)",
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["chat"]  # bare `mha` → interactive

    parser = argparse.ArgumentParser(prog="mha", description="multi-harness agent")
    sub = parser.add_subparsers(dest="command", required=True)

    code = sub.add_parser("code", help="run the Code harness on a task")
    code.add_argument("task", help="what to do, in natural language")
    _add_common(code)

    chat = sub.add_parser("chat", help="interactive mode with slash commands (default)")
    chat.add_argument("--tui", action="store_true", help="force the full-screen TUI (error if unavailable)")
    chat.add_argument("--plain", action="store_true", help="force the plain REPL instead of the TUI")
    _add_common(chat)

    serve = sub.add_parser("serve", help="JSON-lines bridge over stdio (used by the TUI)")
    _add_common(serve)

    kg_cmd = sub.add_parser("kg", help="knowledge graph maintenance")
    kg_cmd.add_argument("action", choices=["build", "update", "query", "status"])
    kg_cmd.add_argument("question", nargs="?", help="question (for query)")
    kg_cmd.add_argument("--repo", default=".")
    kg_cmd.add_argument("--budget", type=int, default=2000)

    args = parser.parse_args(argv)
    if args.command == "kg":
        return _kg_main(args)
    if args.command == "serve":
        return _serve_main(args)
    if args.command == "chat":
        # TUI is the default interactive surface when it's installed and we
        # are on a real terminal; --plain forces the REPL, --tui makes a
        # missing TUI an error instead of a fallback.
        if args.tui:
            return _tui_main(args)
        if not args.plain and sys.stdin.isatty() and sys.stdout.isatty() and _tui_dir() is not None:
            return _tui_main(args)
        return _chat_main(args)
    return _code_main(args)


def _kg_main(args) -> int:
    kg = KG(Path(args.repo))
    if args.action == "status":
        print(f"store: {kg.out_dir}")
        print(f"ready: {kg.is_ready()}")
        if kg.graph_path.exists():
            print(f"stale: {kg.is_stale()}")
        return 0
    if args.action == "build":
        stats = kg.build()
    elif args.action == "update":
        stats = kg.update()
    else:
        if not args.question:
            print("kg query needs a question", file=sys.stderr)
            return 2
        result = kg.query(args.question, budget=args.budget)
        print(result.text)
        return 0
    print(f"kg {stats.action}: {stats.nodes} nodes, {stats.edges} edges")
    return 0


def _setup(args):
    """Shared bring-up for code/chat: registry, model, Ollama, KG, session.
    Returns (registry, spec, kg, build_proc, run_id, run_dir) or an int exit code."""
    repo_root = Path(args.repo).resolve()
    if not repo_root.is_dir():
        print(f"not a directory: {repo_root}", file=sys.stderr)
        return 2

    registry = Registry.load(args.models_json)
    spec = registry.resolve(args.model)

    if spec.provider.name == "ollama":
        try:
            Ollama(
                spec.provider.native_url or "http://localhost:11434",
                api_key_env=spec.provider.api_key_env,
            ).ensure(spec.model)
        except OllamaError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    run_id = new_run_id()
    run_dir = repo_root / ".mha" / "sessions" / run_id
    kg = None
    build_proc = None
    if not args.no_kg:
        kg = KG(repo_root)
        build_proc = kg.ensure_background()  # non-blocking (decision #9)
    return registry, spec, kg, build_proc, run_id, run_dir


def _make_runner(args, registry, spec, kg, recorder) -> Runner:
    repo_root = Path(args.repo).resolve()
    ctx = ToolContext(
        repo_root=repo_root,
        kg=kg,
        record=recorder.event,
        client=OpenAICompatClient(),
        skills=load_skills(repo_root),
    )
    return Runner(
        spec=spec,
        client=OpenAICompatClient(),
        registry=registry,
        tools=code_harness_tools(with_kg=not args.no_kg),
        ctx=ctx,
        max_turns=args.max_turns,
    )


def _code_main(args) -> int:
    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    registry, spec, kg, build_proc, run_id, run_dir = setup

    with SessionRecorder(run_dir) as recorder:
        runner = _make_runner(args, registry, spec, kg, recorder)
        attach_printer(runner.ctx)  # `› tool …` headers while the agent works
        print(f"mha code | model={spec.spec} | kg={'off' if args.no_kg else 'on'} | session={run_id}")
        if build_proc is not None:
            print("kg: building in background; harness starts now")
        result = runner.run(args.task)

    print(f"\n[{result.status}] {result.summary}")
    print(
        f"turns={result.turns} tokens={result.usage.input_tokens}in/"
        f"{result.usage.output_tokens}out session={run_dir}"
    )
    return 0 if result.status == "done" else 1


def _chat_main(args) -> int:
    from .repl import Repl

    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    registry, spec, kg, _build_proc, run_id, run_dir = setup

    with SessionRecorder(run_dir) as recorder:
        runner = _make_runner(args, registry, spec, kg, recorder)
        repl = Repl(runner, registry, kg, recorder, run_id)
        # /reload re-execs `mha chat --resume <run_id>: load the prior session's
        # transcript + model so the conversation continues on freshly-loaded
        # code/skills without losing history.
        if getattr(args, "resume", None):
            _resume_into_repl(repl, args.resume, registry)
        return repl.run()


def _serve_main(args) -> int:
    from .repl import Repl
    from .serve import serve

    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    registry, spec, kg, _build_proc, run_id, run_dir = setup

    with SessionRecorder(run_dir) as recorder:
        runner = _make_runner(args, registry, spec, kg, recorder)
        repl = Repl(runner, registry, kg, recorder, run_id)
        # /reload respawns `mha serve` with --resume <old_run_id>: load the
        # previous session's transcript + model so the conversation continues
        # on freshly-loaded code/skills without losing history.
        if getattr(args, "resume", None):
            _resume_into_repl(repl, args.resume, registry)
        return serve(repl)


def _resume_into_repl(repl, run_id: str, registry: "Registry") -> None:
    """Load a prior session's transcript into an existing Repl.

    The saved transcript's first message is a system prompt built from the
    *old* process's code/skills — strip it so the freshly-loaded runner
    rebuilds a current one on the next turn. Re-applies the session's model
    so the resumed chat keeps running on the same LLM."""
    from .harness.session import load_messages, read_session_meta
    from .llm.types import Message

    sessions_dir = repl.recorder.run_dir.parent
    target = None
    for entry in sorted(sessions_dir.iterdir(), key=lambda p: p.stat().st_mtime):
        if entry.name == run_id or entry.name.startswith(run_id + "-"):
            target = entry
            break
    if target is None:
        return  # nothing to resume — start fresh
    rows = load_messages(target)
    if not rows:
        return
    msgs = [Message.from_dict(r) for r in rows]
    # drop the stale system prompt; the new runner regenerates it
    if msgs and msgs[0].role == "system":
        msgs = msgs[1:]
    repl.messages = msgs
    repl.recorder.event("resume", {"from": target.name, "messages": len(msgs), "via": "reload"})
    # re-apply the session's recorded model
    recorded = read_session_meta(target).get("model")
    if recorded and recorded != repl.runner.spec.spec:
        try:
            repl.runner.spec = registry.resolve(recorded)
        except Exception:
            pass  # keep the current model if the recorded one is unavailable


def _tui_dir() -> Path | None:
    """The pi-tui frontend in the mha source tree, or None if not installed."""
    tui_dir = Path(__file__).resolve().parents[2] / "tui"
    if (tui_dir / "package.json").is_file() and (tui_dir / "node_modules").is_dir():
        return tui_dir
    return None


def _tui_main(args) -> int:
    """Exec the pi-tui frontend; it spawns `mha serve` back over stdio for
    the actual harness."""
    import subprocess

    tui_dir = _tui_dir()
    if tui_dir is None:
        expected = Path(__file__).resolve().parents[2] / "tui"
        print(
            f"TUI not available at {expected} — run: cd {expected} && npm install",
            file=sys.stderr,
        )
        return 1
    cmd = ["npx", "tsx", "src/main.ts", "--repo", str(Path(args.repo).resolve())]
    if args.model != "default":
        cmd += ["--model", args.model]
    if args.no_kg:
        cmd.append("--no-kg")
    return subprocess.run(cmd, cwd=tui_dir).returncode


if __name__ == "__main__":
    sys.exit(main())
