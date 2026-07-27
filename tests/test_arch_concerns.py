"""Disagreement as a first-class thing: the `concern` tool, the critic's
findings, what the finalize gate does with an open blocker, and what survives
into the handoff bundle.
"""

from ox.harnesses.arch import judge
from ox.harnesses.arch.bundle import write_bundle
from ox.harnesses.arch.session import ArchSession
from ox.harnesses.arch.sketch import SketchNode, Variant
from ox.harnesses.arch.state import ArchState
from ox.harnesses.arch.tools import (
    ArchDoneTool,
    ComponentTool,
    ConcernTool,
    ConnectTool,
    DecideTool,
    FlowTool,
    BriefTool,
)
from ox.tools import ToolContext


class FakeBroker:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def request(self, payload):
        self.requests.append(payload)
        return self.answers.pop(0)


def ok(tool, ctx, **args):
    res = tool.execute(args, ctx)
    assert not res.is_error, res.output
    return res


def err(tool, ctx, **args):
    res = tool.execute(args, ctx)
    assert res.is_error, f"expected error, got: {res.output}"
    return res.output


def make_ctx(tmp_path, broker=None, run_dir=None):
    session = ArchSession(state=ArchState(), run_dir=run_dir, broker=broker,
                          on_state=lambda e: None)
    return ToolContext(repo_root=tmp_path, arch=session), session


def small_design(ctx):
    ok(BriefTool(), ctx, goal="ship it", actors=["user"], scope="internal")
    ok(ComponentTool(), ctx, id="api", kind="api", responsibility="front door", trace=["g"])
    ok(ComponentTool(), ctx, id="db", kind="store", responsibility="rows",
       trace=["g"], data_owned="orders")
    ok(ConnectTool(), ctx, src="api", dst="db", label="writes", kind="sync")
    ok(FlowTool(), ctx, id="f", name="main", kind="happy",
       steps=[{"src": "api", "dst": "db", "action": "write"}])
    ok(DecideTool(), ctx, topic="Storage", category="storage",
       options=[{"name": "pg"}, {"name": "mongo"}], choice="pg", rationale="relational")


# ---------- the tool ----------


def test_concern_records_an_objection(tmp_path):
    ctx, session = make_ctx(tmp_path)
    res = ok(ConcernTool(), ctx, severity="blocker", target="db",
             claim="single writer becomes the bottleneck at 5k writes/sec",
             alternative="shard by tenant, or accept the ceiling explicitly")
    c = session.state.concerns[0]
    assert (c.id, c.severity, c.target, c.source) == ("c1", "blocker", "db", "model")
    assert c.open and session.state.open_blockers() == [c]
    assert "c1" in res.output


def test_concern_can_target_the_user_request(tmp_path):
    """The point of the overhaul: the agent can push back on the user, on the
    record, not just in prose that dies with the transcript."""
    ctx, session = make_ctx(tmp_path)
    ok(ConcernTool(), ctx, severity="risk", target="user",
       claim="a second database for reporting doubles the ops surface for one dashboard",
       alternative="a read replica of the existing store")
    assert session.state.concerns[0].target == "user"


def test_the_same_objection_is_not_filed_twice(tmp_path):
    ctx, session = make_ctx(tmp_path)
    ok(ConcernTool(), ctx, severity="risk", target="db", claim="unbounded growth")
    res = ok(ConcernTool(), ctx, severity="blocker", target="db", claim="Unbounded  growth!")
    assert len(session.state.concerns) == 1
    assert "Already on the record" in res.output


def test_concern_resolution_keeps_the_reason(tmp_path):
    ctx, session = make_ctx(tmp_path)
    ok(ConcernTool(), ctx, severity="risk", target="db", claim="unbounded growth")
    ok(ConcernTool(), ctx, resolve="c1", status="overruled",
       resolution="fine for the pilot; revisit before GA")
    c = session.state.concerns[0]
    assert c.status == "overruled" and not c.open
    assert "pilot" in c.resolution
    # and it does not come back on the next pass
    ok(ConcernTool(), ctx, severity="risk", target="db", claim="unbounded growth")
    assert len(session.state.concerns) == 1


def test_concern_needs_a_claim_and_a_known_id(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    assert "claim" in err(ConcernTool(), ctx, severity="risk", target="db")
    assert "no concern" in err(ConcernTool(), ctx, resolve="c9", status="accepted")


# ---------- teeth at the finalize gate ----------


def test_open_blocker_reaches_the_finalize_gate(tmp_path):
    broker = FakeBroker([(True, ""), (True, "shipping it anyway, pilot only")])
    ctx, session = make_ctx(tmp_path, broker=broker, run_dir=tmp_path / "run")
    small_design(ctx)
    ok(ConcernTool(), ctx, severity="blocker", target="db",
       claim="single writer caps us at 5k writes/sec", alternative="shard by tenant")
    ok(ArchDoneTool(), ctx, summary="ready")      # top-level approval
    res = ok(ArchDoneTool(), ctx, summary="finalize")

    payload = broker.requests[-1]
    assert payload["kind"] == "finalize"
    assert [b["id"] for b in payload["blockers"]] == ["c1"]
    assert payload["blockers"][0]["alternative"] == "shard by tenant"

    # finalizing over an open blocker is a decision, and it is recorded as one
    c = session.state.concerns[0]
    assert c.status == "overruled"
    assert c.resolution == "shipping it anyway, pilot only"
    assert "1 open blocker(s) overruled" in res.output


def test_a_blocker_settled_while_the_gate_is_up_is_not_overruled_anyway(tmp_path):
    """The gate carries a snapshot of the open blockers, but the page can now
    settle one from the rail while it is up. That ruling is the real one — an
    accepted objection must not be rewritten as 'overruled' on approval."""
    settled = {}

    class SettlingBroker(FakeBroker):
        def request(self, payload):
            if payload["kind"] == "finalize":
                # what the user does in the rail mid-gate
                settled["done"] = ctx.arch.apply_mutation({
                    "op": "concern", "id": "c1", "status": "accepted",
                    "resolution": "sharded by tenant before shipping",
                })
            return super().request(payload)

    broker = SettlingBroker([(True, ""), (True, "")])
    ctx, session = make_ctx(tmp_path, broker=broker, run_dir=tmp_path / "run")
    small_design(ctx)
    ok(ConcernTool(), ctx, severity="blocker", target="db",
       claim="single writer caps us at 5k writes/sec")
    ok(ArchDoneTool(), ctx, summary="ready")
    res = ok(ArchDoneTool(), ctx, summary="finalize")

    assert settled["done"]
    c = session.state.concerns[0]
    assert c.status == "accepted"
    assert c.resolution == "sharded by tenant before shipping"
    assert "overruled" not in res.output


def test_blocker_never_blocks_the_work(tmp_path):
    """Teeth, not a veto: an open blocker doesn't stop a single tool call."""
    ctx, session = make_ctx(tmp_path)
    ok(ConcernTool(), ctx, severity="blocker", target="db", claim="this will not scale")
    ok(ComponentTool(), ctx, id="svc", kind="service", responsibility="r", trace=["g"])
    assert "svc" in session.state.components


# ---------- the critic ----------


def test_parse_findings_is_tolerant():
    strict = judge.parse_findings(
        "- [blocker] order-db | dual write to db and queue loses events | outbox table\n"
        "- [smell] gateway | thin pass-through | fold it into the service"
    )
    assert [f["severity"] for f in strict] == ["blocker", "smell"]
    assert strict[0]["target"] == "order-db"
    assert strict[0]["alternative"] == "outbox table"

    loose = judge.parse_findings("- the queue has no dead letter handling")
    assert loose == [{"severity": "risk", "target": "design",
                      "claim": "the queue has no dead letter handling", "alternative": ""}]

    assert judge.parse_findings("OK") == []
    assert judge.parse_findings("") == []


def test_critic_findings_become_concerns(tmp_path):
    ctx, session = make_ctx(tmp_path)
    session.judge = lambda state: [
        {"severity": "blocker", "target": "db", "claim": "dual write loses events",
         "alternative": "outbox"},
    ]
    small_design(ctx)
    session.start_critic()
    session._critic_thread.join(timeout=5)
    c = session.state.concerns[0]
    assert (c.source, c.severity, c.target) == ("judge", "blocker", "db")


def test_critic_does_not_refile_what_the_user_overruled(tmp_path):
    ctx, session = make_ctx(tmp_path)
    session.judge = lambda state: [
        {"severity": "risk", "target": "db", "claim": "unbounded growth", "alternative": "ttl"},
    ]
    small_design(ctx)
    session.start_critic()
    session._critic_thread.join(timeout=5)
    ok(ConcernTool(), ctx, resolve="c1", status="overruled", resolution="pilot only")

    ok(ComponentTool(), ctx, id="extra", kind="service", responsibility="r", trace=["g"])
    session.start_critic()
    session._critic_thread.join(timeout=5)
    assert len(session.state.concerns) == 1  # settled means settled


# ---------- the handoff ----------


def test_bundle_carries_concerns_gaps_and_rejected_rivals(tmp_path):
    ctx, session = make_ctx(tmp_path, run_dir=tmp_path / "run")
    small_design(ctx)
    ok(ComponentTool(), ctx, id="thin-one", kind="store")  # deliberately underspecified
    ok(ConcernTool(), ctx, severity="risk", target="db",
       claim="single writer caps throughput", alternative="shard by tenant")
    ok(ConcernTool(), ctx, resolve="c1", status="overruled",
       resolution="fine until 5k writes/sec; revisit then")

    book = session.state.sketchbook
    rival = Variant(id="v2", name="evented", summary="queue between api and db",
                    rejected_reason="ops cost not justified at this volume")
    rival.nodes["q"] = SketchNode(id="q", label="Queue", kind="queue")
    book.variants["v2"] = rival

    write_bundle(session.state, session.run_dir)
    md = (session.run_dir / "bundle" / "architecture.md").read_text()

    assert "## Concerns raised" in md
    assert "single writer caps throughput" in md
    assert "revisit then" in md                     # the overrule reason survives
    assert "shard by tenant" in md
    assert "## Alternatives considered" in md
    assert "ops cost not justified" in md
    assert "## Known gaps" in md
    assert "thin-one" in md
