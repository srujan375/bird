"""User mutations — what the page changes, applied the way the model changes it.

The canvas is a projection of ArchState, so an edit made on the canvas has to
land in exactly one place: the same `_apply_component` / `_promote` code the
model's tools call. That is what keeps validation, the amendment audit trail and
the state push identical whether a rename came from the architect or from the
person reading it.

Three ops, matching the three things the page can do to the architecture:

    {"op": "component", "id": "order-db", "name": "...", "responsibility": "..."}
    {"op": "concern",   "id": "c1", "status": "overruled", "resolution": "..."}
    {"op": "promote",   "variant_id": "v2", "replace": true}

Deliberately absent: creating or removing components and connections. v1's
answer to "can the user draw on the canvas" is agent-only — the user edits what
already exists, and asks the architect for the rest.

The reply is `{"ok": true, "applied": "..."}`, not the new state. State travels
on one channel only — the `arch_state` SSE push that `touched()` makes — because
a second copy of it over a different wire is a second thing to keep in sync. A
rejected edit comes back `{"ok": false, "error": "..."}` with the validation
message the model would have seen, and the page rolls its optimistic edit back.
"""

from __future__ import annotations

from typing import Any

from .session import ArchSession
from .state import CONCERN_STATUSES

# What a user may edit on a component: prose about a box that already exists.
# `kind` is structural and `id` is immutable — both stay the architect's call.
EDITABLE_COMPONENT_FIELDS = (
    "name", "responsibility", "tech", "data_owned", "failure_notes", "trace",
)

RESOLVABLE = tuple(s for s in CONCERN_STATUSES if s != "open")


class MutationError(Exception):
    """A refusal the page should show and roll back from."""


def apply_mutation(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one user mutation. Raises MutationError with a message meant to be
    read by a person, since it is going onto the page rather than to a model."""
    if session.state.phase == "finalized":
        raise MutationError("the session is finalized — the architecture is read-only.")
    op = str(payload.get("op") or "").strip()
    handler = _OPS.get(op)
    if handler is None:
        raise MutationError(f"unknown mutation {op!r} (expected one of: {', '.join(_OPS)}).")
    return handler(session, payload)


def _component(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    from .tools import _apply_component, _post_approval_amendment  # circular at module scope

    state = session.state
    cid = str(payload.get("id") or "").strip()
    if not cid:
        raise MutationError("which component? no id was sent.")
    if cid not in state.components:
        raise MutationError(
            f"no component {cid!r} — the page is showing something the harness does not have."
        )
    args = {k: payload[k] for k in EDITABLE_COMPONENT_FIELDS if k in payload}
    if not args:
        raise MutationError(
            "nothing to change — editable fields are: " + ", ".join(EDITABLE_COMPONENT_FIELDS) + "."
        )
    if "name" in args and not str(args["name"]).strip():
        raise MutationError("a component needs a name.")
    if "trace" in args and not isinstance(args["trace"], list):
        # list(str) would silently explode a sentence into single characters
        raise MutationError("trace must be a list of brief goals this component serves.")
    args["id"] = cid
    try:
        action = _apply_component(session, args, cid)
    except Exception as e:  # ToolError and friends carry the message we want
        raise MutationError(str(e)) from e
    # same audit trail the tools leave: a post-approval edit is recorded, and it
    # says who made it, because "the user renamed this" is worth knowing later
    _post_approval_amendment(session, f"user edit — {action}", structural=False)
    session.touched("component", cid)
    return {"applied": action}


def _concern(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("id") or "").strip()
    concern = next((c for c in session.state.concerns if c.id == cid), None)
    if concern is None:
        known = ", ".join(c.id for c in session.state.concerns) or "none"
        raise MutationError(f"no concern {cid!r} (known: {known}).")
    status = str(payload.get("status") or "").strip()
    if status not in RESOLVABLE:
        raise MutationError(f"status must be one of {', '.join(RESOLVABLE)}.")
    resolution = str(payload.get("resolution") or "").strip()
    # Overruling is the one that outlives the session: it is what the code
    # harness inherits as "we knew, we chose anyway". Without the reason it is
    # worth nothing, so it is refused rather than filled in with a placeholder.
    if status == "overruled" and not resolution:
        raise MutationError("overruling an objection needs a reason — that reason is the record.")
    concern.status = status
    concern.resolution = resolution
    session.touched("concern", cid)
    return {"applied": f"Concern {cid} {status}."}


def _promote(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    from .tools import _promote as promote_variant

    book = session.state.sketchbook
    vid = str(payload.get("variant_id") or book.active or "").strip()
    variant = book.variants.get(vid)
    if variant is None:
        known = ", ".join(book.variants) or "none"
        raise MutationError(f"no variant {vid!r} to promote (known: {known}).")
    if not variant.nodes:
        raise MutationError(f"variant {variant.id} ({variant.name}) has nothing sketched in it yet.")
    added_c, added_conn, kept = promote_variant(session, variant, replace=bool(payload.get("replace")))
    session.touched("variant", variant.id)
    tail = f", {kept} already seeded" if kept else ""
    return {
        "applied": f"Chose '{variant.name}': seeded {added_c} component(s) "
                   f"and {added_conn} connection(s){tail}."
    }


_OPS = {"component": _component, "concern": _concern, "promote": _promote}
