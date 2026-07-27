"""The sketch layer: loose tools, the depth slider, splice, and promotion.

Since the overhaul the sketch layer is not a phase you get evicted from — it
stays open for the whole session, and promoting is a non-destructive, repeatable
move rather than a one-way door.
"""

from mha.harnesses.arch.session import ArchSession
from mha.harnesses.arch.state import ArchState
from mha.harnesses.arch.tools import (
    ArchDoneTool,
    BriefTool,
    ComponentTool,
    DecideTool,
    DepthTool,
    FlowTool,
    LinkTool,
    NodeTool,
    PromoteTool,
    SpliceTool,
    VariantTool,
)
from mha.tools import ToolContext


class FakeBroker:
    def __init__(self, answers):
        self.answers = list(answers)
        self.payloads = []

    def request(self, payload):
        self.payloads.append(payload)
        return self.answers.pop(0)


def ok(tool, ctx, **args):
    res = tool.execute(args, ctx)
    assert not res.is_error, res.output
    return res


def err(tool, ctx, **args):
    res = tool.execute(args, ctx)
    assert res.is_error, f"expected error, got: {res.output}"
    return res.output


def fill_brief(ctx, scope="internal"):
    args = {"goal": "ship it", "actors": ["user"], "scope": scope}
    if scope in ("production", "high_scale"):
        args |= {"users": "10k MAU", "consistency": "eventual", "availability": "99.9"}
    return ok(BriefTool(), ctx, **args)


def brainstorm_ctx(tmp_path, broker=None):
    session = ArchSession(state=ArchState(), broker=broker, on_state=lambda e: None)
    session.state.phase = "brainstorm"
    ctx = ToolContext(repo_root=tmp_path, arch=session)
    return ctx, session, []


# ---------- sketching ----------


def test_variant_node_link_build_a_sketch(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    ok(VariantTool(), ctx, id="v1", name="synchronous", summary="direct calls")
    ok(NodeTool(), ctx, id="api", label="API", kind="api", note="front door")
    ok(LinkTool(), ctx, src="api", dst="db", label="writes")  # db auto-created
    v = session.state.sketchbook.active_variant()
    assert v.name == "synchronous"
    assert set(v.nodes) == {"api", "db"}  # link auto-stubbed the missing endpoint
    assert v.nodes["db"].depth == "stub"
    assert [(l.src, l.dst, l.label) for l in v.links] == [("api", "db", "writes")]


def test_splice_inserts_an_intermediate_step(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    ok(VariantTool(), ctx, name="v")
    ok(LinkTool(), ctx, src="api", dst="db", label="writes", kind="sync")
    ok(SpliceTool(), ctx, src="api", dst="db", id="cache", kind="cache")
    v = session.state.sketchbook.active_variant()
    assert "cache" in v.nodes
    assert {(l.src, l.dst) for l in v.links} == {("api", "cache"), ("cache", "db")}


def test_depth_is_a_two_way_slider(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    ok(VariantTool(), ctx, name="v")
    ok(NodeTool(), ctx, id="svc")
    ok(DepthTool(), ctx, node_id="svc", level="detailed", detail="retries + dedup")
    n = session.state.sketchbook.active_variant().nodes["svc"]
    assert n.depth == "detailed" and n.detail == "retries + dedup"
    ok(DepthTool(), ctx, node_id="svc", level="stub")  # collapse
    n = session.state.sketchbook.active_variant().nodes["svc"]
    assert n.depth == "stub" and n.detail == ""  # collapsing clears the internal sketch


def test_node_opens_a_variant_when_none_is_active(tmp_path):
    """Sketching never asks for ceremony first — the surface is always there."""
    ctx, session, _ = brainstorm_ctx(tmp_path)
    ok(NodeTool(), ctx, id="a")
    v = session.state.sketchbook.active_variant()
    assert v is not None and "a" in v.nodes


# ---------- no phase locks ----------


def test_strict_tools_work_during_brainstorm(tmp_path):
    """The old harness refused this outright. Recording a component while still
    sketching is now just allowed."""
    ctx, session, _ = brainstorm_ctx(tmp_path)
    ok(ComponentTool(), ctx, id="api", kind="api", responsibility="r", trace=["g"])
    assert "api" in session.state.components


def test_component_needs_nothing_but_an_id(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    res = ok(ComponentTool(), ctx, id="mystery")
    assert session.state.components["mystery"].kind == "service"
    assert "thin:" in res.output  # what's missing comes back as advice


def test_sketch_layer_stays_open_after_promote(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    fill_brief(ctx)
    ok(VariantTool(), ctx, name="v")
    ok(NodeTool(), ctx, id="a", note="x")
    ok(PromoteTool(), ctx)
    assert session.state.phase == "propose"
    ok(NodeTool(), ctx, id="b")  # back to the napkin: allowed
    assert "b" in session.state.sketchbook.active_variant().nodes


def test_done_with_nothing_promoted_points_at_promote_without_erroring(tmp_path):
    ctx, _, _ = brainstorm_ctx(tmp_path)
    res = ok(ArchDoneTool(), ctx, summary="s")
    assert "promote" in res.output


# ---------- promotion ----------


def test_promote_without_a_complete_brief_works_and_says_what_is_unknown(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    ok(VariantTool(), ctx, name="v")
    ok(NodeTool(), ctx, id="a", note="x")
    res = ok(PromoteTool(), ctx)
    assert "a" in session.state.components
    assert "goal" in res.output and "not a blocker" in res.output


def test_promote_requires_a_sketched_variant(tmp_path):
    ctx, _, _ = brainstorm_ctx(tmp_path)
    fill_brief(ctx)
    assert "no variant" in err(PromoteTool(), ctx)
    ok(VariantTool(), ctx, name="empty")
    assert "no nodes" in err(PromoteTool(), ctx)


def test_promote_seeds_strict_and_leaves_rivals_live(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    fill_brief(ctx, scope="internal")
    ok(VariantTool(), ctx, id="v1", name="sync")
    ok(NodeTool(), ctx, id="api", label="API", kind="api", note="front door")
    ok(NodeTool(), ctx, id="db", kind="database")  # loose hint -> strict store
    ok(LinkTool(), ctx, src="api", dst="db", label="writes", kind="sync")
    ok(VariantTool(), ctx, id="v2", name="evented")  # rival, now active
    ok(NodeTool(), ctx, id="q", kind="queue")

    ok(PromoteTool(), ctx, variant_id="v1")
    st = session.state
    assert st.phase == "propose"
    assert st.sketchbook.variants["v1"].status == "chosen"
    # the rival is NOT archived — the user may still want to go back to it
    assert st.sketchbook.variants["v2"].status == "draft"
    assert set(st.components) == {"api", "db"}
    assert st.components["db"].kind == "store"          # database -> store mapping
    assert st.components["api"].responsibility == "front door"
    assert [(c.src, c.dst) for c in st.connections] == [("api", "db")]
    assert any("trace + responsibility" in m for m in st.toplevel_missing())


def test_repromoting_the_same_variant_is_idempotent(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    ok(VariantTool(), ctx, id="v1", name="sync")
    ok(NodeTool(), ctx, id="api")
    ok(PromoteTool(), ctx)
    ok(NodeTool(), ctx, id="db")           # sketch grew after promoting
    res = ok(PromoteTool(), ctx)           # pick the growth up
    assert set(session.state.components) == {"api", "db"}
    assert "1 already seeded" in res.output


def test_promoting_a_rival_with_replace_clears_the_old_shape(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    ok(VariantTool(), ctx, id="v1", name="sync")
    ok(NodeTool(), ctx, id="api")
    ok(LinkTool(), ctx, src="api", dst="db", label="writes")
    ok(PromoteTool(), ctx, variant_id="v1")
    assert set(session.state.components) == {"api", "db"}

    ok(VariantTool(), ctx, id="v2", name="evented")
    ok(NodeTool(), ctx, id="queue-in")
    ok(PromoteTool(), ctx, variant_id="v2", replace=True)
    st = session.state
    assert set(st.components) == {"queue-in"}   # the old seed is gone
    assert st.connections == []                 # and nothing dangles
    assert st.sketchbook.variants["v2"].status == "chosen"
    assert st.sketchbook.variants["v1"].status == "draft"


def test_promoted_drafts_reach_the_gate_with_what_is_thin(tmp_path):
    """The old harness refused approval until every draft was tightened. Now the
    thinness travels to the user, who decides whether it matters."""
    broker = FakeBroker([(True, "")])
    ctx, session, _ = brainstorm_ctx(tmp_path, broker=broker)
    fill_brief(ctx, scope="internal")
    ok(VariantTool(), ctx, name="sync")
    ok(NodeTool(), ctx, id="api", kind="api", note="serves requests")
    ok(NodeTool(), ctx, id="db", kind="store", note="stores rows")
    ok(LinkTool(), ctx, src="api", dst="db", label="writes")
    ok(PromoteTool(), ctx)

    ok(ArchDoneTool(), ctx, summary="rough but real")
    assert session.state.phase == "expand"  # approved
    payload = broker.payloads[-1]
    assert payload["kind"] == "toplevel_approval"
    assert any("trace" in t for t in payload["thin"])
    assert any("no trace" in g for g in payload["gaps"])


def test_tightening_the_design_clears_the_gaps(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    fill_brief(ctx, scope="internal")
    ok(VariantTool(), ctx, name="sync")
    ok(NodeTool(), ctx, id="api", kind="api", note="serves requests")
    ok(NodeTool(), ctx, id="db", kind="store", note="stores rows")
    ok(LinkTool(), ctx, src="api", dst="db", label="writes")
    ok(PromoteTool(), ctx)
    ok(ComponentTool(), ctx, id="api", trace=["goal"], responsibility="serves requests")
    ok(ComponentTool(), ctx, id="db", kind="store", data_owned="rows",
       trace=["goal"], responsibility="stores rows")
    ok(FlowTool(), ctx, id="f", name="main", kind="happy",
       steps=[{"src": "api", "dst": "db", "action": "write"}])
    ok(DecideTool(), ctx, topic="store", category="storage",
       options=[{"name": "pg"}, {"name": "mongo"}], choice="pg", rationale="relational")
    assert session.state.gaps() == []
    assert session.state.toplevel_missing() == []
