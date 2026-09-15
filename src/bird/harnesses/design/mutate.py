"""The edit seam — the one door every document change goes through.

The model's edit tools and the page's inspector send the SAME ops from dom.py's
vocabulary, and both land here: `apply`. One validation path, two front ends —
a selector that misses is refused with a MutationError the page rolls its
optimistic edit back from. Every applied op is checkpointed as a Version (the
op, the actor, the resulting HTML); `undo` pops the last Version and restores
the one before it, user and AI ops interleaved on one stack per artboard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import dom
from .session import DesignSession, STATUS_READY
from .state import Version

ACTORS = ("user", "ai")


class MutationError(Exception):
    """A refusal both front ends show the same way; the page rolls back."""


_BRIDGE_CACHE: str | None = None


def _bridge_script() -> str:
    """The iframe bridge, read once and cached. It lives beside this module,
    not in static/: static/ is the built Workbench page (source in design-ui/,
    `npm run build` empties it), and the bridge is harness plumbing that rides
    inside every stored artboard rather than part of the page. A missing file
    degrades to "": the caller stores the document unwrapped rather than
    crashing every edit."""
    global _BRIDGE_CACHE
    if _BRIDGE_CACHE is None:
        path = Path(__file__).parent / "editor-bridge.js"
        try:
            _BRIDGE_CACHE = path.read_text(encoding="utf-8")
        except OSError:
            _BRIDGE_CACHE = ""
    return _BRIDGE_CACHE


def apply(session: DesignSession, artboard_id: str, op: dict[str, Any], actor: str = "user") -> Version:
    """Apply one dom.py op to an artboard as `actor` ('user' or 'ai') and return
    the Version it produced. Raises MutationError with a message meant to be read
    by a person, since it may be going onto the page."""
    if actor not in ACTORS:
        raise MutationError(f"actor must be 'user' or 'ai', not {actor!r}.")
    if session.state.finalized:
        raise MutationError("the artboard was finalized — the session is read-only now.")
    # the showcase phase owns the screen: the board is gone, so an op on any
    # other artboard would land on a document nobody is looking at
    elevated = session.state.showcase_artboard
    if elevated and artboard_id != elevated:
        raise MutationError(
            f"the showcase phase owns the screen — edits lock to {elevated!r} "
            "until Back returns the board.")
    if artboard_id not in session.state.artboards:
        raise MutationError(f"no artboard {artboard_id!r} (known: {', '.join(session.state.artboards) or 'none'}).")
    if not isinstance(op, dict):
        raise MutationError("an edit is one op dict from dom.py's vocabulary.")
    html = session.read_html(artboard_id)
    if not html:
        raise MutationError(f"artboard {artboard_id!r} has no document yet — nothing to edit.")
    root = dom.parse(html)
    try:
        dom.apply_op(root, op)
    except dom.DomError as e:
        raise MutationError(str(e)) from e
    if session.run_dir is None:  # memory-only: stash the html this op replaces
        _stash(session, artboard_id, html)
    session.status = STATUS_READY  # the edit landed; nothing is in flight
    # write_version wraps: the bridge rides with every stored version
    version = session.write_version(
        artboard_id, dom.serialize(root), "edited",
        note=f"{actor}: {op.get('op') or 'edit'}",
    )
    session.state.op_log.append(
        {"artboard": artboard_id, "version": version.id, "op": op, "by": actor}
    )
    session.touched("artboard", artboard_id)
    return version


def undo(session: DesignSession, artboard_id: str) -> Version:
    """Pop the last Version — the user's or the model's — and restore the html
    of the one before it. A failed restore puts the popped version back: an
    undo that cannot complete must not eat the entry it was undoing."""
    if session.state.finalized:
        raise MutationError("the artboard was finalized — the session is read-only now.")
    elevated = session.state.showcase_artboard
    if elevated and artboard_id != elevated:
        raise MutationError(
            f"the showcase phase owns the screen — edits lock to {elevated!r} "
            "until Back returns the board.")
    artboard = session.state.artboards.get(artboard_id)
    if artboard is None:
        raise MutationError(f"no artboard {artboard_id!r} (known: {', '.join(session.state.artboards) or 'none'}).")
    if len(artboard.versions) < 2:
        raise MutationError(f"nothing to undo on {artboard_id!r} — only its first version exists.")
    popped = artboard.versions.pop()
    prior = artboard.versions[-1]
    artboard.current = prior.id
    try:
        html = _prior_html(session, artboard_id, prior)
    except (MutationError, OSError) as e:
        artboard.versions.append(popped)
        artboard.current = popped.id
        raise MutationError(f"could not undo {popped.id}: {e}") from e
    # restored exactly as stored — unwrap happens only at the handoff boundary
    session._html[artboard_id] = html  # the same cache write_version maintains
    _drop_op_log_entry(session, artboard_id, popped.id)
    session.touched("artboard", artboard_id)
    return prior


def _drop_op_log_entry(session: DesignSession, artboard_id: str, version_id: str) -> None:
    """The undone op leaves the log with the version it produced. Otherwise the
    handoff's edit history cites versions that no longer exist, and reads as
    though a change the user took back is still in the design."""
    log = session.state.op_log
    for i in range(len(log) - 1, -1, -1):
        e = log[i]
        if e.get("artboard") == artboard_id and e.get("version") == version_id:
            del log[i]
            return


def _stash(session: DesignSession, artboard_id: str, html: str) -> None:
    """The html apply() replaced — undo's source when the session is memory-only."""
    stacks = getattr(session, "_mutate_prior", None)
    if stacks is None:
        stacks = session._mutate_prior = {}
    stacks.setdefault(artboard_id, []).append(html)


def _prior_html(session: DesignSession, artboard_id: str, prior: Version) -> str:
    """The html before the popped version: the version's file when the session
    keeps files, else the stash apply() left (memory-only sessions)."""
    if session.run_dir is not None:
        return (session.run_dir / prior.html_path).read_text(encoding="utf-8")
    stack = getattr(session, "_mutate_prior", {}).get(artboard_id) or []
    if not stack:
        raise MutationError(f"version {prior.id} has no document to restore.")
    return stack.pop()