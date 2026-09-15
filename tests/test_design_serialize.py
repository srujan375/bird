"""Serializing is a round trip: what a document carries in comes back out.

Every op rewrites the whole document, so anything the parser drops or the
serializer mangles is lost the first time anyone touches the artboard — not at
some later boundary. These pin the losses that used to happen silently.
"""

from __future__ import annotations

import pytest

from bird.harnesses.design import dom

DOC = ('<!DOCTYPE html><html><head><title>t</title><style>a::before{content:"<"}</style>'
       '</head><body><!-- keep --><h1>a &amp; b</h1><p>x &lt;y&gt;</p></body></html>')


def test_a_document_round_trips_byte_for_byte():
    assert dom.serialize(dom.parse(DOC)) == DOC


def test_the_doctype_survives_an_edit():
    """Dropping it renders every edited artboard in quirks mode."""
    root = dom.parse(DOC)
    dom.apply_op(root, {"op": "set_text", "selector": "0>1>1", "text": "hi"})
    assert dom.serialize(root).startswith("<!DOCTYPE html>")


def test_escaped_text_stays_escaped():
    """`&lt;y&gt;` coming back out as a live <y> rewrites the user's document."""
    assert "<p>x &lt;y&gt;</p>" in dom.serialize(dom.parse(DOC))


def test_script_and_style_bodies_are_never_escaped():
    src = '<html><body><script>if (a < b && c) x()</script></body></html>'
    assert dom.serialize(dom.parse(src)) == src


def test_set_text_writes_text_not_markup():
    root = dom.parse("<html><body><h1>Hi</h1></body></html>")
    dom.apply_op(root, {"op": "set_text", "selector": "0>0>0",
                        "text": "<script>x</script> & co"})
    out = dom.serialize(root)
    assert "<script>" not in out
    assert "&lt;script&gt;x&lt;/script&gt; &amp; co" in out
    assert not dom.resolve(dom.parse(out), "0>0>0").child_elements()  # still a leaf


def test_nodes_compare_by_identity():
    """Two <p>Hi</p> in one document are different nodes. Value equality walks
    the parent chain, so children.index() on similar subtrees recursed."""
    root = dom.parse("<html><body><p>Hi</p><p>Hi</p></body></html>")
    first, second = dom.resolve(root, "0>0>0"), dom.resolve(root, "0>0>1")
    assert first != second
    assert first == first


def test_duplicating_a_subtree_reports_the_clone_not_the_original():
    root = dom.parse("<html><body><div><p>x</p></div></body></html>")
    inverse = dom.apply_op(root, {"op": "duplicate", "selector": "0>0>0"})
    assert inverse == {"op": "delete", "selector": "0>0>1"}
    assert dom.resolve(root, "0>0>1") is not dom.resolve(root, "0>0>0")


@pytest.mark.parametrize("new_parent", ["0>0>0", "0>0>0>0"])
def test_moving_an_element_inside_itself_is_refused(new_parent):
    """It used to make the node its own parent, and _selector_path spun
    forever — a hang no `except Exception` upstream could catch."""
    root = dom.parse("<html><body><div><p>x</p></div></body></html>")
    with pytest.raises(dom.DomError, match="into itself"):
        dom.apply_op(root, {"op": "move", "selector": "0>0>0", "new_parent": new_parent})
