"""The board as one drawing.

The spatial claim is the whole point of the rebuild's canvas: approaches sit
side by side, the parts they share are drawn once in the middle, and a losing
approach stays visible in grey with the reason it lost.
"""

from __future__ import annotations

from bird.harnesses.arch import render
from bird.harnesses.arch.state import Approach, ArchState, Edge, Node


def _board() -> ArchState:
    s = ArchState()
    s.approaches["lam"] = Approach(id="lam", name="Lambda")
    s.approaches["svc"] = Approach(id="svc", name="Always-on")
    s.nodes["orders"] = Node(id="orders", label="Orders", kind="store")
    s.nodes["fn"] = Node(id="fn", label="Handler", approaches=["lam"], tech="AWS Lambda")
    s.nodes["box"] = Node(id="box", label="Handler", approaches=["svc"])
    s.edges += [Edge("fn", "orders", "writes"), Edge("box", "orders", "writes")]
    return s


def test_each_approach_gets_its_own_box_on_the_board():
    out = render.board_mermaid(_board())
    assert 'subgraph lam["Lambda"]' in out
    assert "Always-on" in out


def test_a_shared_node_is_drawn_once_outside_every_approach():
    """Two boxes for one database is the thing the single-canvas model exists
    to avoid."""
    out = render.board_mermaid(_board())
    assert out.count("Orders") == 1
    body = out.split("end")[-1]
    assert "orders[(" in body, "the shared store sits at the top level"


def test_a_losing_approach_keeps_its_reason_on_the_diagram():
    s = _board()
    s.approaches["svc"].status = "greyed"
    s.approaches["svc"].rejected_reason = "cost, not cold starts"
    out = render.board_mermaid(s)
    assert "not taken: cost, not cold starts" in out
    assert "class box greyed" in out


def test_a_shared_node_never_greys_out_with_an_approach():
    s = _board()
    s.approaches["svc"].status = "greyed"
    s.approaches["svc"].rejected_reason = "cost"
    out = render.board_mermaid(s)
    grey_line = next(ln for ln in out.splitlines() if ln.strip().startswith("class "))
    assert "orders" not in grey_line


def test_kinds_get_their_own_shapes():
    s = ArchState()
    s.nodes["a"] = Node(id="a", label="Store", kind="store")
    s.nodes["b"] = Node(id="b", label="Queue", kind="queue")
    s.nodes["c"] = Node(id="c", label="Stripe", kind="external")
    out = render.board_mermaid(s)
    assert "a[(" in out and "b[[" in out and "c((" in out


def test_edge_kinds_get_their_own_arrows():
    s = ArchState()
    s.nodes["a"] = Node(id="a", label="A")
    s.nodes["b"] = Node(id="b", label="B")
    s.edges += [Edge("a", "b", "sync call"), Edge("b", "a", "event", kind="async")]
    out = render.board_mermaid(s)
    assert "a -->" in out and "b -.->" in out


def test_tech_replaces_the_kind_caption_once_it_is_chosen():
    out = render.board_mermaid(_board())
    assert "AWS Lambda" in out
    assert "fn[\"Handler<br/><i>service</i>\"]" not in out


def test_imported_background_is_styled_apart():
    s = ArchState()
    s.nodes["legacy"] = Node(id="legacy", label="Legacy", existing=True)
    out = render.board_mermaid(s)
    assert "class legacy existing" in out


def test_quotes_in_a_label_never_break_the_diagram():
    s = ArchState()
    s.nodes["a"] = Node(id="a", label='the "fast" path')
    assert "the 'fast' path" in render.board_mermaid(s)


def test_an_empty_board_still_renders():
    assert render.board_mermaid(ArchState()) == "flowchart LR"


def test_render_all_carries_one_board():
    assert set(render.render_all(_board())) == {"board"}
