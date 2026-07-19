import json

import pytest

from mha.harnesses.code import code_harness_tools
from mha.tools import BashTool, DoneTool, EditTool, ReadTool, WriteTool
from mha.tools.base import ToolContext
from mha.tools.bash import check_command


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    print('hello')\n")
    return tmp_path


@pytest.fixture
def ctx(repo):
    events = []
    c = ToolContext(repo_root=repo, record=lambda t, d: events.append((t, d)))
    c.events = events
    return c


# --- read ---

def test_read(ctx):
    r = ReadTool().execute({"path": "src/app.py"}, ctx)
    assert "def main():" in r.output
    assert not r.is_error


def test_read_missing(ctx):
    r = ReadTool().execute({"path": "nope.py"}, ctx)
    assert r.is_error
    assert "not found" in r.output


def test_read_offset_limit(ctx):
    r = ReadTool().execute({"path": "src/app.py", "offset": 2, "limit": 1}, ctx)
    assert "print" in r.output
    assert "def main" not in r.output


def test_read_escape_blocked(ctx):
    r = ReadTool().execute({"path": "../../etc/passwd"}, ctx)
    assert r.is_error
    assert "escapes" in r.output


# --- edit ---

def test_edit_unique(ctx, repo):
    r = EditTool().execute({"path": "src/app.py", "old_text": "'hello'", "new_text": "'world'"}, ctx)
    assert not r.is_error
    assert "'world'" in (repo / "src" / "app.py").read_text()


def test_edit_not_found(ctx):
    r = EditTool().execute({"path": "src/app.py", "old_text": "nonexistent", "new_text": "x"}, ctx)
    assert r.is_error
    assert "not found" in r.output


def test_edit_ambiguous(ctx, repo):
    (repo / "src" / "app.py").write_text("x = 1\nx = 1\n")
    r = EditTool().execute({"path": "src/app.py", "old_text": "x = 1", "new_text": "x = 2"}, ctx)
    assert r.is_error
    assert "2 times" in r.output


# --- write ---

def test_write_creates_dirs(ctx, repo):
    r = WriteTool().execute({"path": "new/dir/f.txt", "content": "hi"}, ctx)
    assert not r.is_error
    assert (repo / "new" / "dir" / "f.txt").read_text() == "hi"


# --- bash allowlist ---

ALLOWED = [
    "rg 'def main' src/",
    "ls -la src",
    "cat src/app.py | head -5",
    "git status && git log --oneline",
    "git diff HEAD~1",
    "pytest tests/ -q",
    "python -m pytest tests/",
    "npm test",
    "go test ./...",
    "make test",
    "ruff check src/",
    "find . -name '*.py' | xargs wc -l",
    # quoted operators must not split the segment or trip the redirect check
    r'grep -rn "foo\|bar" src/',
    'grep "->" src/app.py',
    "cd src && ls",
    "pytest -q 2>&1 | tail -20",
]

REJECTED = [
    "rm -rf /",
    "git push origin main",
    "git commit -m x",
    "python evil.py",
    'python -c "import os"',
    "curl http://example.com",
    "echo hi > file.txt",
    "sed -i '' 's/a/b/' f.py",
    "npm install leftpad",
    "pip install requests",
    "ls && rm -rf /",  # every segment is checked, not just the first
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_bash_allowed(cmd):
    assert check_command(cmd, ("search", "test", "lint", "git_read")) is None, cmd


@pytest.mark.parametrize("cmd", REJECTED)
def test_bash_rejected(cmd):
    assert check_command(cmd, ("search", "test", "lint", "git_read")) is not None, cmd


def test_bash_runs_and_captures(ctx):
    r = BashTool().execute({"command": "ls src"}, ctx)
    assert "app.py" in r.output
    assert r.details["exit_code"] == 0


def test_bash_rejection_is_loud_and_logged(ctx):
    r = BashTool().execute({"command": "rm -rf src"}, ctx)
    assert r.is_error
    assert "Allowed command categories" in r.output  # names what IS allowed
    assert any(t == "bash_rejected" for t, _ in ctx.events)


def test_bash_per_harness_categories(ctx):
    # Architect-style harness: search only, no test runners
    assert check_command("pytest -q", ("search",)) is not None


# --- done ---

def test_done(ctx):
    r = DoneTool().execute({"summary": "fixed the bug"}, ctx)
    assert r.details["done"] is True
    assert r.output == "fixed the bug"


# --- schema budget (decision #6: all schemas < ~1200 tokens) ---

def test_all_schemas_under_token_budget():
    tools = code_harness_tools(with_kg=True)
    assert len(tools) == 11
    wire = json.dumps([t.spec().to_openai() for t in tools])
    approx_tokens = len(wire) / 4
    # 1600 covers the 11-tool toolset including WebSearch + WebFetch + skill
    # (~100 tokens added when web tools landed; skill is tiny). The /4
    # heuristic is loose; the guardrail exists to keep per-turn schema
    # overhead from eating small-model context windows, not as a hard wall.
    assert approx_tokens < 1600, f"schemas ≈ {approx_tokens:.0f} tokens, budget is 1600"


def test_control_arm_has_no_kg_query():
    names = [t.name for t in code_harness_tools(with_kg=False)]
    assert "kg_query" not in names
    assert names == [
        "read", "edit", "write", "bash",
        "WebSearch", "WebFetch",
        "plan", "plan_update", "skill", "done",
    ]


def test_offline_control_arm_strips_web_too():
    # `with_web=False` is the offline-eval knob (no network egress at all).
    names = [t.name for t in code_harness_tools(with_kg=False, with_web=False)]
    assert "WebSearch" not in names
    assert "WebFetch" not in names
    assert "kg_query" not in names
    assert names == ["read", "edit", "write", "bash", "plan", "plan_update", "skill", "done"]
