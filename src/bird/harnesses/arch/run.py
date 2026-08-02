"""Running an arch session.

`run_arch_interactive` is what the lead's `architect` tool ALWAYS uses in the
product: it opens the browser Workbench with the two human gates (top-level
approval, finalize) and blocks until the user finalizes — the same experience
as `bird arch`, but returning the finalized ArchSession to the caller instead of
exiting. Architecture never advances to code without explicit user approval, so
there is deliberately no auto-approve dispatch path.

`run_arch_headless` runs the arch walk with no browser and no broker (both gates
auto-approve). It exists only as a TEST utility for exercising the arch walk /
the lead seam without a browser — it is NOT wired into any user-facing command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ...llm.registry import Registry
from ...tools import ToolContext
from ..registry import build_runner
from .judge import make_judge
from .session import ArchSession


def run_arch_interactive(
    *,
    repo_root: Path,
    task: str,
    registry: Registry,
    client: Any,
    run_dir: Path,
    kg: Any | None = None,
    model: str = "architect",
    no_open: bool = False,
    on_status: Callable[[str], None] | None = None,
) -> ArchSession:
    """Open the browser Workbench, run the arch session with its two human
    gates, and block until the user finalizes (or closes the page). Returns the
    ArchSession — the caller checks `.state.phase == "finalized"`. Mirrors
    cli._arch_main's bring-up; kept separate so `bird arch`'s resume path stays
    untouched."""
    import sys
    import time
    import webbrowser

    from ...engine.session import SessionRecorder
    from ...http_transport import HttpTransport
    from ...repl import Repl
    from ...serve import Server
    from . import harness as arch_def

    spec = registry.resolve(model)
    with SessionRecorder(run_dir) as recorder:
        ctx = ToolContext(
            repo_root=repo_root, kg=kg, record=recorder.event,
            client=client, registry=registry, run_dir=run_dir,
        )
        runner = build_runner(
            "arch", spec=spec, client=client, registry=registry,
            ctx=ctx, with_kg=kg is not None,
        )
        repl = Repl(runner, registry, kg, recorder, run_dir.name)
        transport = HttpTransport(
            static_dir=arch_def.STATIC_DIR,
            stop_when=lambda e: e.get("type") == "arch_state" and e.get("phase") == "finalized",
            # no linger here, unlike `bird arch`: the lead is blocked on this
            # call and has a build to start. The page keeps its finalized
            # read-only view (finalized takes precedence over disconnected).
        )
        server = Server(repl, transport=transport)  # wires ctx.record -> transport + gates

        arch = ArchSession(run_dir=run_dir)  # opens on the sketch layer
        arch.broker = server.broker
        arch.judge = make_judge(registry, client)

        def on_state(payload: dict) -> None:
            recorder.event("arch_state", {"phase": payload["phase"], "changed": payload.get("changed")})
            transport.emit(payload)

        arch.on_state = on_state
        ctx.arch = arch

        url = transport.url
        banner = f"architecture Workbench — review and approve at {url}"
        if on_status is not None:
            on_status(banner)
        # also to stderr: the TUI surfaces `bird serve` stderr as a notice, so the
        # URL stays visible even if the auto-open is blocked (sandbox, no $BROWSER)
        print(banner, file=sys.stderr, flush=True)
        if not no_open:
            try:
                webbrowser.open(url)
            except Exception:
                print(f"could not auto-open a browser — open {url} yourself",
                      file=sys.stderr, flush=True)
        server.on_user_input(task)
        try:
            server.run()  # blocks until finalize (stop_when) or the page disconnects
        except KeyboardInterrupt:
            transport.shutdown()
        time.sleep(0.3)  # let SSE clients drain the finalized/bye events
        return arch


def run_arch_headless(
    *,
    repo_root: Path,
    task: str,
    registry: Registry,
    client: Any,
    run_dir: Path,
    kg: Any | None = None,
    record: Callable[[str, dict], None] | None = None,
    model: str = "architect",
    max_turns: int = 40,
    critic: bool = True,
    with_web: bool = True,
) -> ArchSession:
    """Design `task` to a finalized bundle. Returns the ArchSession; the caller
    checks `.state.phase == "finalized"` and reads the bundle from `run_dir`.

    `critic=False` is the control arm: the second model that reviews the design
    is simply absent, so nothing files a Concern the architect didn't think of.
    `with_web=False` drops web_search/web_fetch, so a measured run can't
    substitute a lucky search for design judgement.
    """
    spec = registry.resolve(model)
    ctx = ToolContext(
        repo_root=repo_root,
        kg=kg,
        record=record,
        client=client,
        registry=registry,
        run_dir=run_dir,
    )
    arch = ArchSession(run_dir=run_dir)
    arch.broker = None  # no broker -> request_gate auto-approves both gates
    arch.judge = make_judge(registry, client) if critic else None
    if record is not None:
        arch.on_state = lambda payload: record(
            "arch_state", {"phase": payload["phase"], "changed": payload.get("changed")}
        )
    ctx.arch = arch

    runner = build_runner(
        "arch",
        spec=spec,
        client=client,
        registry=registry,
        ctx=ctx,
        max_turns=max_turns,
        with_kg=kg is not None,
        with_web=with_web,
    )
    runner.run(task)
    return arch
