"""User mutations — what the page changes, applied the way the model changes it.

The canvas is a projection of ArchState, so an edit made on the canvas has to
land in exactly one place: the same `_upsert_node` code the architect's own
tools call. That is what keeps validation and the state push identical whether a
rename came from the architect or from the person reading it.

Four ops, matching what a reader actually does to a design in front of them:

    {"op": "node",     "id": "order-store", "label": "...", "responsibility": "..."}
    {"op": "approach", "id": "queue-first", "status": "greyed", "rejected_reason": "..."}
    {"op": "move",     "id": "order-store", "x": 590, "y": 250}
    {"op": "note",     "id": "n3", "text": "...", "x": 60, "y": 780, "anchor": "order-store"}

Greying an approach from the page is the important one: "I'm taking the left
one" is the user's call to make, and it should not require asking the model to
type it out.

`move` is the other: arranging the board is how a person says what belongs
with what, and an arrangement that dies with the tab was never really part of
the design. The architect never sends one — no tool takes coordinates — so this
is the only writer.

    {"op": "add_box",    "id": "rate-limiter", "label": "Rate limiter", "x": 200, "y": 300}
    {"op": "remove_box", "id": "rate-limiter"}
    {"op": "connect",    "src": "api", "dst": "rate-limiter"}
    {"op": "disconnect", "src": "api", "dst": "rate-limiter"}

Drawing is talking. A box the user puts up arrives as a stub with no kind —
"here is a thing, you tell me what it is" — and the architect sees it in the
next turn's note, because it is in the same graph everything else is in. That
is the conversation, held in the other medium.

Deliberately absent: reassigning which approach a box belongs to. Dragging a
box across a column boundary is a design change with a reason behind it, and
the reason is what the architect is for.

The reply is `{"ok": true, "applied": "..."}`, not the new state. State travels
on one channel only — the `arch_state` SSE push `touched()` makes — because a
second copy of it over a different wire is a second thing to keep in sync. A
rejected edit comes back `{"ok": false, "error": "..."}` with the validation
message the model would have seen, and the page rolls its optimistic edit back.
"""

from __future__ import annotations

from typing import Any

from .session import ArchSession
from .state import Annotation, Edge, Node, slug

# What a reader may change about a box: prose about something that already
# exists. `kind` is structural and `id` is immutable — both stay the
# architect's call.
EDITABLE_NODE_FIELDS = ("label", "kind", "responsibility", "tech", "detail", "notes", "depth", "facts", "items")


class MutationError(Exception):
    """A refusal the page should show and roll back from."""


def apply_mutation(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one user mutation. Raises MutationError with a message meant to be
    read by a person, since it is going onto the page rather than to a model."""
    if session.state.handed_off:
        raise MutationError("the design was handed off — it is read-only now.")
    op = str(payload.get("op") or "").strip()
    handler = _OPS.get(op)
    if handler is None:
        raise MutationError(f"unknown mutation {op!r} (expected one of: {', '.join(_OPS)}).")
    return handler(session, payload)


def _node(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    from .tools import _upsert_node  # circular at module scope

    nid = str(payload.get("id") or "").strip()
    if not nid:
        raise MutationError("which box? no id was sent.")
    if nid not in session.state.nodes:
        raise MutationError(
            f"no box {nid!r} — the page is showing something the harness does not have."
        )
    spec = {k: payload[k] for k in EDITABLE_NODE_FIELDS if k in payload}
    if not spec:
        raise MutationError(
            "nothing to change — editable fields are: " + ", ".join(EDITABLE_NODE_FIELDS) + "."
        )
    if "label" in spec and not str(spec["label"]).strip():
        raise MutationError("a box needs a label.")
    before = session.state.nodes[nid].label
    spec["id"] = nid
    try:
        _upsert_node(session, spec, [])
    except Exception as e:  # ToolError and friends carry the message we want
        raise MutationError(str(e)) from e
    after = session.state.nodes[nid].label
    if after != before:
        session.note_user_edit(f'renamed "{before}" to "{after}" ({nid})')
    else:
        touched = ", ".join(k for k in spec if k != "id")
        session.note_user_edit(f"edited {nid}: {touched}")
    session.touched("node", nid)
    return {"applied": f"Updated {nid}."}


def _approach(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    from dataclasses import replace

    state = session.state
    aid = str(payload.get("id") or "").strip()
    approach = state.approaches.get(aid)
    if approach is None:
        known = ", ".join(state.approaches) or "none"
        raise MutationError(f"no approach {aid!r} (known: {known}).")
    candidate = replace(approach)
    if "status" in payload:
        candidate.status = str(payload["status"] or "").strip()
    if "rejected_reason" in payload:
        candidate.rejected_reason = str(payload["rejected_reason"] or "").strip()
    try:
        state.validate_approach(candidate)
    except ValueError as e:
        # the "greyed needs a reason" rule reaches the page verbatim: without
        # the reason the greying is worth nothing, so it is refused rather
        # than filled in with a placeholder
        raise MutationError(str(e)) from e
    state.approaches[aid] = candidate
    if candidate.status == "greyed":
        session.note_user_edit(
            f"took '{candidate.name}' off the table: {candidate.rejected_reason}"
        )
    else:
        session.note_user_edit(f"put '{candidate.name}' back on the table")
    session.touched("approach", aid)
    verb = "greyed out" if candidate.status == "greyed" else "brought back"
    return {"applied": f"'{candidate.name}' {verb}."}


def _move(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Put a box somewhere. Position only — which approach it belongs to stays
    the architect's call."""
    nid = str(payload.get("id") or "").strip()
    node = session.state.nodes.get(nid)
    if node is None:
        raise MutationError(
            f"no box {nid!r} — the page is showing something the harness does not have."
        )
    try:
        x, y = float(payload["x"]), float(payload["y"])
    except (KeyError, TypeError, ValueError):
        raise MutationError("a move needs numeric x and y.") from None
    node.x, node.y = x, y
    # deliberately not reported to the architect: a move is where a box sits,
    # not what the design says. Telling it every time somebody tidies the board
    # would bury the edits that do mean something.
    session.touched("node", nid)
    return {"applied": f"Moved {nid}."}


def _tidy(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Hand the whole board back to the layout.

    Every hand-placed box is an override of where the page would have put it,
    and the page cannot arrange around one — the point of a position somebody
    chose is that nothing else gets to move it. So the only way back is to say
    so, which is this. Positions only; nothing about the design changes, and for
    the same reason `move` gives, the architect is not told."""
    del payload
    moved = 0
    for node in session.state.nodes.values():
        if node.x is None and node.y is None:
            continue
        node.x, node.y = None, None
        moved += 1
    if not moved:
        raise MutationError("nothing has been moved by hand — the board is already arranged.")
    # no `changed` id: a re-arrangement is the whole board, and haloing one box
    # would claim something about it that is not true
    session.touched()
    return {"applied": f"Re-arranged {moved} box{'es' if moved != 1 else ''}."}


def _note(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Add, retext, move or remove a note on the board.

    An empty text removes it — the same gesture as clearing the box on the
    canvas, so the page never has to send a separate delete."""
    state = session.state
    nid = str(payload.get("id") or "").strip()
    if not nid:
        raise MutationError("which note? no id was sent.")
    text = str(payload.get("text", "")).strip()
    existing = state.annotation_by_id(nid)

    if existing is not None and not text and "text" in payload:
        state.annotations = [a for a in state.annotations if a.id != nid]
        session.note_user_edit(f'took the note "{existing.text}" off the board')
        session.touched("note", nid)
        return {"applied": f"Removed note {nid}."}

    if existing is None:
        if not text:
            raise MutationError("a new note needs text.")
        anchor = str(payload.get("anchor") or "").strip()
        if anchor and anchor not in state.nodes:
            raise MutationError(f"cannot pin a note to {anchor!r} — no such box.")
        state.annotations.append(Annotation(
            id=nid, text=text,
            x=float(payload.get("x", 0)), y=float(payload.get("y", 0)),
            w=float(payload.get("w", 190)), anchor=anchor,
        ))
        session.note_user_edit(
            f'pinned a note to {anchor}: "{text}"' if anchor
            else f'left a note on the board: "{text}"'
        )
        session.touched("note", nid)
        return {"applied": f"Note {nid} added."}

    if "text" in payload and existing.text != text:
        existing.text = text
        session.note_user_edit(f'reworded a note to: "{text}"')
    for field_name in ("x", "y", "w"):
        if field_name in payload:
            try:
                setattr(existing, field_name, float(payload[field_name]))
            except (TypeError, ValueError):
                raise MutationError(f"{field_name} must be a number.") from None
    if "anchor" in payload:
        anchor = str(payload.get("anchor") or "").strip()
        if anchor and anchor not in state.nodes:
            raise MutationError(f"cannot pin a note to {anchor!r} — no such box.")
        existing.anchor = anchor
    session.touched("note", nid)
    return {"applied": f"Note {nid} updated."}


def _add_box(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    """A box the user drew. A stub with no kind, on purpose: it says "here is a
    thing" and leaves what it is to the conversation."""
    state = session.state
    label = str(payload.get("label") or "").strip()
    raw = str(payload.get("id") or "").strip()
    if not raw and not label:
        raise MutationError("a box needs a label.")
    nid = slug(raw or label)
    if nid in state.nodes:
        raise MutationError(f"there is already a box called {nid!r}.")
    node = Node(id=nid, label=label or nid, kind="service", depth="stub")
    for axis in ("x", "y"):
        if axis in payload:
            try:
                setattr(node, axis, float(payload[axis]))
            except (TypeError, ValueError):
                raise MutationError(f"{axis} must be a number.") from None
    try:
        state.validate_node(node)
    except ValueError as e:
        raise MutationError(str(e)) from e
    state.nodes[nid] = node
    session.note_user_edit(
        f'drew a box "{node.label}" ({nid}) — no kind, no responsibility; ask what it is'
    )
    session.touched("node", nid)
    return {"applied": f"Added {nid}.", "id": nid}


def _remove_box(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    state = session.state
    nid = str(payload.get("id") or "").strip()
    if nid not in state.nodes:
        raise MutationError(
            f"no box {nid!r} — the page is showing something the harness does not have."
        )
    dropped = state.references_to(nid)
    state.orphan_children(nid)
    state.edges = [e for e in state.edges if nid not in (e.src, e.dst)]
    state.annotations = [a for a in state.annotations if a.anchor != nid]
    label = state.nodes[nid].label
    del state.nodes[nid]
    session.note_user_edit(f'deleted the box "{label}" ({nid})')
    session.touched("node", nid)
    tail = f" and {len(dropped)} edge(s)" if dropped else ""
    return {"applied": f"Removed {nid}{tail}."}


def _connect(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    state = session.state
    src = str(payload.get("src") or "").strip()
    dst = str(payload.get("dst") or "").strip()
    edge = Edge(src=src, dst=dst, label=str(payload.get("label") or "").strip())
    try:
        state.validate_edge(edge)
    except ValueError as e:
        raise MutationError(str(e)) from e
    if state.edge_index(src, dst) >= 0:
        raise MutationError(f"{src} already connects to {dst}.")
    state.edges.append(edge)
    session.note_user_edit(f"drew a wire {src} -> {dst}; it has no label yet")
    session.touched("edge", f"{src}->{dst}")
    return {"applied": f"Connected {src} to {dst}."}


def _disconnect(session: ArchSession, payload: dict[str, Any]) -> dict[str, Any]:
    state = session.state
    src = str(payload.get("src") or "").strip()
    dst = str(payload.get("dst") or "").strip()
    idx = state.edge_index(src, dst)
    if idx < 0:
        raise MutationError(f"no edge from {src!r} to {dst!r}.")
    state.edges.pop(idx)
    session.note_user_edit(f"removed the wire {src} -> {dst}")
    session.touched("edge", f"{src}->{dst}")
    return {"applied": f"Disconnected {src} from {dst}."}


_OPS = {
    "node": _node, "approach": _approach, "move": _move, "tidy": _tidy, "note": _note,
    "add_box": _add_box, "remove_box": _remove_box,
    "connect": _connect, "disconnect": _disconnect,
}
