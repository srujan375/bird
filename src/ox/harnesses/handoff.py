"""Consuming an architecture handoff bundle.

The seam between arch and code: arch finalizes and writes
`.ox/sessions/<run_id>/bundle/architecture.md`; a code session seeds itself
from that markdown (as stable system-prompt context). This module only knows
how to *locate and read* the doc — deliberately nothing about the bundle's
internal schema, so the consumer stays decoupled from the parked schema
decision. Both `ox code --from-arch` and the lead's `code` tool use it.
"""

from __future__ import annotations

from pathlib import Path

SESSIONS = ("sessions",)  # under .ox/
BUNDLE_MD = "bundle/architecture.md"

SEED_HEADER = (
    "# Architecture handoff\n"
    "A prior architecture session produced the design below. Treat it as the "
    "authoritative spec for this build: honor its components, contracts, and "
    "decisions. Derive your plan from it, then implement against the actual "
    "repository (the design may not have seen this codebase — reconcile file "
    "layout as you go).\n\n---\n"
)


def sessions_dir(repo_root: Path) -> Path:
    return repo_root / ".ox" / "sessions"


def bundle_md_path(run_dir: Path) -> Path:
    return run_dir / BUNDLE_MD


def find_bundle_dir(repo_root: Path, run_id: str) -> Path | None:
    """Resolve a run-id (or the literal 'latest') to a session dir that holds
    a finalized bundle. Returns None if nothing matches."""
    root = sessions_dir(repo_root)
    if not root.is_dir():
        return None
    if run_id == "latest":
        finalized = [d for d in root.iterdir() if bundle_md_path(d).is_file()]
        if not finalized:
            return None
        return max(finalized, key=lambda d: bundle_md_path(d).stat().st_mtime)
    # exact name, or the run-id prefix the session dirs are named with
    for entry in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if entry.name == run_id or entry.name.startswith(run_id + "-"):
            if bundle_md_path(entry).is_file():
                return entry
    return None


def read_seed(repo_root: Path, run_id: str) -> str | None:
    """The architecture doc wrapped with instructions, ready to hand a Runner
    as seed_context. None if no finalized bundle matches `run_id`."""
    run_dir = find_bundle_dir(repo_root, run_id)
    if run_dir is None:
        return None
    return seed_from_md(bundle_md_path(run_dir).read_text(encoding="utf-8"))


def seed_from_md(markdown: str) -> str:
    return SEED_HEADER + markdown
