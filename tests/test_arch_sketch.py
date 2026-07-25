"""The loose sketch layer: construction, the depth slider, variants, and a
loss-free round trip through ArchState serialization (the sketchbook rides the
same wire as the strict state)."""

import json

from mha.harnesses.arch.sketch import (
    DEPTHS,
    Sketchbook,
    SketchLink,
    SketchNode,
    Variant,
)
from mha.harnesses.arch.state import ArchState


def _book() -> Sketchbook:
    book = Sketchbook()
    sync = Variant(id="v1", name="synchronous", summary="direct calls, no broker")
    sync.nodes["api"] = SketchNode(id="api", label="API", kind="api", depth="sketch",
                                   note="front door")
    sync.nodes["db"] = SketchNode(id="db", label="DB", kind="store")
    sync.links.append(SketchLink(src="api", dst="db", label="write", kind="sync"))
    evt = Variant(id="v2", name="event-driven", summary="queue between api and worker",
                  status="archived", rejected_reason="overkill at this scale")
    book.variants["v1"] = sync
    book.variants["v2"] = evt
    book.active = "v1"
    book.notes.append("~10k MAU, internal tool")
    return book


def test_active_and_chosen():
    book = _book()
    assert book.active_variant().name == "synchronous"
    assert book.chosen_variant() is None
    book.variants["v1"].status = "chosen"
    assert book.chosen_variant().id == "v1"


def test_depth_is_a_two_way_slider():
    # the point of the layer: you can deepen AND collapse
    assert DEPTHS == ("stub", "sketch", "detailed")
    n = SketchNode(id="x", label="X")
    assert n.depth == "stub"
    n.depth, n.detail = "detailed", "handles retries + dedup"
    n.depth, n.detail = "stub", ""  # collapse back — first-class, not an exception
    assert n.depth == "stub" and n.detail == ""


def test_splice_helpers():
    v = _book().variants["v1"]
    assert v.link_index("api", "db") == 0
    assert v.link_index("api", "nope") == -1
    assert v.references_to("db") == ["api -> db"]


def test_sketchbook_round_trip():
    book = _book()
    back = Sketchbook.from_dict(json.loads(json.dumps(book.to_dict())))
    assert back.to_dict() == book.to_dict()
    assert back.variants["v2"].rejected_reason == "overkill at this scale"
    assert back.variants["v1"].nodes["api"].depth == "sketch"


def test_round_trip_through_archstate_is_loss_free():
    st = ArchState()
    st.sketchbook = _book()
    wire = json.dumps(st.to_dict())
    back = ArchState.from_dict(json.loads(wire))
    assert back.to_dict() == st.to_dict()
    assert back.sketchbook.active == "v1"
    assert back.sketchbook.variants["v1"].links[0].label == "write"


def test_empty_sketchbook_is_the_default():
    st = ArchState()
    assert st.sketchbook.is_empty()
    assert st.to_dict()["sketchbook"] == {"variants": {}, "active": None, "notes": []}
