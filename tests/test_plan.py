import json

import pytest

from mha.harness.runner import PLAN_TRACKER_PREFIX, Runner
from mha.llm.registry import ModelSpec, ProviderConfig, Registry
from mha.llm.types import LLMResponse, Message, ToolCall, Usage
from mha.tools import DoneTool, PlanTool, PlanUpdateTool, ToolContext, code_harness_tools

SPEC = ModelSpec(
    spec="fake:model",
    provider=ProviderConfig(name="fake", base_url="http://x"),
    model="model",
    context_window=32768,
)
REGISTRY = Registry(providers={}, models={}, aliases={})


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []  # message lists as seen at each completion

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
        self.requests.append(list(messages))
        msg = self.script.pop(0)
        return LLMResponse(message=msg, usage=Usage(100, 10), stop_reason="stop", model=spec.spec)


def tc(name, args, id="c1"):
    return ToolCall.from_raw(id, name, json.dumps(args))


def assistant(content=None, calls=()):
    return Message(role="assistant", content=content, tool_calls=list(calls))


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n")
    (tmp_path / "g.py").write_text("y = 2\n")
    return tmp_path


@pytest.fixture
def ctx(repo):
    events = []
    c = ToolContext(repo_root=repo, record=lambda t, d: events.append((t, d)))
    c.events = events
    return c


TWO_STEPS = {
    "steps": [
        {"title": "Add store", "files": ["src/store.py"]},
        {"title": "Wire REPL", "files": ["src/repl.py"]},
    ]
}


# --- plan tool ---

def test_plan_creates_tracker(ctx):
    r = PlanTool().execute(TWO_STEPS, ctx)
    assert not r.is_error
    assert r.output.startswith(PLAN_TRACKER_PREFIX)
    assert "-> 1." in r.output and "src/store.py" in r.output
    assert ctx.plan.steps[0].status == "in_progress"
    assert ctx.plan.steps[1].status == "pending"
    assert ("plan_created", ctx.plan.to_dict()) in ctx.events


def test_plan_rejects_replan(ctx):
    PlanTool().execute(TWO_STEPS, ctx)
    r = PlanTool().execute(TWO_STEPS, ctx)
    assert r.is_error
    assert "already exists" in r.output


def test_plan_replaces_fully_closed_plan(ctx):
    PlanTool().execute(TWO_STEPS, ctx)
    PlanUpdateTool().execute({"step": 1, "status": "done"}, ctx)
    PlanUpdateTool().execute({"step": 2, "status": "skipped"}, ctx)
    r = PlanTool().execute({"steps": [{"title": "Next task", "files": ["src/next.py"]}]}, ctx)
    assert not r.is_error
    assert [s.title for s in ctx.plan.steps] == ["Next task"]


def test_plan_update_advances_cursor(ctx):
    PlanTool().execute(TWO_STEPS, ctx)
    r = PlanUpdateTool().execute({"step": 1, "status": "done"}, ctx)
    assert not r.is_error
    assert ctx.plan.steps[0].status == "done"
    assert ctx.plan.steps[1].status == "in_progress"
    assert "-> 2." in r.output


def test_plan_update_bad_step(ctx):
    PlanTool().execute(TWO_STEPS, ctx)
    r = PlanUpdateTool().execute({"step": 9, "status": "done"}, ctx)
    assert r.is_error and "does not exist" in r.output


def test_plan_update_without_plan(ctx):
    r = PlanUpdateTool().execute({"step": 1, "status": "done"}, ctx)
    assert r.is_error and "no plan" in r.output


def test_all_steps_closed_render(ctx):
    PlanTool().execute(TWO_STEPS, ctx)
    PlanUpdateTool().execute({"step": 1, "status": "done"}, ctx)
    r = PlanUpdateTool().execute({"step": 2, "status": "skipped", "note": "not needed"}, ctx)
    assert "All steps closed" in r.output
    assert "(not needed)" in r.output


# --- done gating ---

def test_done_blocked_while_steps_open(ctx):
    PlanTool().execute(TWO_STEPS, ctx)
    r = DoneTool().execute({"summary": "done"}, ctx)
    assert r.is_error
    assert "still open" in r.output and "Wire REPL" in r.output


def test_done_allowed_after_close(ctx):
    PlanTool().execute(TWO_STEPS, ctx)
    PlanUpdateTool().execute({"step": 1, "status": "done"}, ctx)
    PlanUpdateTool().execute({"step": 2, "status": "skipped"}, ctx)
    r = DoneTool().execute({"summary": "done"}, ctx)
    assert not r.is_error


def test_done_unaffected_without_plan(ctx):
    r = DoneTool().execute({"summary": "done"}, ctx)
    assert not r.is_error


# --- KG blast radius ---

@pytest.fixture(scope="module")
def kg_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("planrepo")
    (root / "auth.py").write_text(
        "class AuthHandler:\n"
        "    def login(self, user):\n"
        "        return check_password(user)\n"
        "\n"
        "def check_password(user):\n"
        "    return True\n"
    )
    (root / "db.py").write_text(
        "from auth import AuthHandler\n"
        "\n"
        "class Database:\n"
        "    def connect(self):\n"
        "        self.handler = AuthHandler()\n"
    )
    return root


@pytest.fixture(scope="module")
def kg(kg_repo):
    from mha.context.kg import KG

    kg = KG(kg_repo)
    kg.build()
    return kg


def test_affected_files_finds_neighbors(kg):
    affected = kg.affected_files(["auth.py"], depth=2)
    assert any(f.endswith("db.py") for f in affected)
    assert not any(f.endswith("auth.py") for f in affected)


def test_affected_files_bridges_bare_name_nodes(tmp_path):
    """graphify attaches cross-file calls to file-less bare-name nodes with no
    edge to the definition node — affected_files must bridge that by label."""
    import json as _json

    import networkx as nx
    from networkx.readwrite import json_graph

    from mha.context.kg import KG

    G = nx.Graph()
    G.add_node("core_store", label="Store", source_file="core.py")
    G.add_node("store", label="Store")  # bare unresolved-name node, no file
    G.add_node("app_main", label="main()", source_file="app.py")
    G.add_edge("store", "app_main", relation="calls")  # consumer -> bare node only

    kg = KG(tmp_path)
    kg.out_dir.mkdir(parents=True, exist_ok=True)
    kg.graph_path.write_text(_json.dumps(json_graph.node_link_data(G, edges="links")))
    affected = kg.affected_files(["core.py"], depth=2)
    assert affected == ["app.py"]


def test_affected_files_no_match(kg):
    assert kg.affected_files(["nothere.py"]) == []


def test_plan_attaches_blast_radius(kg, kg_repo):
    ctx = ToolContext(repo_root=kg_repo, kg=kg)
    PlanTool().execute({"steps": [{"title": "Harden auth", "files": ["auth.py"]}]}, ctx)
    assert any(f.endswith("db.py") for f in ctx.plan.steps[0].affected)


def test_plan_without_kg_has_empty_blast_radius(ctx):
    PlanTool().execute(TWO_STEPS, ctx)
    assert ctx.plan.steps[0].affected == []


# --- runner integration ---

@pytest.fixture
def make_runner(repo):
    def _make(script, **kw):
        events = []
        ctx = ToolContext(repo_root=repo, record=lambda t, d: events.append((t, d)))
        r = Runner(
            spec=SPEC,
            client=FakeClient(script),
            registry=REGISTRY,
            tools=code_harness_tools(with_kg=False),
            ctx=ctx,
            **kw,
        )
        r.events = events
        return r

    return _make


def _tracker_messages(messages):
    return [
        m for m in messages
        if m.role == "user" and (m.content or "").startswith(PLAN_TRACKER_PREFIX)
    ]


def test_runner_pins_single_fresh_tracker(make_runner):
    r = make_runner([
        assistant(calls=[tc("plan", TWO_STEPS)]),
        assistant(calls=[tc("write", {"path": "src/store.py", "content": "x = 1\n"})]),
        assistant(calls=[tc("plan_update", {"step": 1, "status": "done"})]),
        assistant(calls=[tc("plan_update", {"step": 2, "status": "skipped"})]),
        assistant(calls=[tc("done", {"summary": "shipped"})]),
    ])
    result = r.run("build the feature")
    assert result.status == "done"
    # every completion after the plan existed saw exactly one live tracker,
    # rendered with the state current at that moment
    for req in r.client.requests[1:]:
        assert len(_tracker_messages(req)) == 1
    assert "-> 1." in _tracker_messages(r.client.requests[1])[0].content
    assert "-> 2." in _tracker_messages(r.client.requests[3])[0].content


def test_runner_done_blocked_then_allowed(make_runner):
    r = make_runner([
        assistant(calls=[tc("plan", {"steps": [{"title": "One", "files": ["f.py"]}]})]),
        assistant(calls=[tc("done", {"summary": "premature"})]),
        assistant(calls=[tc("plan_update", {"step": 1, "status": "done"})]),
        assistant(calls=[tc("done", {"summary": "actually done"})]),
    ])
    result = r.run("task")
    assert result.status == "done"
    assert result.summary == "actually done"
    assert result.turns == 4


def test_runner_done_clears_plan_and_tracker(make_runner):
    r = make_runner([
        assistant(calls=[tc("plan", {"steps": [{"title": "One", "files": ["f.py"]}]})]),
        assistant(calls=[tc("plan_update", {"step": 1, "status": "done"})]),
        assistant(calls=[tc("done", {"summary": "shipped"})]),
    ])
    result = r.run("task")
    assert result.status == "done"
    # the plan belonged to this task — a stale "all steps closed" scoreboard
    # must not steer the next exchange
    assert r.ctx.plan is None
    assert _tracker_messages(result.messages) == []


def test_runner_chat_mutates_callers_list_in_place(make_runner):
    """Interactive callers keep one list across exchanges — a runner that
    rebinds it forks the transcript, and every later exchange runs without
    the history (the say-the-same-thing-forever loop)."""
    r = make_runner([
        assistant(calls=[tc("plan", {"steps": [{"title": "One", "files": ["f.py"]}]})]),
        assistant(calls=[tc("plan_update", {"step": 1, "status": "done"})]),
        assistant(calls=[tc("done", {"summary": "shipped"})]),
    ])
    msgs = []
    result = r.chat(msgs, "do the thing")
    assert result.messages is msgs
    assert sum(1 for m in msgs if m.role == "assistant") == 3  # nothing lost


def test_runner_plan_aware_explore_nudge(make_runner):
    reads = [
        assistant(f"look {i}", calls=[tc("read", {"path": "f.py" if i % 2 else "g.py"}, id=f"r{i}")])
        for i in range(5)
    ]
    r = make_runner([
        assistant(calls=[tc("plan", {"steps": [{"title": "One", "files": ["f.py"]}]})]),
        *reads,
        assistant(calls=[tc("plan_update", {"step": 1, "status": "done"})]),
        assistant(calls=[tc("done", {"summary": "ok"})]),
    ])
    result = r.run("task")
    assert result.status == "done"
    nudges = [
        m for m in result.messages
        if m.role == "user" and "Edit or write its listed files NOW" in (m.content or "")
    ]
    assert len(nudges) == 1
    assert "step 1: One" in nudges[0].content and "f.py" in nudges[0].content
