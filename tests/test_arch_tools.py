"""Tests for the arch toolset — advisory gaps, upserts, the two human gates."""

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


def test_component_works_before_the_brief_is_complete(tmp_path):
    """The brief is load-bearing, not a turnstile — record structure whenever
    the conversation produces it."""
    ctx, session, _ = make_ctx(tmp_path)
    ok(ComponentTool(), ctx, id="api", kind="api", responsibility="r", trace=["g"])
    assert "api" in session.state.components


def test_brief_reports_what_is_still_unknown(tmp_path):
    ctx, session, events = make_ctx(tmp_path)
    res = ok(BriefTool(), ctx, goal="ship it")
    assert "actors" in res.output and "scope" in res.output
    assert "assuming" in res.output          # ask, don't invent
    fill_brief(ctx)
    # a complete brief no longer moves the session anywhere: it gates nothing,
    # it accretes. The phase follows the *design*, not the paperwork.
    assert session.state.phase == "brainstorm"
    assert events and events[-1]["phase"] == "brainstorm"


def test_the_first_component_leaves_the_sketch_layer(tmp_path):
    """Whichever way a component arrives — promoted or hand-written — the
    session stops being 'brainstorm' once the design layer holds something."""
    ctx, session, _ = make_ctx(tmp_path)
    assert session.state.phase == "brainstorm"
    add_component(ctx, "api")
    assert session.state.phase == "propose"
    # and it never yanks a later phase backwards
    session.state.phase = "expand"
    add_component(ctx, "db", kind="store")
    assert session.state.phase == "expand"


def test_expand_works_before_approval(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    fill_brief(ctx)
    add_component(ctx, "db", kind="store")
    ok(ExpandTool(), ctx, component_id="db", entities=[{"name": "x", "keys": "id"}])
    assert session.state.components["db"].facet.facet_kind == "store"


def test_done_with_an_empty_design_does_not_error(tmp_path):
    ctx, _, _ = make_ctx(tmp_path)
    res = ok(ArchDoneTool(), ctx, summary="s")
    assert "nothing to approve" in res.output.lower()


def test_done_takes_a_thin_design_to_the_user_anyway(tmp_path):
    """The old gate refused until the top level was complete. Now it goes to the
    user with the thinness attached and they rule on it."""
    broker = FakeBroker([(True, "")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    fill_brief(ctx)
    add_component(ctx, "api", kind="api")
    ok(ArchDoneTool(), ctx, summary="early but worth a look")
    assert session.state.phase == "expand"
    payload = broker.requests[0]
    assert payload["kind"] == "toplevel_approval"
    assert any("happy flow" in t for t in payload["thin"])
    assert any("decision" in t for t in payload["thin"])


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


def test_async_without_mechanism_is_recorded_with_advice(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    fill_brief(ctx)
    add_component(ctx, "a")
    add_component(ctx, "b")
    res = ok(ConnectTool(), ctx, src="a", dst="b", label="events", kind="async")
    assert len(session.state.connections) == 1
    assert "thin:" in res.output and "mechanism" in res.output


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
    # risk order is a suggestion: store before api
    assert "db" in res.output

    # expanding out of order is allowed, and says which one mattered more
    res = ok(ExpandTool(), ctx, component_id="gw",
             endpoints=[{"route": "/in", "method": "POST", "request": "{}",
                         "response": "{}", "auth": "hmac"}])
    assert "db" in res.output and "riskier" in res.output

    ok(ExpandTool(), ctx, component_id="db",
       entities=[{"name": "events", "keys": "id", "fields": ["id", "payload"]}],
       access_patterns=["events by time"], retention="90d")
    ok(ExpandTool(), ctx, component_id="gw",
       endpoints=[{"route": "/in", "method": "POST", "request": "{}",
                   "response": "{}", "auth": "hmac"}])
    assert session.state.pending_obligations() == []

    res = ok(ArchDoneTool(), ctx, summary="expanded")  # straight to the finalize gate
    assert session.state.phase == "finalized"
    assert broker.requests[1]["kind"] == "finalize"
    assert broker.requests[1]["artifacts"]
    assert (run_dir / "bundle" / "architecture.json").is_file()
    assert (run_dir / "bundle" / "architecture.md").is_file()
    assert "mha code" in res.output
    # every mutation emitted a full arch_state event
    assert events[-1]["phase"] == "finalized"
    assert events[-1]["state"]["components"]["db"]["facet"]["facet_kind"] == "store"


def test_expand_rejects_non_string_fields_cleanly(tmp_path):
    # a model passing structured objects where the schema wants strings must get
    # a clean, recoverable error — not an opaque TypeError from rendering — and
    # the component's facet must stay unset (no half-applied mutation)
    broker = FakeBroker([(True, "")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    build_toplevel(ctx)
    ok(ArchDoneTool(), ctx, summary="top level ready")
    assert session.state.phase == "expand"

    # fields as a list of objects (e.g. an Atlas index field spec)
    out = err(ExpandTool(), ctx, component_id="db",
              entities=[{"name": "contacts", "keys": "id",
                         "fields": [{"name": "firstName", "type": "string"}]}],
              access_patterns=["by name"])
    assert "fields" in out and "list of plain strings" in out
    assert session.state.components["db"].facet is None  # nothing half-applied

    # keys as an object
    out = err(ExpandTool(), ctx, component_id="db",
              entities=[{"name": "contacts", "keys": {"primary": "id"}}],
              access_patterns=["by name"])
    assert "keys" in out and "must be a plain string" in out
    assert session.state.components["db"].facet is None

    # a valid string-only expand still works
    ok(ExpandTool(), ctx, component_id="db",
       entities=[{"name": "contacts", "keys": "id", "fields": ["firstName", "email"]}],
       access_patterns=["by name"], retention="forever")
    assert session.state.components["db"].facet.facet_kind == "store"


def test_toplevel_rejection_returns_feedback_same_turn(tmp_path):
    broker = FakeBroker([(False, "drop the worker pool")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    build_toplevel(ctx, scope="internal")
    res = ok(ArchDoneTool(), ctx, summary="ready?")  # a refusal is the user talking, not an error
    assert "wants changes" in res.output and "drop the worker pool" in res.output
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
    res = ok(ArchDoneTool(), ctx, summary="finalize?")
    assert "add retention policy" in res.output
    assert session.state.phase == "expand"  # back to work on the design


def test_audit_findings_reach_the_finalize_gate_without_blocking_it(tmp_path):
    """A production design with an unconnected component and no failure twin
    files concerns — and still finalizes if the user says so."""
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
    ok(ArchDoneTool(), ctx, summary="done?")
    assert session.state.phase == "finalized"
    assert any(c.source == "harness_audit" for c in session.state.concerns)
    claims = " ".join(c.claim for c in session.state.concerns)
    assert "orphan" in claims and "failure twin" in claims
    # they travelled to the user with the finalize request
    assert broker.requests[-1]["kind"] == "finalize"
    assert broker.requests[-1]["concerns"]


def test_critic_files_concerns_off_the_turn(tmp_path):
    calls = []

    def judge(state):
        calls.append(state.phase)
        return [{"severity": "blocker", "target": "db",
                 "claim": "would the queue survive a region outage?",
                 "alternative": "replicate the queue"}]

    ctx, session, _ = make_ctx(tmp_path)
    session.judge = judge
    build_toplevel(ctx, scope="internal")

    session.start_critic()
    session._critic_thread.join(timeout=5)
    assert [c.claim for c in session.state.concerns] == ["would the queue survive a region outage?"]
    assert session.state.open_blockers()
    assert calls == ["propose"]

    # an unchanged design is not reviewed twice
    session.start_critic()
    if session._critic_thread is not None:
        session._critic_thread.join(timeout=5)
    assert len(calls) == 1
    assert len(session.state.concerns) == 1

    # the design moves on -> reviewed again, but the same finding isn't duplicated
    add_component(ctx, "extra")
    session.start_critic()
    session._critic_thread.join(timeout=5)
    assert len(calls) == 2 and len(session.state.concerns) == 1


def test_critic_failure_is_silent(tmp_path):
    """An offline or broken judge must never surface as a session failure."""
    session = ArchSession(state=ArchState(), judge=lambda s: 1 / 0)
    session.state.components["x"] = session.state.components.get("x") or _stub_component()
    session.start_critic()
    session._critic_thread.join(timeout=5)
    assert session.state.concerns == []


def _stub_component():
    from mha.harnesses.arch.state import Component
    return Component(id="x", name="x", kind="service", responsibility="r")


def test_unanswered_question_travels_to_both_gates(tmp_path):
    """An open question is information for the user's ruling, not a turnstile."""
    broker = FakeBroker([(True, ""), (True, "")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    build_toplevel(ctx, scope="internal")
    ok(AskTool(), ctx, question="which region?", blocking=True)

    ok(ArchDoneTool(), ctx, summary="ready")
    assert session.state.phase == "expand"
    assert broker.requests[0]["questions"] == ["which region?"]

    expand_owed(ctx)
    ok(ArchDoneTool(), ctx, summary="finalize?")
    assert broker.requests[-1]["questions"] == ["which region?"]
    assert session.state.phase == "finalized"


def test_answering_a_question_closes_it(tmp_path):
    ctx, session, _ = make_ctx(tmp_path)
    fill_brief(ctx)
    ok(AskTool(), ctx, question="which region?", blocking=True)
    assert session.state.blocking_questions()
    ok(AnswerTool(), ctx, id="q1", answer="us-east-1")
    assert session.state.blocking_questions() == []


def test_post_approval_edits_record_amendments_instead_of_being_refused(tmp_path):
    broker = FakeBroker([(True, "")])
    ctx, session, _ = make_ctx(tmp_path, broker=broker)
    build_toplevel(ctx, scope="internal")
    ok(ArchDoneTool(), ctx, summary="ready")

    # the plain component tool still works after approval — and leaves a trail
    res = ok(ComponentTool(), ctx, id="cache", kind="cache", responsibility="r", trace=["g"])
    assert "structural amendment" in res.output
    assert session.state.amendments[-1].structural is True
    ok(ComponentTool(), ctx, id="cache", remove=True)

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
    assert names == ["read", "kg_query", "WebSearch", "WebFetch",
                     "import_state",
                     "variant", "node", "link", "splice", "depth", "promote",
                     "brief", "component", "connect", "flow", "expand", "decide",
                     "concern", "ask", "answer", "amend_toplevel", "skill", "done"]
    for absent in ("edit", "write", "bash", "plan"):
        assert absent not in names
    names = [t.name for t in arch_harness_tools(with_kg=False, with_web=False)]
    assert "kg_query" not in names and "WebSearch" not in names
