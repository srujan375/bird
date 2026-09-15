"""The Code harness ReAct loop.

Owns everything the adapter doesn't: tool-call validation with helpful-error
retries (2 per call, then structured abort — decision #7), stuck-model
guards (borrowed from vishwa), explicit done-tool termination, the 90%
compaction trigger, and the "KG now available" injection (decision #9).
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..llm.registry import ModelSpec, Registry
from ..llm.types import Message, Usage
from ..llm.validate import validate_tool_call
from ..llm.wire.openai_compat import (
    OnDelta,
    OnToolDelta,
    OpenAICompatClient,
    WireAborted,
    WireError,
)
from ..tools import Tool, ToolContext
from ..skills import render_index
from .compactor import compact, needs_compaction

# No default turn cap: a run ends when it is finished (done/reply) or when a
# stuck guard below catches it actually spinning — repeated messages, the same
# tool call three times, one read-only call six times, a text-only streak.
# Those measure the thing the cap was a proxy for, and unlike the cap they do
# not cut off a long job that is making real progress. Callers that genuinely
# need a ceiling (a headless eval, a budgeted batch) pass max_turns
# explicitly; --max-turns is still there for exactly that.
DONE_TOOL = "done"  # the default terminal tool; harnesses may name their own
MAX_VALIDATION_RETRIES = 2  # per tool call chain
MAX_TEXT_ONLY_TURNS = 3
SAME_CALL_LOOP_THRESHOLD = 3
# read-only tools repeated with identical args are pure spinning, even when
# other calls are interleaved (mutating tools like bash/pytest may legitimately
# repeat between edits, so they are exempt)
READONLY_LOOP_TOOLS = {"read", "kg_query", "grep", "glob"}
READONLY_CALL_TOTAL_CAP = 6
# One bash segment repeated this many times inside a single argument is not a
# command, it is a model whose output has collapsed. No real invocation repeats
# one pipeline segment eight times.
DEGENERATE_SEGMENT_REPEATS = 20
# ...but repeat count alone convicts innocent output. Structural fragments
# repeat legitimately in content the model is *writing*: ``` fences in a
# markdown document, `|---|` table rules, closing braces. One logged session
# was killed mid-document for writing nine code blocks. So a repeat counts only
# when the fragment is substantive, or when a short one has taken over the
# argument outright.
DEGENERATE_MIN_SEGMENT = 24
DEGENERATE_SEGMENT_SHARE = 0.5
# Of the segments between the first and last copy of the fragment, the share
# that must BE the fragment. A collapse is dense — the model emits the line
# again and again — where a test file that opens every test with the same
# setup line spreads ten copies across a hundred lines of test bodies.
DEGENERATE_DENSITY = 0.5
MUTATING_TOOLS = {"edit", "write"}
EXPLORE_NUDGE_TURNS = 6
# bash searches allowed while the KG is ready before nudging back to kg_query;
# one kg miss must not turn into bash-for-the-rest-of-the-session
KG_DRIFT_NUDGE_SEARCHES = 3
DRIFT_SEARCH_COMMANDS = {"rg", "grep", "find"}

# Total wall-clock budget for ONE model turn (one complete() call). The wire's
# READ timeout is per-read and never fires on a provider that drips chunks —
# slow reasoning deltas, keep-alives — so a turn could previously hang forever
# with nothing in the log: the live design session sat silent at turn 4 for
# hours, its events ending at the design_plan tool_result, no thinking/
# assistant/retry/error ever arriving. The budget must stay generous: a full
# design_create artboard streaming off a slow cloud model is the big
# legitimate case, and the wire's own comment warns a full retry budget once
# held the UI ~20 minutes. 15 minutes covers that with margin; pass
# turn_budget_seconds to change it (0 or less disables the watchdog).
DEFAULT_TURN_BUDGET_SECONDS = 15 * 60.0

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
# These fire when a run has read for a while without changing anything. That
# state has TWO causes and only the model can tell them apart: it is either
# procrastinating, or it is missing something and browsing for it. The old text
# assumed the first and ordered an immediate edit — which is the correct cure
# for procrastination and a direct cause of half-informed edits in the second
# case. So the nudge now asks for the diagnosis instead of prescribing.
EXPLORE_NUDGE = (
    "[system notice] {n} turns of reading without changing anything. Two "
    "possibilities and only you can tell them apart. If you already know what "
    "to change, make the edit now. If something is still missing, name it in "
    "one sentence and fetch exactly that — batch the lookups into a single "
    "call instead of another turn of browsing. Do not restate the plan."
)
PLAN_EXPLORE_NUDGE = (
    "[system notice] {n} turns without an edit/write. You are on step {num}: "
    "{title} ({files}). If you know what to change, edit now. If something is "
    "still missing, name it and fetch exactly that — including code outside "
    "this step's files when the change depends on it. Do not restate the plan."
)
# the tracker message the runner re-renders each turn; must match the first
# line PlanState.render() produces
PLAN_TRACKER_PREFIX = "[plan tracker"
KG_DRIFT_NOTICE = (
    "[system notice] You are shelling out to search. There are dedicated tools "
    "for this and they are better at it: `kg_query` for where something is "
    "defined and what calls it, `grep` for literal text (including inside "
    "node_modules/dist, which kg_query does not index), `glob` for finding "
    "files by name. Match the tool to the question instead of scripting the "
    "search by hand."
)


def _elide_arguments(tc, details) -> None:
    """Rewrite a call that has already run so named arguments become the
    stubs the tool asked for (`details["elide_arguments"] = {name: stub}`).

    A design_create carries a 16-30 KB html document. The harness checkpoints
    it and can print it back on demand (design_read), yet the transcript kept
    every copy, so every later turn re-sent all of them: the logged 112-turn
    design session paid 5.25M input tokens, mostly for artboards the model had
    already handed over. The rewrite lands in the assistant message already
    on the transcript — after the call executed and after the `assistant`
    event carried the real arguments to the session log — so the stub is what
    every later model call, and the saved transcript, sees. Arguments that do
    not parse are left alone: there is nothing safe to rewrite in them."""
    stubs = details.get("elide_arguments") if isinstance(details, dict) else None
    if not isinstance(stubs, dict) or not stubs:
        return
    try:
        args = json.loads(tc.arguments_json) if tc.arguments_json else dict(tc.arguments or {})
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(args, dict):
        return
    hit = False
    for name, stub in stubs.items():
        if name in args:
            args[name] = str(stub)
            hit = True
    if not hit:
        return
    # a fresh dict on purpose: the tool ran with `tc.arguments` and may still
    # hold that object; the rewrite must not reach into its state
    tc.arguments = args
    tc.arguments_json = json.dumps(args, ensure_ascii=False)


def _counts_toward_readonly_cap(tc) -> bool:
    """Does repeating this exact call mean the run is spinning?

    `bash` used to be exempt wholesale, on the reasoning that a test run may
    legitimately repeat between edits. True of `pytest`; not true of
    `grep -n "import" src/branding.ts | head -3`, which one logged session ran
    six times before collapsing entirely — uncounted, because the cap only ever
    watched `read` and `kg_query`. bash.py already draws the line, so use it: a
    pure search that repeats is spinning, a check that repeats is work.
    """
    if tc.name in READONLY_LOOP_TOOLS:
        return True
    if tc.name == "bash":
        from ..tools.bash import is_pure_search

        return is_pure_search(tc.arguments.get("command", "") or "")
    return False


def _degenerate_repetition(assistant) -> str | None:
    """A single argument that has collapsed into one fragment repeated.

    Every other stuck guard compares whole messages or whole call signatures for
    EXACT equality, so a model degenerating inside one argument slips all of
    them: the repeats corrupt as they go (`branding.ts` -> `brancding.ts` ->
    `mountain.ts`), and no two turns match. One logged session emitted the same
    grep roughly two hundred times in a single command and was stopped only by
    the shell parser tripping over an unbalanced quote.

    What makes it a collapse is neither the tally nor the fragment alone.
    Replayed over every logged tool call, the count-only version of this guard
    (threshold 8) aborted five healthy `write`s of test files — every test
    opening with `root = _repo(tmp_path, monkeypatch)`, ten times over — against
    two real collapses, at 181x and 387x. Those five are what taught the lead
    to slice tasks into "one file under 80 lines". So three things have to
    hold: the fragment repeats a LOT (DEGENERATE_SEGMENT_REPEATS, and real
    collapses run to hundreds); the copies are DENSE (DEGENERATE_DENSITY — a
    collapse emits the line back-to-back, a test file has a test body between
    each pair); and the fragment carries content of its own
    (DEGENERATE_MIN_SEGMENT) or a short one repeats until it is most of the
    argument (DEGENERATE_SEGMENT_SHARE), so nine ``` fences in a document stay
    healthy.
    """
    for tc in assistant.tool_calls:
        for value in (tc.arguments or {}).values():
            if not isinstance(value, str) or len(value) < 200:
                continue
            segments = [seg.strip() for seg in re.split(r"[;\n]", value) if seg.strip()]
            if len(segments) < DEGENERATE_SEGMENT_REPEATS:
                continue
            worst, count = Counter(segments).most_common(1)[0]
            if count < DEGENERATE_SEGMENT_REPEATS:
                continue
            first = segments.index(worst)
            last = len(segments) - 1 - segments[::-1].index(worst)
            if count / (last - first + 1) < DEGENERATE_DENSITY:
                continue  # spread out through real content: a pattern, not a collapse
            substantive = len(worst) >= DEGENERATE_MIN_SEGMENT
            dominant = count * len(worst) >= len(value) * DEGENERATE_SEGMENT_SHARE
            if substantive or dominant:
                return f"{tc.name}: {worst[:60]!r} repeated {count}x in one argument"
    return None


def _is_drift_search(command: str) -> bool:
    """A bash call that competes with the dedicated search tools: rg/grep/find
    or `git grep`. Note this counts *shelling out* only — the grep and glob
    tools are the intended route, not drift, so reaching for bash now means
    both of them were passed over."""
    tokens = command.strip().split()
    if not tokens:
        return False
    head = tokens[0].rsplit("/", 1)[-1]
    if head in DRIFT_SEARCH_COMMANDS:
        return True
    return head == "git" and len(tokens) > 1 and tokens[1] == "grep"


PROJECT_INSTRUCTIONS_LIMIT = 8192  # ~8KB cap on a project-level instructions file
PROJECT_INSTRUCTIONS_TRUNCATION_NOTICE = (
    "\n\n[project instructions truncated — file exceeded 8KB limit]"
)


def _resolve_imports(text: str, repo_root: Path) -> str:
    """Inline `@path` import directives (one level only, no recursion).

    A line that is *entirely* an import directive — matching `^@(.+)$` — names
    a file relative to `repo_root`. The file's raw text replaces the `@` line.
    A missing file becomes a `<!-- import not found: @path -->` comment so the
    user can debug. `@` references inside an imported file are NOT resolved
    (one level only — keeps I/O bounded and avoids recursion). Inline `@`
    mentions in prose ("see @CLAUDE.md for details") are left untouched: only
    a line that is wholly `@path` is an import.
    """
    directive = re.compile(r"^@(.+)$")
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        # only a line that is *entirely* `@path` (no trailing prose, no leading
        # whitespace) is an import; rstrip the newline before matching
        m = directive.match(line.rstrip("\r\n"))
        if not m:
            out.append(line)
            continue
        rel = m.group(1).strip()
        target = (repo_root / rel).resolve()
        if target.is_file():
            try:
                out.append(target.read_text(encoding="utf-8"))
            except OSError:
                out.append(f"<!-- import not found: @{rel} -->")
        else:
            out.append(f"<!-- import not found: @{rel} -->")
    return "".join(out)


def _load_project_instructions(repo_root: Path) -> str:
    """Project-level custom instructions (like CLAUDE.md), injected into every
    harness's system prompt. First-match-wins at the repo root only — no parent
    walking, no user-level files. Returns the file contents (with `@path`
    imports inlined, then truncated past the 8KB cap with a notice) or "" if
    neither file exists."""
    for name in (".bird/instructions.md", "CLAUDE.md"):
        path = repo_root / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            text = _resolve_imports(text, repo_root)
            if len(text.encode("utf-8")) > PROJECT_INSTRUCTIONS_LIMIT:
                text = text.encode("utf-8")[:PROJECT_INSTRUCTIONS_LIMIT].decode(
                    "utf-8", errors="ignore"
                ) + PROJECT_INSTRUCTIONS_TRUNCATION_NOTICE
            return text
    return ""


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
    # "max_turns" is only reachable when a caller passed an explicit ceiling
    status: str  # "done" | "aborted_invalid_tool" | "aborted_stuck" | "max_turns"
    summary: str
    usage: Usage
    turns: int
    messages: list[Message] = field(default_factory=list)


def _plan_tracker(ctx: ToolContext) -> str | None:
    """Default tracker provider: the Code harness's plan tracker."""
    return ctx.plan.render() if ctx.plan is not None else None


class _TurnWatchdog:
    """One model turn's wall-clock budget, armed around its complete() call.

    The wire's READ timeout is per-read, so a provider that drips chunks never
    trips it and a turn can hang in perfect silence — the live design session
    that stuck at turn 4 for hours logged nothing at all. The timer runs on
    its own thread and tears the in-flight request down with
    client.abort("watchdog"), the same socket shutdown a user interrupt uses,
    so the blocked read wakes immediately instead of waiting on a provider
    that may never send another byte.

    `fired` is how the runner tells a budget expiry from a user interrupt:
    both arrive as WireAborted, but only one of them was asked for."""

    def __init__(self, client: OpenAICompatClient, budget: float):
        self.client = client
        self.fired = False
        self._timer: threading.Timer | None = None
        if budget and budget > 0:
            self._timer = threading.Timer(budget, self._expire)
            self._timer.daemon = True  # a dead turn must never hold process exit

    def _expire(self) -> None:
        self.fired = True
        self.client.abort(reason="watchdog")

    def arm(self) -> None:
        if self._timer is not None:
            self._timer.start()

    def disarm(self) -> None:
        """Stop the timer, and clear the abort flag if it fired too late.

        The flag's stickiness exists for user interrupts — one landing between
        requests must abort the next request rather than be lost. A budget
        that expired against a turn that had ALREADY completed is the opposite
        case: the response landed, so leaving the flag set would kill the next
        turn, which did nothing wrong. Only a watchdog flag is cleared here;
        a user interrupt (abort_reason "user") is left stuck, as designed."""
        if self._timer is not None:
            self._timer.cancel()
        if self.fired and self.client.abort_reason == "watchdog":
            self.client.clear_abort()


class Runner:
    def __init__(
        self,
        spec: ModelSpec,
        client: OpenAICompatClient,
        registry: Registry,
        tools: list[Tool],
        ctx: ToolContext,
        max_turns: int | None = None,
        temperature: float = 0.0,
        on_delta: OnDelta | None = None,
        on_thinking: OnDelta | None = None,
        on_tool_delta: OnToolDelta | None = None,
        instructions_path: Path | None = None,
        mutating_tools: set[str] | frozenset[str] | None = None,
        tracker: Callable[[ToolContext], str | None] | None = None,
        tracker_prefix: str | None = None,
        explore_nudge: str | None = None,
        seed_context: str | None = None,
        done_tool: str | None = None,
        turn_budget_seconds: float | None = None,
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
        # streams the reasoning trace (Ollama thinking models) as it generates;
        # None keeps it off the wire (reasoning then takes the heartbeat path)
        self.on_thinking = on_thinking
        # streams a tool call's arguments as they generate — what lets the
        # design page render an artboard while the model is still writing it.
        # None leaves the arguments to arrive whole with the message.
        self.on_tool_delta = on_tool_delta
        # harness tuning — defaults are the Code harness's, so existing callers
        # are untouched; other harnesses (arch) pass their own
        self.instructions_path = instructions_path or INSTRUCTIONS_PATH
        self.mutating_tools = set(mutating_tools) if mutating_tools is not None else set(MUTATING_TOOLS)
        self.tracker = tracker or _plan_tracker
        self.tracker_prefix = tracker_prefix or PLAN_TRACKER_PREFIX
        # None takes the code harness's nudge; "" switches it off. The design
        # harness passes "": its read-only calls (design_read on three
        # artboards, the theme list) ARE the work, and the code copy it used
        # to inherit — "turns of reading without changing anything" — fired 22
        # times across the logged design sessions at a designer doing exactly
        # what it had been asked.
        self.explore_nudge = EXPLORE_NUDGE if explore_nudge is None else explore_nudge
        # which tool ends the session. Parameterized rather than hard-coded so a
        # harness can call its terminal move what it actually is (arch hands off
        # a design; it does not finish a task) without the engine learning any
        # harness's name.
        self.done_tool = done_tool or DONE_TOOL
        # stable reference material seeded into the system prompt (survives
        # compaction, which always keeps messages[:2]) — e.g. the arch handoff
        # doc the lead hands a code sub-session
        self.seed_context = seed_context
        # per-turn wall-clock budget (see DEFAULT_TURN_BUDGET_SECONDS): the
        # last line of defense against a provider that streams forever without
        # ever tripping the per-read timeout. None takes the default; <=0
        # disables (tests, and callers that own their own cancellation).
        self.turn_budget_seconds = (
            DEFAULT_TURN_BUDGET_SECONDS if turn_budget_seconds is None else turn_budget_seconds
        )

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
            _load_project_instructions(self.ctx.repo_root),
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

    def hot_paths(self) -> frozenset[str]:
        """Files this run is actively working on: edited-but-not-yet-verified,
        plus the current plan step's own files. Compaction pins their content
        (see compactor.stub_tool_results) — dropping it is what sends a run
        back to re-read the file it was in the middle of editing."""
        paths = set(self.ctx.unverified_paths)
        plan = self.ctx.plan
        if plan is not None:
            cur = plan.current_index()
            if cur is not None:
                paths.update(plan.steps[cur].files)
        return frozenset(paths)

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

        turn = 0
        while self.max_turns is None or turn < self.max_turns:
            turn += 1
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

            # --- mid-turn user input ---
            # Everything typed while this run was working, appended after the
            # previous step's tool results and after the tracker, so the user's
            # words are the last thing the model reads.
            #
            # The top of a step is the ONLY safe seam. Between an assistant
            # message carrying tool_calls and its tool results the transcript is
            # a shape every provider rejects, so the drain can never happen
            # between individual calls — it happens once the batch has closed.
            if self.ctx.pending_input is not None:
                injected = self.ctx.pending_input()
                for text in injected:
                    messages.append(Message(role="user", content=text))
                    self.ctx.emit("user_injected", {"turn": turn, "text": text})
                if injected:
                    # A course correction is not drift. Every guard below counts
                    # repetition within one task, and the task just changed;
                    # carrying the counters across makes the pivot read as
                    # spinning and aborts the run the user was steering.
                    last_assistant_repr = None
                    text_only_streak = 0
                    explore_streak = 0
                    drift_searches = 0
                    recent_calls.clear()
                    readonly_call_counts.clear()

            if needs_compaction(messages, self.spec.context_window):
                messages[:] = compact(
                    messages, self.spec.context_window, self.registry, self.client,
                    record=self.ctx.emit, keep_paths=self.hot_paths(),
                )

            watchdog = _TurnWatchdog(self.client, self.turn_budget_seconds)
            watchdog.arm()
            try:
                resp = self.client.complete(
                    self.spec,
                    messages,
                    tools=list(self.specs.values()),
                    temperature=self.temperature,
                    on_delta=self.on_delta,
                    on_thinking=self.on_thinking,
                    on_tool_delta=self.on_tool_delta,
                    # A transport retry is the only thing that happens between
                    # "the model was asked" and "the model answered", and it used
                    # to happen in silence — a provider that goes quiet held the UI
                    # on a spinner for the whole retry budget with nothing to show.
                    # Surfacing it makes a stalled provider look like a stalled
                    # provider instead of a hung bird.
                    on_retry=lambda info: self.ctx.emit("wire_retry", {"turn": turn, **info}),
                )
            except WireAborted as e:
                if watchdog.fired:
                    # The budget, not the user, tore this request down. Surface
                    # it the way a hard stall surfaces — a WireError the caller
                    # reports as a failed turn — and never as WireAborted, which
                    # serve.py swallows as "interrupted" and a REPL treats as a
                    # cancel nobody made. The turn_timeout event is the point of
                    # the whole guard: the live design session hung here in
                    # perfect silence, its log ending mid-run with no error to
                    # name what happened.
                    self.ctx.emit(
                        "turn_timeout",
                        {
                            "turn": turn,
                            "budget_seconds": self.turn_budget_seconds,
                            "model": self.spec.spec,
                        },
                    )
                    raise WireError(
                        f"model turn {turn} timed out after "
                        f"{self.turn_budget_seconds:.0f}s — the watchdog aborted "
                        f"the stream ({self.spec.spec} never completed a response)"
                    ) from None
                raise  # a user interrupt keeps its own semantics
            finally:
                watchdog.disarm()
            usage += resp.usage
            assistant = resp.message
            messages.append(assistant)
            # the completed reasoning trace reaches the recorder + all
            # transports as its own event (byte-compatible with the existing
            # `assistant` event, which stays unchanged). Only emitted when
            # non-empty — a non-thinking turn produces nothing here.
            if assistant.thinking:
                self.ctx.emit("thinking", {"turn": turn, "text": assistant.thinking})
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
                    # per-turn cost, so an analysis can bound spend to a point in
                    # the run ("tokens burned before the first edit") instead of
                    # only reading the total at the end
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
            )

            # --- stuck guards ---
            collapsed = _degenerate_repetition(assistant)
            if collapsed:
                self.ctx.emit("abort", {"reason": "degenerate_repetition", "turn": turn, "detail": collapsed})
                return RunResult(
                    "aborted_stuck",
                    f"model output collapsed into repetition — {collapsed}",
                    usage, turn, messages,
                )

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

            # The same-tool guard's signature is taken BEFORE the calls run: a
            # tool may ask for an argument to be elided from the transcript
            # once it has executed (see _elide_arguments), and two calls that
            # differed only in that argument must not compare equal afterwards.
            call_sig = json.dumps(
                [[tc.name, tc.arguments_json] for tc in assistant.tool_calls], sort_keys=True
            )

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
                # stamp the verification ledger before `done` is reached below:
                # a turn that edits and then runs the tests must count both, in
                # the order the model made the calls
                self.ctx.note_tool_result(tc.name, result)
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
                if tc.name == self.done_tool and not result.is_error and result.details.get("done"):
                    self.ctx.emit("run_done", {"turn": turn, "summary": result.output})
                    # the plan lived its life with this task — clear it (and its
                    # pinned tracker) so the next exchange isn't steered by a
                    # stale "all steps closed" scoreboard
                    if self.ctx.plan is not None:
                        self.ctx.plan = None
                        self._strip_tracker(messages)
                    return RunResult("done", result.output, usage, turn, messages)
                if not result.is_error and _counts_toward_readonly_cap(tc):
                    readonly_call_counts[f"{tc.name}:{tc.arguments_json}"] += 1
                # last thing that reads the real arguments on this call
                _elide_arguments(tc, result.details)
                if tc.name == "kg_query":
                    drift_searches = 0
                elif (
                    self.ctx.kg is not None
                    and self.ctx.kg.is_ready()
                    and (
                        # The guard used to watch `bash` alone. bird ships a
                        # first-class `grep`, so the drift it exists to catch —
                        # searching the repo by hand while the graph sits unused
                        # — was invisible: a session logged 19 grep/glob calls,
                        # zero kg_query, and zero nudges. `glob` stays out on
                        # purpose; the graph cannot answer filename questions.
                        tc.name == "grep"
                        or (tc.name == "bash" and _is_drift_search(tc.arguments.get("command", "")))
                    )
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
            elif self.explore_nudge:  # "" = the harness opted out (see __init__)
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
            recent_calls.append(call_sig)  # taken before the calls ran, see above
            if len(recent_calls) >= SAME_CALL_LOOP_THRESHOLD and len(
                set(recent_calls[-SAME_CALL_LOOP_THRESHOLD:])
            ) == 1:
                self.ctx.emit("abort", {"reason": "same_tool_loop", "turn": turn})
                return RunResult(
                    "aborted_stuck",
                    f"same tool call repeated {SAME_CALL_LOOP_THRESHOLD}x: {assistant.tool_calls[0].name}",
                    usage, turn, messages,
                )

        # only reachable with an explicit ceiling — the default loop has none
        self.ctx.emit("abort", {"reason": "max_turns"})
        return RunResult("max_turns", f"hit {self.max_turns}-turn cap", usage, turn, messages)
