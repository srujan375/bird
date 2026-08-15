"""Mini-compactor (decision #11).

Triggers at 90% of the model's context window. Stage 1 is free: stub old
tool results down to one-line placeholders. Stage 2 (only if still over):
summarize the older half with the pinned `compactor` model — never the model
under test. Offline / no key → trim-stubs fallback. Every compaction is a
session event.
"""

from __future__ import annotations

import json
from typing import Callable

from ..llm.registry import Registry, RegistryError
from ..llm.types import Message
from ..llm.wire.openai_compat import OpenAICompatClient, WireError

TRIGGER_FRACTION = 0.90
KEEP_RECENT_TOOL_RESULTS = 5
KEEP_RECENT_THINKING = 1
STUB_THRESHOLD_CHARS = 400


def estimate_tokens(messages: list[Message]) -> int:
    total = 0
    for m in messages:
        total += len(m.content or "") + sum(
            len(tc.arguments_json) + len(tc.name) for tc in m.tool_calls
        )
        # thinking is bulky on max-effort sessions — count it so compaction
        # actually fires before a think-heavy transcript hits the context wall
        total += len(m.thinking or "")
    return total // 4 + 4 * len(messages)


def needs_compaction(messages: list[Message], context_window: int) -> bool:
    return estimate_tokens(messages) > TRIGGER_FRACTION * context_window


def stub_tool_results(messages: list[Message]) -> tuple[list[Message], int]:
    """Replace all but the last N large tool results with one-line stubs."""
    tool_indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    stub_candidates = set(tool_indices[:-KEEP_RECENT_TOOL_RESULTS])
    out: list[Message] = []
    stubbed = 0
    for i, m in enumerate(messages):
        if i in stub_candidates and m.content and len(m.content) > STUB_THRESHOLD_CHARS:
            first_line = m.content.split("\n", 1)[0][:80]
            out.append(
                Message(
                    role="tool",
                    content=f"[result elided ({len(m.content)} chars): {first_line}...]",
                    tool_call_id=m.tool_call_id,
                )
            )
            stubbed += 1
        else:
            out.append(m)
    return out, stubbed


def stub_thinking(messages: list[Message]) -> tuple[list[Message], int]:
    """Stage-1 hygiene for the bulky display-only reasoning trace: stub
    `thinking` on all but the most recent assistant turns to a one-line
    placeholder (mirrors stub_tool_results / KEEP_RECENT_TOOL_RESULTS). The
    most recent turn keeps its full trace; older ones are elided."""
    assistant_indices = [i for i, m in enumerate(messages) if m.role == "assistant" and m.thinking]
    # keep the last N thinking-bearing turns intact
    stub_candidates = set(assistant_indices[:-KEEP_RECENT_THINKING])
    out: list[Message] = []
    stubbed = 0
    for i, m in enumerate(messages):
        if i in stub_candidates and m.thinking:
            out.append(
                Message(
                    role=m.role,
                    content=m.content,
                    tool_calls=m.tool_calls,
                    tool_call_id=m.tool_call_id,
                    thinking=f"[thinking elided ({len(m.thinking)} chars)]",
                )
            )
            stubbed += 1
        else:
            out.append(m)
    return out, stubbed


def summarize_older_half(
    messages: list[Message],
    registry: Registry,
    client: OpenAICompatClient,
) -> list[Message]:
    """Replace the older half of the transcript (after the system prompt and
    initial task) with an LLM summary. Raises WireError if the compactor
    model is unreachable — caller falls back to trim-only."""
    if len(messages) < 8:
        return messages
    head, tail = messages[:2], messages[2:]
    cut = len(tail) // 2
    # never split an assistant tool_call from its tool results
    while cut < len(tail) and tail[cut].role == "tool":
        cut += 1
    older, recent = tail[:cut], tail[cut:]
    if not older:
        return messages

    transcript = "\n".join(
        f"{m.role}: {(m.content or '')[:1000]}"
        + ("".join(f" [called {tc.name}({tc.arguments_json[:200]})]" for tc in m.tool_calls))
        for m in older
    )
    spec = registry.resolve("compactor")
    resp = client.complete(
        spec,
        [
            Message(
                role="system",
                content=(
                    "Summarize this coding-agent transcript segment in under 300 words. "
                    "Keep: files read/edited and key findings, decisions made, current "
                    "state of the task. Drop: raw file contents, command output."
                ),
            ),
            Message(role="user", content=transcript),
        ],
        temperature=0.0,
        max_tokens=600,
    )
    summary = Message(
        role="user",
        content="[Earlier progress, summarized]\n" + (resp.message.content or ""),
    )
    return head + [summary] + recent


def compact(
    messages: list[Message],
    context_window: int,
    registry: Registry,
    client: OpenAICompatClient,
    record: Callable[[str, dict], None] | None = None,
) -> list[Message]:
    """Full policy: stub first (free), summarize only if still over, trim as
    a last resort. Logs one `compaction` event describing what happened."""
    before = estimate_tokens(messages)
    messages, stubbed = stub_tool_results(messages)
    messages, stubbed_thinking = stub_thinking(messages)
    stage = "stub"
    if needs_compaction(messages, context_window):
        try:
            messages = summarize_older_half(messages, registry, client)
            stage = "stub+summarize"
        except (WireError, RegistryError):
            # compactor model unreachable or not configured → trim-stubs fallback
            stage = "stub+trim"
        while needs_compaction(messages, context_window) and len(messages) > 6:
            # drop oldest post-task turn; keep system + task
            del messages[2]
            while len(messages) > 2 and messages[2].role == "tool":  # orphaned results
                del messages[2]
    after = estimate_tokens(messages)
    if record:
        record(
            "compaction",
            {
                "stage": stage,
                "stubbed": stubbed,
                "stubbed_thinking": stubbed_thinking,
                "tokens_before": before,
                "tokens_after": after,
            },
        )
    return messages
