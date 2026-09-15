"""mutate.apply stores the WRAPPED document — the editor bridge rides with
every version — and undo restores prior versions exactly as stored."""

from __future__ import annotations

from pathlib import Path

from bird.harnesses.design import dom, mutate
from bird.harnesses.design.mutate import MutationError, undo
from bird.harnesses.design.session import DesignSession

HERO = "<html><body><h1>Hello</h1></body></html>"
EDIT = {"op": "set_text", "selector": "0>0>0", "text": "Hi"}


def _session(run_dir: Path | None = None) -> DesignSession:
    s = DesignSession(run_dir=run_dir)
    s.start("a landing page")
    s.add_artboard(HERO, "Hero")
    return s


def test_bridge_script_loads_the_real_file_and_caches_it():
    script = mutate._bridge_script()
    assert script and dom.EDITOR_ATTR not in script
    assert mutate._BRIDGE_CACHE is script  # second call is the cached object


def test_bridge_script_falls_back_to_empty_when_the_file_is_missing(monkeypatch):
    monkeypatch.setattr(mutate, "_BRIDGE_CACHE", None)

    class _Missing:
        @property
        def parent(self) -> "_Missing":
            return self

        def __truediv__(self, other: str) -> Path:
            return Path("/nonexistent") / other

    monkeypatch.setattr(mutate, "Path", lambda _file: _Missing())
    assert mutate._bridge_script() == ""


def test_apply_stores_wrapped_html_in_memory_and_on_disk(tmp_path):
    s = _session(tmp_path)
    s.apply_edit("hero", EDIT)
    stored = s.read_html("hero")
    assert dom.EDITOR_ATTR in stored  # the cache the page is fed from
    on_disk = (tmp_path / "artboards" / "hero" / "v2.html").read_text(encoding="utf-8")
    assert on_disk == stored  # disk and memory agree, both wrapped
    assert "Hi" in dom.unwrap_editor(on_disk)


def test_undo_restores_the_prior_version_exactly_as_stored(tmp_path):
    s = _session(tmp_path)
    s.apply_edit("hero", EDIT)
    prior = undo(s, "hero")
    assert prior.id == "v1"
    # v1 was stored wrapped like every version; undo restores it byte for byte
    on_disk = (tmp_path / "artboards" / "hero" / "v1.html").read_text(encoding="utf-8")
    assert s.read_html("hero") == on_disk
    assert dom.unwrap_editor(s.read_html("hero")) == HERO


def test_undo_between_two_edited_versions_stays_wrapped(tmp_path):
    s = _session(tmp_path)
    s.apply_edit("hero", EDIT)
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Yo"})
    undo(s, "hero")  # back to v2, which was stored wrapped
    assert dom.EDITOR_ATTR in s.read_html("hero")


def test_repeated_edits_keep_exactly_one_bridge_script():
    s = _session()
    s.apply_edit("hero", EDIT)
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Yo"})
    assert s.read_html("hero").count(f"<script {dom.EDITOR_ATTR}") == 1


def test_state_event_html_the_page_receives_is_wrapped():
    s = _session()
    s.apply_edit("hero", EDIT)
    event = s.state_event()
    assert dom.EDITOR_ATTR in event["html"]["hero"]


def test_missing_bridge_file_degrades_to_unwrapped_storage(monkeypatch):
    monkeypatch.setattr(mutate, "_BRIDGE_CACHE", "")
    s = _session()
    s.apply_edit("hero", EDIT)
    assert dom.EDITOR_ATTR not in s.read_html("hero")
    assert "Hi" in s.read_html("hero")


def test_undo_takes_the_op_out_of_the_log_with_its_version():
    """A log entry citing a popped version reads as though a change the user
    took back is still in the design — and the handoff prints it."""
    s = _session()
    s.apply_edit("hero", EDIT)
    assert len(s.state.op_log) == 1
    undo(s, "hero")
    assert s.state.op_log == []


def test_an_applied_edit_leaves_the_session_ready():
    """The status the page gates Finalize on. It used to stick mid-flight."""
    s = _session()
    s.apply_edit("hero", EDIT)
    assert s.status == "ready"


def test_undo_on_a_finalized_session_is_refused():
    s = _session()
    s.finalize("hero")
    try:
        undo(s, "hero")
    except MutationError as e:
        assert "finalized" in str(e)
    else:
        raise AssertionError("undo on a finalized session must be refused")

def test_the_bridge_lives_beside_the_harness_not_in_the_built_page():
    """static/ is emptied by every `npm run build` of design-ui, so the script
    the harness injects into artboards cannot live there."""
    from pathlib import Path as _P

    import bird.harnesses.design.mutate as m

    path = _P(m.__file__).parent / "editor-bridge.js"
    assert path.is_file()
    assert not (path.parent / "static" / "editor-bridge.js").exists()
    assert mutate._bridge_script() == path.read_text(encoding="utf-8")
