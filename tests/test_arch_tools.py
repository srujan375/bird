"""Tests for the arch toolset — gates, upserts, done-as-universal-gate."""

import pytest

from mha.harnesses.arch.session import ArchSession
from mha.harnesses.arch.state import ArchState
from mha.harnesses.arch.tools import (
    AmendTool,
    AnswerTool,
    ArchDoneTool,
    AskTool,
    BriefTool,
    ComponentTool,
    ConnectTool,
    DecideTool,
    ExpandTool,
    FlowTool,
    arch_harness_tools,
)
from mha.tools import ToolContext


class FakeBroker:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def request(self, payload):
        self.requests.append(payload)
        return self.answers.pop(0)


def make_ctx(tmp_path, broker=None, run_dir=None):
    events = []
    session = ArchSession(
        state=ArchState(),
        run_dir=run_dir,
        broker=broker,
        on_state=events.append,
    )
    ctx = ToolContext(repo_root=tmp_path, arch=session)
    return ctx, session, events


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


def add_component(ctx, cid, kind="service", **kw):
    kw.setdefault("responsibility", f"{cid} does things")
    kw.setdefault("trace", ["goal"])
    if kind == "store":
        kw.setdefault("data_owned", "its data")
    return ok(ComponentTool(), ctx, id=cid, kind=kind, **kw)


# ---------- gates ----------


def test_component_locked_until_brief(tmp_path):
    ctx, _, _ = make_ctx(tmp_path)
    out = err(ComponentTool(), ctx, id="api", kind="api", responsibility="r", trace=["g"])
    assert "locked until the brief" in out and "goal" in out


def test_brief_unlocks_and_flips_phase(tmp_path):
    ctx, session, events = make_ctx(tmp_path)
    res = fill_brief(ctx)
    assert session.state.phase == "propose"
    assert "components unlocked" in res.output.lower()
    assert events and events[-1]["phase"] == "propose"


def test_expand_locked_until_approval(tmp_path):
    ctx, _, _ = make_ctx(tmp_path)
    fill_brief(ctx)
    add_component(ctx, "db", kind="store")
    out = err(ExpandTool(), ctx, component_id="db", entities=[{"name": "x", "keys": "id"}])
    assert "locked until the top level is approved" in out


def test_done_in_intake_lists_missing(tmp_path):
    ctx, _, _ = make_ctx(tmp_path)
    out = err(ArchDoneTool(), ctx, summary="s")
    assert "goal" in out and "actors" in out


def test_done_in_propose_requires_toplevel(tmp_path):
    ctx, _, _ = make_ctx(tmp_path)
    fill_brief(ctx)
    out = err(ArchDoneTool(), ctx, summary="s")
    assert "happy flow" in out and "decision" in out


# ---------- upsert / remove ----------


def test_component_upsert_and_remove_guard(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    fill_brief(ctx)
    add_component(ctx, "api-gw", kind="gateway")
    add_component(ctx, "db", kind="store")
    ok(ConnectTool(), ctx, src="api-gw", dst="db", label="write", kind="sync")
    # update keeps id, changes fields
    ok(ComponentTool(), ctx, id="api-gw", name="Gateway v2")
    assert session.state.components["api-gw"].name == "Gateway v2"
    # removal blocked while referenced
    out = err(ComponentTool(), ctx, id="db", remove=True)
    assert "still referenced" in out
    ok(ConnectTool(), ctx, src="api-gw", dst="db", remove=True)
    ok(ComponentTool(), ctx, id="db", remove=True)
    assert "db" not in session.state.components


def test_connect_upsert_by_label(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    fill_brief(ctx)
    add_component(ctx, "a")
    add_component(ctx, "b")
    ok(ConnectTool(), ctx, src="a", dst="b", label="read", kind="sync")
    ok(ConnectTool(), ctx, src="a", dst="b", label="write", kind="sync")
    assert len(session.state.connections) == 2
    ok(ConnectTool(), ctx, src="a", dst="b", label="write", kind="async", mechanism="sqs")
    assert len(session.state.connections) == 2
    assert session.state.connections[1].mechanism == "sqs"
    out = err(ConnectTool(), ctx, src="a", dst="b", remove=True)
    assert "multiple connections" in out


def test_async_requires_mechanism(tmp_path):
    ctx, _, _ = make_ctx(tmp_path)
    fill_brief(ctx)
    add_component(ctx, "a")
    add_component(ctx, "b")
    out = err(ConnectTool(), ctx, src="a", dst="b", label="events", kind="async")
    assert "mechanism" in out


# ---------- the full session walk ----------


def build_toplevel(ctx, scope="production"):
    fill_brief(ctx, scope)
    common = {"failure_notes": "contained"} if scope in ("production", "high_scale") else {}
    add_component(ctx, "gw", kind="gateway", **common)
    add_component(ctx, "db", kind="store", **common)
    add_component(ctx, "wp", kind="service", **common)
    conn = {"failure_mode": "503"} if scope in ("production", "high_scale") else {}
    ok(ConnectTool(), ctx, src="gw", dst="wp", label="dispatch", kind="sync", **conn)
    ok(ConnectTool(), ctx, src="wp", dst="db", label="write", kind="sync", **conn)
    ok(FlowTool(), ctx, id="ingest", name="ingest", kind="happy",
       steps=[{"src": "gw", "dst": "wp", "action": "POST /in"},
              {"src": "wp", "dst": "db", "action": "INSERT"}])
    ok(FlowTool(), ctx, id="ingest-fail", name="ingest failure", kind="failure",
       steps=[{"src": "gw", "dst": "wp", "action": "POST /in -> 503"}])
    ok(DecideTool(), ctx, topic="Storage", category="storage",
       options=[{"name": "postgres"}, {"name": "dynamo"}],
       choice="postgres", rationale="relational fits")


def test_full_session_to_finalize(tmp_path):
    broker = FakeBroker([(True, ""), (True, "")])  # approve toplevel, approve finalize
    run_dir = tmp_path / "run"
    ctx, session, events = make_ctx(tmp_path, broker=broker, run_dir=run_dir)
    build_toplevel(ctx)

    res = ok(ArchDoneTool(), ctx, summary="top level ready")
    assert session.state.phase == "expand"
    assert broker.requests[0]["kind"] == "toplevel_approval"
    # risk order: store before api
    assert 'expand("db")' in res.output

    out = err(ExpandTool(), ctx, component_id="gw",
              endpoints=[{"route": "/in", "method": "POST", "request": "{}",
                          "response": "{}", "auth": "hmac"}])
    assert "risk order" in out and "'db'" in out

    ok(ExpandTool(), ctx, component_id="db",
       entities=[{"name": "events", "keys": "id", "fields": ["id", "payload"]}],
       access_patterns=["events by time"], retention="90d")
    ok(ExpandTool(), ctx, component_id="gw",
       endpoints=[{"route": "/in", "method": "POST", "request": "{}",
                   "response": "{}", "auth": "hmac"}])
    assert session.state.pending_obligations() == []

    res = ok(ArchDoneTool(), ctx, summary="expanded")  # challenge clean -> finalize
    assert session.state.phase == "finalized"
    assert broker.requests[1]["kind"] == "finalize"
    assert broker.requests[1]["artifacts"]
    assert (run_dir / "bundle" / "architecture.json").is_file()
    assert (run_dir / "bundle" / "architecture.md").is_file()
    assert "mha code" in res.output
    # every mutation emitted a full arch_state event
    assert events[-1]["phase"] == "finalized"
    assert events[-1]["state"]["components"]["db"]["facet"]["facet_kind"] == "store"


def test_toplevel_rejection_returns_feedback_same_turn(tmp_path):
    broker = FakeBroker([(False, "drop the worker pool")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    build_toplevel(ctx, scope="internal")
    out = err(ArchDoneTool(), ctx, summary="ready?")
    assert "The user requested changes" in out and "drop the worker pool" in out
    assert session.state.phase == "propose"  # back to editing


def expand_owed(ctx):
    """Close the internal-scope obligation queue: db (store), then gw (api)."""
    ok(ExpandTool(), ctx, component_id="db",
       entities=[{"name": "events", "keys": "id"}],
       access_patterns=["by time"], retention="90d")
    ok(ExpandTool(), ctx, component_id="gw",
       endpoints=[{"route": "/in", "method": "POST", "request": "{}",
                   "response": "{}", "auth": "hmac"}])


def test_finalize_rejection_keeps_session_alive(tmp_path):
    broker = FakeBroker([(True, ""), (False, "add retention policy")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    build_toplevel(ctx, scope="internal")
    ok(ArchDoneTool(), ctx, summary="ready")
    expand_owed(ctx)
    out = err(ArchDoneTool(), ctx, summary="finalize?")
    assert "add retention policy" in out
    assert session.state.phase == "resolved"


def test_challenge_findings_block_then_resolve(tmp_path):
    """A production design with an unconnected component and no failure twin
    gets challenge findings; resolving them lets finalize through."""
    broker = FakeBroker([(True, ""), (True, "")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    fill_brief(ctx, "production")
    add_component(ctx, "gw", kind="gateway", failure_notes="x")
    add_component(ctx, "db", kind="store", failure_notes="x")
    add_component(ctx, "orphan", kind="service", failure_notes="x")
    ok(ConnectTool(), ctx, src="gw", dst="db", label="write", kind="sync", failure_mode="503")
    ok(FlowTool(), ctx, id="f", name="ingest", kind="happy",
       steps=[{"src": "gw", "dst": "db", "action": "write"}])
    ok(DecideTool(), ctx, topic="t", category="storage",
       options=[{"name": "a"}, {"name": "b"}], choice="a", rationale="r")
    ok(ArchDoneTool(), ctx, summary="ready")
    ok(ExpandTool(), ctx, component_id="db",
       entities=[{"name": "e", "keys": "id"}], access_patterns=["p"], retention="30d")
    ok(ExpandTool(), ctx, component_id="gw",
       endpoints=[{"route": "/x", "method": "POST", "request": "{}",
                   "response": "{}", "auth": "key"}])
    out = err(ArchDoneTool(), ctx, summary="done?")
    assert "challenge pass found" in out
    assert session.state.phase == "challenge"
    assert any(q.source == "harness_audit" for q in session.state.questions)
    # findings are non-blocking here; done again goes to finalize
    res = ok(ArchDoneTool(), ctx, summary="findings noted")
    assert session.state.phase == "finalized"


def test_judge_findings_appended_and_failure_tolerated(tmp_path):
    calls = []

    def judge(state):
        calls.append(state.phase)
        return ["would the queue survive a region outage?"]

    ctx, session, _ = make_ctx(tmp_path, broker=FakeBroker([(True, "")]))
    session.judge = judge
    build_toplevel(ctx, scope="internal")
    ok(ArchDoneTool(), ctx, summary="ready")
    expand_owed(ctx)
    out = err(ArchDoneTool(), ctx, summary="expanded")
    assert "region outage" in out
    assert any(q.source == "judge" for q in session.state.questions)
    assert calls == ["expand"]

    # a judge that blows up degrades to the audit alone
    session2 = ArchSession(state=ArchState(), judge=lambda s: 1 / 0)
    assert session2.run_challenge() == []


def test_blocking_question_gates_finalize(tmp_path):
    broker = FakeBroker([(True, "")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    build_toplevel(ctx, scope="internal")
    ok(AskTool(), ctx, question="which region?", blocking=True)
    ok(ArchDoneTool(), ctx, summary="ready")
    expand_owed(ctx)
    out = err(ArchDoneTool(), ctx, summary="finalize?")
    assert "blocking questions unresolved" in out and "which region?" in out
    ok(AnswerTool(), ctx, id="q1", answer="us-east-1")
    assert session.state.blocking_questions() == []


def test_amend_toplevel_gates_and_structural_flag(tmp_path):
    broker = FakeBroker([(True, "")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    build_toplevel(ctx, scope="internal")
    out = err(AmendTool(), ctx, description="x",
              component={"id": "cache", "kind": "cache", "responsibility": "r", "trace": ["g"]})
    assert "not approved yet" in out
    ok(ArchDoneTool(), ctx, summary="ready")
    # post-approval: component tool locked, amend works
    out = err(ComponentTool(), ctx, id="cache", kind="cache", responsibility="r", trace=["g"])
    assert "amend_toplevel" in out
    res = ok(AmendTool(), ctx, description="add cache for hot reads",
             component={"id": "cache", "kind": "cache", "responsibility": "r", "trace": ["g"]})
    assert "structural" in res.output
    assert session.state.amendments[-1].structural is True
    # cache is stateful at internal scope -> new obligation appeared
    assert any(o.component_id == "cache" for o in session.state.pending_obligations())
    res = ok(AmendTool(), ctx, description="rename cache",
             component={"id": "cache", "name": "Hot Cache"})
    assert session.state.amendments[-1].structural is False


def test_persistence_and_resume(tmp_path):
    run_dir = tmp_path / "run"
    ctx, session, _ = make_ctx(tmp_path, run_dir=run_dir)
    fill_brief(ctx)
    add_component(ctx, "svc")
    restored = ArchSession.load(run_dir)
    assert restored.state.phase == "propose"
    assert "svc" in restored.state.components


def test_toolset_composition(tmp_path):
    names = [t.name for t in arch_harness_tools()]
    assert names == ["read", "kg_query", "WebSearch", "WebFetch", "brief", "component",
                     "connect", "flow", "expand", "decide", "ask", "answer",
                     "amend_toplevel", "skill", "done"]
    for absent in ("edit", "write", "bash", "plan"):
        assert absent not in names
    names = [t.name for t in arch_harness_tools(with_kg=False, with_web=False)]
    assert "kg_query" not in names and "WebSearch" not in names
