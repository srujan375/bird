"""The design, written into the knowledge graph at handoff.

`architecture.md` answers "what am I building". This answers "what writes to the
attempts table", asked eleven turns later — and, uniquely, "why is it like this"
and "why not the other way", because decisions and losing approaches are seeded
as queryable nodes too.
"""

from __future__ import annotations

from bird.context.kg import KG
from bird.harnesses.arch.kg_seed import build_seed, seed_kg
from bird.harnesses.arch.state import (
    Approach,
    ArchState,
    Decision,
    Edge,
    Node,
    Option,
)


def _designed() -> ArchState:
    s = ArchState()
    s.brief.goal = "relay webhooks"
    s.approaches["queue-first"] = Approach(
        id="queue-first", name="Queue first", status="greyed",
        rejected_reason="the volume never justifies operating a broker",
    )
    s.nodes["api"] = Node(id="api", label="Ingest API", kind="api",
                          responsibility="accepts the hook and 202s", tech="FastAPI")
    s.nodes["store"] = Node(id="store", label="Attempts", kind="store",
                            detail="attempts table; 30-day retention")
    s.nodes["queue"] = Node(id="queue", label="Broker", kind="queue",
                            approaches=["queue-first"])
    s.edges.append(Edge("api", "store", "records attempts", notes="503s when it is down"))
    s.decisions.append(Decision(
        id="d1", topic="delivery", choice="in-process retry",
        options=[Option(name="in-process retry"), Option(name="durable queue")],
        rationale="a broker buys nothing at this volume",
    ))
    return s


def _labels(nodes):
    return " || ".join(n["label"] for n in nodes)


def test_a_box_label_carries_the_words_someone_would_search_for():
    """`query()` matches on labels alone, so a bare id retrieves nothing."""
    nodes, _ = build_seed(_designed())
    assert "Ingest API — accepts the hook and 202s (on FastAPI)" in _labels(nodes)


def test_every_seeded_node_says_it_came_from_the_design():
    nodes, _ = build_seed(_designed())
    assert all(n["file_type"] == "design" for n in nodes)
    assert all(n["_origin"] == "arch" for n in nodes)
    assert all("design:" in n["source_location"] for n in nodes)


def test_edges_carry_their_label_and_failure_note():
    _, edges = build_seed(_designed())
    edge = next(e for e in edges if e["relation"] == "records_attempts")
    assert edge["source"] == "arch_api" and edge["target"] == "arch_store"
    assert "503s when it is down" in edge["context"]


def test_a_decision_is_queryable_with_its_reason_and_its_rivals():
    """"Why is it like this" is a question the graph should answer."""
    nodes, _ = build_seed(_designed())
    label = next(n["label"] for n in nodes if n["type"] == "decision")
    assert "delivery → in-process retry" in label
    assert "a broker buys nothing at this volume" in label
    assert "[not: durable queue]" in label


def test_a_losing_approach_is_queryable_with_why_it_lost():
    """"Why not X" — the other question a builder asks most."""
    nodes, _ = build_seed(_designed())
    label = next(n["label"] for n in nodes if n["type"] == "approach")
    assert "Approach not taken: Queue first" in label
    assert "Why not: the volume never justifies operating a broker" in label


def test_a_box_that_lost_is_seeded_marked_rather_than_dropped():
    """"We considered this and dropped it" is a real answer to a query."""
    nodes, _ = build_seed(_designed())
    box = next(n for n in nodes if n["id"] == "arch_queue")
    assert box["greyed"] is True
    assert box["label"].startswith("[not taken]")


def test_a_boxs_detail_becomes_its_own_searchable_node():
    nodes, edges = build_seed(_designed())
    detail = next(n for n in nodes if n["type"] == "detail")
    assert "30-day retention" in detail["label"]
    assert any(e["relation"] == "detailed_as" for e in edges)


def test_seeding_a_real_graph_makes_the_design_queryable(tmp_path):
    kg = KG(repo_root=tmp_path, store_dir=tmp_path / "kg")
    report = seed_kg(kg, _designed())
    assert "seeded the knowledge graph" in report
    assert kg.is_ready()
    G = kg._load_graph()
    assert any(
        "design:" in str(nd.get("source_location", "")) for _, nd in G.nodes(data=True)
    )


def test_no_graph_is_silence_not_a_failure():
    assert seed_kg(None, _designed()) == ""


def test_a_graph_that_refuses_the_seed_never_fails_the_handoff():
    """The design is already on disk by the time this runs; a bad graph is a
    worse next session, not a lost one."""

    class Broken:
        def seed(self, nodes, edges):
            raise RuntimeError("disk full")

    report = seed_kg(Broken(), _designed())
    assert "not seeded" in report and "the bundle is unaffected" in report


def test_an_empty_design_seeds_nothing():
    assert build_seed(ArchState()) == ([], [])
