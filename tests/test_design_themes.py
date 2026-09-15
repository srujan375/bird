"""The theme system: loader precedence, injection, and the session plumbing.

The builtins are data shipped with the package; these tests read the real
files, so a theme that loses its tokens.css fails here, not on a user's
screen.
"""

from __future__ import annotations

import pytest

from bird.harnesses.design import dom, themes
from bird.harnesses.design.session import DesignSession

HERO = "<html><head><title>t</title></head><body><h1>Hello</h1></body></html>"
BUILTINS = {"editorial", "swiss-minimal", "brutalist", "warm-playful",
            "dark-atmospheric", "wireframe"}


def test_list_themes_contains_the_six_builtins():
    # membership, not equality: more builtins are vendored in a follow-up
    # step, so the total count is deliberately not pinned here.
    assert BUILTINS <= set(themes.list_themes())


def test_load_theme_returns_md_and_css_with_tokens():
    theme = themes.load_theme("editorial")
    assert theme["name"] == "editorial"
    assert "Mood" in theme["md"] and "Don'ts" in theme["md"]
    assert "--accent" in theme["css"] and ":root" in theme["css"]


def test_load_theme_miss_names_the_theme_and_the_known_ones():
    with pytest.raises(FileNotFoundError) as e:
        themes.load_theme("nope")
    assert "nope" in str(e.value) and "editorial" in str(e.value)


def test_project_themes_override_builtins_by_name(tmp_path, monkeypatch):
    """skills.py's precedence, mirrored: project over user over builtin."""
    override = tmp_path / "editorial"
    override.mkdir()
    (override / "THEME.md").write_text("# mine", encoding="utf-8")
    (override / "tokens.css").write_text(":root { --accent: #123456; }", encoding="utf-8")
    monkeypatch.setattr(themes, "USER_THEMES_DIRS",
                        [tmp_path, tmp_path / "no-user-dir"])
    monkeypatch.setattr(themes, "_CACHE", {})
    assert themes.load_theme("editorial")["md"] == "# mine"
    assert themes.load_theme("editorial")["css"] == ":root { --accent: #123456; }"


def test_bare_md_file_is_a_theme_with_no_css(tmp_path, monkeypatch):
    """A single <name>.md file is a whole theme: the stem is the name, the
    content is the brief, and there is no tokens.css to inject."""
    (tmp_path / "mono.md").write_text("# mono brief", encoding="utf-8")
    monkeypatch.setattr(themes, "USER_THEMES_DIRS",
                        [tmp_path, tmp_path / "no-user-dir"])
    monkeypatch.setattr(themes, "_CACHE", {})
    assert "mono" in themes.list_themes()
    theme = themes.load_theme("mono")
    assert (theme["name"], theme["md"], theme["css"]) == ("mono", "# mono brief", "")
    assert theme["motion"] == ""  # a bare file has no dir to hold a motion.md
    assert theme["digest"].startswith("# Theme: mono")


def test_design_md_dir_loads_md_and_css_without_a_theme_md(tmp_path, monkeypatch):
    """DESIGN.md alone makes a directory a theme — THEME.md is optional."""
    d = tmp_path / "sunset"
    d.mkdir()
    (d / "DESIGN.md").write_text("# sunset brief", encoding="utf-8")
    (d / "tokens.css").write_text(":root { --accent: #abcdef; }", encoding="utf-8")
    monkeypatch.setattr(themes, "USER_THEMES_DIRS",
                        [tmp_path, tmp_path / "no-user-dir"])
    monkeypatch.setattr(themes, "_CACHE", {})
    assert "sunset" in themes.list_themes()
    theme = themes.load_theme("sunset")
    assert theme["md"] == "# sunset brief"
    assert theme["css"] == ":root { --accent: #abcdef; }"


def test_theme_md_wins_over_design_md_when_a_dir_has_both(tmp_path, monkeypatch):
    d = tmp_path / "duo"
    d.mkdir()
    (d / "THEME.md").write_text("# the winner", encoding="utf-8")
    (d / "DESIGN.md").write_text("# the loser", encoding="utf-8")
    monkeypatch.setattr(themes, "USER_THEMES_DIRS",
                        [tmp_path, tmp_path / "no-user-dir"])
    monkeypatch.setattr(themes, "_CACHE", {})
    assert themes.load_theme("duo")["md"] == "# the winner"


def test_bare_project_file_shadows_a_builtin_of_the_same_stem(tmp_path, monkeypatch):
    """Precedence is by name, not by form: a bare project file beats a
    packaged directory theme of the same stem."""
    (tmp_path / "editorial.md").write_text("# bare editorial", encoding="utf-8")
    monkeypatch.setattr(themes, "USER_THEMES_DIRS",
                        [tmp_path, tmp_path / "no-user-dir"])
    monkeypatch.setattr(themes, "_CACHE", {})
    theme = themes.load_theme("editorial")
    assert theme["md"] == "# bare editorial" and theme["css"] == ""


def test_attribution_md_is_not_a_theme(tmp_path, monkeypatch):
    """ATTRIBUTION.md is provenance for the vendored builtins, not a theme:
    discovery skips it (any casing) and load_theme raises the normal miss."""
    (tmp_path / "ATTRIBUTION.md").write_text("# credits", encoding="utf-8")
    monkeypatch.setattr(themes, "USER_THEMES_DIRS",
                        [tmp_path, tmp_path / "no-user-dir"])
    monkeypatch.setattr(themes, "_CACHE", {})
    assert "ATTRIBUTION" not in themes.list_themes()
    assert "attribution" not in themes.list_themes()
    with pytest.raises(FileNotFoundError, match="ATTRIBUTION"):
        themes.load_theme("ATTRIBUTION")


def test_inject_theme_lands_as_the_first_child_of_head():
    out = dom.inject_theme(HERO, ":root { --accent: red; }")
    root = dom.parse(out)
    html_el = root.child_elements()[0]
    assert html_el.tag == "html"
    head = html_el.child_elements()[0]
    assert head.tag == "head"
    style = head.child_elements()[0]
    assert style.tag == "style" and dom.THEME_ATTR in style.attrs
    assert "--accent: red" in style.text_content()


def test_inject_theme_is_idempotent_and_replaces_content():
    once = dom.inject_theme(HERO, ":root { --accent: red; }")
    twice = dom.inject_theme(once, ":root { --accent: blue; }")
    assert twice.count(dom.THEME_ATTR) == 1
    assert "--accent: blue" in twice and "--accent: red" not in twice


def test_inject_theme_creates_a_head_when_the_document_has_none():
    out = dom.inject_theme("<html><body><h1>x</h1></body></html>", "p {}")
    html_el = dom.parse(out).child_elements()[0]
    assert html_el.tag == "html"
    head = html_el.child_elements()[0]
    assert head.tag == "head" and head.child_elements()[0].tag == "style"


def test_unwrap_editor_leaves_the_theme_style_alone():
    """The theme is design content, not editor chrome — the handoff keeps it."""
    themed = dom.inject_theme(HERO, ":root { --accent: red; }")
    wrapped = dom.wrap_editor(themed, "window.__bird = true;")
    out = dom.unwrap_editor(wrapped)
    assert dom.EDITOR_ATTR not in out
    assert dom.THEME_ATTR in out and "--accent: red" in out


def _session() -> DesignSession:
    s = DesignSession()
    s.start("a landing page")
    s.add_artboard(HERO, "Hero")
    return s


def test_set_theme_records_the_theme_without_bumping_a_version():
    """set_theme validates + records; the version bump is design_set_theme's
    job, which re-injects the css into every existing artboard."""
    s = _session()
    s.set_theme("editorial")
    assert s.theme == "editorial"
    assert [v.id for v in s.state.artboards["hero"].versions] == ["v1"]


def test_design_set_theme_bumps_a_themed_version_on_every_artboard():
    from pathlib import Path

    from bird.tools import ToolContext
    from bird.harnesses.design.tools import DesignSetThemeTool

    s = _session()
    s.add_artboard("<html><body><p>two</p></body></html>", "Two")
    out = DesignSetThemeTool().run({"theme": "editorial"},
                                   ToolContext(repo_root=Path("."), design=s))
    assert s.theme == "editorial"
    for aid in ("hero", "two"):
        board = s.state.artboards[aid]
        assert (board.versions[-1].kind, board.versions[-1].note) == (
            "themed", "theme: editorial")
        assert dom.THEME_ATTR in s.read_html(aid) and "--accent" in s.read_html(aid)
    assert "2 artboards" in out.output


def test_set_theme_unknown_name_raises_the_loaders_error():
    s = _session()
    with pytest.raises(FileNotFoundError, match="nope"):
        s.set_theme("nope")
    assert s.theme is None
    assert len(s.state.artboards["hero"].versions) == 1


def test_artboards_created_after_set_theme_inherit_the_theme():
    s = _session()
    s.set_theme("wireframe")
    s.add_artboard("<html><body><p>second</p></body></html>", "Second")
    stored = s.read_html("second")
    assert dom.THEME_ATTR in stored
    assert [(v.id, v.kind) for v in s.state.artboards["second"].versions] == [
        ("v1", "generated")]

# ------------------------------------------------------- the two vocabularies

# what the instructions and the skill cite, plus what the vendored themes
# define instead — after loading, every one of these resolves in every theme
BOTH_VOCABULARIES = ("bg", "surface", "text", "fg", "accent", "border",
                     "font-display", "font-body", "space-section",
                     "section-y-desktop", "radius", "radius-md", "max-width",
                     "container-max", "muted")


def test_every_installed_theme_resolves_both_token_vocabularies():
    for name in themes.list_themes():
        theme = themes.load_theme(name)
        if not theme["css"]:
            continue  # a bare .md theme has no tokens at all
        have = themes.defined_tokens(theme["css"])
        missing = [t for t in BOTH_VOCABULARIES if t not in have]
        assert not missing, f"{name} leaves {missing} undefined"


def test_alias_block_defines_only_the_missing_half_and_is_idempotent():
    css = ":root { --text: #111; --radius-md: 8px; }"
    block = themes.alias_block(css)
    assert "--fg: var(--text);" in block
    assert "--radius: var(--radius-md);" in block
    assert "--text:" not in block.replace("--text)", "")  # the present half is left alone
    once = themes.with_aliases(css)
    assert themes.with_aliases(once) == once
    assert themes.alias_block(":root { --accent: #123; }") == ""


def test_loaded_css_carries_the_alias_block_and_injection_keeps_it():
    theme = themes.load_theme("apple")
    assert "--space-section: var(--section-y-desktop);" in theme["css"]
    html = dom.inject_theme(HERO, theme["css"])
    assert "--space-section: var(--section-y-desktop);" in html


# ------------------------------------------------------------------ digest

def test_digest_of_a_vendored_theme_carries_tokens_donts_and_the_type_scale():
    d = themes.load_theme("apple")["digest"]
    assert d.startswith("# Theme: apple")
    assert "--accent: #0071e3" in d
    assert "## Don't" in d and "decorative gradients" in d
    assert "## Type scale" in d and "Hero Display" in d
    assert len(d) <= themes._DIGEST_CHARS
    assert not d.rstrip().endswith("…")  # whole sections, never a mid-line cut


def test_digest_of_a_hand_written_theme_reads_its_own_headings():
    d = themes.load_theme("editorial")["digest"]
    assert "--accent: #8b2f1f" in d
    assert "Do's and Don'ts" in d and "Inter" in d
    assert "## Type" in d


def test_motion_recipe_loads_from_the_theme_dir_and_joins_the_digest(tmp_path, monkeypatch):
    d = tmp_path / "moody"
    d.mkdir()
    (d / "THEME.md").write_text("# Moody\n\n## Do's and Don'ts\n- DO glide\n", encoding="utf-8")
    (d / "tokens.css").write_text(":root { --accent: #123456; --text: #111; }", encoding="utf-8")
    (d / "motion.md").write_text("Page-load: 400ms fade, 4 elements staggered 60ms.", encoding="utf-8")
    monkeypatch.setattr(themes, "USER_THEMES_DIRS", [tmp_path, tmp_path / "no-user-dir"])
    monkeypatch.setattr(themes, "_CACHE", {})
    theme = themes.load_theme("moody")
    assert theme["motion"].startswith("Page-load: 400ms")
    assert "## Motion" in theme["digest"] and "staggered 60ms" in theme["digest"]
    assert "--fg: var(--text);" in theme["css"]


def test_set_theme_persists_the_theme_in_state_and_looks_up_project_themes_by_repo_root(tmp_path, monkeypatch):
    """The session passes its repo_root to the loader — a project theme under
    <repo>/.bird/design-themes used to be found by design_themes (which had
    ctx.repo_root) and missed by set_theme (which used cwd)."""
    repo = tmp_path / "repo"
    proj = repo / ".bird" / "design-themes" / "house"
    proj.mkdir(parents=True)
    (proj / "THEME.md").write_text("# House", encoding="utf-8")
    (proj / "tokens.css").write_text(":root { --accent: #abcdef; }", encoding="utf-8")
    monkeypatch.setattr(themes, "USER_THEMES_DIRS",
                        [themes.USER_THEMES_DIRS[0], tmp_path / "no-user-dir"])
    monkeypatch.setattr(themes, "_CACHE", {})
    monkeypatch.setattr(themes, "_LIST_CACHE", {})
    s = DesignSession(repo_root=repo)
    s.start("x")
    theme = s.set_theme("house")
    assert theme["name"] == "house" and s.state.theme == "house"
    s.add_artboard(HERO, "Hero")
    assert "#abcdef" in s.read_html("hero")
    assert "house" in s.state_event()["themes"]
