"""DesignState — the session's shape, and nothing about how it changes.

The state is deliberately small: a prompt, artboards, and per-artboard
version graphs. The HTML itself lives on disk (artboards/ dir) — the state
file carries paths, not megabytes of markup. Everything that changes a
document goes through the op vocabulary in dom.py, applied by DesignSession.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..picker import AskQueue


def _slug(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "artboard"


@dataclass
class Version:
    """One checkpoint of an artboard's document. `kind` is how it came to be:
    'generated' (the first one), 'refined' (a regenerate landed), 'edited'
    (ops checkpointed)."""

    id: str
    kind: str  # generated | refined | edited
    html_path: str  # relative to run_dir, e.g. artboards/a1/v3.html
    note: str = ""  # what changed, in design terms


@dataclass
class Artboard:
    id: str
    title: str
    versions: list[Version] = field(default_factory=list)
    current: str = ""  # current version id
    # insert-palette snippets generated once per artboard (name -> html)
    palette: dict[str, str] = field(default_factory=dict)
    # notes the harness wants to show on the card (e.g. "skill X not found")
    badges: list[str] = field(default_factory=list)

    def current_version(self) -> Version | None:
        for v in self.versions:
            if v.id == self.current:
                return v
        return self.versions[-1] if self.versions else None


@dataclass
class DesignState:
    prompt: str = ""
    artboards: dict[str, Artboard] = field(default_factory=dict)
    selected: str = ""  # artboard id
    finalized: bool = False
    finalized_artboard: str = ""
    # the showcase phase's elevated artboard, or "" outside the phase. Session
    # state, not page state: a disconnected page costs the capture, never the
    # phase, and a reload mid-showcase restores it from here with no
    # special-casing (the late-joiner replay already carries the status).
    showcase_artboard: str = ""
    # the op log, shared by user + AI: [{artboard, version, op, by}]
    op_log: list[dict[str, Any]] = field(default_factory=list)
    # the questions the designer must have answered before it draws — empty in
    # a session with nobody in the room to answer them (see session.py)
    intake: AskQueue = field(default_factory=AskQueue)
    # the designer's submitted plan (design_plan), judged by the critic before
    # the first artboard exists and carried into the render rubric after
    plan: str = ""
    # vision critiques keyed "artboard:version" -> {critique, at, model}. The
    # critique rides in state keyed by the version it judged — visible in the
    # handoff bundle, invisible to the version stack: critique-triggered fixes
    # are ordinary ops, so undo and the history panel keep working unchanged.
    critiques: dict[str, dict[str, Any]] = field(default_factory=dict)
    # the critic's verdict on the plan, kept apart from the per-version ones:
    # it judged intent, not a render, and the page shows it on its own
    plan_critique: str = ""
    # the active design system's name, "" until set_theme. In state rather
    # than only on the session so a resumed run (DesignSession.load) comes
    # back styled the way it was left
    theme: str = ""

    def next_artboard_id(self, title: str) -> str:
        base = _slug(title)
        if base not in self.artboards:
            return base
        n = 2
        while f"{base}-{n}" in self.artboards:
            n += 1
        return f"{base}-{n}"

    def next_version_id(self, artboard: Artboard) -> str:
        n = len(artboard.versions) + 1
        used = {v.id for v in artboard.versions}
        vid = f"v{n}"
        while vid in used:
            n += 1
            vid = f"v{n}"
        return vid

    def record_critique(self, artboard_id: str, version_id: str, critique: str,
                        model: str = "") -> None:
        """Record a vision critique against the version it judged. A second
        critique of the same version overwrites: the latest eyes are the ones
        the bundle should carry."""
        self.critiques[f"{artboard_id}:{version_id}"] = {
            "critique": critique,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": model,
        }

    def critique_for(self, artboard_id: str, version_id: str) -> dict[str, Any] | None:
        """The critique of one version, or None."""
        return self.critiques.get(f"{artboard_id}:{version_id}")

    def latest_critique_version(self, artboard_id: str) -> tuple[str, dict[str, Any]] | None:
        """(version id, critique) for the newest critique of an artboard,
        current version first — see latest_critique for why."""
        artboard = self.artboards.get(artboard_id)
        if artboard is not None and artboard.current:
            found = self.critique_for(artboard_id, artboard.current)
            if found is not None:
                return artboard.current, found
        best: tuple[str, dict[str, Any]] | None = None
        for key, entry in self.critiques.items():
            aid, _, vid = key.partition(":")
            if aid != artboard_id:
                continue
            if best is None or entry.get("at", "") > best[1].get("at", ""):
                best = (vid, entry)
        return best

    def latest_critique(self, artboard_id: str) -> dict[str, Any] | None:
        """The newest critique recorded for an artboard, current version first.

        The handoff wants the final word on the chosen artboard: the critique
        of the version being handed off when there is one, else the most
        recent one — a fix round that ended in an undo still said something
        worth inheriting."""
        found = self.latest_critique_version(artboard_id)
        return found[1] if found else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "artboards": {
                aid: {
                    "id": a.id,
                    "title": a.title,
                    "versions": [vars(v) for v in a.versions],
                    "current": a.current,
                    "palette": a.palette,
                    "badges": a.badges,
                }
                for aid, a in self.artboards.items()
            },
            "selected": self.selected,
            "finalized": self.finalized,
            "finalized_artboard": self.finalized_artboard,
            "showcase_artboard": self.showcase_artboard,
            "op_log": self.op_log,
            "intake": self.intake.to_list(),
            "plan": self.plan,
            "critiques": self.critiques,
            "plan_critique": self.plan_critique,
            "theme": self.theme,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DesignState":
        st = cls(
            prompt=str(data.get("prompt") or ""),
            selected=str(data.get("selected") or ""),
            finalized=bool(data.get("finalized")),
            finalized_artboard=str(data.get("finalized_artboard") or ""),
            showcase_artboard=str(data.get("showcase_artboard") or ""),
            op_log=list(data.get("op_log") or []),
            intake=AskQueue.from_list(data.get("intake")),
            plan=str(data.get("plan") or ""),
            critiques=dict(data.get("critiques") or {}),
            plan_critique=str(data.get("plan_critique") or ""),
            theme=str(data.get("theme") or ""),
        )
        for aid, a in (data.get("artboards") or {}).items():
            versions = [
                Version(
                    id=str(v.get("id")),
                    kind=str(v.get("kind") or "generated"),
                    html_path=str(v.get("html_path") or ""),
                    note=str(v.get("note") or ""),
                )
                for v in a.get("versions", [])
            ]
            st.artboards[str(aid)] = Artboard(
                id=str(a.get("id") or aid),
                title=str(a.get("title") or aid),
                versions=versions,
                current=str(a.get("current") or ""),
                palette=dict(a.get("palette") or {}),
                badges=list(a.get("badges") or []),
            )
        return st