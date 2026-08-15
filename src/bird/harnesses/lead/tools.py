"""The lead's dispatch tools: architect and code.

Each spins up a sub-harness through the registry and hands back a compact
receipt — the heavy handoff document (the finalized architecture) flows
*laterally* from architect to code via ctx.last_bundle, never through the
lead's own context. Deps (registry, run_dir) ride on ToolContext, so the
tools stay dep-free like every other tool in the shared toolbox.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...engine.session import new_run_id
from ...tools.base import Tool, ToolContext, ToolError, ToolResult


def _sub_run_dir(ctx: ToolContext, kind: str):
    base = ctx.run_dir if ctx.run_dir is not None else ctx.repo_root / ".bird" / "sessions"
    return base / f"{kind}-{new_run_id()}"


def _require(ctx: ToolContext) -> None:
    if ctx.registry is None:
        raise ToolError("lead tools need a registry on the context (internal wiring bug)")


class ArchitectTool(Tool):
    name = "architect"
    description = (
        "Design the architecture for a feature or system before it is built. Runs an "
        "architecture session and produces a handoff design (components, contracts, "
        "decisions). For a new feature, the lead should first ask the user whether "
        "they want the full Workbench design session or to skip straight to coding; "
        "call this only when the user wants the Workbench (or is ambiguous and leans "
        "toward design). The resulting design is passed to `code` automatically."
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
        # Architecture ALWAYS opens the browser Workbench and blocks on the two
        # human gates. There is deliberately no auto-approve path: nothing moves
        # forward to code until the user explicitly finalizes the design.
        ctx.emit("dispatch", {"harness": "arch", "task": args["task"], "run_dir": str(run_dir)})
        arch = run_arch_interactive(
            repo_root=ctx.repo_root, task=args["task"], registry=ctx.registry,
            client=ctx.client, run_dir=run_dir, kg=ctx.kg,
            on_status=lambda m: ctx.emit("dispatch_status", {"message": m}),
        )
        phase = arch.state.phase
        if phase != "finalized":
            raise ToolError(
                f"the architecture session ended in phase '{phase}', not finalized — "
                "the user did not approve the design (they may have closed the page). "
                "Do NOT proceed to code. Ask the user whether to reopen the "
                "architecture Workbench to finish it, or how they want to proceed."
            )
        md_path = bundle_md_path(run_dir)
        ctx.last_bundle = seed_from_md(md_path.read_text(encoding="utf-8"))
        comps = list(arch.state.components)
        headline = arch.state.brief.goal or args["task"][:60]
        return ToolResult(
            output=(
                f"Architecture finalized: '{headline}' — {len(comps)} components "
                f"({', '.join(comps) or 'none'}). Handoff bundle at {md_path.parent}. "
                "Now call `code` to build it."
            ),
            details={
                "harness": "arch",
                "phase": "finalized",
                "components": comps,
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
        seed = ctx.last_bundle
        # fork the ctx: the sub-session gets its own plan/arch/bundle so it can
        # never clobber the lead's pinned state; repo/kg/record/client are shared
        child = replace(ctx, plan=None, arch=None, last_bundle=None)
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
        return ToolResult(
            output=f"[{result.status}] {result.summary}",
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
