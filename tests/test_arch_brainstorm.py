"""The brainstorm phase: loose sketch tools, the depth slider, splice, phase
guards, and promotion into the strict ArchState."""

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

    def request(self, payload):
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


def test_node_needs_an_active_variant(tmp_path):
    ctx, _, _ = brainstorm_ctx(tmp_path)
    out = err(NodeTool(), ctx, id="a")
    assert "no active variant" in out


# ---------- phase guards ----------


def test_strict_tools_locked_in_brainstorm(tmp_path):
    ctx, _, _ = brainstorm_ctx(tmp_path)
    fill_brief(ctx)
    out = err(ComponentTool(), ctx, id="api", kind="api", responsibility="r", trace=["g"])
    assert "brainstorming" in out and "node" in out


def test_done_in_brainstorm_points_to_promote(tmp_path):
    ctx, _, _ = brainstorm_ctx(tmp_path)
    out = err(ArchDoneTool(), ctx, summary="s")
    assert "promote" in out


def test_loose_tools_locked_after_promote(tmp_path):
    ctx, session, _ = brainstorm_ctx(tmp_path)
    fill_brief(ctx)
    ok(VariantTool(), ctx, name="v")
    ok(NodeTool(), ctx, id="a", note="x")
    ok(PromoteTool(), ctx)
    assert session.state.phase == "propose"
    out = err(NodeTool(), ctx, id="b")
    assert "committed" in out and "propose" in out


# ---------- promotion ----------


def test_promote_requires_complete_brief(tmp_path):
    ctx, _, _ = brainstorm_ctx(tmp_path)
    ok(VariantTool(), ctx, name="v")
    ok(NodeTool(), ctx, id="a", note="x")
    out = err(PromoteTool(), ctx)
    assert "brief still needs" in out and "goal" in out


def test_promote_requires_a_sketched_variant(tmp_path):
    ctx, _, _ = brainstorm_ctx(tmp_path)
    fill_brief(ctx)
    assert "no variant" in err(PromoteTool(), ctx)
    ok(VariantTool(), ctx, name="empty")
    assert "no nodes" in err(PromoteTool(), ctx)


def test_promote_seeds_strict_and_archives_rivals(tmp_path):
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
    assert st.sketchbook.variants["v2"].status == "archived"
    assert set(st.components) == {"api", "db"}
    assert st.components["db"].kind == "store"          # database -> store mapping
    assert st.components["api"].responsibility == "front door"
    assert [(c.src, c.dst) for c in st.connections] == [("api", "db")]
    # promoted components are drafts — no trace yet
    assert any("trace + responsibility" in m for m in st.toplevel_missing())


def test_promoted_drafts_block_approval_until_tightened(tmp_path):
    broker = FakeBroker([(True, "")])
    ctx, session, _ = brainstorm_ctx(tmp_path, broker=broker)
    fill_brief(ctx, scope="internal")
    ok(VariantTool(), ctx, name="sync")
    ok(NodeTool(), ctx, id="api", kind="api", note="serves requests")
    ok(NodeTool(), ctx, id="db", kind="store", note="stores rows")
    ok(LinkTool(), ctx, src="api", dst="db", label="writes")
    ok(PromoteTool(), ctx)

    # drafts lack trace -> the top-level gate refuses
    assert "trace + responsibility" in err(ArchDoneTool(), ctx, summary="s")

    # tighten the promoted drafts, add the flow + decision the strict layer wants
    ok(ComponentTool(), ctx, id="api", trace=["goal"], responsibility="serves requests")
    ok(ComponentTool(), ctx, id="db", kind="store", data_owned="rows",
       trace=["goal"], responsibility="stores rows")
    ok(FlowTool(), ctx, id="f", name="main", kind="happy",
       steps=[{"src": "api", "dst": "db", "action": "write"}])
    ok(DecideTool(), ctx, topic="store", category="storage",
       options=[{"name": "pg"}, {"name": "mongo"}], choice="pg", rationale="relational")

    ok(ArchDoneTool(), ctx, summary="ready")
    assert session.state.phase == "expand"  # user approved the top level
