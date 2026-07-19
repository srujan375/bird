"""mha — CLI. v1: `mha code "task"` one-shot, or `mha` for interactive mode."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from .activity import attach_printer
from .context.kg import KG
from .engine.runner import Runner
from .engine.session import SessionRecorder, new_run_id
from .llm.ollama import Ollama, OllamaError
from .llm.registry import Registry
from .llm.wire.openai_compat import OpenAICompatClient
from .harnesses.code import code_harness_tools
from .tools import ToolContext
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

    arch = sub.add_parser("arch", help="architecture session in a browser page")
    arch.add_argument("task", nargs="?", default=None, help="what to design, in natural language")
    arch.add_argument("--no-open", action="store_true", help="don't open the browser (tests/headless)")
    _add_common(arch)

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
    if args.command == "arch":
        return _arch_main(args)
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


def _make_runner(args, registry, spec, kg, recorder, tools=None, **runner_kw) -> Runner:
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
        tools=tools if tools is not None else code_harness_tools(with_kg=not args.no_kg),
        ctx=ctx,
        max_turns=args.max_turns,
        **runner_kw,
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


def _arch_main(args) -> int:
    import time
    import webbrowser

    from .harnesses.arch import harness as arch_def
    from .harnesses.arch.judge import make_judge
    from .harnesses.arch.render import TRACKER_PREFIX
    from .harnesses.arch.session import ArchSession
    from .harnesses.arch.tools import arch_harness_tools
    from .http_transport import HttpTransport
    from .repl import Repl
    from .serve import Server

    # arch defaults to the architect alias (a stronger model than coding);
    # an explicit --model always wins
    if args.model == "default":
        args.model = "architect"
    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    registry, spec, kg, _build_proc, run_id, run_dir = setup

    with SessionRecorder(run_dir) as recorder:
        runner = _make_runner(
            args, registry, spec, kg, recorder,
            tools=arch_harness_tools(with_kg=not args.no_kg),
            instructions_path=arch_def.INSTRUCTIONS_PATH,
            mutating_tools=arch_def.MUTATING_TOOLS,
            tracker=arch_def.arch_tracker,
            tracker_prefix=TRACKER_PREFIX,
            explore_nudge=arch_def.EXPLORE_NUDGE,
        )
        repl = Repl(runner, registry, kg, recorder, run_id)
        resumed_dir = None
        if getattr(args, "resume", None):
            resumed_dir = _resume_into_repl(repl, args.resume, registry)

        transport = HttpTransport(
            static_dir=arch_def.STATIC_DIR,
            stop_when=lambda e: e.get("type") == "arch_state" and e.get("phase") == "finalized",
        )
        server = Server(repl, transport=transport)

        # the arch session: restored state on resume, persisted to THIS run
        if resumed_dir is not None:
            arch = ArchSession.load(resumed_dir)
            arch.run_dir = run_dir
        else:
            arch = ArchSession(run_dir=run_dir)
        arch.broker = server.broker
        arch.judge = make_judge(registry, OpenAICompatClient())

        def on_state(payload: dict) -> None:
            # slim record (full state lives in arch_state.json); full payload to the page
            recorder.event(
                "arch_state", {"phase": payload["phase"], "changed": payload.get("changed")}
            )
            transport.emit(payload)

        arch.on_state = on_state
        runner.ctx.arch = arch

        print(f"mha arch | model={spec.spec} | kg={'off' if args.no_kg else 'on'} | session={run_id}")
        print(f"page: {transport.url}")
        if not args.no_open:
            webbrowser.open(transport.url)
        if args.task:
            server.on_user_input(args.task)
        elif resumed_dir is None:
            print("note: no task given — send the first message from the page", file=sys.stderr)
        try:
            rc = server.run()
        except KeyboardInterrupt:
            transport.shutdown()
            rc = 0
        time.sleep(0.3)  # let SSE clients drain the finalized/bye events
        if arch.state.phase == "finalized":
            from .harnesses.arch.bundle import bundle_paths

            print("architecture finalized. handoff bundle:")
            for p in bundle_paths(run_dir):
                print(f"  {p}")
            print("next: mha code")
        return rc


def _resume_into_repl(repl, run_id: str, registry: "Registry") -> Path | None:
    """Load a prior session's transcript into an existing Repl; returns the
    resumed session's directory (None if nothing matched).

    The saved transcript's first message is a system prompt built from the
    *old* process's code/skills — strip it so the freshly-loaded runner
    rebuilds a current one on the next turn. Re-applies the session's model
    so the resumed chat keeps running on the same LLM."""
    from .engine.session import load_messages, read_session_meta
    from .llm.types import Message

    sessions_dir = repl.recorder.run_dir.parent
    target = None
    for entry in sorted(sessions_dir.iterdir(), key=lambda p: p.stat().st_mtime):
        if entry.name == run_id or entry.name.startswith(run_id + "-"):
            target = entry
            break
    if target is None:
        return None  # nothing to resume — start fresh
    rows = load_messages(target)
    if not rows:
        return target
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
    return target


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
