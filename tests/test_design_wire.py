"""The design harness on the existing pump: state_event's shape, on_mutate's
fall-through to ctx.design, and HttpTransport's design_state replay.

Memory-only sessions throughout (run_dir=None), like test_design_session.py;
the Server is poked with just the attributes on_mutate reads, because a full
Server needs a Repl and a broker and this branch needs neither.
"""

from types import SimpleNamespace

from bird.harnesses.design import dom
from bird.harnesses.design.session import DesignSession
from bird.http_transport import HttpTransport
from bird.serve import Server

HERO = "<html><body><h1>Hello</h1></body></html>"


def _session() -> DesignSession:
    s = DesignSession()
    s.start("a landing page for a podcast")
    s.add_artboard(HERO, "Hero")
    return s


def _server_with(design: DesignSession) -> Server:
    """A Server carrying only what on_mutate touches: self.repl.runner.ctx."""
    server = Server.__new__(Server)
    server.repl = SimpleNamespace(runner=SimpleNamespace(ctx=SimpleNamespace(design=design)))
    return server


def test_state_event_carries_artboards_html_and_selection():
    s = _session()
    ev = s.state_event({"kind": "artboard", "id": "hero"})
    assert ev["type"] == "design_state" and ev["status"] == "ready"
    assert ev["changed"] == {"kind": "artboard", "id": "hero"}
    assert ev["artboards"] == [
        {"id": "hero", "title": "Hero", "current": "v1", "finalized": False,
         # versions carry their authorship: the page's history panel shows who
         # made each one, and `note` is where mutate.apply records the actor
         "versions": [{"id": "v1", "kind": "generated", "note": "generated"}]}
    ]
    # every version ships the bridge, the generated first one included
    assert dom.EDITOR_ATTR in ev["html"]["hero"]
    assert dom.unwrap_editor(ev["html"]["hero"]) == HERO
    assert ev["selected"] == "hero"
    assert "finalized_artboard" not in ev, "nothing is chosen yet"
    assert isinstance(ev["themes"], list) and ev["themes"], "the page's start overlay reads the theme list off the push"


def test_state_event_carries_the_theme_names_for_the_start_overlay():
    """The page cannot scan the disk, so the offerable theme names ride the
    state push — the overlay's design-system options are built from them."""
    ev = DesignSession().state_event()
    assert {"editorial", "apple"} <= set(ev["themes"])


def test_state_event_themes_feed_the_chat_bar_picker():
    """The picker replaced the overlay: the same push now populates the chat
    bar's theme select, so the contract is unchanged — only the consumer."""
    ev = DesignSession().state_event()
    assert "wireframe" in ev["themes"]


def test_on_mutate_routes_a_dom_op_to_the_design_session():
    s = _session()
    out = _server_with(design=s).on_mutate(
        {"op": "set_text", "selector": "0>0>0", "text": "Hi", "artboard": "hero"}
    )
    assert out["ok"] is True
    assert out["version"] == "v2"
    assert "Hi" in s.read_html("hero")


def test_on_mutate_routes_finalize_to_the_design_session():
    s = _session()
    out = _server_with(design=s).on_mutate({"op": "finalize", "artboard": "hero"})
    assert out == {"ok": True}
    assert s.state.finalized and s.state.finalized_artboard == "hero"


def test_on_mutate_routes_undo_to_the_design_session():
    """The page's Undo button and design_undo share one stack per artboard."""
    s = _session()
    server = _server_with(design=s)
    server.on_mutate({"op": "set_text", "selector": "0>0>0", "text": "Hi", "artboard": "hero"})
    out = server.on_mutate({"op": "undo", "artboard": "hero"})
    assert out["ok"] is True and out["version"] == "v1"
    assert "Hello" in dom.unwrap_editor(s.read_html("hero"))
    assert s.state.op_log == []


def test_on_mutate_refuses_an_undo_with_nothing_to_undo():
    """A refusal is an answer: the page shows it, the session stays put."""
    s = _session()
    out = _server_with(design=s).on_mutate({"op": "undo", "artboard": "hero"})
    assert out["ok"] is False and "nothing to undo" in out["error"]
    assert s.state.artboards["hero"].current == "v1"


def test_http_transport_replays_a_design_state_to_a_late_subscriber(tmp_path):
    transport = HttpTransport(static_dir=tmp_path)
    try:
        transport.emit({"type": "design_state", "status": "ready", "artboards": []})
        _, replay = transport.subscribe()
        states = [e for e in replay if e.get("type") == "design_state"]
        assert len(states) == 1
        assert states[0]["replayed"] is True
    finally:
        transport.shutdown()

def test_state_event_carries_the_brief_and_the_theme():
    """The rail shows what the artboards are answering, and the page has no
    other way to learn it: the brief rides every push, and so does the design
    system once one is set."""
    s = _session()
    ev = s.state_event()
    assert ev["prompt"] == "a landing page for a podcast"
    assert ev["theme"] is None
    assert ev["theme_css"] == ""
    s.set_theme("wireframe")
    after = s.state_event()
    assert after["theme"] == "wireframe"
    from bird.harnesses.design import themes as _themes
    assert after["theme_css"] == _themes.load_theme("wireframe")["css"]


def test_http_transport_does_not_replay_tool_call_fragments(tmp_path):
    """A page joining mid-session has nothing half-drawn to append a fragment
    to, and the tool_result that follows carries the whole call."""
    transport = HttpTransport(static_dir=tmp_path)
    try:
        transport.emit({"type": "harness_event", "event": "tool_call_delta",
                        "data": {"index": 0, "name": "design_create", "text": "<h1>"}})
        transport.emit({"type": "harness_event", "event": "tool_result",
                        "data": {"name": "design_create", "details": {"ok": True}}})
        _, replay = transport.subscribe()
        events = [e["event"] for e in replay if e.get("type") == "harness_event"]
        assert events == ["tool_result"]
    finally:
        transport.shutdown()


def test_describe_subjects_names_an_element_the_user_selected():
    """A selection travels as `artboard#selector`; the designer gets the
    element in the artboard's own terms, and the page's transcript reads the
    bullet back as "about h1 in Hero"."""
    s = _session()
    out = s.describe_subjects(["hero#0>0>0"])
    assert out is not None
    lines = out.split("\n")
    assert lines[0] == "[the user is pointing at]"
    assert lines[1] == "- h1 in Hero"
    assert "selector 0>0>0 (artboard hero, at v1)" in lines[2]
    assert 'text: "Hello"' in lines[3]


def test_describe_subjects_names_a_whole_artboard():
    s = _session()
    out = s.describe_subjects(["hero"])
    assert out == "[the user is pointing at]\n- Hero\n  artboard hero, at v1"


def test_describe_subjects_drops_what_no_longer_resolves():
    """A stale selection is the page being a moment behind the document, not
    something to make the designer explain: unknown artboards and selectors
    that miss are dropped, and nothing at all yields no block."""
    s = _session()
    assert s.describe_subjects(["nope", "hero#0>9>9", "hero#0>0>0>0", "hero#x"]) is None
    assert s.describe_subjects([]) is None
    both = s.describe_subjects(["hero#0>0>0", "hero", "hero"])  # deduped
    assert both is not None and both.count("\n- ") == 2


def test_the_pump_finds_the_design_session_for_what_a_message_points_at():
    """serve's focus block used to ask only the arch session; a design session
    with no arch beside it answers the same question."""
    s = _session()
    server = _server_with(design=s)
    server.repl.runner.ctx.arch = None
    focus = server._board_focus(["hero#0>0>0"])
    assert focus is not None and focus.startswith("[the user is pointing at]\n- h1 in Hero")
    assert server._board_focus([]) is None
