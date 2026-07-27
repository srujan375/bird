"""ox — CLI. v1: `ox code "task"` one-shot, or `ox` for interactive mode."""

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
from .harnesses.handoff import read_seed
from .harnesses.lead import lead_harness_tools
from .harnesses.registry import build_runner
from .tools import ToolContext
from .skills import load_skills


# How long `ox arch` keeps serving a finalized design so it can be read. It
# gives up sooner the moment the page closes; this is only the backstop for a
# tab left open and forgotten.
ARCH_LINGER_SECONDS = 30 * 60


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
        argv = ["lead"]  # bare `ox` → the front-door lead (interactive)

    parser = argparse.ArgumentParser(prog="ox", description="multi-harness agent")
    sub = parser.add_subparsers(dest="command", required=True)

    code = sub.add_parser("code", help="run the Code harness on a task")
    code.add_argument("task", help="what to do, in natural language")
    code.add_argument("--from-arch", default=None, metavar="RUN_ID",
                      help="seed the code session from a finalized arch bundle ('latest' or a run-id)")
    code.add_argument("-y", "--yes", action="store_true",
                      help="auto-approve every edit/write/bash (unattended runs; "
                           "without it a tty prompts and a non-tty refuses)")
    _add_common(code)

    chat = sub.add_parser("chat", help="interactive mode with slash commands (default)")
    chat.add_argument("--tui", action="store_true", help="force the full-screen TUI (error if unavailable)")
    chat.add_argument("--plain", action="store_true", help="force the plain REPL instead of the TUI")
    chat.add_argument("--from-arch", default=None, metavar="RUN_ID",
                      help="seed the session from a finalized arch bundle ('latest' or a run-id)")
    _add_common(chat)

    lead = sub.add_parser("lead", help="the front-door agent: chats, researches, and dispatches architect/code")
    lead.add_argument("task", nargs="?", default=None,
                      help="one-shot request; omit for an interactive session")
    lead.add_argument("--tui", action="store_true", help="force the full-screen TUI (error if unavailable)")
    lead.add_argument("--plain", action="store_true", help="force the plain REPL instead of the TUI")
    lead.add_argument("-y", "--yes", action="store_true",
                      help="one-shot mode: auto-approve every edit/write/bash the "
                           "dispatched code sub-session makes")
    _add_common(lead)

    serve = sub.add_parser("serve", help="JSON-lines bridge over stdio (used by the TUI)")
    serve.add_argument("--harness", default="code", choices=["code", "lead"],
                       help="which harness to serve (default: code)")
    serve.add_argument("--from-arch", default=None, metavar="RUN_ID",
                       help="seed from a finalized arch bundle ('latest' or a run-id)")
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
    if args.command == "lead":
        if args.task:
            return _lead_main(args)  # one-shot: route and build, no interactive shell
        return _interactive_session(args, "lead", tools=lead_harness_tools(with_kg=not args.no_kg))
    if args.command == "chat":
        seed = _from_arch_seed(args)
        if isinstance(seed, int):
            return seed
        return _interactive_session(args, "code", seed_context=seed)
    return _code_main(args)


def _interactive_session(args, harness: str, tools=None, seed_context=None) -> int:
    """The shared interactive surface: the full-screen TUI when it's installed
    and we're on a real terminal (--plain forces the REPL; --tui makes a
    missing TUI an error), else the plain REPL. Same selection for every
    interactive harness (chat=code, bare ox=lead)."""
    if getattr(args, "tui", False):
        return _tui_main(args, harness)
    if (not getattr(args, "plain", False) and sys.stdin.isatty()
            and sys.stdout.isatty() and _tui_dir() is not None):
        return _tui_main(args, harness)
    return _repl_session(args, harness, tools=tools, seed_context=seed_context)


def _from_arch_seed(args):
    """Resolve --from-arch to seed_context. Returns the seed string, None (no
    flag), or an int exit code when the bundle can't be found."""
    if not getattr(args, "from_arch", None):
        return None
    seed = read_seed(Path(args.repo).resolve(), args.from_arch)
    if seed is None:
        print(f"no finalized architecture bundle found for '{args.from_arch}'", file=sys.stderr)
        return 2
    return seed


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
    run_dir = repo_root / ".ox" / "sessions" / run_id
    kg = None
    build_proc = None
    if not args.no_kg:
        kg = KG(repo_root)
        build_proc = kg.ensure_background()  # non-blocking (decision #9)
    return registry, spec, kg, build_proc, run_id, run_dir


def _headless_broker(args):
    """The broker for a one-shot run. --yes approves everything; on a real
    terminal we prompt; otherwise deny with a reason — a gate nobody can
    answer must not default to yes."""
    from .permissions import AutoApproveBroker, ConsoleBroker, DenyBroker

    if getattr(args, "yes", False):
        return AutoApproveBroker()
    if sys.stdin.isatty():
        return ConsoleBroker()
    return DenyBroker(
        "no interactive terminal to approve it — re-run with --yes to auto-approve"
    )


def _make_runner(args, registry, spec, kg, recorder, *, harness="code",
                 tools=None, seed_context=None, broker=None) -> Runner:
    repo_root = Path(args.repo).resolve()
    ctx = ToolContext(
        repo_root=repo_root,
        kg=kg,
        record=recorder.event,
        client=OpenAICompatClient(),
        skills=load_skills(repo_root),
        registry=registry,
        run_dir=recorder.run_dir,
        # gating happens in build_runner, so the broker must be on the ctx
        # before the runner is built — and it rides the ctx into any
        # sub-harness the lead dispatches
        broker=broker,
    )
    # all harness construction goes through the registry now — the arch/code/lead
    # tuning (instructions, toolset, nudges, tracker) lives in HarnessDef, not here
    return build_runner(
        harness,
        spec=spec,
        client=OpenAICompatClient(),
        registry=registry,
        ctx=ctx,
        max_turns=args.max_turns,
        with_kg=not args.no_kg,
        tools=tools,
        seed_context=seed_context,
    )


def _code_main(args) -> int:
    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    registry, spec, kg, build_proc, run_id, run_dir = setup

    seed_context = None
    if getattr(args, "from_arch", None):
        seed_context = read_seed(Path(args.repo).resolve(), args.from_arch)
        if seed_context is None:
            print(f"no finalized architecture bundle found for '{args.from_arch}'", file=sys.stderr)
            return 2

    with SessionRecorder(run_dir) as recorder:
        runner = _make_runner(args, registry, spec, kg, recorder,
                              seed_context=seed_context, broker=_headless_broker(args))
        attach_printer(runner.ctx)  # `› tool …` headers while the agent works
        print(f"ox code | model={spec.spec} | kg={'off' if args.no_kg else 'on'} | session={run_id}")
        if seed_context is not None:
            print(f"seeded from arch bundle: {args.from_arch}")
        if build_proc is not None:
            print("kg: building in background; harness starts now")
        result = runner.run(args.task)

    print(f"\n[{result.status}] {result.summary}")
    print(
        f"turns={result.turns} tokens={result.usage.input_tokens}in/"
        f"{result.usage.output_tokens}out session={run_dir}"
    )
    return 0 if result.status == "done" else 1


def _repl_session(args, harness: str = "code", tools=None, seed_context=None) -> int:
    """The plain REPL for any interactive harness (chat=code, bare ox=lead)."""
    from .permissions import ConsoleBroker
    from .repl import Repl

    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    registry, spec, kg, _build_proc, run_id, run_dir = setup

    with SessionRecorder(run_dir) as recorder:
        # the REPL runs turns on the main thread, so the prompt is just input()
        runner = _make_runner(args, registry, spec, kg, recorder,
                              harness=harness, tools=tools, seed_context=seed_context,
                              broker=ConsoleBroker())
        repl = Repl(runner, registry, kg, recorder, run_id)
        # /reload re-execs `ox <cmd> --resume <run_id>`: load the prior
        # session's transcript + model so the conversation continues on
        # freshly-loaded code/skills without losing history.
        if getattr(args, "resume", None):
            _resume_into_repl(repl, args.resume, registry)
        return repl.run()


def _serve_main(args) -> int:
    from .permissions import PermissionBroker
    from .repl import Repl
    from .serve import Server

    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    registry, spec, kg, _build_proc, run_id, run_dir = setup

    harness = getattr(args, "harness", "code")
    seed = _from_arch_seed(args)
    if isinstance(seed, int):
        return seed
    tools = lead_harness_tools(with_kg=not args.no_kg) if harness == "lead" else None

    with SessionRecorder(run_dir) as recorder:
        # broker before runner: build_runner gates on it. The Server binds its
        # emit sink once the transport is up.
        broker = PermissionBroker()
        runner = _make_runner(args, registry, spec, kg, recorder,
                              harness=harness, tools=tools, seed_context=seed,
                              broker=broker)
        repl = Repl(runner, registry, kg, recorder, run_id)
        # /reload respawns `ox serve` with --resume <old_run_id>: load the
        # previous session's transcript + model so the conversation continues
        # on freshly-loaded code/skills without losing history.
        if getattr(args, "resume", None):
            _resume_into_repl(repl, args.resume, registry)
        return Server(repl, broker=broker).run()


def _arch_main(args) -> int:
    import time
    import webbrowser

    from .harnesses.arch import harness as arch_def
    from .harnesses.arch.judge import make_judge
    from .harnesses.arch.session import ArchSession
    from .http_transport import HttpTransport
    from .permissions import PermissionBroker
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
        # arch mounts no gated tools (it designs, it doesn't touch the repo);
        # the broker is here for its two human phase gates — see
        # ArchSession.request_gate
        broker = PermissionBroker()
        runner = _make_runner(args, registry, spec, kg, recorder, harness="arch",
                              broker=broker)
        repl = Repl(runner, registry, kg, recorder, run_id)
        resumed_dir = None
        if getattr(args, "resume", None):
            resumed_dir = _resume_into_repl(repl, args.resume, registry)

        transport = HttpTransport(
            static_dir=arch_def.STATIC_DIR,
            stop_when=lambda e: e.get("type") == "arch_state" and e.get("phase") == "finalized",
            # finalize ends the session, not the reading of it: keep serving the
            # read-only design until the tab is closed (or half an hour passes).
            # Nothing can change any more — every tool is locked at `finalized`.
            linger=ARCH_LINGER_SECONDS,
        )
        server = Server(repl, transport=transport, broker=broker)

        # the arch session: restored state on resume, persisted to THIS run
        if resumed_dir is not None:
            arch = ArchSession.load(resumed_dir)
            arch.run_dir = run_dir
        else:
            arch = ArchSession(run_dir=run_dir)  # opens on the sketch layer
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

        print(f"ox arch | model={spec.spec} | kg={'off' if args.no_kg else 'on'} | session={run_id}")
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
            # (we are only here once the page closed or the linger ran out)
            from .harnesses.arch.bundle import bundle_paths

            print("architecture finalized. handoff bundle:")
            for p in bundle_paths(run_dir):
                print(f"  {p}")
            print("next: ox code")
        return rc


def _lead_main(args) -> int:
    """One-shot lead: route the task (architect → code / code) and report.
    The interactive lead goes through _interactive_session, not here."""
    setup = _setup(args)
    if isinstance(setup, int):
        return setup
    registry, spec, kg, build_proc, run_id, run_dir = setup

    with SessionRecorder(run_dir) as recorder:
        runner = _make_runner(
            args, registry, spec, kg, recorder,
            harness="lead", tools=lead_harness_tools(with_kg=not args.no_kg),
            # the lead mounts nothing gated, but its `code` dispatch inherits
            # this broker through the forked ctx
            broker=_headless_broker(args),
        )
        attach_printer(runner.ctx)
        print(f"ox lead | model={spec.spec} | kg={'off' if args.no_kg else 'on'} | session={run_id}")
        if build_proc is not None:
            print("kg: building in background")
        result = runner.run(args.task)

    print(f"\n[{result.status}] {result.summary}")
    return 0 if result.status == "done" else 1


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
    """The pi-tui frontend in the ox source tree, or None if not installed."""
    tui_dir = Path(__file__).resolve().parents[2] / "tui"
    if (tui_dir / "package.json").is_file() and (tui_dir / "node_modules").is_dir():
        return tui_dir
    return None


def _tui_main(args, harness: str = "code") -> int:
    """Exec the pi-tui frontend; it spawns `ox serve` back over stdio for
    the actual harness. The harness selection flows through to that serve."""
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
    if harness != "code":
        cmd += ["--harness", harness]
    if getattr(args, "from_arch", None):
        cmd += ["--from-arch", args.from_arch]
    return subprocess.run(cmd, cwd=tui_dir).returncode


if __name__ == "__main__":
    sys.exit(main())
