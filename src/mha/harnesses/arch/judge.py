"""Challenge-phase critique: dump ArchState to the judge model, get findings.

Findings land as OpenQuestions (source "judge") via ArchSession.run_challenge.
Any failure here degrades to the deterministic coverage audit alone — the
session must never block on an offline judge.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ...llm.types import Message

MAX_FINDINGS = 5

CRITIQUE_SYSTEM = (
    "You are a principal engineer reviewing a system architecture before it is "
    "finalized. You will get the full design state as JSON. Look for exactly two "
    "things:\n"
    "1. Breakage under the stated scale/scope in the brief: where does this design "
    "fall over at the numbers given (throughput, data volume, availability)?\n"
    "2. Simplification: what can be merged or deleted without losing a stated goal? "
    "Flag any component whose trace does not justify its cost.\n\n"
    f"Reply with at most {MAX_FINDINGS} findings, one per line, each starting with "
    "'- '. Only findings — no preamble, no praise, no restating the design. If the "
    "design is sound, reply with exactly: OK"
)


def make_judge(registry: Any, client: Any) -> Callable:
    """Returns a judge callable ArchState -> list[str] using the `judge` alias."""

    def judge(state: Any) -> list[str]:
        spec = registry.resolve("judge")
        messages = [
            Message(role="system", content=CRITIQUE_SYSTEM),
            Message(
                role="user",
                content="Architecture state:\n" + json.dumps(state.to_dict(), indent=1),
            ),
        ]
        resp = client.complete(spec, messages)
        return parse_findings(resp.message.content or "")

    return judge


def parse_findings(text: str) -> list[str]:
    findings = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- ") and len(line) > 10:
            findings.append(line[2:].strip())
    return findings[:MAX_FINDINGS]
