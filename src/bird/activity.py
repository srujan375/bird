"""Concise one-line activity headers for terminal surfaces (REPL, bird code).

The harness already emits structured events through ToolContext.record; this
module turns the interesting ones into short "what is the agent doing" lines —
tool name plus a one-line hint of its main argument, never full inputs or
outputs. The TUI does its own equivalent rendering in tui/src/main.ts.
"""

from __future__ import annotations

import json
from typing import Any

MAX_LABEL_LEN = 100


def _label(name: str, arguments_json: str | None) -> str:
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        args = {}
    detail = ""
    if isinstance(args, dict):
        detail = str(
            args.get("command") or args.get("path") or args.get("question")
            or args.get("summary") or ""
        )
    line = " ".join(f"{name} {detail}".split())
    return line if len(line) <= MAX_LABEL_LEN else line[:MAX_LABEL_LEN] + "…"


def format_activity(event_type: str, data: dict[str, Any]) -> list[str]:
    if event_type == "assistant":
        return [f"  › {_label(tc['name'], tc.get('arguments_json'))}" for tc in data.get("tool_calls", [])]
    if event_type == "tool_result" and data.get("is_error"):
        return [f"  ✕ {data.get('name')} failed"]
    if event_type == "kg_ready_notice":
        return ["  ✓ knowledge graph ready — kg_query is live"]
    if event_type == "plan_created":
        return [f"  ◆ plan set: {len(data.get('steps', []))} steps"]
    if event_type == "plan_updated":
        steps = data.get("steps", [])
        closed = sum(1 for s in steps if s.get("status") in ("done", "skipped"))
        return [f"  ◆ step {data.get('step')} → {data.get('status')} ({closed}/{len(steps)} closed)"]
    return []


def attach_printer(ctx: Any) -> None:
    """Tee ctx.record so activity headers print as the harness works."""
    inner = ctx.record

    def record(event_type: str, data: dict[str, Any]) -> None:
        if inner:
            inner(event_type, data)
        for line in format_activity(event_type, data):
            print(line, flush=True)

    ctx.record = record
