"""DesignSession — the lifecycle: generate, edit through mutate, undo, finalize.

The state shape is pinned in test_design_state.py; here it is the harness's
behaviour around it — what the transport is told, and that every document
change lands through mutate.apply's one door.
"""

from __future__ import annotations

import pytest

from bird.harnesses.design import dom
from bird.harnesses.design.mutate import MutationError, undo
from bird.harnesses.design.session import DesignSession
from bird.harnesses.design.state import DesignState

HERO = "<html><body><h1>Hello</h1></body></html>"


def _session() -> DesignSession:
    """One generated artboard, memory-only — the shape every edit test needs."""
    s = DesignSession()
    s.start("a landing page for a podcast")
    s.add_artboard(HERO, "Hero")
    return s


def test_start_emits_generating_then_add_artboard_emits_ready_with_v1():
    events: list[dict] = []
    s = DesignSession(on_state=events.append)
    s.start("a landing page for a podcast")
    assert s.status == "generating"
    assert events[-1]["status"] == "generating"
    s.add_artboard(HERO, "Hero")
    assert s.status == "ready"
    assert events[-1]["status"] == "ready"
    assert events[-1]["changed"] == {"kind": "artboard", "id": "hero"}
    board = s.state.artboards["hero"]
    assert board.current == "v1"
    assert [(v.id, v.kind) for v in board.versions] == [("v1", "generated")]
    assert s.state.selected == "hero"


def test_apply_edit_goes_through_mutate_apply_and_bumps_the_version():
    s = _session()
    out = s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})
    assert out["version"] == "v2"
    board = s.state.artboards["hero"]
    assert board.current == "v2"
    assert [(v.id, v.kind) for v in board.versions] == [
        ("v1", "generated"), ("v2", "edited"),
    ]
    entry = s.state.op_log[-1]
    assert (entry["artboard"], entry["version"], entry["by"]) == ("hero", "v2", "user")
    assert "Hi" in s.read_html("hero")


def test_undo_restores_the_prior_html():
    s = _session()
    before = s.read_html("hero")
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})
    prior = undo(s, "hero")
    assert prior.id == "v1"
    board = s.state.artboards["hero"]
    assert board.current == "v1" and len(board.versions) == 1
    assert s.read_html("hero") == before


def test_finalize_records_the_chosen_artboard_and_locks_the_session():
    s = _session()
    s.finalize("hero")
    assert s.status == "finalized"
    assert s.state.finalized and s.state.finalized_artboard == "hero"


def test_finalize_writes_the_handoff_bundle(tmp_path):
    """Whoever dispatched this session reads DESIGN.md the moment finalize
    returns, so writing it is part of finalizing — not a step a caller can
    forget. Both finalize paths, the tool and the page's button, come here."""
    s = DesignSession(run_dir=tmp_path)
    s.start("a landing page")
    s.add_artboard(HERO, "Hero")
    s.finalize("hero")
    md = (tmp_path / "bundle" / "DESIGN.md").read_text(encoding="utf-8")
    assert md.startswith("# Design — Hero")
    assert "<h1>Hello</h1>" in md
    assert dom.EDITOR_ATTR not in md  # an OUT boundary: the bridge stays home
    assert (tmp_path / "bundle" / "design.json").is_file()


def test_regenerate_lands_as_a_refined_version_of_the_same_artboard():
    s = _session()
    version = s.regenerate("hero", "<html><body><h2>Again</h2></body></html>")
    assert (version.id, version.kind) == ("v2", "refined")
    assert len(s.state.artboards) == 1, "a rebuild is a version, not a new board"
    assert "Again" in s.read_html("hero")


def test_an_edit_on_a_finalized_session_is_refused():
    s = _session()
    s.finalize("hero")
    with pytest.raises(MutationError, match="finalized"):
        s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})


# --------------------------------------------------------------- showcase

def test_showcase_elevates_the_picked_artboard_and_returns_the_polish_turn():
    s = _session()
    s.add_artboard(HERO, "Second")
    turn = s.showcase("hero")
    assert s.status == "showcase"
    assert s.state.showcase_artboard == "hero"
    assert s.state.selected == "hero"
    assert "hero" in turn and "design_polish" in turn


def test_showcase_pushes_the_phase_before_returning():
    """Push, then dispatch: the caller sends the polish turn only after the
    state push, so the page has swapped to the showcase view before the
    designer starts writing."""
    events: list[dict] = []
    s = _session()
    s.on_state = events.append
    s.showcase("hero")
    assert events[-1]["status"] == "showcase"
    assert events[-1]["showcase_artboard"] == "hero"


def test_showcase_exit_returns_to_ready_and_keeps_the_versions():
    s = _session()
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})
    s.showcase("hero")
    s.showcase_exit()
    assert s.status == "ready" and s.state.showcase_artboard == ""
    assert len(s.state.artboards["hero"].versions) == 2, "going back never throws work away"


def test_showcase_refuses_a_second_elevation():
    s = _session()
    s.add_artboard(HERO, "Second")
    s.showcase("hero")
    with pytest.raises(ValueError, match="already"):
        s.showcase("second")


def test_showcase_refuses_after_finalize():
    s = _session()
    s.finalize("hero")
    with pytest.raises(ValueError, match="read-only"):
        s.showcase("hero")


def test_showcase_exit_outside_the_phase_is_refused():
    s = _session()
    with pytest.raises(ValueError, match="not in the showcase phase"):
        s.showcase_exit()


def test_edits_lock_to_the_elevated_artboard():
    s = _session()
    s.add_artboard(HERO, "Second")
    s.showcase("hero")
    with pytest.raises(MutationError, match="showcase phase owns the screen"):
        s.apply_edit("second", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})
    # the elevated one keeps taking edits — the user's inspector and the
    # designer's ops both keep working mid-polish
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})


def test_finalize_from_showcase_clears_the_phase():
    s = _session()
    s.showcase("hero")
    s.finalize("hero")
    assert s.status == "finalized" and s.state.showcase_artboard == ""


def test_the_phase_survives_a_state_round_trip():
    """The phase is session state, persisted in design_state.json: the page's
    late-joiner replay carries it in the state push, and a DesignState restored
    from disk comes back with the phase on — no special-casing on either side."""
    s = _session()
    s.showcase("hero")
    event = s.state_event()
    assert event["status"] == "showcase" and event["showcase_artboard"] == "hero"
    restored = DesignState.from_dict(s.state.to_dict())
    assert restored.showcase_artboard == "hero"
    assert DesignSession(state=restored).status == "showcase"


# --------------------------------------------------------------- delete

def test_delete_artboard_removes_the_card_and_pushes():
    events: list[dict] = []
    s = _session()
    s.on_state = events.append
    s.delete_artboard("hero")
    assert s.state.artboards == {}
    assert s.state.selected == ""
    assert events[-1]["artboards"] == [] and events[-1]["html"] == {}
    assert events[-1]["changed"] == {"kind": "artboard", "id": "hero"}


def test_delete_artboard_cleans_the_stored_html(tmp_path):
    """The versions under run_dir/artboards/<aid>/ are generated artifacts of
    a document that no longer exists — the same files write_version created."""
    s = DesignSession(run_dir=tmp_path)
    s.start("a landing page")
    s.add_artboard(HERO, "Hero")
    assert (tmp_path / "artboards" / "hero" / "v1.html").is_file()
    s.delete_artboard("hero")
    assert not (tmp_path / "artboards" / "hero").exists()


def test_delete_artboard_selects_the_first_remaining():
    s = _session()
    s.add_artboard(HERO, "Second")
    s.delete_artboard("hero")
    assert s.state.selected == "second"


def test_delete_artboard_drops_its_critiques():
    """Critiques are keyed 'artboard:version' — with the artboard gone they
    are orphans no version can point at again."""
    s = _session()
    s.state.record_critique("hero", "v1", "fix the hero")
    s.delete_artboard("hero")
    assert s.state.critiques == {}


def test_delete_artboard_is_refused_after_finalize():
    s = _session()
    s.finalize("hero")
    with pytest.raises(ValueError, match="read-only"):
        s.delete_artboard("hero")


def test_delete_artboard_is_refused_mid_showcase():
    """The phase belongs to the elevated artboard — deleting under it, the
    elevated one included, would pull the full-bleed view out from under
    itself. Back is the clean path out."""
    s = _session()
    s.add_artboard(HERO, "Second")
    s.showcase("hero")
    with pytest.raises(ValueError, match="showcase phase owns the screen"):
        s.delete_artboard("second")
    with pytest.raises(ValueError, match="showcase phase owns the screen"):
        s.delete_artboard("hero")
    assert s.status == "showcase" and "hero" in s.state.artboards


def test_delete_artboard_names_the_known_ones_on_a_bad_id():
    s = _session()
    with pytest.raises(KeyError, match="hero"):
        s.delete_artboard("nope")


def test_an_empty_board_after_delete_is_legal_and_create_refills_it():
    s = _session()
    s.delete_artboard("hero")
    s.add_artboard(HERO, "Hero")
    assert list(s.state.artboards) == ["hero"]
    assert s.state.artboards["hero"].current == "v1"

# ----------------------------------------------- what the push tells the page

def test_state_event_carries_the_latest_critique_per_artboard_and_the_plans():
    s = _session()
    s.add_artboard(HERO, "Alt")
    s.state.record_critique("hero", "v1", "hero: too polite", model="fake")
    s.state.plan_critique = "the plan defaults to cream and serif"
    ev = s.state_event()
    assert ev["critiques"] == {"hero": {"version": "v1", "kind": "render", "text": "hero: too polite"}}
    assert ev["plan_critique"] == "the plan defaults to cream and serif"
    assert ev["pending_user_edits"] == 0
    # a critique of a polished version is a polish critique
    s.showcase("hero")
    s.write_version("hero", s.read_html("hero"), "polished", note="polish: motion")
    s.state.record_critique("hero", "v2", "600ms is fine", model="fake")
    assert s.state_event()["critiques"]["hero"] == {"version": "v2", "kind": "polish", "text": "600ms is fine"}


def test_plan_critique_is_none_on_the_push_until_the_critic_spoke():
    assert _session().state_event()["plan_critique"] is None


# --------------------------------------------- what the user did on the board

def test_compose_activity_prompt_drains_user_edits_in_words_once():
    s = _session()
    events: list[dict] = []
    s.on_state = events.append
    s.apply_edit("hero", {"op": "set_style", "selector": "0>0>0", "props": {"font-size": "5rem"}})
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Bird"})
    assert events[-1]["pending_user_edits"] == 2
    prompt = s.compose_activity_prompt()
    assert prompt.startswith("[the user edited on the board]")
    assert "set_style on h1" in prompt and "font-size: 5rem" in prompt
    assert 'set_text on h1' in prompt and '"Bird"' in prompt
    assert "'Hero'" in prompt
    # delivered once: the same gesture is never reported twice
    assert s.compose_activity_prompt() is None
    assert s.state_event()["pending_user_edits"] == 0


def test_compose_activity_prompt_ignores_the_designers_own_edits():
    from bird.harnesses.design.mutate import apply

    s = _session()
    apply(s, "hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"}, actor="ai")
    assert s.compose_activity_prompt() is None
    assert s.state_event()["pending_user_edits"] == 0


# -------------------------------------------------------------- load

def test_load_rebuilds_a_session_from_its_run_dir(tmp_path):
    s = DesignSession(run_dir=tmp_path, intake_gate=True, repo_root=tmp_path)
    s.start("a landing page for a podcast")
    s.answer_ask({"id": "fidelity", "value": "high-fidelity"})
    s.answer_ask({"id": "design-system", "value": "editorial"})
    s.set_theme("editorial")
    s.add_artboard(HERO, "Hero")
    s.add_artboard("<html><body><h1>Other</h1></body></html>", "Other")
    s.apply_edit("hero", {"op": "set_text", "selector": "0>1>0", "text": "Hi"})
    s.state.plan = "one hero, one marquee"
    s.state.plan_critique = "name the signature element"
    s.state.record_critique("hero", "v2", "hero: too polite", model="fake")
    s.touched()

    back = DesignSession.load(tmp_path, intake_gate=True, repo_root=tmp_path)
    assert back.status == "ready"
    assert back.pending_ask() is None  # settled stays settled: nothing is re-asked
    assert back.state.intake.value("design-system") == "editorial"
    assert back.theme == "editorial" and back.state.theme == "editorial"
    assert list(back.state.artboards) == ["hero", "other"]
    assert [v.id for v in back.state.artboards["hero"].versions] == ["v1", "v2"]
    assert back.state.artboards["hero"].current == "v2"
    assert "<h1>Hi</h1>" in back.read_html("hero")
    assert "<h1>Other</h1>" in back.read_html("other")
    assert back.state.plan == "one hero, one marquee"
    assert back.state.plan_critique == "name the signature element"
    assert back.state.critique_for("hero", "v2")["critique"] == "hero: too polite"
    assert back.state.op_log[-1]["by"] == "user"
    # and it keeps working: the next edit lands as v3 on the same stack
    back.apply_edit("hero", {"op": "set_text", "selector": "0>1>0", "text": "Again"})
    assert back.state.artboards["hero"].current == "v3"
    assert (tmp_path / "artboards" / "hero" / "v3.html").is_file()


def test_load_resumes_an_unsettled_intake_at_its_question(tmp_path):
    s = DesignSession(run_dir=tmp_path, intake_gate=True)
    s.start("x")
    s.answer_ask({"id": "fidelity", "value": "high-fidelity"})
    back = DesignSession.load(tmp_path, intake_gate=True)
    assert back.status == "asking"
    assert back.pending_ask().id == "design-system"


def test_load_names_a_missing_session(tmp_path):
    with pytest.raises(FileNotFoundError, match="design_state.json"):
        DesignSession.load(tmp_path / "nope")


def test_start_on_a_board_that_already_has_artboards_is_ready_not_generating():
    s = _session()
    s.start("the same brief again")
    assert s.status == "ready"
