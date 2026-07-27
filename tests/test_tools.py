import json

import pytest

from ox.harnesses.code import code_harness_tools
from ox.tools import BashTool, DoneTool, EditTool, ReadImageTool, ReadTool, WriteTool
from ox.tools.base import ToolContext
from ox.tools.bash import check_command


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


# --- read image nudge ---

def test_read_nudges_raster_image_to_read_image(ctx, repo):
    # a real PNG header so _detect_image_mime's magic-byte sniff fires even
    # without an extension
    (repo / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    r = ReadTool().execute({"path": "shot.png"}, ctx)
    assert not r.is_error
    assert "read_image" in r.output
    assert "image" in r.output.lower()
    assert r.details["nudge"] == "read_image"


def test_read_treats_svg_as_text(ctx, repo):
    (repo / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    r = ReadTool().execute({"path": "logo.svg"}, ctx)
    assert not r.is_error
    assert "<svg" in r.output  # read as text, not nudged
    assert "read_image" not in r.output


# --- read_image ---

class _FakeVisionClient:
    """Captures the messages sent to the vision model and returns a canned
    text description. Stands in for OpenAICompatClient.complete()."""

    def __init__(self, description="a red square", error=None):
        self._description = description
        self._error = error
        self.calls = []

    def complete(self, spec, messages, tools=None, **kw):
        self.calls.append({"spec": spec, "messages": messages, "tools": tools})
        if self._error is not None:
            raise self._error
        from ox.llm.types import LLMResponse, Message, Usage
        return LLMResponse(
            message=Message(role="assistant", content=self._description),
            usage=Usage(),
            stop_reason="stop",
            model=spec.spec,
        )


class _FakeRegistry:
    def __init__(self, spec_str="ollama:llava:7b"):
        from ox.llm.registry import ModelSpec, ProviderConfig
        self._spec = ModelSpec(
            spec=spec_str,
            provider=ProviderConfig(name="ollama", base_url="http://x"),
            model=spec_str.split(":", 1)[1],
        )

    def resolve(self, name):
        if name != "vision":
            from ox.llm.registry import RegistryError
            raise RegistryError(f"unknown alias {name}")
        return self._spec


def _vision_ctx(repo, client=None, registry=None):
    events = []
    c = ToolContext(
        repo_root=repo,
        record=lambda t, d: events.append((t, d)),
        client=client,
        registry=registry,
    )
    c.events = events
    return c


def test_read_image_happy_path(ctx, repo):
    client = _FakeVisionClient(description="a red square on white")
    vctx = _vision_ctx(repo, client=client, registry=_FakeRegistry())
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    r = ReadImageTool().execute({"path": "pic.png"}, vctx)
    assert not r.is_error
    assert r.output == "a red square on white"
    assert r.details["source"] == "pic.png"
    assert r.details["mime"] == "image/png"
    assert r.details["vision_model"] == "ollama:llava:7b"
    # the vision call passed no tools (single-turn describer, not an agent)
    assert client.calls[0]["tools"] is None
    # the message sent to the vision model was content-parts (text + image)
    sent = client.calls[0]["messages"][0]
    assert isinstance(sent.content, list)
    assert sent.content[0].text == "describe this image in detail"
    assert sent.content[1].type == "image_url"
    assert sent.content[1].image_url["url"].startswith("data:image/png;base64,")


def test_read_image_custom_question(ctx, repo):
    client = _FakeVisionClient()
    vctx = _vision_ctx(repo, client=client, registry=_FakeRegistry())
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    ReadImageTool().execute({"path": "pic.png", "question": "what color?"}, vctx)
    assert client.calls[0]["messages"][0].content[0].text == "what color?"


def test_read_image_missing_file(ctx, repo):
    vctx = _vision_ctx(repo, client=_FakeVisionClient(), registry=_FakeRegistry())
    r = ReadImageTool().execute({"path": "nope.png"}, vctx)
    assert r.is_error
    assert "not found" in r.output


def test_read_image_refuses_svg(ctx, repo):
    vctx = _vision_ctx(repo, client=_FakeVisionClient(), registry=_FakeRegistry())
    (repo / "logo.svg").write_text("<svg></svg>")
    r = ReadImageTool().execute({"path": "logo.svg"}, vctx)
    assert r.is_error
    assert "vector" in r.output.lower() or "SVG" in r.output


def test_read_image_refuses_non_image(ctx, repo):
    vctx = _vision_ctx(repo, client=_FakeVisionClient(), registry=_FakeRegistry())
    (repo / "notes.txt").write_text("just text")
    r = ReadImageTool().execute({"path": "notes.txt"}, vctx)
    assert r.is_error
    assert "not a recognized image format" in r.output


def test_read_image_refuses_oversized(ctx, repo):
    vctx = _vision_ctx(repo, client=_FakeVisionClient(), registry=_FakeRegistry())
    (repo / "big.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    # patch stat to report a huge size without writing 4MB
    from unittest.mock import patch
    with patch.object(type((repo / "big.png")), "stat") as mock_stat:
        class _S:
            st_size = 5 * 1024 * 1024
        mock_stat.return_value = _S()
        r = ReadImageTool().execute({"path": "big.png"}, vctx)
    assert r.is_error
    assert "caps images" in r.output


def test_read_image_no_vision_alias(ctx, repo):
    from ox.llm.registry import RegistryError

    class _NoVisionRegistry:
        def resolve(self, name):
            raise RegistryError("no vision alias")

    vctx = _vision_ctx(repo, client=_FakeVisionClient(), registry=_NoVisionRegistry())
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    r = ReadImageTool().execute({"path": "pic.png"}, vctx)
    assert r.is_error
    assert "vision model not configured" in r.output
    assert "vision alias" in r.output


def test_read_image_no_registry(ctx, repo):
    vctx = _vision_ctx(repo, client=_FakeVisionClient(), registry=None)
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    r = ReadImageTool().execute({"path": "pic.png"}, vctx)
    assert r.is_error
    assert "vision model not configured" in r.output


def test_read_image_wire_error_surfaces(ctx, repo):
    class _ErrClient:
        def complete(self, *a, **k):
            raise Exception("HTTP 400: unsupported image format")
    vctx = _vision_ctx(repo, client=_ErrClient(), registry=_FakeRegistry())
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    r = ReadImageTool().execute({"path": "pic.png"}, vctx)
    assert r.is_error
    assert "vision-capable" in r.output or "vision model" in r.output


def test_read_image_empty_description(ctx, repo):
    client = _FakeVisionClient(description="")
    vctx = _vision_ctx(repo, client=client, registry=_FakeRegistry())
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    r = ReadImageTool().execute({"path": "pic.png"}, vctx)
    assert not r.is_error
    assert r.output == ""


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
    assert len(tools) == 12
    wire = json.dumps([t.spec().to_openai() for t in tools])
    approx_tokens = len(wire) / 4
    # 1600 covers the 12-tool toolset including WebSearch + WebFetch + skill
    # + read_image (~100 tokens added when web tools landed; skill is tiny).
    # The /4 heuristic is loose; the guardrail exists to keep per-turn schema
    # overhead from eating small-model context windows, not as a hard wall.
    assert approx_tokens < 1600, f"schemas ≈ {approx_tokens:.0f} tokens, budget is 1600"


def test_control_arm_has_no_kg_query():
    names = [t.name for t in code_harness_tools(with_kg=False)]
    assert "kg_query" not in names
    assert names == [
        "read", "read_image", "edit", "write", "bash",
        "WebSearch", "WebFetch",
        "plan", "plan_update", "skill", "done",
    ]


def test_offline_control_arm_strips_web_too():
    # `with_web=False` is the offline-eval knob (no network egress at all).
    names = [t.name for t in code_harness_tools(with_kg=False, with_web=False)]
    assert "WebSearch" not in names
    assert "WebFetch" not in names
    assert "kg_query" not in names
    assert names == ["read", "read_image", "edit", "write", "bash", "plan", "plan_update", "skill", "done"]
