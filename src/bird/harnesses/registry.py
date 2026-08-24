"""The harness registry: a harness as a named, constructible thing.

Before this, "which harness" was a hand-inlined fork in cli.py — code got the
defaults, arch got a pile of keyword arguments. A HarnessDef bundles that
tuning (instructions + toolset factory + engine knobs) behind a name, and
build_runner is the one construction path the CLI *and* the lead's dispatch
tools go through. The engine still knows no harness by name; this module is
the only place that maps a name to its wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..engine.runner import MAX_TURNS, Runner
from ..llm.registry import ModelSpec, Registry
from ..llm.wire.openai_compat import OpenAICompatClient
from ..tools import Tool, ToolContext


@dataclass(frozen=True)
class HarnessDef:
    name: str
    tools: Callable[..., list[Tool]]  # (with_kg, with_web) -> toolset
    instructions_path: Path
    default_model: str = "default"
    # engine tuning — None means "use the engine's code-harness default"
    mutating_tools: frozenset[str] | None = None
    tracker: Callable[[ToolContext], str | None] | None = None
    tracker_prefix: str | None = None
    explore_nudge: str | None = None
    # which tool ends a session. None = the engine's default ("done"); arch
    # names its own, because handing a design off is not finishing a task.
    done_tool: str | None = None
    # arch opens a browser and expects a person in the room; a harness with this
    # set is not something to dispatch headless and call a design session
    interactive: bool = False
    # `done` refuses to close over files edited since the last passing check.
    # Only for harnesses that edit the repo and can run its tests — arch and
    # lead mutate through other means and have nothing to check.
    require_verification: bool = False


def _code_def() -> HarnessDef:
    from .code import INSTRUCTIONS_PATH, code_harness_tools

    return HarnessDef(
        name="code",
        tools=code_harness_tools,
        instructions_path=INSTRUCTIONS_PATH,
        default_model="default",
        require_verification=True,
    )


def _arch_def() -> HarnessDef:
    from . import arch as _  # noqa: F401  (ensure package import)
    from .arch import harness as a
    from .arch.tools import MUTATING_TOOLS, arch_harness_tools

    return HarnessDef(
        name="arch",
        tools=arch_harness_tools,
        instructions_path=a.INSTRUCTIONS_PATH,
        default_model="architect",
        mutating_tools=MUTATING_TOOLS,
        tracker=a.arch_tracker,
        tracker_prefix=a.TRACKER_PREFIX,
        explore_nudge=a.EXPLORE_NUDGE,
        done_tool="handoff",
        interactive=True,
    )


def _lead_def() -> HarnessDef:
    from .lead import EXPLORE_NUDGE, INSTRUCTIONS_PATH, MUTATING_TOOLS, lead_harness_tools

    return HarnessDef(
        name="lead",
        tools=lead_harness_tools,
        instructions_path=INSTRUCTIONS_PATH,
        default_model="default",
        mutating_tools=MUTATING_TOOLS,
        explore_nudge=EXPLORE_NUDGE,
    )


# name -> lazy factory (lazy so importing the registry doesn't pull every
# harness package, and to keep import cycles impossible)
HARNESSES: dict[str, Callable[[], HarnessDef]] = {
    "code": _code_def,
    "arch": _arch_def,
    "lead": _lead_def,
}


def get(name: str) -> HarnessDef:
    try:
        return HARNESSES[name]()
    except KeyError:
        raise KeyError(f"unknown harness '{name}' (known: {', '.join(HARNESSES)})") from None


def build_runner(
    name: str,
    *,
    spec: ModelSpec,
    client: OpenAICompatClient,
    registry: Registry,
    ctx: ToolContext,
    max_turns: int = MAX_TURNS,
    with_kg: bool = True,
    with_web: bool = True,
    seed_context: str | None = None,
    tools: list[Tool] | None = None,
) -> Runner:
    """Construct a Runner tuned for the named harness. `tools` overrides the
    def's factory — used by harnesses (lead) whose tools need injected deps
    the factory can't supply from (with_kg, with_web) alone.

    Every mutating tool is wrapped with ctx.broker here, which is the only
    place that can cover *all* runners: the CLI's, and the ones the lead's
    `code` dispatch builds mid-session. Gating in the Server instead meant
    dispatched sub-sessions were born ungated.
    """
    from ..permissions import gate_tools

    d = get(name)
    resolved = tools if tools is not None else d.tools(with_kg=with_kg, with_web=with_web)
    resolved = gate_tools(resolved, ctx.broker)
    # the ledger is a property of the harness, and starts empty: a ctx forked
    # from a parent session (lead -> code) would otherwise inherit — by
    # reference — whatever the parent had already edited
    ctx.require_verification = d.require_verification
    ctx.unverified_paths = []
    ctx.last_verify = None
    ctx.done_blocked_once = False
    return Runner(
        spec=spec,
        client=client,
        registry=registry,
        tools=resolved,
        ctx=ctx,
        max_turns=max_turns,
        instructions_path=d.instructions_path,
        mutating_tools=d.mutating_tools,
        tracker=d.tracker,
        tracker_prefix=d.tracker_prefix,
        explore_nudge=d.explore_nudge,
        seed_context=seed_context,
        done_tool=d.done_tool,
    )
