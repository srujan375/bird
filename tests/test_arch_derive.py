"""The design-tree walk and the coverage checks.

`frontier` is the part of the rebuild that replaced the obligation queue: what
can be asked *now*, given what is settled. The rule that makes it a tree rather
than a checklist is that an unsettled fork blocks everything downstream of it,
and these tests pin exactly that.

`coverage` is what the background critic used to file as "concerns". Same
checks, no second model, and nothing it says can refuse a call.
"""

from __future__ import annotations

from bird.harnesses.arch import derive
from bird.harnesses.arch.state import Approach, ArchState, Decision, Edge, Node


def _forked() -> ArchState:
    s = ArchState()
    s.approaches["lam"] = Approach(id="lam", name="lambda")
    s.approaches["svc"] = Approach(id="svc", name="service")
    s.nodes["orders"] = Node(id="orders", label="Orders", kind="store")
    s.nodes["api"] = Node(id="api", label="API", kind="api")
    s.nodes["fn"] = Node(id="fn", label="Handler", approaches=["lam"])
    s.nodes["svc-box"] = Node(id="svc-box", label="Handler", approaches=["svc"])
    s.edges.append(Edge("api", "orders", "writes"))
    return s


def _settle(s: ArchState) -> ArchState:
    s.approaches["svc"].status = "greyed"
    s.approaches["svc"].rejected_reason = "cost"
    return s


# ------------------------------------------------------------ the frontier


def test_an_open_fork_blocks_everything_that_is_not_shared():
    """You cannot deepen a box that only exists in one of two live takes —
    its details are answers to a question nobody has settled."""
    s = _forked()
    askable = {n.id for n in derive.askable(s)}
    assert askable == {"orders", "api"}
    assert "fn" not in askable and "svc-box" not in askable


def test_settling_the_fork_pushes_the_frontier_outward():
    s = _settle(_forked())
    askable = {n.id for n in derive.askable(s)}
    assert "fn" in askable, "the surviving take's boxes become askable"
    assert "svc-box" not in askable, "the losing take's boxes drop off entirely"


def test_the_open_fork_is_itself_the_first_frontier_item():
    front = derive.frontier(_forked())
    assert "fork is still open" in front[0]
    assert "lambda" in front[0] and "service" in front[0]


def test_the_frontier_orders_by_cost_of_getting_it_wrong():
    """A store's schema outlives every rewrite around it, so it is asked about
    before a stateless service."""
    s = _settle(_forked())
    s.nodes["worker"] = Node(id="worker", label="Worker", kind="service")
    ids = [line.split()[0] for line in derive.frontier(s)]
    assert ids.index("orders") < ids.index("api") < ids.index("worker")


def test_a_detailed_box_leaves_the_frontier():
    s = _settle(_forked())
    s.nodes["orders"].depth = "detailed"
    assert "orders" not in " ".join(derive.frontier(s))


def test_imported_background_is_never_on_the_frontier():
    """Existing code is what is there, not something being designed here."""
    s = ArchState()
    s.nodes["legacy"] = Node(id="legacy", label="Legacy", kind="store", existing=True)
    assert derive.askable(s) == []


# -------------------------------------------------------------- coverage


def test_a_box_nothing_connects_to_is_noticed():
    s = _settle(_forked())
    s.nodes["orphan"] = Node(id="orphan", label="Orphan")
    assert any("orphan is on no edge" in line for line in derive.coverage(s))


def test_a_store_that_never_says_how_long_data_lives_is_noticed():
    s = _settle(_forked())
    s.nodes["orders"].depth = "sketch"
    assert any("how long its data lives" in line for line in derive.coverage(s))


def test_saying_it_in_any_words_settles_the_retention_check():
    """The check exists to catch a store nobody thought about, not to police
    wording — 'kept forever' is a real answer."""
    s = _settle(_forked())
    s.nodes["orders"].depth = "sketch"
    s.nodes["orders"].detail = "append-only; kept forever, it is the audit log"
    assert not any("how long its data lives" in line for line in derive.coverage(s))


def test_a_stub_store_is_not_yet_asked_about_retention():
    """A box that is still just a name has not been designed enough to be
    thin — that is the frontier's job, not coverage's."""
    s = _settle(_forked())
    assert s.nodes["orders"].depth == "stub"
    assert not any("how long its data lives" in line for line in derive.coverage(s))


def test_an_undesigned_failure_path_is_noticed():
    """The graph-native replacement for "a happy flow with no failure twin":
    something is depended on and nothing says what the caller does when it is
    down."""
    s = _settle(_forked())
    assert any(
        "what api does when orders is down" in line for line in derive.coverage(s)
    )


def test_a_note_on_the_edge_settles_the_failure_check():
    s = _settle(_forked())
    s.edges[0].notes = "on failure the API 503s and the client retries"
    assert not any("is down" in line for line in derive.coverage(s))


def test_a_user_choice_with_nothing_weighed_against_it_is_noticed():
    """The harness cannot know whether the user's pick is right; it can know
    that nobody checked."""
    s = _settle(_forked())
    s.decisions.append(Decision(id="d1", topic="queue", choice="SQS", source="user"))
    assert any("came from the user with no" in line for line in derive.coverage(s))


def test_the_same_choice_weighed_against_a_rival_is_not_noticed():
    from bird.harnesses.arch.state import Option

    s = _settle(_forked())
    s.decisions.append(Decision(
        id="d1", topic="queue", choice="SQS", source="user",
        options=[Option(name="SQS"), Option(name="in-process retry")],
    ))
    assert not any("came from the user" in line for line in derive.coverage(s))


def test_a_losing_approachs_boxes_stop_generating_noise():
    """Once a take loses, its boxes are history — they must not keep reading
    as gaps in the live design."""
    s = _forked()
    s.nodes["svc-box"] = Node(id="svc-box", label="Handler", approaches=["svc"])
    assert any("svc-box" in line for line in derive.coverage(s))
    _settle(s)
    assert not any("svc-box" in line for line in derive.coverage(s))


# ------------------------------------------------------------ the note


def test_the_note_is_short_and_never_a_wall():
    s = _settle(_forked())
    for i in range(20):
        s.nodes[f"n{i}"] = Node(id=f"n{i}", label=f"N{i}")
    assert len(derive.note(s).splitlines()) <= 8


def test_an_empty_board_asks_for_a_shape_not_an_interview():
    assert "Put a shape up" in derive.note(ArchState())


def test_settled_decisions_are_pinned_so_they_are_not_re_opened():
    s = _settle(_forked())
    s.decisions.append(Decision(id="d1", topic="compute", choice="lambda"))
    note = derive.note(s)
    assert "compute->lambda" in note
    assert "don't re-open" in note


def test_open_questions_are_pinned_as_waiting_on_the_user():
    from bird.harnesses.arch.state import Question

    s = _settle(_forked())
    s.questions.append(Question(id="q1", question="which region?"))
    assert "waiting on the user: q1 which region?" in derive.note(s)


def test_a_handed_off_session_says_so_and_stops():
    s = _settle(_forked())
    s.handed_off = True
    assert derive.note(s) == "[arch] handed off — the session is closed."


# ------------------------------------- the user drawing on the same surface


def test_a_note_the_user_pinned_reaches_the_architect_in_their_words():
    from bird.harnesses.arch.state import Annotation

    s = _settle(_forked())
    s.annotations.append(Annotation(id="n1", text="this is the bit I'm worried about",
                                    anchor="orders"))
    assert 'on orders: "this is the bit I\'m worried about"' in derive.note(s)


def test_a_loose_note_reaches_it_too():
    from bird.harnesses.arch.state import Annotation

    s = _settle(_forked())
    s.annotations.append(Annotation(id="n1", text="revisit at a hundred episodes"))
    assert '"revisit at a hundred episodes"' in derive.note(s)


def test_the_architect_is_told_the_user_edited_the_board():
    """The design already reflects it, but a snapshot cannot say that a label
    is new or that a human is the one who changed it."""
    note = derive.note(_settle(_forked()), ['renamed "Postgres" to "Transcript store" (orders)'])
    assert "the user just:" in note
    assert 'renamed "Postgres" to "Transcript store"' in note
    assert "answer it" in note


def test_no_edits_means_no_line_about_them():
    assert "the user just:" not in derive.note(_settle(_forked()), [])
