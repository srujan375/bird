"""What this session has already worked out about the repo.

The seam problem, measured: a dispatched sub-harness starts from an empty
transcript, so everything the lead (or a previous dispatch) learned dies with
that fork. In one logged session, 42 of 91 file reads — 46% — were a fork
reading a file an earlier fork had already read and never edited. Pure
rediscovery.

This store carries the *conclusions* across that seam, never the file bytes.
Two reasons it must stay small:

  * The fork has to read any file it intends to edit regardless — `old_text`
    must be copied verbatim from a real read — so shipping content would not
    remove the read, only duplicate it.
  * Whatever the store renders lands in `seed_context`, which is the system
    prompt (engine/runner.py) — re-sent on every request and never evicted,
    since compaction always keeps `messages[:2]`. Bulk there is strictly worse
    than bulk in the transcript, which compaction *can* reclaim.

Staleness is handled by content digest rather than by bookkeeping: a finding
records the hash of the file it was made against, and `render` re-hashes
before showing it. A note about a file that has since changed is dropped, so
a finding can never outlive the code it describes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# Budget. The rendered block is permanent system-prompt weight, so it is capped
# hard and the newest findings win — an old note about a file nobody is working
# on is exactly what should fall off first.
MAX_FINDINGS = 40
MAX_RULINGS = 12
MAX_SEEN = 40
MAX_NOTE_CHARS = 240


def digest_of(repo_root: Path, path: str) -> str:
    """Content hash of a repo-relative path, or "" when it cannot be read.

    An unreadable file yields "" and never matches a stored digest, so a
    finding about a deleted file drops out of the render on its own.
    """
    try:
        data = (repo_root / path).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass
class Finding:
    """One thing a harness established about one file."""

    path: str
    by: str  # "lead" | "arch" | "code"
    note: str
    digest: str  # the file's content hash when the note was made

    def is_current(self, repo_root: Path) -> bool:
        return bool(self.digest) and digest_of(repo_root, self.path) == self.digest


@dataclass
class ContextStore:
    """Session-scoped, shared by reference across harness forks.

    `CodeTool` forks its context with `dataclasses.replace`, which is shallow —
    so a store placed on ToolContext is the *same object* in the parent and the
    fork. That is what makes findings flow both ways for free: a code session's
    conclusions are visible to the lead the moment the dispatch returns, and to
    the next dispatch after that. Arch builds a fresh ToolContext and so must be
    handed the store explicitly.
    """

    findings: list[Finding] = field(default_factory=list)
    rulings: list[str] = field(default_factory=list)  # negative results
    seen: dict[str, str] = field(default_factory=dict)  # path -> harness that read it

    def note(self, repo_root: Path, path: str, by: str, note: str) -> None:
        """Record a finding, replacing any earlier note on the same path."""
        note = " ".join(note.split())[:MAX_NOTE_CHARS]
        if not note:
            return
        self.findings = [f for f in self.findings if f.path != path]
        self.findings.append(Finding(path, by, note, digest_of(repo_root, path)))
        del self.findings[:-MAX_FINDINGS]

    def rule_out(self, text: str) -> None:
        text = " ".join(text.split())[:MAX_NOTE_CHARS]
        if text and text not in self.rulings:
            self.rulings.append(text)
            del self.rulings[:-MAX_RULINGS]

    def mark_seen(self, paths: list[str], by: str) -> None:
        """Index a path as already examined. Deliberately weaker than a
        finding: it says someone looked, not what they concluded."""
        for p in paths:
            self.seen.setdefault(p, by)
        for stale in list(self.seen)[:-MAX_SEEN]:
            del self.seen[stale]

    def render(self, repo_root: Path) -> str | None:
        """The seed block, or None when there is nothing current to say.

        The framing is load-bearing. This must never read as "do not open these
        files": a fork still has to read whatever it edits, and a model that
        reconstructs `old_text` from a summary instead of a real read produces
        an edit that either bounces off `old_text not found` or silently patches
        the wrong text. So it states what is known and explicitly reaffirms the
        read-before-edit rule.
        """
        current = [f for f in self.findings if f.is_current(repo_root)]
        unnoted = [p for p in self.seen if p not in {f.path for f in current}]
        if not current and not self.rulings and not unnoted:
            return None

        lines = [
            "# Already established this session",
            "Earlier work in this repo produced the findings below. Treat them as "
            "true — each has been re-checked against the file's current contents. "
            "They save you the search, NOT the read: you must still read any file "
            "you intend to edit, because `old_text` has to be copied verbatim "
            "from real read output.",
        ]
        if current:
            lines.append("")
            for f in current:
                lines.append(f"- {f.path} [{f.by}] — {f.note}")
        if self.rulings:
            lines.append("")
            lines.append("Ruled out (do not re-investigate):")
            lines.extend(f"- {r}" for r in self.rulings)
        if unnoted:
            lines.append("")
            lines.append(
                "Also already examined, with no finding recorded: "
                + ", ".join(sorted(unnoted))
            )
        return "\n".join(lines)
