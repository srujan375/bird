"""ArchSession — the harness-owned wrapper the arch tools mutate through.

Holds the ArchState plus the wiring the tools need: the permission broker
(for the two user gates), the arch_state event sink (straight to the
transport — arch_state is a top-level protocol event, not a harness_event),
per-mutation persistence, and the optional judge hook for the challenge
pass. Lives on ToolContext.arch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import render
from .state import ArchState, OpenQuestion

STATE_FILENAME = "arch_state.json"


class ArchSession:
    def __init__(
        self,
        state: ArchState | None = None,
        run_dir: Path | None = None,
        broker: Any | None = None,  # duck: .request(payload) -> (approved, feedback)
        on_state: Callable[[dict[str, Any]], None] | None = None,
        judge: Callable[[ArchState], list[str]] | None = None,
    ) -> None:
        self.state = state or ArchState()
        self.run_dir = run_dir
        self.broker = broker
        self.on_state = on_state
        self.judge = judge

    def state_event(self, changed: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "type": "arch_state",
            "phase": self.state.phase,
            "state": self.state.to_dict(),
            "renders": render.render_all(self.state),
            "tracker": render.tracker(self.state),
            "changed": changed,
        }

    def touched(self, changed_kind: str | None = None, changed_id: str | None = None) -> None:
        """Persist + push the full state after every mutation (mid-turn)."""
        if self.run_dir is not None:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / STATE_FILENAME).write_text(
                json.dumps(self.state.to_dict(), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        if self.on_state is not None:
            changed = None
            if changed_kind is not None and changed_id is not None:
                changed = {"kind": changed_kind, "id": changed_id}
            self.on_state(self.state_event(changed))

    def request_gate(self, payload: dict[str, Any]) -> tuple[bool, str]:
        """Blocking user gate via the permission broker. No broker (headless
        tests) means auto-approve."""
        if self.broker is None:
            return True, ""
        return self.broker.request(payload)

    def run_challenge(self) -> list[OpenQuestion]:
        """The challenge pass: deterministic coverage audit, plus the judge
        model's critique when available. Judge failure degrades to the audit
        alone — the session never blocks on an offline judge."""
        findings = [
            OpenQuestion(id=self._next_qid(), question=q, blocking=blocking, source="harness_audit")
            for q, blocking in coverage_audit(self.state)
        ]
        if self.judge is not None:
            try:
                for q in self.judge(self.state):
                    findings.append(
                        OpenQuestion(id=self._next_qid(len(findings)), question=q,
                                     blocking=False, source="judge")
                    )
            except Exception:
                pass
        self.state.questions.extend(findings)
        return findings

    def _next_qid(self, offset: int = 0) -> str:
        return f"q{len(self.state.questions) + offset + 1}"

    @classmethod
    def load(cls, run_dir: Path, **kw: Any) -> "ArchSession":
        """Restore persisted state for --resume; fresh state if none saved."""
        path = run_dir / STATE_FILENAME
        state = None
        if path.is_file():
            state = ArchState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls(state=state, run_dir=run_dir, **kw)


def coverage_audit(state: ArchState) -> list[tuple[str, bool]]:
    """Deterministic checks → (question, blocking) findings."""
    findings: list[tuple[str, bool]] = []
    if state.scope_is_production():
        failure_names = {f.name.lower() for f in state.flows if f.kind == "failure"}
        for flow in state.happy_flows():
            if not any(flow.name.lower() in n or n in flow.name.lower() for n in failure_names):
                findings.append((
                    f"happy flow '{flow.name}' has no failure twin — what happens when "
                    "a step in it fails?",
                    False,
                ))
    on_flows = {ref for f in state.flows for s in f.steps for ref in (s.src, s.dst)}
    connected = {ref for c in state.connections for ref in (c.src, c.dst)}
    for comp in state.components.values():
        if comp.existing:
            continue
        if comp.id not in on_flows and comp.id not in connected:
            findings.append((
                f"component '{comp.id}' is on no flow and no connection — is it needed, "
                "or should it be merged/deleted?",
                False,
            ))
    for comp in state.components.values():
        facet = comp.facet
        if facet is not None and facet.facet_kind == "store" and not facet.retention:
            findings.append((
                f"store '{comp.id}' has no retention policy — how long does its data live?",
                False,
            ))
    return findings
