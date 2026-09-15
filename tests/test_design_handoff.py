"""The handoff bundle is an OUT boundary: the editor bridge must not leak
into what the code harness consumes."""

from __future__ import annotations

from bird.harnesses.design import dom
from bird.harnesses.design.handoff import make_handoff_bundle
from bird.harnesses.design.session import DesignSession

HERO = "<html><body><h1>Hello</h1></body></html>"


def _session() -> DesignSession:
    s = DesignSession()
    s.start("a landing page")
    s.add_artboard(HERO, "Hero")
    return s


def test_bundle_html_is_unwrapped_while_storage_stays_wrapped():
    s = _session()
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})
    s.finalize("hero")
    assert dom.EDITOR_ATTR in s.read_html("hero")  # storage keeps the bridge
    bundle = make_handoff_bundle(s, s.state)
    assert dom.EDITOR_ATTR not in bundle["html"]
    assert "<h1>Hi</h1>" in bundle["html"]


def test_bundle_markdown_fence_carries_the_clean_html():
    s = _session()
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})
    s.finalize("hero")
    md = make_handoff_bundle(s, s.state)["markdown"]
    assert dom.EDITOR_ATTR not in md
    assert "<h1>Hi</h1>" in md


def test_bundle_of_a_never_edited_artboard_round_trips():
    s = _session()
    s.finalize("hero")
    bundle = make_handoff_bundle(s, s.state)
    assert bundle["html"] == HERO  # unwrap of unwrapped html is a no-op


def test_bundle_summary_carries_the_theme_when_one_is_set():
    """The chosen design system survives the session: in the summary dict and
    as a markdown bullet — and neither appears when no theme was ever set."""
    s = _session()
    s.theme = "editorial"
    s.finalize("hero")
    bundle = make_handoff_bundle(s, s.state)
    assert bundle["summary"]["theme"] == "editorial"
    assert "- **design system:** `editorial`" in bundle["markdown"]

    bare = DesignSession()
    bare.start("a landing page")
    bare.add_artboard(HERO, "Hero")
    bare.finalize("hero")
    no_theme = make_handoff_bundle(bare, bare.state)
    assert "theme" not in no_theme["summary"]
    assert "design system" not in no_theme["markdown"]


def test_bundle_markdown_names_the_polish_pass():
    """The builder inherits transitions, not static pixels: the markdown names
    the showcase pass, and the html block is the animated current version —
    motion nodes in, editor bridge out."""
    s = _session()
    s.showcase("hero")
    polished = dom.inject_motion(s.read_html("hero"), "h1{color:red}", "a()")
    s.write_version("hero", polished, "polished", note="polish: motion pass")
    s.finalize("hero")
    bundle = make_handoff_bundle(s, s.state)
    assert "- **polish:**" in bundle["markdown"]
    assert dom.MOTION_ATTR in bundle["html"] and "a()" in bundle["html"]
    assert dom.EDITOR_ATTR not in bundle["html"]

    bare = _session()
    bare.finalize("hero")
    assert "**polish:**" not in make_handoff_bundle(bare, bare.state)["markdown"]

# ------------------------------------------- what the builder is now told

def test_bundle_carries_intake_the_plan_critique_and_the_other_directions():
    s = DesignSession(intake_gate=True)
    s.start("a landing page")
    s.answer_ask({"id": "fidelity", "value": "high-fidelity"})
    s.answer_ask({"id": "design-system", "value": "editorial"})
    s.add_artboard(HERO, "Hero")
    s.add_artboard("<html><body><h1>Loud</h1></body></html>", "Loud")
    s.state.plan = "one hero"
    s.state.plan_critique = "name the signature element"
    s.state.record_critique("loud", "v1", "too loud: the h1 shouts", model="fake")
    s.finalize("hero")
    bundle = make_handoff_bundle(s, s.state)
    assert bundle["intake"]["direction"] == "High-fidelity"
    assert bundle["intake"]["design_system"] == "editorial"
    assert bundle["plan_critique"] == "name the signature element"
    assert bundle["others"] == [{"artboard": "loud", "title": "Loud", "current": "v1",
                                 "critique": "too loud: the h1 shouts"}]
    md = bundle["markdown"]
    assert "## Intake" in md and "- **design system:** editorial" in md
    assert "### The critic on the plan" in md
    assert "## The other directions" in md and "**Loud**" in md and "the h1 shouts" in md


def test_bundle_without_intake_or_others_leaves_those_sections_out():
    s = _session()
    s.finalize("hero")
    bundle = make_handoff_bundle(s, s.state)
    assert bundle["intake"] == {} and bundle["others"] == []
    assert "## Intake" not in bundle["markdown"]
    assert "other directions" not in bundle["markdown"]


def test_write_bundle_files_the_themes_tokens_beside_the_doc(tmp_path):
    from bird.harnesses.design.handoff import write_bundle

    s = DesignSession(run_dir=tmp_path)
    s.start("a landing page")
    s.set_theme("editorial")
    s.add_artboard(HERO, "Hero")
    s.finalize("hero")  # finalize writes the bundle itself
    tokens = tmp_path / "bundle" / "tokens.css"
    assert tokens.is_file() and "--accent: #8b2f1f" in tokens.read_text(encoding="utf-8")
    assert "`tokens.css`" in (tmp_path / "bundle" / "DESIGN.md").read_text(encoding="utf-8")
    assert tokens in write_bundle(s, tmp_path)

    bare = DesignSession(run_dir=tmp_path / "bare")
    bare.start("x")
    bare.add_artboard(HERO, "Hero")
    bare.finalize("hero")
    assert not (tmp_path / "bare" / "bundle" / "tokens.css").exists()


def test_history_groups_version_notes_by_actor_instead_of_numeric_paths():
    from bird.harnesses.design.mutate import apply

    s = _session()
    s.apply_edit("hero", {"op": "set_text", "selector": "0>0>0", "text": "Hi"})  # the user
    apply(s, "hero", {"op": "set_style", "selector": "0>0>0", "props": {"color": "red"}}, actor="ai")
    s.finalize("hero")
    md = make_handoff_bundle(s, s.state)["markdown"]
    assert "## History" in md
    assert "### The designer" in md and "- `v1` generated" in md and "- `v3` set_style" in md
    assert "### The user, by hand" in md and "- `v2` set_text" in md
    assert "## Edit history" not in md and "set_text 0>0>0" not in md
