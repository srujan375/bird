"""design_create and design_read — the two ends of the loop that were missing.

Without create, nothing the model does reaches the user's screen; without read,
it has no way to learn a selector for anything it did not just write.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bird.harnesses.design import dom
from bird.harnesses.design.critique import Critic
from bird.harnesses.design.session import DesignSession
from bird.harnesses.design.tools import (
    DesignCreateTool,
    DesignDeleteArtboardTool,
    DesignEditTool,
    DesignLookTool,
    DesignPolishTool,
    DesignReadTool,
    DesignSetThemeTool,
    DesignShowcaseTool,
)
from bird.llm.types import LLMResponse, Message, Usage
from bird.tools import ToolContext, ToolError

DOC = ('<!doctype html><html><head><title>Pod</title></head>'
       '<body><header class="hero"><h1>Signal</h1><p>Weekly &amp; unfiltered</p></header>'
       '<main><ul><li>Ep 1</li></ul></main></body></html>')


@pytest.fixture
def ctx() -> ToolContext:
    c = ToolContext(repo_root=Path("."))
    c.design = DesignSession()
    c.design.start("a podcast landing page")
    return c


def _rows(output: str) -> list[tuple[str, str]]:
    """(selector, tag) for every line of an outline, minus the header."""
    out = []
    for line in output.splitlines()[1:]:
        selector, label = line.split()[0], line.split()[1]
        out.append((selector, label.split("#")[0].split(".")[0]))
    return out


# ------------------------------------------------------------------ create

def test_create_puts_an_artboard_on_screen_at_v1(ctx):
    result = DesignCreateTool().run({"html": DOC, "title": "Hero — dark"}, ctx)
    assert result.details["ok"] and result.details["version"] == "v1"
    board = ctx.design.state.artboards[result.details["artboard"]]
    assert board.title == "Hero — dark"
    assert [(v.id, v.kind) for v in board.versions] == [("v1", "generated")]
    assert ctx.design.state.selected == board.id
    assert ctx.design.status == "ready"


def test_create_with_an_existing_id_rebuilds_that_artboard(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    result = DesignCreateTool().run(
        {"html": "<html><body><h1>Rebuilt</h1></body></html>", "artboard": aid}, ctx)
    board = ctx.design.state.artboards[aid]
    assert len(ctx.design.state.artboards) == 1  # rebuilt, not a second board
    assert [(v.id, v.kind) for v in board.versions] == [("v1", "generated"), ("v2", "refined")]
    assert "Rebuilt" in ctx.design.read_html(aid)
    assert "fresh" in result.output  # the selectors moved; say so


def test_create_is_refused_after_finalize(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    ctx.design.finalize(aid)
    with pytest.raises(ToolError, match="read-only"):
        DesignCreateTool().run({"html": DOC}, ctx)


def test_create_needs_html(ctx):
    with pytest.raises(ToolError, match="needs the artboard's html"):
        DesignCreateTool().run({"html": "   "}, ctx)


# -------------------------------------------------------------------- read

def test_every_selector_read_prints_resolves_to_the_element_it_names(ctx):
    """The whole point of the outline: paths the edit path accepts."""
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    rows = _rows(DesignReadTool().run({}, ctx).output)
    assert rows, "an outline with no elements is not an outline"
    root = dom.parse(dom.unwrap_editor(ctx.design.read_html(aid)))
    for selector, tag in rows:
        if tag == "html":  # the root anchor, unselectable like head and script
            continue
        assert dom.resolve(root, selector).tag == tag


def test_read_shows_structure_and_leaf_text(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    out = DesignReadTool().run({}, ctx).output
    assert "0>1>0      header.hero" in out
    assert '"Signal"' in out
    assert "<head>" not in out and "title" not in out  # plumbing, not design


def test_read_hides_the_editor_bridge(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    assert dom.EDITOR_ATTR not in DesignReadTool().run({"format": "html"}, ctx).output
    assert dom.EDITOR_ATTR not in DesignReadTool().run({}, ctx).output


def test_read_scopes_to_a_subtree(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    out = DesignReadTool().run({"format": "html", "selector": "0>1>0>0"}, ctx).output
    assert out == "<h1>Signal</h1>"


def test_read_refuses_a_selector_that_does_not_resolve(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    with pytest.raises(ToolError, match="no longer resolves"):
        DesignReadTool().run({"selector": "0>9>9"}, ctx)


# -------------------------------------------------------------------- loop

def test_a_selector_from_read_is_one_edit_accepts(ctx):
    """create -> read -> edit, the loop the harness could not previously close."""
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    selector = next(s for s, tag in _rows(DesignReadTool().run({}, ctx).output) if tag == "h1")
    DesignEditTool().run({"op": "set_text", "selector": selector, "text": "Noise"}, ctx)
    assert "<h1>Noise</h1>" in dom.unwrap_editor(ctx.design.read_html(aid))


def test_the_design_tools_refuse_outside_a_design_session():
    plain = ToolContext(repo_root=Path("."))
    for tool in (DesignCreateTool(), DesignReadTool()):
        with pytest.raises(ToolError, match="not a design session"):
            tool.run({"html": DOC}, plain)


# --------------------------------------------------------------- showcase

def test_design_showcase_enters_the_phase_and_hands_back_the_polish_brief(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    result = DesignShowcaseTool().run({"artboard": aid}, ctx)
    assert result.details["showcase"] is True
    assert ctx.design.status == "showcase"
    assert ctx.design.state.showcase_artboard == aid
    assert "design_polish" in result.output


def test_create_is_refused_mid_showcase(ctx):
    """The canvas is gone: a new artboard has nowhere to land, and even a
    rebuild of the elevated one would pull the full-bleed view out from
    under itself."""
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    DesignShowcaseTool().run({"artboard": aid}, ctx)
    with pytest.raises(ToolError, match="showcase phase owns the screen"):
        DesignCreateTool().run({"html": DOC, "artboard": aid}, ctx)


def test_set_theme_is_refused_mid_showcase(ctx):
    """A theme pass restyles every artboard; mid-showcase the user is looking
    at one of them full-bleed. The guard fires before the theme is validated."""
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    DesignShowcaseTool().run({"artboard": aid}, ctx)
    with pytest.raises(ToolError, match="showcase phase owns the screen"):
        DesignSetThemeTool().run({"theme": "no-such-theme"}, ctx)


def test_edit_on_another_artboard_is_refused_mid_showcase(ctx):
    first = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    second = DesignCreateTool().run({"html": DOC, "title": "Alt"}, ctx).details["artboard"]
    DesignShowcaseTool().run({"artboard": first}, ctx)
    with pytest.raises(ToolError, match="showcase phase owns the screen"):
        DesignEditTool().run(
            {"op": "set_text", "selector": "0>1", "text": "X", "artboard": second}, ctx)


# ----------------------------------------------------------------- polish

def test_polish_outside_the_showcase_phase_is_refused(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    with pytest.raises(ToolError, match="showcase phase"):
        DesignPolishTool().run({"css": "h1{color:red}"}, ctx)


def test_polish_on_another_artboard_is_refused(ctx):
    first = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    second = DesignCreateTool().run({"html": DOC, "title": "Alt"}, ctx).details["artboard"]
    DesignShowcaseTool().run({"artboard": first}, ctx)
    with pytest.raises(ToolError, match="polish applies to"):
        DesignPolishTool().run({"css": "h1{color:red}", "artboard": second}, ctx)


def test_polish_with_unbalanced_braces_is_refused(ctx):
    """No css parser here, and none needed: the truncated paste is the
    failure that matters, and the refusal is information the model retries."""
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    DesignShowcaseTool().run({"artboard": aid}, ctx)
    with pytest.raises(ToolError, match="unbalanced braces"):
        DesignPolishTool().run({"css": "h1{color:red"}, ctx)


def test_polish_lands_a_marked_polished_version_and_folds_the_critique_note(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    DesignShowcaseTool().run({"artboard": aid}, ctx)
    result = DesignPolishTool().run({"css": "h1{color:red}", "script": "a()"}, ctx)
    assert result.details["ok"] and result.details["version"] == "v2"
    board = ctx.design.state.artboards[aid]
    assert board.versions[-1].kind == "polished"
    html = ctx.design.read_html(aid)
    assert dom.MOTION_ATTR in html and "a()" in html
    # no critic configured: the auto-critique degrades to a skip note —
    # the polish lands exactly as it would with no critic at all
    assert "not critiqued" in result.output


# ----------------------------------------------------------------- delete

def test_delete_artboard_tool_removes_the_card(ctx):
    """The board-level delete: the whole artboard goes, not an element inside
    it — that distinction is the tool's reason to exist beside design_edit."""
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    DesignCreateTool().run({"html": DOC, "title": "Alt"}, ctx)
    result = DesignDeleteArtboardTool().run({"artboard": "hero"}, ctx)
    assert result.details["ok"] is True and result.details["deleted"] is True
    assert "hero" not in ctx.design.state.artboards
    assert ctx.design.state.selected == "alt"
    assert "1 artboard" in result.output


def test_delete_artboard_tool_allows_an_empty_board(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    result = DesignDeleteArtboardTool().run({"artboard": "hero"}, ctx)
    assert ctx.design.state.artboards == {}
    assert "empty" in result.output


def test_delete_artboard_tool_refuses_after_finalize(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    ctx.design.finalize("hero")
    with pytest.raises(ToolError, match="read-only"):
        DesignDeleteArtboardTool().run({"artboard": "hero"}, ctx)


def test_delete_artboard_tool_refuses_mid_showcase(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    DesignShowcaseTool().run({"artboard": aid}, ctx)
    with pytest.raises(ToolError, match="showcase phase owns the screen"):
        DesignDeleteArtboardTool().run({"artboard": aid}, ctx)
    assert aid in ctx.design.state.artboards


def test_delete_artboard_tool_names_the_known_ones(ctx):
    with pytest.raises(ToolError, match="known"):
        DesignDeleteArtboardTool().run({"artboard": "nope"}, ctx)


# ------------------------------------------------------------------ look

VISION_SPEC = SimpleNamespace(spec="fake:vision")


class FakeVisionClient:
    """The wire client the real Critic talks to: records the call, replies —
    the same shape the critique tests fake it with."""

    def __init__(self, reply: str = "a dark hero with a broken grid",
                 fail: str | None = None):
        self.reply = reply
        self.fail = fail
        self.calls: list[tuple[object, list, object]] = []

    def complete(self, spec, messages, tools=None, **kwargs):
        self.calls.append((spec, messages, tools))
        if self.fail:
            raise RuntimeError(self.fail)
        return LLMResponse(message=Message(role="assistant", content=self.reply),
                           usage=Usage(1, 1), stop_reason="stop", model=spec.spec)

    def abort(self) -> None:
        pass


def _look_ctx(tmp_path, fail: str | None = None) -> ToolContext:
    """A session whose repo holds an uploaded screenshot where the upload
    route would have put it — the run dir's attachments/, referenced
    repo-relative, the exact string the page puts in the message text."""
    (tmp_path / "run" / "attachments").mkdir(parents=True)
    (tmp_path / "run" / "attachments" / "shot.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    session = DesignSession()
    session.start("a podcast landing page")
    session.run_dir = tmp_path / "run"
    session.critic = Critic(FakeVisionClient(fail=fail), VISION_SPEC,
                            run_dir=session.run_dir)
    c = ToolContext(repo_root=tmp_path)
    c.design = session
    return c


def test_look_runs_the_vision_model_on_the_attachment(tmp_path):
    ctx = _look_ctx(tmp_path)
    result = DesignLookTool().run({"image": "run/attachments/shot.png"}, ctx)
    assert result.details["ok"] and "broken grid" in result.output
    spec, messages, tools = ctx.design.critic.client.calls[0]
    assert tools is None  # a look is a read, not an agent turn
    parts = messages[0].content
    assert parts[0].type == "text"
    assert parts[1].type == "image_url"
    assert parts[1].image_url["url"].startswith("data:image/png;base64,")


def test_look_resolves_a_bare_attachments_reference_against_the_run_dir(tmp_path):
    ctx = _look_ctx(tmp_path)
    result = DesignLookTool().run({"image": "attachments/shot.png"}, ctx)
    assert result.details["ok"]


def test_look_without_a_vision_model_names_what_is_missing(tmp_path):
    ctx = _look_ctx(tmp_path)
    ctx.design.critic = None
    with pytest.raises(ToolError, match="vision"):
        DesignLookTool().run({"image": "run/attachments/shot.png"}, ctx)


def test_look_refuses_a_missing_file(tmp_path):
    ctx = _look_ctx(tmp_path)
    with pytest.raises(ToolError, match="no readable image"):
        DesignLookTool().run({"image": "run/attachments/nope.png"}, ctx)


def test_look_refuses_a_non_raster_file(tmp_path):
    ctx = _look_ctx(tmp_path)
    (tmp_path / "run" / "attachments" / "notes.txt").write_text("hello")
    with pytest.raises(ToolError, match="raster"):
        DesignLookTool().run({"image": "run/attachments/notes.txt"}, ctx)


def test_look_refuses_paths_outside_the_repo(tmp_path):
    """The bytes go to the vision provider, so this is a read gate in
    miniature: an absolute path outside the repo never resolves."""
    ctx = _look_ctx(tmp_path)
    with pytest.raises(ToolError, match="no readable image"):
        DesignLookTool().run({"image": "/etc/passwd"}, ctx)


def test_look_surfaces_a_failed_vision_call(tmp_path):
    ctx = _look_ctx(tmp_path, fail="provider down")
    with pytest.raises(ToolError, match="provider down"):
        DesignLookTool().run({"image": "run/attachments/shot.png"}, ctx)


# ------------------------------------------------------------ selectors, edits

IDS = ('<!doctype html><html><head><title>t</title></head>'
       '<body><header class="hero"><h1 id="title">Signal</h1><p>Weekly</p></header>'
       '<main><ul><li>Ep 1</li></ul></main></body></html>')


def test_edit_schema_documents_a_selector_that_resolves(ctx):
    """The old text said `body>0>1>2`, which int() refused on 'body'."""
    desc = DesignEditTool().parameters["properties"]["selector"]["description"]
    assert "0>1>3" in desc and "#id" in desc and "body>" not in desc
    assert "required" not in DesignEditTool().parameters  # `op` or `ops`
    assert "set_style{selector,props}" in DesignEditTool().description


def test_edit_result_shows_the_parents_fresh_outline(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    result = DesignEditTool().run(
        {"op": "set_text", "selector": "0>1>0>0", "text": "New", "artboard": aid}, ctx)
    assert result.output.startswith("Applied set_text to hero (v2).")
    assert "Under 0>1>0 now" in result.output
    assert "0>1>0  " in result.output and "header.hero" in result.output
    assert '0>1>0>0' in result.output and 'h1  "New"' in result.output


def test_edit_accepts_id_selectors_and_they_survive_an_insert(ctx):
    aid = DesignCreateTool().run({"html": IDS, "title": "Hero"}, ctx).details["artboard"]
    DesignEditTool().run({"op": "insert", "parent": "0>1", "position": 0,
                          "html": "<nav>n</nav>", "artboard": aid}, ctx)
    result = DesignEditTool().run(
        {"op": "set_text", "selector": "#title", "text": "Bird", "artboard": aid}, ctx)
    assert result.details["version"] == "v3"
    assert '<h1 id="title">Bird</h1>' in ctx.design.read_html(aid)


def test_edit_ops_batch_applies_in_order_one_version_each(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    result = DesignEditTool().run({"artboard": aid, "ops": [
        {"op": "set_text", "selector": "0>1>0>0", "text": "Bird"},
        {"op": "set_style", "selector": "0>1>0>1", "props": {"color": "red"}},
        {"op": "delete", "selector": "0>1>1"},
    ]}, ctx)
    assert result.output.startswith("Applied 3 of 3 ops to hero (v2 → v4):")
    assert [a["version"] for a in result.details["applied"]] == ["v2", "v3", "v4"]
    assert result.details["failed"] is None and result.details["remaining"] == 0
    board = ctx.design.state.artboards[aid]
    assert [v.id for v in board.versions] == ["v1", "v2", "v3", "v4"]
    html = ctx.design.read_html(aid)
    assert "<h1>Bird</h1>" in html and "color: red" in html and "<main>" not in html
    # the last op's subtree, fresh
    assert "Under 0>1 now" in result.output


def test_edit_ops_batch_stops_at_the_first_refusal_and_says_which(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    result = DesignEditTool().run({"artboard": aid, "ops": [
        {"op": "set_text", "selector": "0>1>0>0", "text": "Bird"},
        {"op": "set_style", "selector": "0>9", "props": {"color": "red"}},
        {"op": "set_text", "selector": "0>1>0>1", "text": "never applied"},
    ]}, ctx)
    assert not result.is_error
    assert "Applied 1 of 3 ops to hero (v2)" in result.output
    assert "Stopped at op 2 (set_style [0>9]): selector '0>9' no longer resolves" in result.output
    assert "The 1 op after it was not applied" in result.output
    assert result.details["failed"]["index"] == 1 and result.details["remaining"] == 1
    assert ctx.design.state.artboards[aid].current == "v2"
    assert "never applied" not in ctx.design.read_html(aid)


def test_edit_ops_batch_whose_first_op_fails_is_a_refusal(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    with pytest.raises(ToolError, match="nothing was applied"):
        DesignEditTool().run({"artboard": aid, "ops": [
            {"op": "set_text", "selector": "0>9", "text": "x"}]}, ctx)
    assert ctx.design.state.artboards[aid].current == "v1"


def test_edit_without_op_or_ops_names_the_shapes(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    with pytest.raises(ToolError, match=r"set_style\{selector,props\}"):
        DesignEditTool().run({}, ctx)


# --------------------------------------------------------------- elision

def test_create_result_asks_the_runner_to_elide_the_html(ctx):
    result = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    stub = result.details["elide_arguments"]["html"]
    assert stub.startswith(f"[html elided: {len(DOC)} chars, checkpointed as hero v1")
    assert "design_read" in stub


def test_polish_result_asks_the_runner_to_elide_css_and_script(ctx):
    aid = DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx).details["artboard"]
    DesignShowcaseTool().run({"artboard": aid}, ctx)
    result = DesignPolishTool().run({"css": "h1{color:red}", "script": "a()", "artboard": aid}, ctx)
    elide = result.details["elide_arguments"]
    assert elide["css"].startswith("[css elided: 13 chars, checkpointed as hero v2")
    assert elide["script"].startswith("[script elided: 3 chars")
    no_script = DesignPolishTool().run({"css": "h1{color:blue}", "artboard": aid}, ctx)
    assert "script" not in no_script.details["elide_arguments"]


# ---------------------------------------------------------------- themes

def test_design_themes_lists_blurbs_and_defers_to_the_intake(ctx):
    from bird.harnesses.design.tools import DesignThemesTool

    tool = DesignThemesTool()
    assert "already asked" in tool.description and "Agree one" not in tool.description
    output = tool.run({}, ctx).output
    line = next(ln for ln in output.splitlines() if ln.startswith("- editorial"))
    assert " — " in line and len(line) > len("- editorial — x")


def test_set_theme_hands_the_designer_the_digest(ctx):
    DesignCreateTool().run({"html": DOC, "title": "Hero"}, ctx)
    result = DesignSetThemeTool().run({"theme": "editorial"}, ctx)
    assert result.output.startswith("Theme 'editorial' applied to 1 artboard.")
    assert "# Theme: editorial" in result.output
    assert "--accent: #8b2f1f" in result.output
    assert "Don't" in result.output or "DON'T" in result.output
