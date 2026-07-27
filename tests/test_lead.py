"""The lead layer: harness registry, the arch->code seed seam, the lead's
dispatch tools, and one real end-to-end (lead -> architect -> code)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ox.engine.runner import Runner
from ox.engine.session import SessionRecorder
from ox.harnesses import handoff, registry
from ox.harnesses.lead import lead_harness_tools
from ox.harnesses.lead.tools import ArchitectTool, CodeTool
from ox.llm.registry import ModelSpec, ProviderConfig, Registry
from ox.llm.types import LLMResponse, Message, ToolCall, Usage
from ox.tools import ToolContext

SPEC = ModelSpec(
    spec="fake:model",
    provider=ProviderConfig(name="fake", base_url="http://x"),
    model="model",
    context_window=200000,
)
REG = Registry(
    providers={"fake": ProviderConfig(name="fake", base_url="http://x")},
    models={},
    aliases={"default": "fake:model", "architect": "fake:model"},
)


def tc(name, args, id=None):
    return ToolCall(id=id or f"c{abs(hash((name, json.dumps(args)))) % 10000}",
                    name=name, arguments=args, arguments_json=json.dumps(args))


def assistant(content=None, calls=()):
    return Message(role="assistant", content=content, tool_calls=list(calls))


# --------------------------------------------------------------- registry

def test_registry_tunes_each_harness():
    code = registry.get("code")
    arch = registry.get("arch")
    lead = registry.get("lead")
    assert code.interactive is False and arch.interactive is True
    assert arch.default_model == "architect"
    # lead's progress = dispatching, and it gets its own explore nudge
    assert lead.mutating_tools == frozenset({"architect", "code"})
    assert "architect" in lead.explore_nudge and "edit" not in lead.explore_nudge
    # instructions differ per harness
    assert code.instructions_path != arch.instructions_path != lead.instructions_path


def test_build_runner_applies_def_tuning():
    ctx = ToolContext(repo_root=".", registry=REG)
    r = registry.build_runner("arch", spec=SPEC, client=None, registry=REG, ctx=ctx)
    # arch tuning flowed through: its instructions, its mutating set, its tracker
    assert r.instructions_path == registry.get("arch").instructions_path
    assert "brief" in r.mutating_tools
    assert {t for t in r.tools} >= {"brief", "component", "done"}


def test_unknown_harness_raises():
    with pytest.raises(KeyError):
        registry.get("nope")


# --------------------------------------------------------------- seed seam

class CapturingClient:
    """Records the messages it was handed, then replies with `done`."""

    def __init__(self):
        self.seen = []

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
        self.seen = list(messages)
        return LLMResponse(
            message=assistant(calls=[tc("done", {"summary": "ok"})]),
            usage=Usage(1, 1), stop_reason="stop", model=spec.spec,
        )


def test_seed_context_lands_in_system_prompt(tmp_path):
    client = CapturingClient()
    ctx = ToolContext(repo_root=tmp_path, registry=REG)
    r = registry.build_runner(
        "code", spec=SPEC, client=client, registry=REG, ctx=ctx,
        with_kg=False, with_web=False, seed_context="SEED-MARKER-1234",
    )
    r.run("do a thing")
    system = client.seen[0]
    assert system.role == "system"
    assert "SEED-MARKER-1234" in system.content
    # it's in messages[:2], which compaction always preserves -> compaction-safe
    assert client.seen[1].content.startswith("Task:")


# --------------------------------------------------------------- handoff

def _write_bundle(root, run_name, text="# Architecture\n\ncontent"):
    d = root / ".ox" / "sessions" / run_name / "bundle"
    d.mkdir(parents=True)
    (d / "architecture.md").write_text(text)
    return d.parent


def test_find_bundle_by_id_and_latest(tmp_path):
    a = _write_bundle(tmp_path, "arch-aaa", "# A")
    import os, time
    time.sleep(0.01)
    b = _write_bundle(tmp_path, "arch-bbb", "# B")
    # exact / prefix match by id
    assert handoff.find_bundle_dir(tmp_path, "arch-aaa") == a
    assert handoff.find_bundle_dir(tmp_path, "arch-bbb") == b
    # 'latest' picks the most recently written bundle
    assert handoff.find_bundle_dir(tmp_path, "latest") == b
    # a session dir without a bundle is not matched
    (tmp_path / ".ox" / "sessions" / "arch-empty").mkdir()
    assert handoff.find_bundle_dir(tmp_path, "arch-empty") is None


def test_read_seed_wraps_with_header(tmp_path):
    _write_bundle(tmp_path, "arch-xyz", "# My Design\n\nstuff")
    seed = handoff.read_seed(tmp_path, "latest")
    assert seed is not None
    assert "Architecture handoff" in seed  # the instruction header
    assert "# My Design" in seed
    assert handoff.read_seed(tmp_path, "does-not-exist") is None


# --------------------------------------------------------------- code tool

def test_code_tool_seeds_and_forks_ctx(tmp_path, monkeypatch):
    captured = {}

    def fake_build_runner(name, *, spec, client, registry, ctx, **kw):
        captured["name"] = name
        captured["seed"] = kw.get("seed_context")
        captured["child"] = ctx
        return SimpleNamespace(run=lambda task: SimpleNamespace(
            status="done", summary="built", turns=1))

    monkeypatch.setattr("ox.harnesses.registry.build_runner", fake_build_runner)
    ctx = ToolContext(repo_root=tmp_path, registry=REG, run_dir=tmp_path,
                      plan="PARENT_PLAN", last_bundle="THE-DESIGN")
    res = CodeTool().run({"task": "build it"}, ctx)

    assert captured["name"] == "code"
    assert captured["seed"] == "THE-DESIGN"          # seeded from the stash
    assert captured["child"].plan is None            # forked: sub-session's own plan
    assert captured["child"].last_bundle is None
    assert ctx.plan == "PARENT_PLAN"                 # parent ctx untouched
    assert ctx.last_bundle is None                   # bundle consumed once
    assert "[done] built" in res.output
    assert res.details["seeded"] is True


# ------------------------------------------------------------- architect tool

def _fake_arch_session(phase, run_dir=None, comps=("gw", "db")):
    state = SimpleNamespace(
        phase=phase,
        components={c: c for c in comps},
        brief=SimpleNamespace(goal="a url shortener"),
    )
    return SimpleNamespace(state=state, run_dir=run_dir)


def test_architect_tool_stashes_bundle_on_finalize(tmp_path, monkeypatch):
    def fake_interactive(*, run_dir, **kw):
        (run_dir / "bundle").mkdir(parents=True)
        (run_dir / "bundle" / "architecture.md").write_text("# Shortener\n\ndesign")
        return _fake_arch_session("finalized", run_dir)

    monkeypatch.setattr("ox.harnesses.arch.run.run_arch_interactive", fake_interactive)
    ctx = ToolContext(repo_root=tmp_path, registry=REG, run_dir=tmp_path)
    res = ArchitectTool().run({"task": "shorten urls"}, ctx)

    assert ctx.last_bundle is not None
    assert "# Shortener" in ctx.last_bundle
    assert "Architecture handoff" in ctx.last_bundle  # wrapped for the code session
    assert "finalized" in res.output.lower()
    assert res.details["components"] == ["gw", "db"]


def test_architect_always_uses_interactive_workbench(tmp_path, monkeypatch):
    calls = []

    def make_fake(kind):
        def fake(*, run_dir, **kw):
            calls.append(kind)
            (run_dir / "bundle").mkdir(parents=True)
            (run_dir / "bundle" / "architecture.md").write_text("# d")
            return _fake_arch_session("finalized", run_dir)
        return fake

    monkeypatch.setattr("ox.harnesses.arch.run.run_arch_interactive", make_fake("interactive"))
    monkeypatch.setattr("ox.harnesses.arch.run.run_arch_headless", make_fake("headless"))

    # There is no auto path: architecture must ALWAYS open the interactive
    # Workbench (browser + human gates), never the headless auto-approve walk.
    ArchitectTool().run({"task": "x"}, ToolContext(repo_root=tmp_path, registry=REG, run_dir=tmp_path))
    assert calls == ["interactive"]


def test_architect_tool_errors_if_not_finalized(tmp_path, monkeypatch):
    from ox.tools import ToolError

    monkeypatch.setattr(
        "ox.harnesses.arch.run.run_arch_interactive",
        lambda **kw: _fake_arch_session("expand"),
    )
    ctx = ToolContext(repo_root=tmp_path, registry=REG, run_dir=tmp_path)
    with pytest.raises(ToolError, match="not finalized"):
        ArchitectTool().run({"task": "x"}, ctx)
    assert ctx.last_bundle is None  # nothing handed off


# ---------------------------------------------------------- arch headless walk

ARCH_WALK = [
    assistant(calls=[tc("brief", {"goal": "shorten urls", "actors": ["visitor"],
                                  "scope": "internal"})]),
    assistant(calls=[
        tc("component", {"id": "gw", "kind": "gateway", "responsibility": "http entry",
                         "trace": ["shorten urls"]}),
        tc("component", {"id": "db", "kind": "store", "responsibility": "url mappings",
                         "trace": ["shorten urls"], "data_owned": "short->long map"}),
        tc("connect", {"src": "gw", "dst": "db", "label": "lookup", "kind": "sync"}),
        tc("flow", {"id": "shorten", "name": "shorten", "kind": "happy",
                    "steps": [{"src": "gw", "dst": "db", "action": "INSERT"}]}),
        tc("decide", {"topic": "Storage", "category": "storage",
                      "options": [{"name": "sqlite"}, {"name": "postgres"}],
                      "choice": "sqlite", "rationale": "single box"}),
    ]),
    assistant(calls=[tc("done", {"summary": "top level ready"})]),
    assistant(calls=[tc("expand", {"component_id": "db",
                                   "entities": [{"name": "urls", "keys": "short"}],
                                   "access_patterns": ["short -> long"],
                                   "retention": "forever"})]),
    assistant(calls=[tc("expand", {"component_id": "gw",
                                   "endpoints": [{"route": "/s", "method": "POST",
                                                  "request": "{url}", "response": "{short}",
                                                  "auth": "none"}]})]),
    assistant(calls=[tc("done", {"summary": "finalize"})]),
]


class ScriptClient:
    def __init__(self, script):
        self.script = list(script)

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
        return LLMResponse(message=self.script.pop(0), usage=Usage(10, 5),
                           stop_reason="stop", model=spec.spec)


def test_arch_headless_reaches_finalized(tmp_path):
    from ox.harnesses.arch.run import run_arch_headless

    run_dir = tmp_path / ".ox" / "sessions" / "arch-h"
    arch = run_arch_headless(
        repo_root=tmp_path, task="design a url shortener", registry=REG,
        client=ScriptClient(ARCH_WALK), run_dir=run_dir,
    )
    assert arch.state.phase == "finalized"
    assert (run_dir / "bundle" / "architecture.md").is_file()
    assert (run_dir / "bundle" / "architecture.json").is_file()


# ------------------------------------------------------------- end to end

class RoutingClient:
    """One client for the whole tree — routes by which harness's tools it sees."""

    def __init__(self, lead, arch, code):
        self.q = {"lead": list(lead), "arch": list(arch), "code": list(code)}

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
        names = {t.name for t in (tools or [])}
        if "brief" in names:
            key = "arch"
        elif "edit" in names or "write" in names:
            key = "code"
        else:
            key = "lead"
        return LLMResponse(message=self.q[key].pop(0), usage=Usage(10, 5),
                           stop_reason="stop", model=spec.spec)


def test_lead_end_to_end(tmp_path, monkeypatch):
    # architect ALWAYS opens the interactive Workbench in the product; for a
    # browserless test, delegate that to the headless walk so the scripted
    # client can drive the arch session to finalized.
    from ox.harnesses.arch.run import run_arch_headless

    monkeypatch.setattr("ox.harnesses.arch.run.run_arch_interactive",
                        lambda *, on_status=None, **kw: run_arch_headless(**kw))

    events = []
    run_dir = tmp_path / ".ox" / "sessions" / "lead-e2e"
    recorder = SessionRecorder(run_dir)

    lead_script = [
        assistant(calls=[tc("architect", {"task": "build a url shortener with analytics"})]),
        assistant(calls=[tc("code", {"task": "build the url shortener per the architecture"})]),
        assistant(calls=[tc("done", {"summary": "architected and built the url shortener"})]),
    ]
    code_script = [assistant(calls=[tc("done", {"summary": "built the url shortener"})])]
    client = RoutingClient(lead_script, ARCH_WALK, code_script)

    ctx = ToolContext(
        repo_root=tmp_path, record=lambda t, d: events.append((t, d)),
        client=client, registry=REG, run_dir=run_dir,
    )
    runner = registry.build_runner(
        "lead", spec=SPEC, client=client, registry=REG, ctx=ctx,
        tools=lead_harness_tools(with_kg=False, with_web=False),
    )
    result = runner.run("build me a url shortener with click analytics")

    assert result.status == "done"
    dispatches = [d for t, d in events if t == "dispatch"]
    kinds = [d["harness"] for d in dispatches]
    assert kinds == ["arch", "code"]                      # order enforced
    # the arch sub-session actually finalized and wrote a bundle
    arch_dir = Path(dispatches[0]["run_dir"])
    assert (arch_dir / "bundle" / "architecture.md").is_file()
    # and code was dispatched WITH the design seeded in
    assert dispatches[1]["seeded"] is True
