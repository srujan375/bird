"""The handoff — what survives the session.

The rebuild moved the bundle's centre of gravity from contract sheets to
*reasons*: what was decided and why, and which approaches lost and why. These
tests pin that ordering, because it is the half a design doc usually throws away
and the half a builder actually needs.
"""

from __future__ import annotations

import json

from bird.harnesses.arch.bundle import bundle_paths, write_bundle
from bird.harnesses.arch.state import (
    Approach,
    ArchState,
    Decision,
    Edge,
    Node,
    Option,
    Question,
)


def _designed() -> ArchState:
    s = ArchState()
    s.brief.goal = "relay webhooks"
    s.brief.scale = "a few hundred a day"
    s.approaches["queue-first"] = Approach(
        id="queue-first", name="Queue first", summary="a broker drains it",
        status="greyed", rejected_reason="the volume never justifies operating a broker",
    )
    s.nodes["api"] = Node(id="api", label="Ingest API", kind="api",
                          responsibility="accepts the hook and 202s", tech="FastAPI")
    s.nodes["store"] = Node(id="store", label="Attempts", kind="store", depth="sketch",
                            detail="attempts table; 30-day retention then purge")
    s.nodes["queue"] = Node(id="queue", label="Broker", kind="queue",
                            approaches=["queue-first"])
    s.edges.append(Edge("api", "store", "records", notes="503s if the store is down"))
    s.decisions.append(Decision(
        id="d1", topic="delivery", choice="in-process retry",
        options=[Option(name="in-process retry"),
                 Option(name="durable queue", cons=["a broker to operate"])],
        rationale="the queue is infrastructure you babysit for nothing here",
        pragmatism_note="loses in-flight retries on restart; you re-fire by hand at this volume",
    ))
    s.questions.append(Question(id="q1", question="Do customers need a status API?",
                                recommendation="skip it for v1"))
    return s


def _md(tmp_path) -> str:
    write_bundle(_designed(), tmp_path)
    return bundle_paths(tmp_path)[1].read_text()


def test_the_bundle_is_a_json_board_and_a_readable_doc(tmp_path):
    written = write_bundle(_designed(), tmp_path)
    assert [p.name for p in written] == ["architecture.json", "architecture.md"]
    data = json.loads(written[0].read_text())
    assert set(data["nodes"]) == {"api", "store", "queue"}


def test_why_comes_before_what(tmp_path):
    """A builder reads for the reasoning first; the box list is reference."""
    md = _md(tmp_path)
    assert md.index("## Decisions") < md.index("## Components")


def test_a_decision_carries_its_rationale_and_what_it_beat(tmp_path):
    md = _md(tmp_path)
    assert "delivery → in-process retry" in md
    assert "babysit for nothing" in md
    assert "**durable queue**" in md and "a broker to operate" in md


def test_a_pragmatic_choice_reads_as_deliberate_not_apologetic(tmp_path):
    """Someone who reads this as a defect will 'fix' it — the record has to say
    the tradeoff was seen and taken."""
    md = _md(tmp_path)
    assert "Deliberately good enough" in md
    assert "re-fire by hand" in md


def test_the_approach_that_lost_keeps_its_reason(tmp_path):
    """"Why not X" is the question a builder asks most, and this is the answer."""
    md = _md(tmp_path)
    assert "## Approaches not taken" in md
    assert "**Not taken:** the volume never justifies operating a broker" in md


def test_a_losing_approachs_boxes_are_not_listed_as_components(tmp_path):
    md = _md(tmp_path)
    components = md.split("## Components")[1].split("## Still open")[0]
    assert "Ingest API" in components
    assert "`queue`" not in components, "a box that lost is history, not a component"


def test_unanswered_questions_travel_with_their_recommendation(tmp_path):
    """The builder inherits a starting position, not just a hole."""
    md = _md(tmp_path)
    assert "Do customers need a status API?" in md
    assert "suggested: skip it for v1" in md


def test_what_the_design_does_not_say_is_listed_as_left_to_the_build(tmp_path):
    s = _designed()
    s.nodes["orphan"] = Node(id="orphan", label="Orphan")
    write_bundle(s, tmp_path)
    md = bundle_paths(tmp_path)[1].read_text()
    assert "## Left to the build" in md
    assert "orphan is on no edge" in md


def test_the_board_ships_as_a_diagram(tmp_path):
    md = _md(tmp_path)
    assert "```mermaid" in md and "flowchart LR" in md


def test_an_empty_design_still_writes_a_readable_doc(tmp_path):
    write_bundle(ArchState(), tmp_path)
    md = bundle_paths(tmp_path)[1].read_text()
    assert md.startswith("# Architecture — untitled")


def test_a_note_pinned_to_a_box_travels_with_it(tmp_path):
    from bird.harnesses.arch.state import Annotation

    s = _designed()
    s.annotations.append(Annotation(id="n1", text="30-day retention is the bill", anchor="store"))
    write_bundle(s, tmp_path)
    md = bundle_paths(tmp_path)[1].read_text()
    sheet = md.split("### Attempts")[1].split("###")[0]
    assert "**note:** 30-day retention is the bill" in sheet


def test_a_note_on_the_canvas_gets_its_own_section(tmp_path):
    from bird.harnesses.arch.state import Annotation

    s = _designed()
    s.annotations.append(Annotation(id="n2", text="revisit at a hundred episodes"))
    write_bundle(s, tmp_path)
    md = bundle_paths(tmp_path)[1].read_text()
    assert "## Notes on the board" in md
    assert "- revisit at a hundred episodes" in md
