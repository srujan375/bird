"""ArchSession — the harness-owned wrapper the arch tools mutate through.

Holds the ArchState plus the wiring the tools need: the permission broker
(for the two user gates), the arch_state event sink (straight to the
transport — arch_state is a top-level protocol event, not a harness_event),
per-mutation persistence, and the critic.

The critic runs *off the turn*: `start_critic` is called at each turn boundary
and returns immediately, with the judge model reviewing a snapshot on a daemon
thread. Findings land as Concerns and push a state event of their own, so an
objection can appear on the page while the architect is still talking. A critic
that is offline, slow or malformed degrades to silence — the session never
waits on it and never fails because of it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable

from . import render
from .state import ArchState, Concern

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
        self._lock = threading.Lock()
        self._write_lock = threading.RLock()  # touched() may nest via file_concerns
        self._critic_thread: threading.Thread | None = None
        self._reviewed: str | None = None  # fingerprint of the last design reviewed

    def state_event(self, changed: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "type": "arch_state",
            "phase": self.state.phase,
            "state": self.state.to_dict(),
            "renders": render.render_all(self.state),
            "tracker": render.tracker(self.state),
            # thinness keyed by subject so the page can mark the node itself;
            # advisory, exactly like everywhere else it appears
            "gaps": self.state.gaps_by_subject(),
            "changed": changed,
        }

    def touched(self, changed_kind: str | None = None, changed_id: str | None = None) -> None:
        """Persist + push the full state after every mutation (mid-turn).

        Serialized: the critic calls this from its own thread, so two writers
        must never interleave into the state file or the event stream."""
        with self._write_lock:
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

    def apply_mutation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A user edit made on the page, applied through the model's own code
        paths. Duck-typed on purpose: the pump finds this by name on `ctx.arch`,
        so `POST /mutate` needs no knowledge that the arch harness exists.

        Held under the write lock, so a mutation arriving on an HTTP thread
        cannot interleave with the critic's own writes or a half-written state
        file. Raises MutationError, which the transport turns into a 400."""
        from .mutate import apply_mutation

        with self._write_lock:  # touched() re-enters it on this thread
            return apply_mutation(self, payload)

    def request_gate(self, payload: dict[str, Any]) -> tuple[bool, str]:
        """Blocking user gate via the permission broker. No broker (headless
        tests) means auto-approve."""
        if self.broker is None:
            return True, ""
        return self.broker.request(payload)

    # ---- concerns: the record of disagreement ----

    def file_concerns(self, findings: list[dict[str, str]], source: str) -> list[Concern]:
        """Add findings as Concerns, skipping ones already on the books.

        Dedupe spans every status, not just open ones: an objection the user
        already overruled must not come back next turn wearing a new id."""
        added: list[Concern] = []
        with self._lock:
            seen = {_norm(c.claim) for c in self.state.concerns}
            for f in findings:
                claim = (f.get("claim") or "").strip()
                key = _norm(claim)
                if not claim or key in seen:
                    continue
                seen.add(key)
                concern = Concern(
                    id=f"c{len(self.state.concerns) + 1}",
                    severity=f.get("severity", "risk"),
                    target=(f.get("target") or "design").strip(),
                    claim=claim,
                    alternative=(f.get("alternative") or "").strip(),
                    source=source,
                )
                self.state.concerns.append(concern)
                added.append(concern)
        return added

    def run_audit(self) -> list[Concern]:
        """The deterministic coverage pass — no model, always available."""
        return self.file_concerns(coverage_audit(self.state), "harness_audit")

    # ---- the critic, off the turn ----

    def start_critic(self) -> None:
        """Kick a review of the current design on a daemon thread, if the
        design has changed since the last one and no review is in flight.
        Returns immediately: the turn never waits for the critic."""
        if self.judge is None:
            return
        if self._critic_thread is not None and self._critic_thread.is_alive():
            return
        if not self.state.components and not self.state.sketchbook.variants:
            return  # nothing to review yet
        try:
            snapshot = ArchState.from_dict(self.state.to_dict())
        except Exception:
            return
        fingerprint = _fingerprint(snapshot)
        if fingerprint == self._reviewed:
            return
        self._reviewed = fingerprint
        self._critic_thread = threading.Thread(
            target=self._critic_pass, args=(snapshot,), daemon=True, name="arch-critic",
        )
        self._critic_thread.start()

    def _critic_pass(self, snapshot: ArchState) -> None:
        try:
            findings = self.judge(snapshot) if self.judge is not None else []
        except Exception:
            return  # offline / malformed judge: silence, never a session failure
        if not findings:
            return
        filed = self.file_concerns(findings, "judge")
        if filed:
            # its own state push: the objection lands on the page mid-turn,
            # while the architect is still talking
            self.touched("concern", filed[0].id)

    def next_qid(self) -> str:
        return f"q{len(self.state.questions) + 1}"

    @classmethod
    def load(cls, run_dir: Path, **kw: Any) -> "ArchSession":
        """Restore persisted state for --resume; fresh state if none saved."""
        path = run_dir / STATE_FILENAME
        state = None
        if path.is_file():
            state = ArchState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls(state=state, run_dir=run_dir, **kw)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _fingerprint(state: ArchState) -> str:
    """What the critic reviews: the design, not the conversation around it.
    Concerns are excluded so filing one never re-triggers a pass."""
    d = state.to_dict()
    d.pop("concerns", None)
    blob = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def coverage_audit(state: ArchState) -> list[dict[str, str]]:
    """Deterministic checks, in the critic's finding shape. No model involved,
    so these are the objections that are always available."""
    findings: list[dict[str, str]] = []
    if state.scope_is_production():
        failure_names = {f.name.lower() for f in state.flows if f.kind == "failure"}
        for flow in state.happy_flows():
            if not any(flow.name.lower() in n or n in flow.name.lower() for n in failure_names):
                findings.append({
                    "severity": "risk",
                    "target": flow.id,
                    "claim": f"happy flow '{flow.name}' has no failure twin — nothing says "
                             "what happens when a step in it fails",
                    "alternative": f"record a failure flow for '{flow.name}'",
                })
    on_flows = {ref for f in state.flows for s in f.steps for ref in (s.src, s.dst)}
    connected = {ref for c in state.connections for ref in (c.src, c.dst)}
    for comp in state.components.values():
        if comp.existing:
            continue
        if comp.id not in on_flows and comp.id not in connected:
            findings.append({
                "severity": "smell",
                "target": comp.id,
                "claim": f"component '{comp.id}' is on no flow and no connection — nothing "
                         "in the design uses it",
                "alternative": "merge it into a neighbour or delete it",
            })
    for comp in state.components.values():
        facet = comp.facet
        if facet is not None and facet.facet_kind == "store" and not facet.retention:
            findings.append({
                "severity": "risk",
                "target": comp.id,
                "claim": f"store '{comp.id}' has no retention policy — its data grows "
                         "without bound",
                "alternative": "state how long each entity lives, or say explicitly that it is kept forever",
            })
    return findings
