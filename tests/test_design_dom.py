"""The server-side DOM ops: set_text, set_style, insert — the edit seam."""

from __future__ import annotations

import pytest

from bird.harnesses.design import dom


def _doc() -> dom.Node:
    return dom.parse("<body><h1>Hello</h1><p>Bye</p></body>")


def test_set_text_changes_an_elements_text():
    root = _doc()
    inv = dom.apply_op(root, {"op": "set_text", "selector": "0>0", "text": "Hi"})
    assert dom.resolve(root, "0>0").text_content() == "Hi"
    assert inv == {"op": "set_text", "selector": "0>0", "text": "Hello"}


def test_set_style_changes_a_style_property():
    root = _doc()
    dom.apply_op(root, {"op": "set_style", "selector": "0>1", "props": {"color": "red"}})
    assert dom.resolve(root, "0>1").attrs["style"] == "color: red"


def test_insert_adds_a_child():
    root = _doc()
    dom.apply_op(root, {"op": "insert", "html": "<p>New</p>"})
    body = dom.resolve(root, "0")
    assert [c.text_content() for c in body.child_elements()] == ["Hello", "Bye", "New"]


# ----------------------------------------------------------- inject_motion

def _wrapped_doc() -> str:
    """A stored (wrapped) document: the bridge is the last child of <body>."""
    return dom.serialize(dom.parse(
        '<html><head><title>P</title></head>'
        f'<body><h1>Hello</h1><script {dom.EDITOR_ATTR}>bridge</script></body></html>'))


def _head_body(html: str) -> tuple[dom.Node, dom.Node]:
    html_el = dom.parse(html).child_elements()[0]
    head, body = html_el.child_elements()[0], html_el.child_elements()[1]
    return head, body


def test_inject_motion_marks_style_and_script():
    out = dom.inject_motion(_wrapped_doc(), "h1{color:red}", "a()")
    head, body = _head_body(out)
    style = next(c for c in head.child_elements()
                 if c.tag == "style" and dom.MOTION_ATTR in c.attrs)
    assert "h1{color:red}" in style.text_content()
    kids = body.child_elements()
    # the bridge stays last: the motion script lands before it, after every
    # element the selector paths point at
    assert dom.EDITOR_ATTR in kids[-1].attrs
    motion = kids[-2]
    assert motion.tag == "script" and dom.MOTION_ATTR in motion.attrs
    assert "a()" in motion.text_content()


def test_inject_motion_is_idempotent():
    once = dom.inject_motion(_wrapped_doc(), "h1{color:red}", "a()")
    twice = dom.inject_motion(once, "h1{color:blue}", "b()")
    head, body = _head_body(twice)
    styles = [c for c in head.child_elements()
              if c.tag == "style" and dom.MOTION_ATTR in c.attrs]
    scripts = [c for c in body.child_elements() if dom.MOTION_ATTR in c.attrs]
    assert len(styles) == 1 and "color:blue" in styles[0].text_content()
    assert len(scripts) == 1 and "b()" in scripts[0].text_content()


def test_inject_motion_removes_a_node_its_part_leaves_empty():
    """A re-polish that passes only css takes the previous script back out —
    the layer is whatever the last polish said, never a leftover."""
    both = dom.inject_motion(_wrapped_doc(), "h1{color:red}", "a()")
    css_only = dom.inject_motion(both, "h1{color:blue}", "")
    _, body = _head_body(css_only)
    assert not [c for c in body.child_elements() if dom.MOTION_ATTR in c.attrs]


def test_unwrap_editor_leaves_the_motion_nodes():
    """Motion is design content, like the theme's style: the handoff boundary
    strips the bridge and nothing else."""
    polished = dom.inject_motion(_wrapped_doc(), "h1{color:red}", "a()")
    clean = dom.unwrap_editor(polished)
    assert dom.EDITOR_ATTR not in clean
    assert dom.MOTION_ATTR in clean and "a()" in clean


def test_motion_layer_reads_back_the_choreography():
    polished = dom.inject_motion(_wrapped_doc(), "h1{color:red}", "a()")
    layer = dom.motion_layer(dom.unwrap_editor(polished))
    assert "h1{color:red}" in layer and "a()" in layer
    assert dom.motion_layer(_wrapped_doc()) == ""

# ------------------------------------------------------------ selectors

FULL = ('<!doctype html><html><head><title>t</title></head>'
        '<body><h1 id="hero">Hi</h1><p id="dup">a</p><p id="dup">b</p></body></html>')


def test_resolve_accepts_a_unique_id():
    root = dom.parse(FULL)
    assert dom.resolve(root, "#hero").tag == "h1"
    assert dom.resolve(root, " #hero ").tag == "h1"


def test_resolve_refuses_a_duplicated_or_missing_id_by_name():
    root = dom.parse(FULL)
    with pytest.raises(dom.DomError, match="2 elements carry id 'dup'"):
        dom.resolve(root, "#dup")
    with pytest.raises(dom.DomError, match="no element has id 'nope'"):
        dom.resolve(root, "#nope")
    with pytest.raises(dom.DomError, match="no id after the hash"):
        dom.resolve(root, "#")


def test_resolve_tolerates_the_old_body_prefix():
    """The old schema text taught `body>0>1>2`; that spelling now means the
    body's own path, so a model that learned it is not refused."""
    root = dom.parse(FULL)
    assert dom.resolve(root, "body").tag == "body"
    assert dom.resolve(root, "body>0").tag == "h1"
    assert dom.resolve(root, "0>1>0") is dom.resolve(root, "body>0")


def test_id_selectors_survive_an_insert_ahead_of_the_element():
    """The whole point: an index path shifts when something lands before the
    element; the id does not."""
    root = dom.parse(FULL)
    dom.apply_op(root, {"op": "insert", "parent": "0>1", "position": 0, "html": "<nav>n</nav>"})
    assert dom.resolve(root, "0>1>0").tag == "nav"  # the path moved
    assert dom.resolve(root, "#hero").tag == "h1"  # the id did not


def test_insert_with_no_parent_lands_in_the_body_of_a_full_document():
    root = dom.parse(FULL)
    inverse = dom.apply_op(root, {"op": "insert", "html": "<footer>f</footer>"})
    body = dom.resolve(root, "0>1")
    assert body.child_elements()[-1].tag == "footer"
    assert inverse == {"op": "delete", "selector": "0>1>3"}
