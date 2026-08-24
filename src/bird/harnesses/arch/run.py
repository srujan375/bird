"""Running an arch session.

`run_arch_interactive` is what the lead's `architect` tool uses: it opens the
browser Workbench and blocks until the user is done — the same experience as
`bird arch`, but returning the finalized ArchSession to the caller instead of
exiting. The caller checks `.state.handed_off`.

`run_arch_headless` runs the walk with no page and nobody in the room. It is a
TEST utility for exercising the arch loop and the lead seam; a design
conversation with no user is not the product.

Neither takes a broker any more. The old harness had two human gates to block
on (top-level approval, finalize); this one has none — the session ends when
the user says so, in the conversation, which is not a modal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ...llm.registry import Registry
from ...tools import ToolContext
from ..registry import build_runner
from .session import ArchSession

# what tells the transport the session is over
HANDED_OFF = lambda e: e.get("type") == "arch_state" and e.get("status") == "handed_off"  # noqa: E731


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
    """Open the browser Workbench, run the session, and block until the user
    hands the design off (or closes the page). Mirrors cli._arch_main's
    bring-up; kept separate so `bird arch`'s resume path stays untouched."""
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
            stop_when=HANDED_OFF,
            # no linger here, unlike `bird arch`: the lead is blocked on this
            # call and has a build to start.
        )
        server = Server(repl, transport=transport)

        arch = ArchSession(run_dir=run_dir)

        def on_state(payload: dict) -> None:
            recorder.event(
                "arch_state",
                {"status": payload["status"], "changed": payload.get("changed")},
            )
            transport.emit(payload)

        arch.on_state = on_state
        ctx.arch = arch

        url = transport.url
        banner = f"architecture Workbench — design with the architect at {url}"
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
            server.run()  # blocks until handoff (stop_when) or the page disconnects
        except KeyboardInterrupt:
            transport.shutdown()
        time.sleep(0.3)  # let SSE clients drain the closing events
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
    with_web: bool = True,
) -> ArchSession:
    """Design `task` with nobody in the room. Returns the ArchSession; the
    caller checks `.state.handed_off` and reads the bundle from `run_dir`.

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
    if record is not None:
        arch.on_state = lambda payload: record(
            "arch_state", {"status": payload["status"], "changed": payload.get("changed")}
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
