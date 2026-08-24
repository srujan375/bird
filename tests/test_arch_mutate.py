"""User edits from the page, applied through the architect's own code paths.

The contract: an edit made on the canvas lands in the same `_upsert_node` the
model's tools call, so validation and the state push cannot diverge between
"the architect renamed this" and "the person reading it renamed this".
"""

from __future__ import annotations

import pytest

from bird.harnesses.arch.mutate import EDITABLE_NODE_FIELDS, MutationError, apply_mutation
from bird.harnesses.arch.session import ArchSession
from bird.harnesses.arch.state import Approach, ArchState, Node


@pytest.fixture
def session(tmp_path):
    s = ArchState()
    s.approaches["queue-first"] = Approach(id="queue-first", name="Queue first",
                                           summary="a broker drains it")
    s.nodes["orders"] = Node(id="orders", label="Orders", kind="store",
                             responsibility="holds attempts")
    s.nodes["queue"] = Node(id="queue", label="Queue", kind="queue",
                            approaches=["queue-first"])
    return ArchSession(state=s, run_dir=tmp_path / "run")


def test_editing_prose_on_a_box(session):
    out = apply_mutation(session, {"op": "node", "id": "orders",
                                   "responsibility": "holds delivery attempts"})
    assert "Updated orders" in out["applied"]
    assert session.state.nodes["orders"].responsibility == "holds delivery attempts"


def test_the_kind_stays_the_architects_call(session):
    """Structural changes come from the conversation, not from typing in a
    panel — so `kind` is simply not in the editable set."""
    assert "kind" not in EDITABLE_NODE_FIELDS
    with pytest.raises(MutationError, match="editable fields"):
        apply_mutation(session, {"op": "node", "id": "orders", "kind": "queue"})


def test_editing_a_box_that_is_not_there(session):
    with pytest.raises(MutationError, match="does not have"):
        apply_mutation(session, {"op": "node", "id": "ghost", "label": "Ghost"})


def test_a_box_cannot_be_left_nameless(session):
    with pytest.raises(MutationError, match="needs a label"):
        apply_mutation(session, {"op": "node", "id": "orders", "label": "  "})


def test_the_user_can_grey_an_approach_from_the_page(session):
    """"I'm taking the left one" is the user's call — it should not require
    asking the model to type it out."""
    out = apply_mutation(session, {"op": "approach", "id": "queue-first",
                                   "status": "greyed",
                                   "rejected_reason": "not worth operating a broker"})
    assert "greyed out" in out["applied"]
    app = session.state.approaches["queue-first"]
    assert app.status == "greyed"
    assert app.rejected_reason == "not worth operating a broker"
    assert session.state.is_greyed(session.state.nodes["queue"])


def test_greying_from_the_page_still_needs_the_reason(session):
    """The refusal the model would have seen reaches the page verbatim, rather
    than being filled in with a placeholder."""
    with pytest.raises(MutationError, match="reason it lost"):
        apply_mutation(session, {"op": "approach", "id": "queue-first", "status": "greyed"})


def test_bringing_a_greyed_approach_back(session):
    apply_mutation(session, {"op": "approach", "id": "queue-first", "status": "greyed",
                             "rejected_reason": "too much infra"})
    out = apply_mutation(session, {"op": "approach", "id": "queue-first", "status": "active"})
    assert "brought back" in out["applied"]
    assert not session.state.is_greyed(session.state.nodes["queue"])


def test_an_unknown_approach_is_named_in_the_error(session):
    with pytest.raises(MutationError, match="queue-first"):
        apply_mutation(session, {"op": "approach", "id": "nope"})


def test_an_unknown_op_is_refused_with_what_is_allowed(session):
    with pytest.raises(MutationError, match="node, approach"):
        apply_mutation(session, {"op": "delete_everything"})


def test_a_handed_off_design_is_read_only(session):
    session.state.handed_off = True
    with pytest.raises(MutationError, match="read-only"):
        apply_mutation(session, {"op": "node", "id": "orders", "label": "Late"})


def test_a_mutation_pushes_state_exactly_once(session):
    """State travels on one channel — the arch_state push. A second copy over
    a different wire is a second thing to keep in sync."""
    pushed = []
    session.on_state = pushed.append
    out = apply_mutation(session, {"op": "node", "id": "orders", "tech": "Postgres"})
    assert len(pushed) == 1
    assert "state" not in out, "the reply is a receipt, not a copy of the state"
    assert pushed[0]["state"]["nodes"]["orders"]["tech"] == "Postgres"


# ------------------------------------------------- arranging the board


def test_moving_a_box_is_the_users_call_and_it_persists(session):
    """Arranging the board is how a person says what belongs with what. No tool
    takes coordinates, so this is the only writer."""
    out = apply_mutation(session, {"op": "move", "id": "orders", "x": 590, "y": 250})
    assert "Moved orders" in out["applied"]
    assert (session.state.nodes["orders"].x, session.state.nodes["orders"].y) == (590, 250)


def test_a_move_needs_real_coordinates(session):
    with pytest.raises(MutationError, match="numeric x and y"):
        apply_mutation(session, {"op": "move", "id": "orders", "x": "left a bit"})


def test_moving_a_box_that_is_not_there(session):
    with pytest.raises(MutationError, match="does not have"):
        apply_mutation(session, {"op": "move", "id": "ghost", "x": 1, "y": 2})


def test_tidy_hands_the_board_back_to_the_layout(session):
    """The page arranges around what nobody has moved. A position somebody chose
    is chosen precisely so nothing else moves it, so the only way to get one back
    is to say so."""
    apply_mutation(session, {"op": "move", "id": "orders", "x": 590, "y": 250})
    apply_mutation(session, {"op": "move", "id": "queue", "x": -40, "y": 1900})
    out = apply_mutation(session, {"op": "tidy"})
    assert "2 boxes" in out["applied"]
    for node in session.state.nodes.values():
        assert (node.x, node.y) == (None, None)


def test_tidy_says_so_when_there_is_nothing_to_tidy(session):
    with pytest.raises(MutationError, match="already arranged"):
        apply_mutation(session, {"op": "tidy"})


def test_tidy_changes_nothing_but_position(session):
    apply_mutation(session, {"op": "move", "id": "queue", "x": 1, "y": 2})
    before = list(session.state.nodes["queue"].approaches)
    edges = list(session.state.edges)
    apply_mutation(session, {"op": "tidy"})
    assert session.state.nodes["queue"].approaches == before
    assert session.state.edges == edges


def test_moving_does_not_reassign_which_approach_a_box_belongs_to(session):
    """Dragging across a column boundary is a design change with a reason
    behind it, and the reason is what the architect is for."""
    before = list(session.state.nodes["queue"].approaches)
    apply_mutation(session, {"op": "move", "id": "queue", "x": 10, "y": 10})
    assert session.state.nodes["queue"].approaches == before


def test_a_note_can_be_pinned_to_a_box(session):
    apply_mutation(session, {"op": "note", "id": "n1", "text": "bill nobody budgets for",
                             "x": 60, "y": 780, "anchor": "orders"})
    note = session.state.annotation_by_id("n1")
    assert note.text == "bill nobody budgets for"
    assert [a.id for a in session.state.notes_on("orders")] == ["n1"]


def test_a_note_can_sit_on_the_canvas_with_no_anchor(session):
    apply_mutation(session, {"op": "note", "id": "n1", "text": "revisit at 100 episodes"})
    assert session.state.annotation_by_id("n1").anchor == ""


def test_pinning_a_note_to_a_box_that_is_not_there(session):
    with pytest.raises(MutationError, match="no such box"):
        apply_mutation(session, {"op": "note", "id": "n1", "text": "x", "anchor": "ghost"})


def test_clearing_a_notes_text_removes_it(session):
    """The same gesture as emptying the box on the canvas — the page never has
    to send a separate delete."""
    apply_mutation(session, {"op": "note", "id": "n1", "text": "temporary"})
    apply_mutation(session, {"op": "note", "id": "n1", "text": "  "})
    assert session.state.annotations == []


def test_a_new_note_with_no_text_is_refused(session):
    with pytest.raises(MutationError, match="needs text"):
        apply_mutation(session, {"op": "note", "id": "n1", "text": ""})


def test_a_note_can_be_dragged_without_retexting_it(session):
    apply_mutation(session, {"op": "note", "id": "n1", "text": "keep me", "x": 0, "y": 0})
    apply_mutation(session, {"op": "note", "id": "n1", "x": 300, "y": 400})
    note = session.state.annotation_by_id("n1")
    assert (note.x, note.y) == (300, 400)
    assert note.text == "keep me"
