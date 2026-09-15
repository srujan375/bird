"""More server-side DOM ops: delete, duplicate, move."""

from __future__ import annotations

from bird.harnesses.design import dom


def _doc() -> dom.Node:
    return dom.parse("<body><h1>Hello</h1><p>Bye</p></body>")


def test_delete_removes_an_element():
    root = _doc()
    inv = dom.apply_op(root, {"op": "delete", "selector": "0>0"})
    body = dom.resolve(root, "0")
    assert [c.text_content() for c in body.child_elements()] == ["Bye"]
    assert inv == {"op": "insert", "parent": "0", "html": "<h1>Hello</h1>",
                   "position": 0}


def test_duplicate_copies_an_element_with_a_new_id():
    root = _doc()
    inv = dom.apply_op(root, {"op": "duplicate", "selector": "0>1"})
    body = dom.resolve(root, "0")
    ps = [c for c in body.child_elements() if c.tag == "p"]
    assert len(ps) == 2
    assert ps[1] is not ps[0]  # a distinct node, not a second reference
    assert ps[1].text_content() == "Bye"
    # the copy is addressable at its own selector, after the original
    assert dom.resolve(root, "0>2") is ps[1]
    assert inv["op"] == "delete"  # undo is replay: duplicate inverts to a delete


def test_move_relocates_an_element_under_a_new_parent():
    root = dom.parse("<body><div><p>Inner</p></div><h1>Top</h1></body>")
    inv = dom.apply_op(root, {"op": "move", "selector": "0>1", "new_parent": "0>0"})
    div = dom.resolve(root, "0>0")
    assert [c.text_content() for c in div.child_elements()] == ["Inner", "Top"]
    body = dom.resolve(root, "0")
    assert [c.tag for c in body.child_elements()] == ["div"]
    assert inv["_from"] == {"parent": "0", "index": 1}