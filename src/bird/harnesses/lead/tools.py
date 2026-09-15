"""The lead's dispatch tools: architect, design, and code.

Each spins up a sub-harness through the registry and hands back a compact
receipt — the heavy handoff document (the finalized architecture, or the
design bundle's DESIGN.md) flows *laterally* to code via ctx.last_bundle,
never through the lead's own context. Deps (registry, run_dir) ride on
ToolContext, so the tools stay dep-free like every other tool in the shared
toolbox.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ...engine.session import new_run_id
from ...tools.base import Tool, ToolContext, ToolError, ToolResult


def _sub_run_dir(ctx: ToolContext, kind: str):
    base = ctx.run_dir if ctx.run_dir is not None else ctx.repo_root / ".bird" / "sessions"
    return base / f"{kind}-{new_run_id()}"


def _dispatch_debrief(task: str, result: Any, max_paths: int = 25) -> str:
    """What a failed code session paid for, in a form the next one can use.

    The fork owns its transcript and it dies with the fork, so a retry starts
    from zero and re-reads every file the dead run read — the single most
    expensive thing a lead can do (one logged pair of dispatches spent 1.3M
    tokens exploring, then 0.68M re-exploring the same files). Two things
    survive summarising: which files were already opened, and where the run had
    got to when it ran out of room.
    """
    paths: list[str] = []
    for m in getattr(result, "messages", None) or []:
        for tc in getattr(m, "tool_calls", None) or []:
            if tc.name not in ("read", "grep", "glob"):
                continue
            try:
                args = json.loads(tc.arguments_json)
            except Exception:  # a malformed call taught the next run nothing
                continue
            raw = args.get("path") or args.get("paths") or args.get("pattern")
            for p in [raw] if isinstance(raw, str) else (raw or []):
                if p and p not in paths:
                    paths.append(p)
    last_text = ""
    for m in reversed(getattr(result, "messages", None) or []):
        if m.role == "assistant" and (m.content or "").strip():
            last_text = (m.content or "").strip()
            break

    lines = [
        f"[a previous attempt at this task ended: {result.status} after {result.turns} turns]",
        f"It was given: {task}",
    ]
    if paths:
        lines.append(
            "Files/patterns it had already opened — do not re-read these unless you "
            "specifically need them: " + ", ".join(paths[:max_paths])
        )
    if last_text:
        lines.append("Where it had got to:\n" + last_text[:1500])
    lines.append(
        "Start from this rather than from zero, and do not repeat the search it "
        "already did."
    )
    return "\n".join(lines)


def _seed_for(ctx: ToolContext) -> str | None:
    """The stable context a sub-session starts from: what this session has
    already established, then the architecture handoff (or the debrief a failed
    attempt left behind). Both land in the system prompt, so both survive
    compaction for the whole life of the fork."""
    parts = []
    if ctx.store is not None:
        parts.append(ctx.store.render(ctx.repo_root))
    parts.append(ctx.last_bundle)
    return "\n\n".join(p for p in parts if p) or None


def _require(ctx: ToolContext) -> None:
    if ctx.registry is None:
        raise ToolError("lead tools need a registry on the context (internal wiring bug)")


class ArchitectTool(Tool):
    name = "architect"
    description = (
        "Design the architecture for a feature or system before it is built. Opens a "
        "design conversation with the user — they and the architect walk the design "
        "tree together — and produces a handoff the builder works from. For a new "
        "feature, the lead should first ask the user whether they want the design "
        "session or to skip straight to coding; call this only when the user wants it "
        "(or is ambiguous and leans toward design). The resulting design is passed to "
        "`code` automatically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The user's full description of what to design — do not summarize.",
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        _require(ctx)
        from ..arch.run import run_arch_interactive
        from ..handoff import bundle_md_path, seed_from_md

        run_dir = _sub_run_dir(ctx, "arch")
        # Architecture ALWAYS opens the Workbench with the user in the room.
        # There is deliberately no headless path here: nothing moves forward to
        # code until the user has said the design is done.
        ctx.emit("dispatch", {"harness": "arch", "task": args["task"], "run_dir": str(run_dir)})
        arch = run_arch_interactive(
            repo_root=ctx.repo_root, task=args["task"], registry=ctx.registry,
            client=ctx.client, run_dir=run_dir, kg=ctx.kg,
            on_status=lambda m: ctx.emit("dispatch_status", {"message": m}),
            # the page can't answer gates; the lead's broker (the TUI) can
            broker=ctx.broker,
            # arch builds its own ToolContext, so unlike `code` it inherits
            # nothing by reference — hand the store over explicitly
            store=ctx.store,
        )
        if not arch.state.handed_off:
            raise ToolError(
                "the architecture session ended without a handoff — the user did not "
                "say the design was done (they may have closed the page). Do NOT "
                "proceed to code. Ask them whether to reopen the design session to "
                "finish it, or how they want to proceed."
            )
        md_path = bundle_md_path(run_dir)
        ctx.last_bundle = seed_from_md(md_path.read_text(encoding="utf-8"))
        boxes = list(arch.state.nodes)
        headline = arch.state.brief.goal or args["task"][:60]
        return ToolResult(
            output=(
                f"Design handed off: '{headline}' — {len(boxes)} component(s) "
                f"({', '.join(boxes) or 'none'}). Bundle at {md_path.parent}. "
                "Now call `code` to build it."
            ),
            details={
                "harness": "arch",
                "handed_off": True,
                "components": boxes,
                "run_dir": str(run_dir),
            },
        )


def _design_left_open(design: Any, run_dir: Path) -> str:
    """The result of a design session that ended without finalize.

    The page closing is the one way this happens now (the transport ends the
    session when the room stays empty), so say what the user walked away
    from and that nothing is lost: the board is on disk and `resume` reopens
    it. The old wording only guessed ("they may have closed the page") and
    the reopen it suggested did not exist."""
    state = design.state
    boards = list(state.artboards.values())
    focus = (
        getattr(state, "showcase_artboard", "")
        or getattr(state, "selected", "")
        or (boards[-1].id if boards else "")
    )
    where = f"; the artboard in focus was '{focus}'" if focus else ""
    return (
        "the design session ended without a finalize — the page was closed (or lost "
        f"its connection) with {len(boards)} artboard(s) on the board{where}. Nothing "
        f"was handed to code. Everything is saved under {run_dir}: call `design` again "
        f'with resume="{run_dir}" to reopen it where it left off. Do NOT proceed to '
        "code — ask the user whether to reopen the design session or how they want to "
        "proceed."
    )


class DesignTool(Tool):
    name = "design"
    description = (
        "Design visual/UI work — a landing page, a dashboard, a component's look — "
        "in the browser design Workbench with the user in the room. The user and the "
        "designer iterate on rendered artboards; finalize produces a handoff bundle "
        "(DESIGN.md with the final HTML) that a `code` session can build from. Use "
        "for look-and-feel work where seeing it beats describing it; use `architect` "
        "for system structure instead. The resulting design is passed to `code` "
        "automatically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The user's full description of what to design — do not summarize.",
            },
            "resume": {
                "type": "string",
                "description": (
                    "The run_dir of an earlier design session to reopen where it left "
                    "off (an unfinalized result names it). Omit to start fresh."
                ),
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        _require(ctx)
        from ..design.handoff import bundle_md_path
        from ..design.run import run_design_interactive
        from ..design.session import STATE_FILENAME

        resume = str(args.get("resume") or "").strip()
        if resume:
            run_dir = Path(resume)
            if not (run_dir / STATE_FILENAME).is_file():
                raise ToolError(
                    f"nothing to resume at {run_dir} — no {STATE_FILENAME} there. Start "
                    "fresh (no `resume`) or check the path the earlier result named."
                )
        else:
            run_dir = _sub_run_dir(ctx, "design")
        # Design ALWAYS opens the Workbench with the user in the room — there is
        # no headless path: a design conversation with nobody there has nothing
        # to finalize.
        ctx.emit("dispatch", {
            "harness": "design", "task": args["task"], "run_dir": str(run_dir),
            "resume": bool(resume),
        })
        design = run_design_interactive(
            repo_root=ctx.repo_root, prompt=args["task"], registry=ctx.registry,
            client=ctx.client, run_dir=run_dir, kg=ctx.kg,
            on_status=lambda m: ctx.emit("dispatch_status", {"message": m}),
            # the page can't answer gates; the lead's broker (the TUI) can.
            # No `store`: design builds its own ToolContext without one, so
            # unlike `arch` there is nothing to hand over explicitly.
            broker=ctx.broker,
            resume=run_dir if resume else None,
        )
        if not design.state.finalized:
            raise ToolError(_design_left_open(design, run_dir))
        md_path = bundle_md_path(run_dir)
        if not md_path.is_file():
            raise ToolError(
                f"the design finalized but no bundle was written at {md_path} — there is "
                "nothing to hand the builder. Do NOT proceed to code; tell the user the "
                "design was not saved and ask how they want to proceed."
            )
        # raw DESIGN.md, not seed_from_md: that header is arch-specific ("a prior
        # architecture session..."). The design doc is already a readable spec.
        ctx.last_bundle = md_path.read_text(encoding="utf-8")
        chosen = design.state.finalized_artboard
        artboard = design.state.artboards.get(chosen)
        title = artboard.title if artboard else args["task"][:60]
        versions = len(artboard.versions) if artboard else 0
        return ToolResult(
            output=(
                f"Design finalized: '{title}' — {versions} version(s). "
                f"Bundle at {md_path.parent}. Now call `code` to build it."
            ),
            details={
                "harness": "design",
                "finalized": True,
                "artboard": chosen,
                "run_dir": str(run_dir),
            },
        )


class CodeTool(Tool):
    name = "code"
    description = (
        "Build or implement a task in the repository. If `architect` just finalized a "
        "design, it is provided to this sub-session as the authoritative spec "
        "automatically. Use `code` directly (without `architect`) for localized "
        "changes or bug fixes, and also for a new feature when the user explicitly "
        "chose to skip architecture — in that case the code harness explores, calls "
        "`plan` once, and implements from that pinned plan tracker."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "What to build or change."},
        },
        "required": ["task"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        _require(ctx)
        from ...llm.types import Usage
        from ..registry import build_runner

        spec = ctx.registry.resolve("default")
        seed = _seed_for(ctx)
        # fork the ctx: the sub-session gets its own plan/arch/bundle so it can
        # never clobber the lead's pinned state; repo/kg/record/client are shared.
        # pending_input is dropped on purpose — a message the user typed while
        # watching the lead is addressed to the lead, and letting the fork drain
        # it would bury the correction in a sub-transcript the user never sees.
        # The lead picks it up at its own next step, once this dispatch returns.
        # NOTE: `store` is deliberately NOT reset here. replace() is shallow, so
        # the fork shares the parent's store object — which is what lets the
        # sub-session's findings flow back to the lead and on to the next
        # dispatch instead of dying with the fork.
        child = replace(ctx, plan=None, arch=None, design=None, last_bundle=None,
                        pending_input=None)
        inner_record = child.record  # session log tee (None in headless tests)

        # The sub-session runs inside this turn, so its spend must land in the
        # parent's session total while the turn is still open — the parent
        # pump adds it on top of whatever the runner reports around the
        # dispatch. Each assistant call's usage rides the runner's record
        # (already on the transcript's wire event), folds cumulatively onto
        # the fork's ctx, and a "usage_notify" carries it up to the parent.
        def sub_record(event_type: str, data: dict[str, Any]) -> None:
            if inner_record is not None:
                inner_record(event_type, data)
            if event_type == "assistant":
                child.usage += Usage(
                    int(data.get("input_tokens", 0) or 0),
                    int(data.get("output_tokens", 0) or 0),
                )
                ctx.emit(
                    "usage_notify",
                    {"input_tokens": child.usage.input_tokens, "output_tokens": child.usage.output_tokens},
                )

        child.record = sub_record
        child.usage = Usage()

        runner = build_runner(
            "code",
            spec=spec,
            client=ctx.client,
            registry=ctx.registry,
            ctx=child,
            with_kg=ctx.kg is not None,
            seed_context=seed,
        )
        ctx.emit("dispatch", {"harness": "code", "task": args["task"], "seeded": seed is not None})
        result = runner.run(args["task"])
        # best-effort background KG refresh so the graph reflects the edits the
        # code session just made. Non-blocking (subprocess), and a stale graph
        # is never a failure — swallow everything.
        if ctx.kg is not None:
            try:
                proc = ctx.kg.ensure_background()
                if proc is not None:
                    ctx.emit("dispatch_status", {"message": "kg: refreshing in background after code session"})
            except Exception:
                pass  # best-effort; a stale graph is not a failure
        # consume the bundle once — a later code call is a fresh task, not a re-build
        ctx.last_bundle = None
        # A run that hit the turn cap or tripped a stuck guard finished nothing,
        # and used to come back as a successful tool result: the lead read
        # "[max_turns] ..." as an answer and re-dispatched the SAME task string,
        # paying for the whole exploration twice and learning nothing. Failure
        # has to look like failure.
        failed = result.status in ("max_turns", "aborted_stuck", "aborted_invalid_tool")
        if failed:
            # Hand the next attempt what this one paid for. Without it the retry
            # re-reads the same files from zero — the single most expensive
            # thing a lead can do. An architect bundle that was in flight rides
            # along behind it: the design is still the design, the debrief is
            # just what the failed attempt learned about applying it.
            debrief = _dispatch_debrief(args["task"], result)
            ctx.last_bundle = f"{debrief}\n\n{seed}" if seed else debrief
        return ToolResult(
            is_error=failed,
            output=(
                f"[{result.status}] {result.summary}"
                if not failed
                else f"[{result.status}] the code session ended WITHOUT finishing: {result.summary}. "
                f"It ran {result.turns} turns and its findings are seeded into the next `code` call. "
                f"Do not re-send the same task — narrow it, or say what the next attempt should skip."
            ),
            details={
                "harness": "code",
                "status": result.status,
                "turns": result.turns,
                "seeded": seed is not None,
                # the dispatch's own spend, as the child ctx recorded it —
                # identical to result.usage unless a record tee dropped calls
                "input_tokens": child.usage.input_tokens,
                "output_tokens": child.usage.output_tokens,
            },
        )
