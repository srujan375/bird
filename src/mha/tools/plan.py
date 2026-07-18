"""plan / plan_update — harness-owned task tracking (decision: option 2).

The model calls `plan` exactly once with its steps and the files each step
touches; the harness expands each step's blast radius through the knowledge
graph (2 hops, capped) so related files are visible without reading them,
then re-renders the tracker into the conversation every turn (pinned by the
runner, immune to compaction). `plan_update` moves the cursor. `done` is
blocked while steps are open (see done.py). This gives the runner a positive
progress signal instead of only negative stuck-guards, and starves the
restate-the-plan loop: the plan has one home and it is not assistant text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import Tool, ToolContext, ToolError, ToolResult

MAX_STEPS = 12
MAX_FILES_PER_STEP = 8
AFFECTED_DEPTH = 2
AFFECTED_LIMIT = 6

STATUS_GLYPHS = {"pending": "·", "in_progress": "…", "done": "✔", "skipped": "-"}
OPEN_STATUSES = ("pending", "in_progress")
UPDATE_STATUSES = ("in_progress", "done", "skipped")


@dataclass
class PlanStep:
    title: str
    files: list[str]
    affected: list[str] = field(default_factory=list)
    status: str = "pending"
    note: str = ""


@dataclass
class PlanState:
    steps: list[PlanStep]

    def current_index(self) -> int | None:
        for i, s in enumerate(self.steps):
            if s.status == "in_progress":
                return i
        for i, s in enumerate(self.steps):
            if s.status == "pending":
                return i
        return None

    def open_steps(self) -> list[int]:
        return [i for i, s in enumerate(self.steps) if s.status in OPEN_STATUSES]

    def advance(self) -> None:
        """If nothing is in progress, promote the first pending step."""
        i = self.current_index()
        if i is not None and self.steps[i].status == "pending":
            self.steps[i].status = "in_progress"

    def render(self) -> str:
        done = sum(1 for s in self.steps if s.status in ("done", "skipped"))
        cur = self.current_index()
        lines = [f"[plan tracker — pinned; {done}/{len(self.steps)} steps closed]"]
        for i, s in enumerate(self.steps):
            marker = "->" if i == cur else "  "
            glyph = STATUS_GLYPHS.get(s.status, "?")
            line = f"{marker} {i + 1}. {glyph} {s.title} — touch: {', '.join(s.files)}"
            if s.affected:
                line += f" | may affect: {', '.join(s.affected)}"
            if s.note:
                line += f" ({s.note})"
            lines.append(line)
        if cur is not None:
            lines.append(
                f"Work ONLY on step {cur + 1} and ONLY in its listed files — do not "
                "re-read files outside them. When it is complete, call plan_update "
                f'{{"step": {cur + 1}, "status": "done"}}. Do not restate this plan in text.'
            )
        else:
            lines.append("All steps closed — verify, then call done with a summary.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "title": s.title,
                    "files": s.files,
                    "affected": s.affected,
                    "status": s.status,
                    "note": s.note,
                }
                for s in self.steps
            ]
        }


def _blast_radius(ctx: ToolContext, files: list[str]) -> list[str]:
    """Files within AFFECTED_DEPTH graph hops of the step's own files. Best
    effort: no KG (or not ready yet) → empty, never an error."""
    kg = ctx.kg
    if kg is None:
        return []
    try:
        if not kg.is_ready():
            return []
        return kg.affected_files(files, depth=AFFECTED_DEPTH, limit=AFFECTED_LIMIT)
    except Exception:
        return []


class PlanTool(Tool):
    name = "plan"
    description = (
        "Set your implementation plan, once per task, after a SHORT exploration. "
        "Each step lists the files it will create or edit. The tracker is pinned "
        "into the conversation and updated as you work — never write a plan as "
        "plain text. Related files are attached automatically from the knowledge "
        "graph so you do not need to read everything first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_STEPS,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "One line: what this step does"},
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_FILES_PER_STEP,
                            "items": {"type": "string"},
                            "description": "Repo-relative paths this step creates or edits",
                        },
                    },
                    "required": ["title", "files"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["steps"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # a fully-closed plan belongs to a finished task — a new plan replaces
        # it; only an OPEN plan blocks re-planning
        if ctx.plan is not None and ctx.plan.open_steps():
            raise ToolError(
                "a plan already exists — do not re-plan. Use plan_update to change "
                "step status, or keep working on the current step."
            )
        steps = [
            PlanStep(
                title=s["title"].strip(),
                files=[f.strip() for f in s["files"]],
                affected=_blast_radius(ctx, s["files"]),
            )
            for s in args["steps"]
        ]
        plan = PlanState(steps=steps)
        plan.advance()
        ctx.plan = plan
        ctx.emit("plan_created", plan.to_dict())
        return ToolResult(
            output=plan.render() + "\nPlan recorded. Start step 1 now: make its first edit or write.",
            details=plan.to_dict(),
        )


class PlanUpdateTool(Tool):
    name = "plan_update"
    description = (
        "Update one plan step's status: in_progress, done, or skipped. Call this "
        "the moment a step is complete, before starting the next."
    )
    parameters = {
        "type": "object",
        "properties": {
            "step": {"type": "integer", "minimum": 1, "description": "1-based step number"},
            "status": {"type": "string", "enum": list(UPDATE_STATUSES)},
            "note": {"type": "string", "description": "Optional short note (e.g. why skipped)"},
        },
        "required": ["step", "status"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        plan: PlanState | None = ctx.plan
        if plan is None:
            raise ToolError("no plan exists yet — call plan first.")
        idx = args["step"] - 1
        if not 0 <= idx < len(plan.steps):
            raise ToolError(f"step {args['step']} does not exist (plan has {len(plan.steps)} steps).")
        step = plan.steps[idx]
        step.status = args["status"]
        if args.get("note"):
            step.note = args["note"].strip()
        plan.advance()
        ctx.emit("plan_updated", {"step": idx + 1, "status": step.status, **plan.to_dict()})
        return ToolResult(output=plan.render(), details=plan.to_dict())
