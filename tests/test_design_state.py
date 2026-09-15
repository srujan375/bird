"""DesignState — the session's shape: artboards, version graphs, round-trip."""

from __future__ import annotations

from bird.harnesses.design.state import Artboard, DesignState, Version


def _state() -> DesignState:
    st = DesignState(prompt="a landing page for a podcast")
    a = Artboard(id="hero", title="Hero")
    a.versions.append(Version(id="v1", kind="generated", html_path="artboards/hero/v1.html"))
    a.current = "v1"
    st.artboards["hero"] = a
    st.selected = "hero"
    return st


def test_artboard_ids_slug_and_dedupe():
    st = DesignState()
    assert st.next_artboard_id("Landing Page!") == "landing-page"
    st.artboards["landing-page"] = Artboard(id="landing-page", title="Landing Page")
    assert st.next_artboard_id("Landing Page!") == "landing-page-2"


def test_version_ids_count_past_gaps():
    st = _state()
    a = st.artboards["hero"]
    assert st.next_version_id(a) == "v2"
    a.versions.append(Version(id="v2", kind="edited", html_path="artboards/hero/v2.html"))
    a.versions.append(Version(id="v3", kind="edited", html_path="artboards/hero/v3.html"))
    assert st.next_version_id(a) == "v4"


def test_current_version_falls_back_to_the_latest():
    a = _state().artboards["hero"]
    a.versions.append(Version(id="v2", kind="edited", html_path="artboards/hero/v2.html"))
    a.current = "ghost"  # a stale pointer must not hide the real latest
    assert a.current_version().id == "v2"


def test_an_empty_artboard_has_no_current_version():
    assert Artboard(id="x", title="X").current_version() is None


def test_round_trip_through_dict():
    st = _state()
    st.op_log.append({"artboard": "hero", "version": "v1", "op": {"op": "set_text"}, "by": "user"})
    st.finalized = True
    st.finalized_artboard = "hero"
    out = DesignState.from_dict(st.to_dict())
    assert out.prompt == st.prompt
    assert out.selected == "hero"
    assert out.finalized and out.finalized_artboard == "hero"
    a = out.artboards["hero"]
    assert (a.id, a.title, a.current) == ("hero", "Hero", "v1")
    assert [v.kind for v in a.versions] == ["generated"]
    assert out.op_log == st.op_log


def test_from_dict_tolerates_missing_and_partial_fields():
    out = DesignState.from_dict({})
    assert out.prompt == "" and out.artboards == {}
    out = DesignState.from_dict({"artboards": {"a": {"versions": [{"id": "v1"}]}}})
    a = out.artboards["a"]
    assert a.title == "a"
    assert a.versions[0].kind == "generated"
    assert a.versions[0].html_path == ""