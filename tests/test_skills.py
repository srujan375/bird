"""Tests for the skills system: loader, SkillTool, and REPL slash dispatch."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from bird.skills import Skill, is_valid_skill_name, load_skills, render_index
from bird.harnesses.code import code_harness_tools
from bird.tools.base import ToolContext
from bird.tools.skill import SkillTool


# --- helpers ---

def _write_skill(d: Path, name: str, description: str, body: str) -> Path:
    """Write a skill file. `d` should be the skills directory (e.g. repo/.bird/skills)."""
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(f"name: {name}\ndescription: {description}\n\n{body}", encoding="utf-8")
    return p


# --- loader: parsing ---

def test_parse_skill_front_matter(tmp_path):
    _write_skill(tmp_path / ".bird" / "skills", "commit-style", "Use when writing commits", "Be concise.")
    skills = [s for s in load_skills(tmp_path) if s.source == "project"]
    assert len(skills) == 1
    sk = skills[0]
    assert sk.name == "commit-style"
    assert sk.description == "Use when writing commits"
    assert sk.body == "Be concise."
    assert sk.source == "project"


def test_parse_skill_no_description(tmp_path):
    """A skill without a description still loads (empty description)."""
    d = tmp_path / ".bird" / "skills"
    d.mkdir(parents=True)
    (d / "bare.md").write_text("name: bare\n\nDo the thing.", encoding="utf-8")
    skills = [s for s in load_skills(tmp_path) if s.source == "project"]
    assert len(skills) == 1
    assert skills[0].name == "bare"
    assert skills[0].description == ""
    assert skills[0].body == "Do the thing."


def test_empty_body_skipped(tmp_path):
    """A file with no body is silently skipped, not an error."""
    d = tmp_path / ".bird" / "skills"
    d.mkdir(parents=True)
    (d / "empty.md").write_text("name: empty\ndescription: nothing here\n\n", encoding="utf-8")
    skills = [s for s in load_skills(tmp_path) if s.source == "project"]
    assert len(skills) == 0


def test_name_derived_from_filename(tmp_path):
    """No front-matter at all → name comes from the filename stem."""
    d = tmp_path / ".bird" / "skills"
    d.mkdir(parents=True)
    (d / "auto-named.md").write_text("Just a body, no front-matter.", encoding="utf-8")
    skills = [s for s in load_skills(tmp_path) if s.source == "project"]
    assert len(skills) == 1
    assert skills[0].name == "auto-named"


# --- loader: precedence ---

def test_project_overrides_user(tmp_path, monkeypatch):
    """Project skills override user skills by name."""
    _write_skill(tmp_path / ".bird" / "skills", "shared", "project version", "project body")
    user_dir = tmp_path / "home" / ".bird" / "skills"
    _write_skill(user_dir, "shared", "user version", "user body")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    skills = load_skills(tmp_path)
    by_name = {s.name: s for s in skills}
    assert by_name["shared"].source == "project"
    assert by_name["shared"].body == "project body"


def test_user_and_project_coexist(tmp_path, monkeypatch):
    """Different names from both sources all appear."""
    _write_skill(tmp_path / ".bird" / "skills", "proj-only", "p", "proj body")
    user_dir = tmp_path / "home" / ".bird" / "skills"
    _write_skill(user_dir, "user-only", "u", "user body")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    skills = load_skills(tmp_path)
    names = {s.name for s in skills}
    assert "proj-only" in names
    assert "user-only" in names


# --- loader: built-in ---

def test_builtin_skill_creator_loaded(tmp_path):
    """The skill-creator built-in skill ships with bird and is always available."""
    skills = load_skills(tmp_path)
    names = {s.name for s in skills}
    assert "skill-creator" in names
    sk = next(s for s in skills if s.name == "skill-creator")
    assert sk.source == "builtin"
    assert "skill" in sk.body.lower()


def test_project_overrides_builtin(tmp_path):
    """A project skill named 'skill-creator' overrides the built-in."""
    _write_skill(tmp_path / ".bird" / "skills", "skill-creator", "custom", "my version")
    skills = load_skills(tmp_path)
    sk = next(s for s in skills if s.name == "skill-creator")
    assert sk.source == "project"
    assert sk.body == "my version"


# --- render_index ---

def test_render_index_empty():
    assert render_index([]) == ""


def test_render_index_lists_skills():
    skills = [
        Skill(name="a", description="desc a", body="", path=Path("a"), source="project"),
        Skill(name="b", description="desc b", body="", path=Path("b"), source="user"),
    ]
    idx = render_index(skills)
    assert "[skills]" in idx
    assert "- a: desc a" in idx
    assert "- b: desc b" in idx


# --- is_valid_skill_name ---

def test_valid_skill_names():
    assert is_valid_skill_name("commit-style")
    assert is_valid_skill_name("a")
    assert is_valid_skill_name("abc123")
    assert is_valid_skill_name("flaky-test-2")


def test_invalid_skill_names():
    assert not is_valid_skill_name("Commit-Style")  # uppercase
    assert not is_valid_skill_name("-leading")  # leading hyphen
    assert not is_valid_skill_name("trailing-")  # trailing hyphen
    assert not is_valid_skill_name("double--hyphen")
    assert not is_valid_skill_name("has_underscore")
    assert not is_valid_skill_name("has space")


# --- SkillTool ---

def _ctx_with_skills(repo, skills):
    events = []
    c = ToolContext(repo_root=repo, record=lambda t, d: events.append((t, d)), skills=skills)
    c.events = events
    return c


def test_skill_tool_loads_existing(tmp_path):
    skills = [Skill(name="commit-style", description="d", body="Be concise.", path=Path("x"), source="project")]
    ctx = _ctx_with_skills(tmp_path, skills)
    r = SkillTool().execute({"name": "commit-style"}, ctx)
    assert not r.is_error
    assert r.output == "Be concise."
    assert r.details["source"] == "project"
    assert ("skill_loaded", {"name": "commit-style", "source": "project"}) in ctx.events


def test_skill_tool_miss_lists_available(tmp_path):
    skills = [Skill(name="a", description="d", body="body a", path=Path("x"), source="project")]
    ctx = _ctx_with_skills(tmp_path, skills)
    r = SkillTool().execute({"name": "nonexistent"}, ctx)
    assert r.is_error
    assert "nonexistent" in r.output
    assert "a" in r.output  # lists available


def test_skill_tool_no_skills(tmp_path):
    ctx = _ctx_with_skills(tmp_path, None)
    r = SkillTool().execute({"name": "anything"}, ctx)
    assert not r.is_error
    assert "No skills" in r.output


# --- tool registration ---

def test_skill_tool_in_harness():
    names = [t.name for t in code_harness_tools(with_kg=True)]
    assert "skill" in names


def test_skill_tool_in_control_arm():
    names = [t.name for t in code_harness_tools(with_kg=False)]
    assert "skill" in names  # skill stays even without KG


# --- token budget ---

def test_all_schemas_under_token_budget():
    from .test_tools import SCHEMA_TOKEN_BUDGET

    tools = code_harness_tools(with_kg=True)
    assert len(tools) == 12
    wire = json.dumps([t.spec().to_openai() for t in tools])
    approx_tokens = len(wire) / 4
    assert approx_tokens < SCHEMA_TOKEN_BUDGET, (
        f"schemas ≈ {approx_tokens:.0f} tokens, budget is {SCHEMA_TOKEN_BUDGET}"
    )


# --- REPL completer ---

def test_repl_completer_builtin_commands(tmp_path):
    """The readline completer returns built-in /commands."""
    from bird.repl import Repl
    from bird.engine.runner import Runner
    from bird.engine.session import SessionRecorder
    from bird.llm.registry import ModelSpec, ProviderConfig, Registry
    from bird.llm.types import Message, Usage, LLMResponse
    from bird.llm.wire.openai_compat import OpenAICompatClient

    class FakeClient:
        def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
            return LLMResponse(message=Message(role="assistant", content="ok"), usage=Usage(0, 0), stop_reason="stop", model=spec.spec)

    recorder = SessionRecorder(tmp_path / ".bird" / "sessions" / "t")
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event, skills=[])
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    spec = ModelSpec(spec="fake:model", provider=ProviderConfig(name="fake", base_url="http://x"), model="model", context_window=32768)
    runner = Runner(spec=spec, client=FakeClient(), registry=registry, tools=code_harness_tools(with_kg=False), ctx=ctx)
    repl = Repl(runner, registry, kg=None, recorder=recorder, run_id="t")

    # Build the completer function the same way _setup_completion does
    skills = repl.runner.ctx.skills or []
    def completer(text, state):
        if not text.startswith("/"):
            return None
        candidates = list(repl.BUILTIN_COMMANDS)
        candidates.extend(f"/{s.name}" for s in skills)
        matches = sorted(c for c in candidates if c.startswith(text))
        return matches[state] if state < len(matches) else None

    # /mod should match /model
    matches = []
    i = 0
    while True:
        m = completer("/mod", i)
        if m is None:
            break
        matches.append(m)
        i += 1
    assert "/model" in matches

    # /h should match /help
    matches = []
    i = 0
    while True:
        m = completer("/h", i)
        if m is None:
            break
        matches.append(m)
        i += 1
    assert "/help" in matches


def test_repl_completer_includes_skills(tmp_path):
    """The readline completer includes /<skill-name> alongside built-ins."""
    from bird.repl import Repl
    from bird.engine.runner import Runner
    from bird.engine.session import SessionRecorder
    from bird.llm.registry import ModelSpec, ProviderConfig, Registry
    from bird.llm.types import Message, Usage, LLMResponse

    class FakeClient:
        def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
            return LLMResponse(message=Message(role="assistant", content="ok"), usage=Usage(0, 0), stop_reason="stop", model=spec.spec)

    skills = [
        Skill(name="commit-style", description="d", body="b", path=Path("x"), source="project"),
        Skill(name="flaky-test", description="d", body="b", path=Path("y"), source="user"),
    ]
    recorder = SessionRecorder(tmp_path / ".bird" / "sessions" / "t")
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event, skills=skills)
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    spec = ModelSpec(spec="fake:model", provider=ProviderConfig(name="fake", base_url="http://x"), model="model", context_window=32768)
    runner = Runner(spec=spec, client=FakeClient(), registry=registry, tools=code_harness_tools(with_kg=False), ctx=ctx)
    repl = Repl(runner, registry, kg=None, recorder=recorder, run_id="t")

    def completer(text, state):
        if not text.startswith("/"):
            return None
        candidates = list(repl.BUILTIN_COMMANDS)
        candidates.extend(f"/{s.name}" for s in (repl.runner.ctx.skills or []))
        matches = sorted(c for c in candidates if c.startswith(text))
        return matches[state] if state < len(matches) else None

    # /commit should match /commit-style
    matches = []
    i = 0
    while True:
        m = completer("/commit", i)
        if m is None:
            break
        matches.append(m)
        i += 1
    assert "/commit-style" in matches

    # /flaky should match /flaky-test
    matches = []
    i = 0
    while True:
        m = completer("/flaky", i)
        if m is None:
            break
        matches.append(m)
        i += 1
    assert "/flaky-test" in matches

    # /s should match both /skills (built-in) and /session, /sessions
    matches = []
    i = 0
    while True:
        m = completer("/s", i)
        if m is None:
            break
        matches.append(m)
        i += 1
    assert "/skills" in matches
    assert "/session" in matches
    assert "/sessions" in matches


def test_repl_completer_non_slash_returns_none(tmp_path):
    """Non-slash input gets no completion."""
    from bird.repl import Repl
    from bird.engine.runner import Runner
    from bird.engine.session import SessionRecorder
    from bird.llm.registry import ModelSpec, ProviderConfig, Registry
    from bird.llm.types import Message, Usage, LLMResponse

    class FakeClient:
        def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
            return LLMResponse(message=Message(role="assistant", content="ok"), usage=Usage(0, 0), stop_reason="stop", model=spec.spec)

    recorder = SessionRecorder(tmp_path / ".bird" / "sessions" / "t")
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event, skills=[])
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    spec = ModelSpec(spec="fake:model", provider=ProviderConfig(name="fake", base_url="http://x"), model="model", context_window=32768)
    runner = Runner(spec=spec, client=FakeClient(), registry=registry, tools=code_harness_tools(with_kg=False), ctx=ctx)
    repl = Repl(runner, registry, kg=None, recorder=recorder, run_id="t")

    def completer(text, state):
        if not text.startswith("/"):
            return None
        candidates = list(repl.BUILTIN_COMMANDS)
        candidates.extend(f"/{s.name}" for s in (repl.runner.ctx.skills or []))
        matches = sorted(c for c in candidates if c.startswith(text))
        return matches[state] if state < len(matches) else None

    assert completer("hello", 0) is None
    assert completer("fix the bug", 0) is None


# --- serve ready event includes skills ---

def test_serve_ready_includes_skills(monkeypatch, tmp_path):
    """The 'ready' event from bird serve includes the skills list."""
    import json
    import queue
    import threading
    import time

    from bird.engine.runner import Runner
    from bird.engine.session import SessionRecorder
    from bird.llm.registry import ModelSpec, ProviderConfig, Registry
    from bird.llm.types import LLMResponse, Message, Usage
    from bird.repl import Repl
    from bird.serve import Server

    class FakeClient:
        def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
            return LLMResponse(message=Message(role="assistant", content="ok"), usage=Usage(0, 0), stop_reason="stop", model=spec.spec)

    skills = [
        Skill(name="commit-style", description="Use when writing commits", body="be concise", path=Path("x"), source="project"),
    ]
    recorder = SessionRecorder(tmp_path / ".bird" / "sessions" / "t")
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event, skills=skills)
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    spec = ModelSpec(spec="fake:model", provider=ProviderConfig(name="fake", base_url="http://x"), model="model", context_window=32768)
    runner = Runner(spec=spec, client=FakeClient(), registry=registry, tools=code_harness_tools(with_kg=False), ctx=ctx)
    repl = Repl(runner, registry, kg=None, recorder=recorder, run_id="t")

    class Feeder:
        def __init__(self):
            self.q = queue.Queue()
        def put(self, obj):
            self.q.put(json.dumps(obj) + "\n")
        def close(self):
            self.q.put(None)
        def __iter__(self):
            return self
        def __next__(self):
            item = self.q.get()
            if item is None:
                raise StopIteration
            return item

    class Out:
        def __init__(self):
            self.msgs = []
            self.cv = threading.Condition()
        def write(self, s):
            s = s.strip()
            if not s:
                return
            with self.cv:
                self.msgs.append(json.loads(s))
                self.cv.notify_all()
        def flush(self):
            pass
        def wait_for(self, type_, timeout=5.0):
            deadline = time.time() + timeout
            with self.cv:
                while True:
                    for m in self.msgs:
                        if m["type"] == type_:
                            return m
                    remaining = deadline - time.time()
                    assert remaining > 0, f"timed out waiting for {type_}; got {self.msgs}"
                    self.cv.wait(remaining)

    feeder, out = Feeder(), Out()
    monkeypatch.setattr("sys.stdin", feeder)
    monkeypatch.setattr("sys.stdout", out)
    server = Server(repl)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    ready = out.wait_for("ready")
    assert "skills" in ready
    assert len(ready["skills"]) >= 1
    skill_names = [s["name"] for s in ready["skills"]]
    assert "commit-style" in skill_names
    # verify the structure of a skill entry
    commit_skill = next(s for s in ready["skills"] if s["name"] == "commit-style")
    assert commit_skill["description"] == "Use when writing commits"
    assert commit_skill["source"] == "project"

    feeder.close()
    thread.join(timeout=5)