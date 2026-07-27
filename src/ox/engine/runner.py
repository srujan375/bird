"""The Code harness ReAct loop.

Owns everything the adapter doesn't: tool-call validation with helpful-error
retries (2 per call, then structured abort — decision #7), stuck-model
guards (borrowed from vishwa), explicit done-tool termination, the 90%
compaction trigger, and the "KG now available" injection (decision #9).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..llm.registry import ModelSpec, Registry
from ..llm.types import Message, Usage
from ..llm.validate import validate_tool_call
from ..llm.wire.openai_compat import OnDelta, OpenAICompatClient
from ..tools import Tool, ToolContext
from ..skills import render_index
from .compactor import compact, needs_compaction

MAX_TURNS = 40
MAX_VALIDATION_RETRIES = 2  # per tool call chain
MAX_TEXT_ONLY_TURNS = 3
SAME_CALL_LOOP_THRESHOLD = 3
# read-only tools repeated with identical args are pure spinning, even when
# other calls are interleaved (mutating tools like bash/pytest may legitimately
# repeat between edits, so they are exempt)
READONLY_LOOP_TOOLS = {"read", "kg_query"}
READONLY_CALL_TOTAL_CAP = 6
MUTATING_TOOLS = {"edit", "write"}
EXPLORE_NUDGE_TURNS = 6
# bash searches allowed while the KG is ready before nudging back to kg_query;
# one kg miss must not turn into bash-for-the-rest-of-the-session
KG_DRIFT_NUDGE_SEARCHES = 3
DRIFT_SEARCH_COMMANDS = {"rg", "grep", "find"}

# the Code harness's instructions; becomes a Runner parameter when the next
# harness lands (the engine should not name a harness)
INSTRUCTIONS_PATH = Path(__file__).parents[1] / "harnesses" / "code" / "instructions.md"

KG_READY_NOTICE = (
    "[system notice] The knowledge graph has finished building — kg_query now "
    "returns real answers. Prefer it over bash search."
)
TEXT_ONLY_NUDGE = (
    "[system notice] Respond with a tool call. If the task is complete, call "
    "done with a summary."
)
EXPLORE_NUDGE = (
    "[system notice] {n} turns of reading without changing anything. Do not "
    "restate the plan. If the task requires changes, make the next edit/write "
    "NOW. If it needs no changes, give your answer (call done with a summary)."
)
PLAN_EXPLORE_NUDGE = (
    "[system notice] {n} turns without an edit/write. You are on step {num}: "
    "{title}. Edit or write its listed files NOW: {files}. Do not read "
    "anything else and do not restate the plan."
)
# the tracker message the runner re-renders each turn; must match the first
# line PlanState.render() produces
PLAN_TRACKER_PREFIX = "[plan tracker"
KG_DRIFT_NOTICE = (
    "[system notice] You are searching with bash while the knowledge graph is "
    "available. kg_query is the primary search tool — a single miss does not "
    "mean it stopped working. Ask kg_query first for each new question about "
    "definitions, callers, or module relationships (retrying with its "
    "suggested nearest terms on a miss); use bash only for literal string "
    "content."
)


def _is_drift_search(command: str) -> bool:
    """A bash call that competes with kg_query: rg/grep/find or `git grep`."""
    tokens = command.strip().split()
    if not tokens:
        return False
    head = tokens[0].rsplit("/", 1)[-1]
    if head in DRIFT_SEARCH_COMMANDS:
        return True
    return head == "git" and len(tokens) > 1 and tokens[1] == "grep"


def _shallow_tree(root: Path, max_entries: int = 40) -> str:
    """Fallback repo orientation when the KG isn't ready: top-level layout."""
    lines = ["[top-level layout]"]
    try:
        entries = sorted(p for p in root.iterdir() if not p.name.startswith("."))
    except OSError:
        return ""
    for p in entries[:max_entries]:
        if p.is_dir():
            try:
                children = sorted(c.name for c in p.iterdir() if not c.name.startswith("."))
            except OSError:
                children = []
            lines.append(f"  {p.name}/: " + ", ".join(children[:8]))
        else:
            lines.append(f"  {p.name}")
    return "\n".join(lines)


def _is_duplicate_read(messages: list[Message], tc_id: str, arguments: dict | None, output: str) -> bool:
    """True when an identical read whose FULL result is still in the transcript
    already happened — compaction stubs old results, so a re-read after
    compaction legitimately proceeds (the stub no longer matches the output)."""
    results = {m.tool_call_id: m.content for m in messages if m.role == "tool"}
    for m in messages:
        for prev in m.tool_calls:
            if (
                prev.id != tc_id
                and prev.name == "read"
                and prev.arguments == arguments
                and results.get(prev.id) == output
            ):
                return True
    return False


def repair_interrupted(messages: list[Message]) -> None:
    """An interrupt can land after the assistant's tool_calls were appended but
    before their results — answer the dangling calls so the transcript the next
    turn builds on stays well-formed."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role == "user":
            return
        if m.role == "assistant":
            answered = {t.tool_call_id for t in messages[i + 1 :] if t.role == "tool"}
            for tc in m.tool_calls:
                if tc.id not in answered:
                    messages.append(
                        Message(
                            role="tool",
                            content="[interrupted by the user before this tool ran]",
                            tool_call_id=tc.id,
                        )
                    )
            return


@dataclass
class RunResult:
    status: str  # "done" | "aborted_invalid_tool" | "aborted_stuck" | "max_turns"
    summary: str
    usage: Usage
    turns: int
    messages: list[Message] = field(default_factory=list)


def _plan_tracker(ctx: ToolContext) -> str | None:
    """Default tracker provider: the Code harness's plan tracker."""
    return ctx.plan.render() if ctx.plan is not None else None


class Runner:
    def __init__(
        self,
        spec: ModelSpec,
        client: OpenAICompatClient,
        registry: Registry,
        tools: list[Tool],
        ctx: ToolContext,
        max_turns: int = MAX_TURNS,
        temperature: float = 0.0,
        on_delta: OnDelta | None = None,
        instructions_path: Path | None = None,
        mutating_tools: set[str] | frozenset[str] | None = None,
        tracker: Callable[[ToolContext], str | None] | None = None,
        tracker_prefix: str | None = None,
        explore_nudge: str | None = None,
        seed_context: str | None = None,
    ):
        self.spec = spec
        self.client = client
        self.registry = registry
        self.tools = {t.name: t for t in tools}
        self.specs = {t.name: t.spec() for t in tools}
        self.ctx = ctx
        self.max_turns = max_turns
        self.temperature = temperature
        # streams assistant text as it generates; None keeps requests non-streaming
        self.on_delta = on_delta
        # harness tuning — defaults are the Code harness's, so existing callers
        # are untouched; other harnesses (arch) pass their own
        self.instructions_path = instructions_path or INSTRUCTIONS_PATH
        self.mutating_tools = set(mutating_tools) if mutating_tools is not None else set(MUTATING_TOOLS)
        self.tracker = tracker or _plan_tracker
        self.tracker_prefix = tracker_prefix or PLAN_TRACKER_PREFIX
        self.explore_nudge = explore_nudge or EXPLORE_NUDGE
        # stable reference material seeded into the system prompt (survives
        # compaction, which always keeps messages[:2]) — e.g. the arch handoff
        # doc the lead hands a code sub-session
        self.seed_context = seed_context

    def run(self, task: str) -> RunResult:
        messages = [
            Message(role="system", content=self._system_prompt()),
            Message(role="user", content=f"Task: {task}"),
        ]
        return self._loop(messages, interactive=False)

    def chat(self, messages: list[Message], user_input: str) -> RunResult:
        """One conversational exchange for interactive mode. Seeds the system
        prompt on first use; a text-only assistant reply returns to the user
        (status "reply") instead of tripping the stuck guard."""
        if not messages:
            messages.append(Message(role="system", content=self._system_prompt()))
        messages.append(Message(role="user", content=user_input))
        return self._loop(messages, interactive=True)

    def _system_prompt(self) -> str:
        """Instructions plus environment grounding: the model must never have
        to guess where it is or what the codebase looks like (that guess is
        where /testbed-style hallucinations come from)."""
        parts = [
            self.instructions_path.read_text(encoding="utf-8"),
            f"Repository root: {self.ctx.repo_root}\n"
            "All tool paths are relative to this root.",
        ]
        orientation = ""
        if self.ctx.kg is not None and self.ctx.kg.is_ready():
            try:
                orientation = self.ctx.kg.digest()
            except Exception:
                orientation = ""
        parts.append(orientation or _shallow_tree(self.ctx.repo_root))
        if self.ctx.skills:
            parts.append(render_index(self.ctx.skills))
        if self.seed_context:
            parts.append(self.seed_context)
        return "\n\n".join(p for p in parts if p)

    def _strip_tracker(self, messages: list[Message]) -> None:
        """Remove the pinned tracker copies, mutating the list in place."""
        messages[:] = [
            m
            for m in messages
            if not (m.role == "user" and (m.content or "").startswith(self.tracker_prefix))
        ]

    def _loop(self, messages: list[Message], interactive: bool) -> RunResult:
        usage = Usage()
        retries_left = MAX_VALIDATION_RETRIES
        text_only_streak = 0
        last_assistant_repr: str | None = None
        recent_calls: list[str] = []
        readonly_call_counts: Counter[str] = Counter()
        explore_streak = 0  # consecutive tool turns without an edit/write
        drift_searches = 0  # bash searches since the last kg_query, KG ready
        kg_was_unready = self.ctx.kg is not None and not self.ctx.kg.is_ready()
        self.ctx.emit(
            "run_start",
            {"task": messages[-1].content, "model": self.spec.spec, "interactive": interactive},
        )

        for turn in range(1, self.max_turns + 1):
            # KG became ready mid-run → tell the model once (decision #9)
            if kg_was_unready and self.ctx.kg.is_ready():
                messages.append(Message(role="user", content=KG_READY_NOTICE))
                self.ctx.emit("kg_ready_notice", {"turn": turn})
                kg_was_unready = False

            # tracker: exactly one live copy, re-rendered every turn and
            # always at the tail — pinned by refresh, so compaction (which
            # keeps the recent tail) can never lose it
            # NB: mutate in place (never rebind `messages`) — interactive
            # callers hold this same list and keep it across exchanges; a
            # rebind here silently forks the transcript and later turns run
            # without the history
            tracker_text = self.tracker(self.ctx)
            if tracker_text is not None:
                self._strip_tracker(messages)
                messages.append(Message(role="user", content=tracker_text))

            if needs_compaction(messages, self.spec.context_window):
                messages[:] = compact(
                    messages, self.spec.context_window, self.registry, self.client,
                    record=self.ctx.emit,
                )

            resp = self.client.complete(
                self.spec,
                messages,
                tools=list(self.specs.values()),
                temperature=self.temperature,
                on_delta=self.on_delta,
            )
            usage += resp.usage
            assistant = resp.message
            messages.append(assistant)
            self.ctx.emit(
                "assistant",
                {
                    "turn": turn,
                    "content": assistant.content,
                    "tool_calls": [
                        {"name": tc.name, "arguments_json": tc.arguments_json}
                        for tc in assistant.tool_calls
                    ],
                    "stop_reason": resp.stop_reason,
                },
            )

            # --- stuck guards ---
            rep = json.dumps(assistant.to_dict(), sort_keys=True)
            if rep == last_assistant_repr:
                self.ctx.emit("abort", {"reason": "repeated_message", "turn": turn})
                return RunResult("aborted_stuck", "model repeated itself verbatim", usage, turn, messages)
            last_assistant_repr = rep

            if not assistant.tool_calls:
                if interactive:
                    self.ctx.emit("reply", {"turn": turn})
                    return RunResult("reply", assistant.content or "", usage, turn, messages)
                text_only_streak += 1
                if text_only_streak >= MAX_TEXT_ONLY_TURNS:
                    self.ctx.emit("abort", {"reason": "text_only_cap", "turn": turn})
                    return RunResult(
                        "aborted_stuck",
                        f"{MAX_TEXT_ONLY_TURNS} consecutive turns without a tool call",
                        usage, turn, messages,
                    )
                messages.append(Message(role="user", content=TEXT_ONLY_NUDGE))
                continue
            text_only_streak = 0

            # --- execute tool calls ---
            had_invalid = False
            for tc in assistant.tool_calls:
                error = validate_tool_call(tc, self.specs)
                if error:
                    had_invalid = True
                    self.ctx.emit(
                        "invalid_tool_call",
                        {"turn": turn, "name": tc.name, "error": error, "retries_left": retries_left},
                    )
                    messages.append(Message(role="tool", content=error, tool_call_id=tc.id))
                    continue
                result = self.tools[tc.name].execute(tc.arguments, self.ctx)
                output = result.output
                if (
                    tc.name == "read"
                    and not result.is_error
                    and _is_duplicate_read(messages, tc.id, tc.arguments, output)
                ):
                    # identical content is already in context; don't pay for it twice
                    output = (
                        "[unchanged since your earlier read — the full content of this "
                        "file is already in the conversation above; do not read it again]"
                    )
                    self.ctx.emit("read_deduped", {"turn": turn, "args": tc.arguments_json})
                self.ctx.emit(
                    "tool_result",
                    {
                        "turn": turn,
                        "name": tc.name,
                        "is_error": result.is_error,
                        "details": result.details,
                    },
                )
                messages.append(Message(role="tool", content=output, tool_call_id=tc.id))
                # termination is signaled by the result's details, not by the
                # name alone: a phase-gate done (arch toplevel approval) can
                # succeed without ending the session
                if tc.name == "done" and not result.is_error and result.details.get("done"):
                    self.ctx.emit("run_done", {"turn": turn, "summary": result.output})
                    # the plan lived its life with this task — clear it (and its
                    # pinned tracker) so the next exchange isn't steered by a
                    # stale "all steps closed" scoreboard
                    if self.ctx.plan is not None:
                        self.ctx.plan = None
                        self._strip_tracker(messages)
                    return RunResult("done", result.output, usage, turn, messages)
                if tc.name in READONLY_LOOP_TOOLS and not result.is_error:
                    readonly_call_counts[f"{tc.name}:{tc.arguments_json}"] += 1
                if tc.name == "kg_query":
                    drift_searches = 0
                elif (
                    tc.name == "bash"
                    and _is_drift_search(tc.arguments.get("command", ""))
                    and self.ctx.kg is not None
                    and self.ctx.kg.is_ready()
                ):
                    drift_searches += 1

            if had_invalid:
                if retries_left <= 0:
                    self.ctx.emit("abort", {"reason": "invalid_tool_calls_exhausted", "turn": turn})
                    return RunResult(
                        "aborted_invalid_tool",
                        "model kept producing invalid tool calls after retries",
                        usage, turn, messages,
                    )
                retries_left -= 1
                continue  # invalid turns don't count toward the same-call loop guard
            retries_left = MAX_VALIDATION_RETRIES

            # --- KG drift guard: bash-searching while the graph is ready means
            # the model abandoned kg_query (usually after one miss) — pull it
            # back; re-nudges every KG_DRIFT_NUDGE_SEARCHES searches ---
            if "kg_query" in self.tools and drift_searches >= KG_DRIFT_NUDGE_SEARCHES:
                messages.append(Message(role="user", content=KG_DRIFT_NOTICE))
                self.ctx.emit("kg_drift_nudge", {"turn": turn, "searches": drift_searches})
                drift_searches = 0

            # --- explore-budget nudge: a model that keeps reading and restating
            # its plan needs an explicit push over the planning→acting boundary ---
            if any(t.name in self.mutating_tools for t in assistant.tool_calls):
                explore_streak = 0
            else:
                explore_streak += 1
                if explore_streak >= EXPLORE_NUDGE_TURNS:
                    plan = self.ctx.plan
                    cur = plan.current_index() if plan is not None else None
                    if cur is not None:
                        step = plan.steps[cur]
                        nudge = PLAN_EXPLORE_NUDGE.format(
                            n=explore_streak,
                            num=cur + 1,
                            title=step.title,
                            files=", ".join(step.files),
                        )
                    else:
                        nudge = self.explore_nudge.format(n=explore_streak)
                    messages.append(Message(role="user", content=nudge))
                    self.ctx.emit("explore_nudge", {"turn": turn, "streak": explore_streak})
                    explore_streak = 0

            # --- cumulative read-only loop guard: identical read/kg_query calls
            # repeated across the run (interleaved or not) mean the model is
            # spinning; checked after the turn's calls so no tool_call dangles ---
            worst_sig, worst_count = "", 0
            if readonly_call_counts:
                worst_sig, worst_count = readonly_call_counts.most_common(1)[0]
            if worst_count >= READONLY_CALL_TOTAL_CAP:
                self.ctx.emit(
                    "abort",
                    {"reason": "repeated_readonly_call", "turn": turn, "call": worst_sig[:200]},
                )
                return RunResult(
                    "aborted_stuck",
                    f"same read-only call repeated {worst_count}x this run: {worst_sig[:120]}",
                    usage, turn, messages,
                )

            # --- same-tool loop guard (valid calls only; invalid ones are the
            # retry policy's job, which must abort with its own status) ---
            call_sig = json.dumps(
                [[tc.name, tc.arguments_json] for tc in assistant.tool_calls], sort_keys=True
            )
            recent_calls.append(call_sig)
            if len(recent_calls) >= SAME_CALL_LOOP_THRESHOLD and len(
                set(recent_calls[-SAME_CALL_LOOP_THRESHOLD:])
            ) == 1:
                self.ctx.emit("abort", {"reason": "same_tool_loop", "turn": turn})
                return RunResult(
                    "aborted_stuck",
                    f"same tool call repeated {SAME_CALL_LOOP_THRESHOLD}x: {assistant.tool_calls[0].name}",
                    usage, turn, messages,
                )

        self.ctx.emit("abort", {"reason": "max_turns"})
        return RunResult("max_turns", f"hit {self.max_turns}-turn cap", usage, self.max_turns, messages)
