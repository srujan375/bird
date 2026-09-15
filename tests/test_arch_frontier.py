"""The frontier as something the user can see and prune, and a pick that
becomes a decision on its own.

Both come from reading the grilling family's issue tracker: sessions ran to
two hundred questions because nobody could see how much tree was left, and
the biggest complaint on the stateful variant was "where did my decisions
go". Arch computes the frontier and records decisions, so the fixes are
structural rather than a question cap."""

from __future__ import annotations

import pytest

from bird.harnesses.arch import derive
from bird.harnesses.arch.mutate import MutationError, apply_mutation
from bird.harnesses.arch.session import ArchSession
from bird.harnesses.arch.tools import arch_harness_tools
from bird.tools import ToolContext


@pytest.fixture
def board(tmp_path):
    session = ArchSession(run_dir=tmp_path / "run")
    ctx = ToolContext(repo_root=tmp_path, arch=session)
    tools = {t.name: t for t in arch_harness_tools()}

    def call(tool_name, /, **args):
        res = tools[tool_name].execute(args, ctx)
        assert not res.is_error, res.output
        return res

    call("canvas", nodes=[
        {"id": "api", "label": "API", "kind": "api"},
        {"id": "store", "label": "URL mappings", "kind": "store"},
        {"id": "cache", "label": "Cache", "kind": "store"},
    ], edges=[{"src": "api", "dst": "store"}, {"src": "api", "dst": "cache"}])
    return call, session


# ------------------------------------------------------------ the frontier


def test_the_push_carries_the_frontier_as_structure(board):
    call, session = board
    ev = session.state_event()
    front = ev["frontier"]
    assert front["fork"] is None
    assert {i["id"] for i in front["open"]} == {"api", "store", "cache"}
    item = next(i for i in front["open"] if i["id"] == "store")
    assert item["label"] == "URL mappings" and item["kind"] == "store" and item["depth"] == "stub"
    assert item["why"] in ("costliest to change later", "unelaborated")
    assert front["more"] == 0 and front["closed"] == []


def test_the_fork_is_carried_when_two_approaches_are_live(board):
    call, session = board
    call("approach", name="single box")
    call("approach", name="managed")
    front = session.state_event()["frontier"]
    assert front["fork"] == {"approaches": ["single box", "managed"]}


def test_more_counts_what_the_cap_hides(board):
    call, session = board
    call("canvas", nodes=[{"id": f"s{i}", "label": f"S{i}", "kind": "service"} for i in range(5)])
    front = session.state_event()["frontier"]
    assert len(front["open"]) == derive.FRONTIER_SHOWN
    assert front["more"] == 8 - derive.FRONTIER_SHOWN


def test_the_user_can_close_a_branch_as_good_enough(board):
    call, session = board
    res = apply_mutation(session, {"op": "settle", "id": "cache"})
    assert "good enough" in res["applied"]
    assert session.state.nodes["cache"].closed == "settled"
    front = session.state_event()["frontier"]
    assert "cache" not in {i["id"] for i in front["open"]}
    assert front["closed"] == [{"id": "cache", "label": "Cache", "how": "settled"}]


def test_out_of_scope_and_reopen(board):
    call, session = board
    apply_mutation(session, {"op": "out_of_scope", "id": "cache"})
    assert session.state.nodes["cache"].closed == "out_of_scope"
    assert "cache" not in [n.id for n in derive.askable(session.state)]
    apply_mutation(session, {"op": "reopen", "id": "cache"})
    assert session.state.nodes["cache"].closed == ""
    assert "cache" in [n.id for n in derive.askable(session.state)]


def test_closing_is_the_user_talking(board):
    """The architect hears it in the note, and is told to leave it alone."""
    call, session = board
    apply_mutation(session, {"op": "settle", "id": "cache"})
    assert any("good enough" in e for e in session.take_user_edits())
    note = derive.note(session.state)
    assert "closed by the user: cache (good enough)" in note
    assert "leave these alone" in note


def test_closing_an_unknown_box_is_refused(board):
    call, session = board
    with pytest.raises(MutationError):
        apply_mutation(session, {"op": "settle", "id": "nope"})


def test_a_closed_box_survives_a_reload(board, tmp_path):
    call, session = board
    apply_mutation(session, {"op": "out_of_scope", "id": "cache"})
    again = ArchSession.load(tmp_path / "run")
    assert again.state.nodes["cache"].closed == "out_of_scope"


# ------------------------------------------------------- picks are decisions


def ask(call):
    call("question", question="Does the data need to survive a restart?",
         recommendation="no", summary="durability",
         options=[
             {"label": "Ephemeral", "cost": "gone on restart; nothing to run"},
             {"label": "Persistent", "cost": "a file to back up", "rec": True},
         ])


def test_a_pick_is_recorded_as_the_users_decision(board):
    call, session = board
    ask(call)
    q = session.state.questions[0]
    value = q.options[0].value
    session.answer_ask({"id": q.id, "value": value})
    assert len(session.state.decisions) == 1
    d = session.state.decisions[0]
    assert d.source == "user" and d.topic == "durability" and d.choice == "Ephemeral"
    assert [o.name for o in d.options] == ["Ephemeral", "Persistent"]
    assert d.options[1].pros == ["a file to back up"]
    assert "gone on restart" in d.rationale


def test_a_revised_pick_updates_the_decision_rather_than_adding_one(board):
    call, session = board
    ask(call)
    q = session.state.questions[0]
    session.answer_ask({"id": q.id, "value": q.options[0].value})
    session.answer_ask({"id": q.id, "value": q.options[1].value})
    assert len(session.state.decisions) == 1
    assert session.state.decisions[0].choice == "Persistent"


def test_the_decision_lands_in_the_bundle_and_the_note(board, tmp_path):
    from bird.harnesses.arch.bundle import write_bundle

    call, session = board
    ask(call)
    q = session.state.questions[0]
    session.answer_ask({"id": q.id, "value": q.options[0].value})
    assert "durability->Ephemeral" in derive.note(session.state)
    _, md = write_bundle(session.state, tmp_path / "run")
    text = md.read_text()
    assert "durability" in text and "Ephemeral" in text


def test_a_question_without_a_summary_uses_its_own_words_as_the_topic(board):
    call, session = board
    call("question", question="Queue or in-process retries?", recommendation="in-process",
         options=[{"label": "queue", "cost": "retries for free"},
                  {"label": "in-process", "cost": "simpler", "rec": True}])
    q = session.state.questions[0]
    session.answer_ask({"id": q.id, "value": q.options[1].value})
    assert session.state.decisions[0].topic == "Queue or in-process retries?"


# ------------------------------------------------------ approach evidence


def test_an_approach_carries_who_runs_it_and_at_what_scale(board, tmp_path):
    from bird.harnesses.arch.bundle import write_bundle

    call, session = board
    call("approach", name="managed", summary="postgres + a load balancer",
         evidence={"who": "Bitly", "scale": "~10k req/s", "sources": ["https://example.com/bitly"]})
    a = session.state.approaches["managed"]
    assert a.evidence == {"who": "Bitly", "scale": "~10k req/s", "sources": ["https://example.com/bitly"]}
    assert session.state_event()["state"]["approaches"]["managed"]["evidence"]["who"] == "Bitly"
    # it survives a reload and reaches the bundle once the approach loses
    again = ArchSession.load(tmp_path / "run")
    assert again.state.approaches["managed"].evidence["scale"] == "~10k req/s"
    call("approach", id="managed", status="greyed", rejected_reason="too much to run")
    _, md = write_bundle(session.state, tmp_path / "run")
    assert "*Seen at:* Bitly · ~10k req/s (https://example.com/bitly)" in md.read_text()
