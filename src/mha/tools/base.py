"""Tool plumbing: dual results, execution context, base class.

Every tool returns a ToolResult with `output` (the string the model sees)
and `details` (structured data for the session log / future UI) — pi's dual
output. Schemas are hand-written JSON Schema dicts on each tool class; they
are exactly what the model sees, so keep them lean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..llm.types import ToolSpec

MAX_OUTPUT_CHARS = 30_000


@dataclass
class ToolResult:
    output: str
    details: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False

    def clipped(self) -> "ToolResult":
        if len(self.output) <= MAX_OUTPUT_CHARS:
            return self
        return ToolResult(
            output=self.output[:MAX_OUTPUT_CHARS]
            + f"\n... [truncated {len(self.output) - MAX_OUTPUT_CHARS} chars]",
            details={**self.details, "truncated_from": len(self.output)},
            is_error=self.is_error,
        )


@dataclass
class ToolContext:
    repo_root: Path
    kg: Any | None = None  # context.kg.KG once built; duck-typed to avoid import cycle
    plan: Any | None = None  # tools.plan.PlanState once the model calls plan
    record: Callable[[str, dict], None] | None = None  # session event sink
    bash_categories: tuple[str, ...] = ("search", "test", "lint", "git_read")
    client: Any | None = None  # llm.wire.openai_compat.OpenAICompatClient — used by web_fetch to ask the model
    skills: list[Any] | None = None  # skills.Skill list; None = no skills loaded
    arch: Any | None = None  # harnesses.arch.session.ArchSession in arch sessions
    # lead-harness wiring: the lead's dispatch tools spin up sub-harnesses, so
    # they need the registry to resolve models and a dir to nest sub-sessions
    # under; last_bundle is the arch->code seam (the finalized design, stashed
    # by `architect` and seeded into the `code` sub-session)
    registry: Any | None = None  # llm.registry.Registry
    run_dir: Path | None = None  # this session's dir; sub-sessions nest beneath it
    last_bundle: str | None = None  # seed_context handed from architect to code
    # permissions.Broker — duck: .request(payload) -> (approved, feedback).
    # build_runner wraps every requires_permission tool with it, so a sub-harness
    # forked from this ctx inherits the gate. None = ungated (library/test use);
    # every cli.py entry point sets one.
    broker: Any | None = None

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self.record:
            self.record(event_type, data)

    def resolve_path(self, path: str) -> Path:
        """Resolve a model-supplied path and confine it to the repo root."""
        p = (self.repo_root / path).resolve()
        root = self.repo_root.resolve()
        if p != root and root not in p.parents:
            raise ToolError(f"path '{path}' escapes the repository root")
        return p


class ToolError(Exception):
    """Raised by tools for model-visible failures; runner turns it into an error result."""


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    # True on anything that can change the user's repo. build_runner wraps
    # these with the session's permission broker. Declared here rather than in
    # a name list elsewhere so a new mutating tool is gated by default, at the
    # point someone writes it.
    requires_permission: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """run() with ToolError converted to a model-visible error result."""
        try:
            return self.run(args, ctx).clipped()
        except ToolError as e:
            return ToolResult(output=f"Error: {e}", details={"error": str(e)}, is_error=True)
