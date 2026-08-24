"""ArchSession: persistence, the state push, and what resume refuses.

What is *absent* here is as much the point as what is present. There is no
broker (nothing gates), no judge, no critic thread, and no fingerprinting —
the user is in the room, which makes them the critic.
"""

from __future__ import annotations

import json

import pytest

from bird.harnesses.arch.session import STATE_FILENAME, ArchSession
from bird.harnesses.arch.state import (
    Approach, ArchState, Edge, LegacyStateError, Node, Question,
)


def test_every_mutation_persists_the_whole_board(tmp_path):
    run = tmp_path / "run"
    session = ArchSession(run_dir=run)
    session.state.nodes["api"] = Node(id="api", label="API", kind="api")
    session.touched("node", "api")
    saved = json.loads((run / STATE_FILENAME).read_text())
    assert saved["nodes"]["api"]["label"] == "API"


def test_the_state_push_is_a_full_replacement(tmp_path):
    """Full-replacement semantics are why the page never has to reconcile a
    diff, and why a reconnect can replay just the latest event."""
    pushed = []
    session = ArchSession(run_dir=tmp_path / "run", on_state=pushed.append)
    session.state.nodes["api"] = Node(id="api", label="API", kind="api")
    session.touched("node", "api")
    event = pushed[0]
    assert event["type"] == "arch_state"
    assert event["state"]["nodes"]["api"]["label"] == "API"
    assert event["changed"] == {"kind": "node", "id": "api"}
    assert "board" in event["renders"]


def test_the_push_carries_status_not_a_phase(tmp_path):
    """Phases are gone; a session is open until it is handed off, and that is
    the only thing the transport's stop condition reads."""
    pushed = []
    session = ArchSession(run_dir=tmp_path / "run", on_state=pushed.append)
    session.touched()
    assert pushed[-1]["status"] == "open"
    session.state.handed_off = True
    session.touched()
    assert pushed[-1]["status"] == "handed_off"


def test_the_push_carries_what_the_harness_noticed(tmp_path):
    """So the page can badge the box the user can then point at — not as a
    task list."""
    pushed = []
    session = ArchSession(run_dir=tmp_path / "run", on_state=pushed.append)
    session.state.nodes["a"] = Node(id="a", label="A")
    session.state.nodes["b"] = Node(id="b", label="B")
    session.touched()
    assert any("on no edge" in line for line in pushed[-1]["noticing"])


def test_a_session_without_a_run_dir_still_pushes(tmp_path):
    """Library and test use: no disk, but the event stream is unchanged."""
    pushed = []
    session = ArchSession(on_state=pushed.append)
    session.touched()
    assert len(pushed) == 1


def test_resume_restores_the_board(tmp_path):
    run = tmp_path / "run"
    first = ArchSession(run_dir=run)
    first.state.nodes["api"] = Node(id="api", label="API", kind="api")
    first.state.brief.goal = "ship it"
    first.touched()

    second = ArchSession.load(run)
    assert set(second.state.nodes) == {"api"}
    assert second.state.brief.goal == "ship it"


def test_resume_with_no_saved_state_is_a_fresh_board(tmp_path):
    assert ArchSession.load(tmp_path / "nothing-here").state.nodes == {}


def test_resuming_a_pre_rebuild_session_is_refused(tmp_path):
    """Reading it into the one-graph model would mean inventing things nobody
    said; starting empty would look like a lost session."""
    run = tmp_path / "run"
    run.mkdir()
    (run / STATE_FILENAME).write_text(json.dumps({
        "phase": "expand",
        "sketchbook": {"variants": {}, "active": None, "notes": []},
        "components": {"api": {"id": "api", "kind": "api"}},
    }))
    with pytest.raises(LegacyStateError, match="previous arch harness"):
        ArchSession.load(run)


def test_the_session_has_no_critic_machinery():
    """Principle 7: a background model filing findings is a third voice in a
    two-person conversation."""
    session = ArchSession()
    for gone in ("judge", "start_critic", "file_concerns", "run_audit",
                 "request_gate", "broker"):
        assert not hasattr(session, gone), gone


def test_ids_never_collide_with_what_is_already_there():
    session = ArchSession(state=ArchState())
    session.state.questions.append(Question(id="q1", question="one?"))
    assert session.next_question_id() == "q2"


# ------------------------------------------- what the user did to the board


def test_a_user_edit_survives_long_enough_to_be_read(tmp_path):
    """The tracker is re-rendered every loop iteration. Draining on the first
    render would show an edit once and then delete it from the conversation
    mid-turn — the architect would glimpse it and lose it."""
    session = ArchSession(run_dir=tmp_path / "run")
    session.note_user_edit('renamed "Postgres" to "Transcript store"')

    seen = [session.take_user_edits() for _ in range(4)]
    assert all(s for s in seen[:3]), "held for three renders"
    assert seen[3] == [], "and then it stops repeating itself"


def test_the_same_edit_twice_is_recorded_once(tmp_path):
    session = ArchSession(run_dir=tmp_path / "run")
    session.note_user_edit("drew a wire api -> pg")
    session.note_user_edit("drew a wire api -> pg")
    assert session.take_user_edits() == ["drew a wire api -> pg"]


def test_doing_it_again_later_restarts_its_life(tmp_path):
    """Saying the same thing again is the user repeating themselves, which is
    worth hearing — not a duplicate to swallow."""
    session = ArchSession(run_dir=tmp_path / "run")
    session.note_user_edit("drew a wire api -> pg")
    session.take_user_edits()
    session.take_user_edits()
    session.note_user_edit("drew a wire api -> pg")
    assert session.take_user_edits() == ["drew a wire api -> pg"]
    assert session.take_user_edits() == ["drew a wire api -> pg"]


def test_a_board_edit_becomes_a_turns_worth_of_prompt(tmp_path):
    session = ArchSession(run_dir=tmp_path / "run")
    session.note_user_edit('drew a box "Rate limiter" (rate-limiter)')
    session.note_user_edit('pinned a note to pg: "retention worries me"')

    prompt = session.compose_activity_prompt()
    assert prompt is not None
    assert "the user changed the board" in prompt
    assert '- drew a box "Rate limiter" (rate-limiter)' in prompt
    assert '- pinned a note to pg: "retention worries me"' in prompt
    # how to answer it lives in the system prompt; repeating it every turn is
    # tokens spent on something the architect already knows
    assert "Keep it to a couple of sentences" not in prompt


def test_nothing_to_say_means_no_turn(tmp_path):
    assert ArchSession(run_dir=tmp_path / "run").compose_activity_prompt() is None


def test_an_edit_the_architect_already_read_does_not_start_a_second_turn(tmp_path):
    """An edit made while a turn was running reaches it through the pinned
    note. Asking it to respond to the same gesture again is worse than not
    asking."""
    session = ArchSession(run_dir=tmp_path / "run")
    session.note_user_edit("drew a wire api -> pg")
    session.take_user_edits()  # a running turn saw it

    assert session.compose_activity_prompt() is None


def test_composing_a_prompt_stops_the_note_repeating_it(tmp_path):
    """Once it is in the transcript it is permanent; the note should let go."""
    session = ArchSession(run_dir=tmp_path / "run")
    session.note_user_edit("drew a wire api -> pg")
    session.compose_activity_prompt()
    assert session.take_user_edits() == []


# ---- what the user had selected when they sent the message ----


def _board(tmp_path):
    """A fork with one box in it that has something to say."""
    session = ArchSession(run_dir=tmp_path / "run")
    session.state.approaches["pool"] = Approach(id="pool", name="Pool", summary="one you run")
    session.state.nodes["idx"] = Node(
        id="idx", label="Search index", kind="store", depth="detailed",
        responsibility="the vector index", tech="pgvector", approaches=["pool"],
    )
    session.state.nodes["api"] = Node(id="api", label="API", kind="api")
    session.state.edges.append(Edge(src="api", dst="idx", label="query"))
    return session


def test_a_selected_box_travels_as_its_details_not_its_id(tmp_path):
    """The whole point: "why this one?" has to arrive knowing which one, and
    knowing it without a lookup."""
    block = _board(tmp_path).describe_subjects(["idx"])
    assert block is not None
    assert "the user is pointing at" in block
    assert "- Search index" in block
    assert "kind: store" in block
    assert "owns: the vector index" in block
    assert "built on: pgvector" in block


def test_a_selected_box_names_its_approach_rather_than_its_id(tmp_path):
    """`pool` means nothing to the architect; "Pool" is what it called it."""
    block = _board(tmp_path).describe_subjects(["idx"])
    assert "approach: Pool" in block


def test_a_selected_box_carries_what_it_is_wired_to(tmp_path):
    """Most questions about a box are really questions about its edges."""
    block = _board(tmp_path).describe_subjects(["idx"])
    assert "wires: ← API (query)" in block


def test_a_shared_box_says_it_is_shared(tmp_path):
    """No approach labels is not missing data — it is the box every approach
    uses, which is a fact worth stating."""
    block = _board(tmp_path).describe_subjects(["api"])
    assert "shared by every approach" in block


def test_the_first_line_of_each_box_is_the_bullet_the_page_reads_back(tmp_path):
    """The page splits this block back out of the turn and shows the bullets as
    what the message was about. Detail lines are indented so it skips them —
    breaking that shape puts raw prompt text in the user's own message."""
    block = _board(tmp_path).describe_subjects(["idx"])
    bullets = [ln for ln in block.split("\n") if ln.startswith("- ")]
    assert bullets == ["- Search index"]


def test_a_selection_that_no_longer_resolves_is_dropped(tmp_path):
    """A stale id is the page being a moment behind the graph. That is not
    something to make the architect explain."""
    assert _board(tmp_path).describe_subjects(["gone"]) is None


def test_selecting_nothing_adds_nothing(tmp_path):
    assert _board(tmp_path).describe_subjects([]) is None


def test_a_repeated_id_is_described_once(tmp_path):
    block = _board(tmp_path).describe_subjects(["idx", "idx"])
    assert block.count("- Search index") == 1
