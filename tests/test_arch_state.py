"""The one graph: nodes, edges, approaches, and what counts as broken.

The split these tests pin is the posture of the whole harness. `validate_*`
refuses what would corrupt the graph and nothing else; thinness is `derive.py`'s
business and never refuses anything (see test_arch_derive.py).
"""

from __future__ import annotations

import pytest

from bird.harnesses.arch.state import (
    Approach,
    ArchState,
    Decision,
    Edge,
    LegacyStateError,
    Node,
    Option,
    Question,
    slug,
)


def _board() -> ArchState:
    """Two live approaches over a shared store — the shape the harness is for."""
    s = ArchState()
    s.approaches["lambda"] = Approach(id="lambda", name="Lambda")
    s.approaches["service"] = Approach(id="service", name="Always-on service")
    s.nodes["orders"] = Node(id="orders", label="Orders", kind="store")
    s.nodes["fn"] = Node(id="fn", label="Handler", approaches=["lambda"])
    s.nodes["svc"] = Node(id="svc", label="Handler", approaches=["service"])
    s.edges += [Edge("fn", "orders", "writes"), Edge("svc", "orders", "writes")]
    return s


# ---------------------------------------------------------- approaches


def test_a_node_with_no_approach_label_is_shared():
    s = _board()
    assert s.nodes["orders"].shared()
    assert not s.nodes["fn"].shared()
    assert [n.id for n in s.shared_nodes()] == ["orders"]


def test_nodes_in_an_approach_excludes_the_shared_ones():
    """Shared nodes belong to every approach, so `nodes_in` returning them
    would make "what is only in this take" unanswerable."""
    s = _board()
    assert [n.id for n in s.nodes_in("lambda")] == ["fn"]


def test_greying_an_approach_greys_only_its_exclusive_nodes():
    s = _board()
    s.approaches["service"].status = "greyed"
    s.approaches["service"].rejected_reason = "cost"
    assert s.is_greyed(s.nodes["svc"])
    assert not s.is_greyed(s.nodes["fn"])
    assert not s.is_greyed(s.nodes["orders"]), "a shared box outlives any approach"


def test_a_node_in_two_approaches_survives_until_both_lose():
    """The hybrid case: "take the lambda from the left and the queue from the
    right" only works if a box with one surviving label stays live."""
    s = _board()
    s.nodes["queue"] = Node(id="queue", label="Queue", kind="queue",
                            approaches=["lambda", "service"])
    s.approaches["service"].status = "greyed"
    s.approaches["service"].rejected_reason = "cost"
    assert not s.is_greyed(s.nodes["queue"])
    s.approaches["lambda"].status = "greyed"
    s.approaches["lambda"].rejected_reason = "cold starts"
    assert s.is_greyed(s.nodes["queue"])


def test_greying_an_approach_without_a_reason_is_refused():
    """The reason is the entire value of keeping a losing approach on the
    board; a greyed approach without one is worse than deleting it."""
    s = ArchState()
    with pytest.raises(ValueError, match="reason it lost"):
        s.validate_approach(Approach(id="a", name="A", status="greyed"))


def test_greying_with_a_reason_is_accepted():
    s = ArchState()
    s.validate_approach(
        Approach(id="a", name="A", status="greyed", rejected_reason="too much infra")
    )


# ---------------------------------------------------------- validation


def test_a_malformed_node_id_is_broken():
    s = ArchState()
    with pytest.raises(ValueError, match="kebab-case"):
        s.validate_node(Node(id="Order Store", label="x"))


def test_an_unknown_kind_is_broken():
    s = ArchState()
    with pytest.raises(ValueError, match="unknown kind"):
        s.validate_node(Node(id="x", label="x", kind="database"))


def test_a_node_labelled_with_an_unnamed_approach_is_broken():
    """Otherwise a typo silently produces a box in an approach nobody can see."""
    s = ArchState()
    with pytest.raises(ValueError, match="unknown approach"):
        s.validate_node(Node(id="x", label="x", approaches=["typo"]))


def test_an_edge_to_a_missing_node_is_broken():
    s = _board()
    with pytest.raises(ValueError, match="unknown node"):
        s.validate_edge(Edge("orders", "nowhere"))


def test_a_self_edge_is_broken():
    s = _board()
    with pytest.raises(ValueError, match="itself"):
        s.validate_edge(Edge("orders", "orders"))


def test_a_choice_that_matches_no_option_is_broken():
    with pytest.raises(ValueError, match="must match one option"):
        ArchState.validate_decision(
            Decision(id="d1", topic="compute", options=[Option(name="lambda")], choice="fargate")
        )


def test_a_decision_with_no_options_is_allowed():
    """A bare note is a legitimate light record; "nothing was weighed" is
    thinness for derive.py to observe, not a refusal."""
    ArchState.validate_decision(Decision(id="d1", topic="compute", choice="lambda"))


# ------------------------------------------------------ ids and lookups


def test_next_node_id_derives_from_a_label_and_never_collides():
    s = ArchState()
    assert s.next_node_id("Order Store") == "order-store"
    s.nodes["order-store"] = Node(id="order-store", label="Order Store")
    assert s.next_node_id("Order Store") == "order-store-2"


def test_edge_index_matches_on_the_pair_so_a_redraw_relabels():
    s = _board()
    assert s.edge_index("fn", "orders") == 0
    assert s.edge_index("fn", "nowhere") == -1


def test_references_to_names_what_would_dangle():
    s = _board()
    assert s.references_to("orders") == ["fn -> orders", "svc -> orders"]


def test_slug_normalizes_anything_into_an_id():
    assert slug("Order Store!") == "order-store"
    assert slug("  ") == "node"


# ------------------------------------------------------- serialization


def test_roundtrip_preserves_the_whole_board():
    s = _board()
    s.brief.goal = "ship it"
    s.brief.actors = ["operator"]
    s.decisions.append(
        Decision(id="d1", topic="compute", options=[Option(name="lambda")],
                 choice="lambda", rationale="cheap", pragmatism_note="rewrite later")
    )
    s.questions.append(Question(id="q1", question="which region?", recommendation="us-east-1"))
    back = ArchState.from_dict(s.to_dict())
    assert set(back.nodes) == set(s.nodes)
    assert back.nodes["fn"].approaches == ["lambda"]
    assert [e.key() for e in back.edges] == [e.key() for e in s.edges]
    assert back.decisions[0].pragmatism_note == "rewrite later"
    assert back.questions[0].recommendation == "us-east-1"
    assert back.brief.actors == ["operator"]


def test_a_pre_rebuild_state_file_is_refused_by_name():
    """Silently starting from an empty design would look like a lost session,
    which is worse than saying the file is from the old harness."""
    with pytest.raises(LegacyStateError, match="sketchbook"):
        ArchState.from_dict({"phase": "expand", "sketchbook": {"variants": {}}, "components": {}})


def test_an_empty_dict_is_a_fresh_board_not_a_legacy_file():
    assert ArchState.from_dict({}).nodes == {}


# ---------------------------------------------- the board as an arrangement


def test_a_box_remembers_where_it_was_put():
    """Which column a box sits in says which approach it belongs to. An
    arrangement that dies with the tab was never really part of the design."""
    s = _board()
    s.nodes["orders"].x, s.nodes["orders"].y = 590, 250
    back = ArchState.from_dict(s.to_dict())
    assert (back.nodes["orders"].x, back.nodes["orders"].y) == (590, 250)


def test_an_unplaced_box_says_so_rather_than_claiming_the_origin():
    """None is "nobody has arranged this yet", which the canvas may lay out;
    0,0 would be a position somebody chose."""
    assert ArchState().nodes == {}
    n = Node(id="x", label="X")
    assert n.x is None and n.y is None


def test_notes_survive_a_roundtrip_and_know_what_they_hang_off():
    from bird.harnesses.arch.state import Annotation

    s = _board()
    s.annotations.append(Annotation(id="n1", text="why this wins", x=10, y=20, anchor="orders"))
    s.annotations.append(Annotation(id="n2", text="loose thought"))
    back = ArchState.from_dict(s.to_dict())
    assert [a.id for a in back.notes_on("orders")] == ["n1"]
    assert back.annotation_by_id("n2").anchor == ""
