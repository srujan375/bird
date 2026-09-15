"""Running a design session.

`run_design_interactive` opens the browser design Workbench and blocks until
the user finalizes the design (or closes the page) — the same experience as
`bird arch`, returning the finalized DesignSession to the caller. The caller
reads `.state.finalized_artboard` for the chosen artboard.

Modelled on arch/run.py's interactive path, minus the headless variant: a
design conversation with nobody in the room has nothing to finalize, so there
is no headless form. The session ends when the user says so — `design_finalize`
is the modal-free gate, same as arch's handoff.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ...llm.registry import Registry
from ...skills import load_skills
from ...tools import ToolContext
from ..registry import build_runner
from .critique import Critic
from .intake import opening_message
from .session import STATE_FILENAME, DesignSession, STATUS_FINALIZED

# the design harness's own static page (the Workbench)
STATIC_DIR = Path(__file__).parent / "static"

# what tells the transport the session is over
FINALIZED = lambda e: e.get("type") == "design_state" and e.get("status") == STATUS_FINALIZED  # noqa: E731

# How long an empty room lasts before the session ends (HttpTransport's
# close_when_empty). The page closing is the only other way a design session
# ends — there is no timeout and the dispatching turn is blocked on it — and
# until this existed a closed tab left that turn hung forever: all 16 logged
# design dispatches ended that way, none with a word to the lead. Long enough
# for the page's reconnect backoff after a dropped SSE stream; short enough
# that a closed tab hands the turn back within about half a minute.
CLOSE_WHEN_EMPTY_SECONDS = 20.0


def run_design_interactive(
    *,
    repo_root: Path,
    prompt: str,
    registry: Registry,
    client: Any,
    run_dir: Path,
    kg: Any | None = None,
    model: str = "designer",
    no_open: bool = False,
    on_status: Callable[[str], None] | None = None,
    broker: Any | None = None,
    resume: Path | None = None,
) -> DesignSession:
    """Open the browser design Workbench, run the session, and block until the
    user finalizes the design (or closes the page). Mirrors run_arch_interactive's
    bring-up: ToolContext -> build_runner -> Repl -> HttpTransport + Server, with
    a DesignSession wired in as `ctx.design` so the design tools can reach it.

    `broker` is the caller's permission broker, same contract as arch: the
    Workbench page renders no permission prompts, so any gate must be answered
    by the caller's UI. Without one the session falls back to a page-bound
    broker and hangs on the first gate.

    `resume` reopens an earlier session's run dir where it left off: the board
    (design_state.json + the version files) comes back through
    DesignSession.load, the transcript the previous process saved comes back
    into the Repl, and `run_dir` is ignored in favour of the old one. A page
    closed by accident, or a session that died at the plan step, used to be
    gone for good — the lead's "ask whether to reopen" had nothing behind it."""
    import sys
    import time
    import webbrowser

    from ...engine.session import SessionRecorder, load_messages
    from ...http_transport import HttpTransport
    from ...llm.types import Message
    from ...repl import Repl
    from ...serve import Server

    if resume is not None:
        run_dir = Path(resume)
        if not (run_dir / STATE_FILENAME).is_file():
            raise FileNotFoundError(f"nothing to resume at {run_dir}: no {STATE_FILENAME} there")

    spec = registry.resolve(model)
    with SessionRecorder(run_dir) as recorder:
        ctx = ToolContext(
            repo_root=repo_root, kg=kg, record=recorder.event,
            client=client, registry=registry, run_dir=run_dir,
            broker=broker,
            # cli._make_runner populates this for code/lead; a design session
            # builds its own ctx here and used to skip it — the designer's
            # skill tool then answered {"available": []} for design-craft and
            # the whole session ran without the house style (the live session
            # whose plan critic caught what the skill would have prevented).
            skills=load_skills(repo_root),
        )
        runner = build_runner(
            "design", spec=spec, client=client, registry=registry,
            ctx=ctx, with_kg=kg is not None,
        )
        repl = Repl(runner, registry, kg, recorder, run_dir.name)
        resumed_turns = 0
        if resume is not None:
            # the transcript the previous process saved after every turn
            # (serve writes messages.jsonl). Its system prompt goes: the new
            # runner rebuilds a current one on the first turn, same as the
            # CLI's --resume.
            msgs = [Message.from_dict(r) for r in (load_messages(run_dir) or [])]
            if msgs and msgs[0].role == "system":
                msgs = msgs[1:]
            repl.messages = msgs
            resumed_turns = sum(1 for m in msgs if m.role == "assistant")
            recorder.event(
                "resume", {"from": run_dir.name, "messages": len(msgs), "via": "design"}
            )
        transport = HttpTransport(
            static_dir=STATIC_DIR,
            stop_when=FINALIZED,
            # no linger: the caller is blocked on this call. The empty-room
            # close is what ends it when the page goes away instead.
            close_when_empty=CLOSE_WHEN_EMPTY_SECONDS,
        )
        server = Server(repl, transport=transport, broker=broker)

        # intake_gate: there is a person at the workbench, so the direction
        # and the design system are asked before anything is drawn (a resumed
        # board carries its answers, so nothing is asked twice)
        if resume is not None:
            design = DesignSession.load(run_dir, intake_gate=True, repo_root=repo_root)
        else:
            design = DesignSession(run_dir=run_dir, intake_gate=True, repo_root=repo_root)
        # The critique loop's two halves, installed here because this is the
        # one path with a page to capture from. The critic is the vision model
        # behind the 'vision' alias — None when it does not resolve, and every
        # critique then degrades to a skip note. The capture hook emits the
        # capture_request over SSE; headless sessions leave it None and the
        # render critique skips while the plan critique still runs.
        try:
            design.critic = Critic(client, registry.resolve("vision"), run_dir=run_dir)
        except Exception:  # no 'vision' alias (or a broken registry): skip, don't fail
            design.critic = None
        design.capture_hook = lambda payload: transport.emit(
            {"type": "capture_request", **payload})

        def on_state(payload: dict) -> None:
            recorder.event(
                "design_state",
                {"status": payload["status"], "changed": payload.get("changed")},
            )
            transport.emit(payload)  # SSE push to the page

        design.on_state = on_state
        ctx.design = design
        # opens the board: sets the brief on the state the handoff reports, and
        # gives the page its first design_state — without one the Workbench sits
        # on an empty status pill with Finalize disabled, whatever the model does.
        # A resumed board is already open: the page gets what is there.
        if design.state.prompt:
            design.on_state(design.state_event())
        else:
            design.start(prompt)

        url = transport.url
        banner = f"design Workbench — design with the designer at {url}"
        if on_status is not None:
            on_status(banner)
        # also to stderr: the URL stays visible even if the auto-open is blocked
        print(banner, file=sys.stderr, flush=True)
        if not no_open:
            try:
                webbrowser.open(url)
            except Exception:
                print(f"could not auto-open a browser — open {url} yourself",
                      file=sys.stderr, flush=True)
        # The brief waits behind the intake. Answering the last question is
        # what dispatches it (serve.on_answer), composed into the opening
        # message the design instructions know how to read — so the designer
        # never starts in a fidelity nobody chose. A resumed session whose
        # designer already had a turn picks up from the transcript instead;
        # one that never got that far (it died at the plan step) is opened
        # with the same composed message on_answer would have sent.
        if design.pending_ask() is None and resumed_turns == 0:
            if resume is not None:
                server.on_user_input(
                    opening_message(design.state.intake, design.state.prompt or prompt)
                )
            else:
                server.on_user_input(prompt)
        try:
            server.run()  # blocks until finalize (stop_when) or the room empties
        except KeyboardInterrupt:
            transport.shutdown()
        time.sleep(0.3)  # let SSE clients drain the closing events
        return design