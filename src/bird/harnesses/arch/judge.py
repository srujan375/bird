"""The critic: a second model reading the design and filing objections.

It runs at turn boundaries, not once at the end, so problems arrive while the
design is still cheap to change — and it files `Concern`s rather than open
questions, because "this falls over at 10k writes/sec" is an objection, not a
request for information.

Findings are advice. The architect answers them, accepts them, or overrules
them with a reason; nothing here can block the session, and a critic that is
offline or slow degrades to silence.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ...llm.types import Message
from .state import CONCERN_SEVERITIES, ArchState

MAX_FINDINGS = 5

CRITIQUE_SYSTEM = (
    "You are a principal engineer reviewing a system architecture that is still "
    "being designed. You get the design state as JSON. Find what is actually "
    "wrong with it — be specific and concrete, and prefer one sharp objection "
    "over five vague ones.\n\n"
    "Look for:\n"
    "1. Breakage under the stated scale/scope in the brief — where does this "
    "design fall over at the numbers given?\n"
    "2. Correctness holes — dual writes, lost events, races, unbounded growth, "
    "missing idempotency, data that has no owner.\n"
    "3. Cost that buys nothing — a component or hop that can be merged or "
    "deleted without losing a stated goal.\n\n"
    "Format — one finding per line, nothing else:\n"
    "- [severity] target | what breaks, concretely | the cheaper or safer option\n"
    "severity is one of: blocker (the design is wrong and will not work), "
    "risk (it works but will hurt), smell (it is more than it needs to be). "
    "target is a component/decision id from the state, or 'design' for the "
    "whole thing.\n\n"
    f"At most {MAX_FINDINGS} findings. No preamble, no praise, no restating the "
    "design. Do NOT repeat an objection listed as already open. If you have "
    "nothing worth saying, reply with exactly: OK"
)


def make_judge(registry: Any, client: Any) -> Callable[[ArchState], list[dict[str, str]]]:
    """Returns a critic callable ArchState -> findings, using the `judge` alias."""

    def judge(state: ArchState) -> list[dict[str, str]]:
        spec = registry.resolve("judge")
        messages = [
            Message(role="system", content=CRITIQUE_SYSTEM),
            Message(role="user", content=_review_prompt(state)),
        ]
        resp = client.complete(spec, messages)
        return parse_findings(resp.message.content or "")

    return judge


def _review_prompt(state: ArchState) -> str:
    import json

    parts = ["Architecture state:\n" + json.dumps(state.to_dict(), indent=1)]
    already = [f"- [{c.severity}] {c.target}: {c.claim}" for c in state.concerns if c.open]
    if already:
        parts.append("Already open — do not repeat these:\n" + "\n".join(already))
    return "\n\n".join(parts)


_LINE_RE = re.compile(r"^\s*[-*]\s*(?:\[(?P<sev>\w+)\]\s*)?(?P<rest>.+)$")


def parse_findings(text: str) -> list[dict[str, str]]:
    """Tolerant parser: the strict form is '- [sev] target | claim | alternative',
    but a bare '- something is wrong' still lands as a risk against the design."""
    findings: list[dict[str, str]] = []
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        rest = m.group("rest").strip()
        if len(rest) < 10:
            continue
        sev = (m.group("sev") or "").lower()
        if sev not in CONCERN_SEVERITIES:
            sev = "risk"
        bits = [b.strip() for b in rest.split("|")]
        if len(bits) >= 2:
            target, claim = bits[0] or "design", bits[1]
            alternative = bits[2] if len(bits) > 2 else ""
        else:
            target, claim, alternative = "design", bits[0], ""
        if not claim:
            continue
        findings.append({
            "severity": sev,
            "target": target,
            "claim": claim,
            "alternative": alternative,
        })
        if len(findings) == MAX_FINDINGS:
            break
    return findings
