import json

import pytest

from ox.harnesses.code import code_harness_tools
from ox.tools import BashTool, DoneTool, EditTool, ReadImageTool, ReadTool, WriteTool
from ox.tools.base import ToolContext
from ox.tools.bash import check_command, is_verification_command


class _StubBroker:
    """Records permission requests and returns a canned answer."""

    def __init__(self, approve=True, feedback=""):
        self.approve = approve
        self.feedback = feedback
        self.seen = []

    def request(self, payload):
        self.seen.append(payload)
        return self.approve, self.feedback


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


def test_read_missing_reports_where_it_looked(ctx):
    # a bare "file not found: nope.py" is undiagnosable when normalization
    # moved the path; the resolved location is the whole diagnosis
    r = ReadTool().execute({"path": "'nope.py'"}, ctx)
    assert r.is_error
    assert "looked in" in r.output


# --- shell-quoted paths (terminal drag-and-drop) ---

@pytest.mark.parametrize(
    "wrap",
    [
        lambda p: p,
        lambda p: f"'{p}'",
        lambda p: f'"{p}"',
        lambda p: f"  '{p}'  ",
        lambda p: f"\"'{p}'\"",  # nested, as a model re-quoting a quoted path
    ],
    ids=["bare", "single", "double", "padded", "nested"],
)
def test_read_accepts_shell_quoted_paths(ctx, repo, wrap):
    """A terminal hands over a dropped file shell-quoted, and models copy the
    path out of the user's message verbatim. The leading quote used to make an
    absolute path relative, so it resolved under the repo root and vanished."""
    target = repo / "src" / "app.py"
    r = ReadTool().execute({"path": wrap(str(target))}, ctx)
    assert not r.is_error, r.output
    assert "def main():" in r.output


def test_read_accepts_backslash_escaped_spaces(ctx, repo):
    (repo / "my file.txt").write_text("hi\n")
    r = ReadTool().execute({"path": str(repo / "my file.txt").replace(" ", "\\ ")}, ctx)
    assert not r.is_error, r.output
    assert "hi" in r.output


def test_backslash_in_a_real_filename_is_not_unescaped(ctx, repo):
    """Unescaping is only preferred when the escaped reading does not exist —
    a file genuinely named with a backslash must still win."""
    weird = repo / "od\\d.txt"
    weird.write_text("literal\n")
    r = ReadTool().execute({"path": str(weird)}, ctx)
    assert not r.is_error, r.output
    assert "literal" in r.output


def test_quoted_path_still_gated_outside_repo(repo, tmp_path):
    """Normalization must not become an escape hatch: the un-quoted path is
    what the broker sees, and it is still gated."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n")
    broker = _StubBroker(approve=False)
    c = ToolContext(repo_root=repo, broker=broker)
    r = ReadTool().execute({"path": f"'{outside}'"}, c)
    assert r.is_error
    assert "DENIED" in r.output
    assert broker.seen[0]["kind"] == "read_outside_repo"
    assert broker.seen[0]["path"] == str(outside.resolve())  # clean, unquoted


def test_write_cannot_escape_repo_via_quoting(ctx, repo):
    r = WriteTool().execute({"path": "'/tmp/ox-escape-probe.txt'", "content": "x"}, ctx)
    assert r.is_error
    assert "escapes the repository root" in r.output


def test_read_offset_limit(ctx):
    r = ReadTool().execute({"path": "src/app.py", "offset": 2, "limit": 1}, ctx)
    assert "print" in r.output
    assert "def main" not in r.output


def test_read_escape_no_broker_denied(ctx):
    # out-of-repo read with no broker configured: refused, not a crash
    r = ReadTool().execute({"path": "../../etc/passwd"}, ctx)
    assert r.is_error
    assert "permission" in r.output.lower()


def _outside_file(tmp_path):
    import os
    outside = tmp_path.parent / "outside_repo_file.txt"
    outside.write_text("external content\n")
    return outside


def test_read_outside_repo_approved(ctx, repo, tmp_path):
    outside = _outside_file(tmp_path)
    ctx.broker = _StubBroker(approve=True)
    r = ReadTool().execute({"path": str(outside)}, ctx)
    assert not r.is_error
    assert "external content" in r.output
    # the broker saw a read_outside_repo payload naming the path
    assert ctx.broker.seen[0]["kind"] == "read_outside_repo"
    assert ctx.broker.seen[0]["path"] == str(outside.resolve())


def test_read_outside_repo_denied(ctx, repo, tmp_path):
    outside = _outside_file(tmp_path)
    ctx.broker = _StubBroker(approve=False, feedback="nope")
    r = ReadTool().execute({"path": str(outside)}, ctx)
    assert r.is_error
    assert "DENIED" in r.output
    assert "nope" in r.output


def test_read_outside_repo_in_repo_still_ungated(ctx, repo):
    # sanity: an in-repo read never touches the broker even when one is set
    ctx.broker = _StubBroker(approve=True)
    r = ReadTool().execute({"path": "src/app.py"}, ctx)
    assert not r.is_error
    assert ctx.broker.seen == []


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


def _outside_image(tmp_path):
    outside = tmp_path.parent / "outside_shot.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return outside


def test_read_image_outside_repo_approved(ctx, repo, tmp_path):
    outside = _outside_image(tmp_path)
    client = _FakeVisionClient(description="a screenshot")
    vctx = _vision_ctx(repo, client=client, registry=_FakeRegistry())
    vctx.broker = _StubBroker(approve=True)
    r = ReadImageTool().execute({"path": str(outside)}, vctx)
    assert not r.is_error
    assert r.output == "a screenshot"
    assert vctx.broker.seen[0]["kind"] == "read_outside_repo"
    assert vctx.broker.seen[0]["path"] == str(outside.resolve())


def test_read_image_outside_repo_denied(ctx, repo, tmp_path):
    outside = _outside_image(tmp_path)
    client = _FakeVisionClient(description="a screenshot")
    vctx = _vision_ctx(repo, client=client, registry=_FakeRegistry())
    vctx.broker = _StubBroker(approve=False, feedback="private")
    r = ReadImageTool().execute({"path": str(outside)}, vctx)
    assert r.is_error
    assert "DENIED" in r.output
    assert "private" in r.output
    # the vision model was never called
    assert client.calls == []


def test_read_image_outside_repo_no_broker(ctx, repo, tmp_path):
    outside = _outside_image(tmp_path)
    vctx = _vision_ctx(repo, client=_FakeVisionClient(), registry=_FakeRegistry())
    r = ReadImageTool().execute({"path": str(outside)}, vctx)
    assert r.is_error
    assert "permission" in r.output.lower()


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


def test_read_image_accepts_a_quoted_path(repo, tmp_path):
    """The reported failure: a macOS screenshot dropped into the terminal
    arrives single-quoted with spaces in the name, and read_image said
    'file not found' for a file that was right there."""
    shot = tmp_path.parent / "Screenshot 2026-07-28 at 10.39.37 PM.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    client = _FakeVisionClient(description="a screenshot of a terminal")
    vctx = _vision_ctx(repo, client=client, registry=_FakeRegistry())
    vctx.broker = _StubBroker(approve=True)
    r = ReadImageTool().execute({"path": f"'{shot}'"}, vctx)
    assert not r.is_error, r.output
    assert r.output == "a screenshot of a terminal"


def test_read_image_missing_names_the_vanished_temp_file(repo, tmp_path):
    """A reaped screenshot temp file should not read as a path bug."""
    d = tmp_path / "TemporaryItems" / "NSIRD_screencaptureui_abc123"
    d.mkdir(parents=True)
    vctx = _vision_ctx(repo, client=_FakeVisionClient(), registry=_FakeRegistry())
    vctx.broker = _StubBroker(approve=True)
    r = ReadImageTool().execute({"path": str(d / "Screenshot 1.png")}, vctx)
    assert r.is_error
    assert "screenshot temp files" in r.output


def test_read_image_error_names_the_configured_model(ctx, repo):
    """'not vision-capable' is only actionable if it says which model."""
    class _ErrClient:
        def complete(self, *a, **k):
            raise Exception("HTTP 400: unsupported image format")
    vctx = _vision_ctx(repo, client=_ErrClient(), registry=_FakeRegistry("ollama:kimi-k2.7-code"))
    (repo / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    r = ReadImageTool().execute({"path": "pic.png"}, vctx)
    assert r.is_error
    assert "ollama:kimi-k2.7-code" in r.output


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
    # runner prefixes: the model could not verify in a uv/npx project before
    "uv run pytest -q",
    "uv run --frozen pytest tests/",
    "poetry run mypy src/",
    "npx tsc --noEmit",
    "npx -y eslint .",
    "npm run build",
    "npm run typecheck",
    "pnpm run test:unit",
    "python -m compileall src",
    "make check",
    "vitest run",
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
    # a runner prefix delegates, it does not widen: the inner command is judged
    "uv run python evil.py",
    "npx rimraf dist",
    "uv run bash -c 'rm -rf src'",
    "npm run deploy",
    # find/xargs are search commands that can still delete or execute
    "find src -name '*.py' -delete",
    "find . -name '*.py' -exec rm {} ;",
    "find . -name '*.py' | xargs rm",
    "ls | xargs -n 1 rm -f",
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
    # linters stay behind the lint category; the test category doesn't grant them
    assert check_command("mypy src/", ("search", "test")) is not None


IS_VERIFICATION = [
    "pytest -q",
    "uv run pytest tests/test_kg.py",
    "npm test",
    "npm run build",
    "npx tsc --noEmit",
    "ruff check src/",
    "mypy src/",
    "make test",
    "cargo test",
    "python -m pytest && ruff check .",  # any segment counts
]
NOT_VERIFICATION = [
    "ls -la",
    "rg 'def main' src/",
    "git diff",
    "cat src/app.py",
    "ruff format src/",  # a formatter rewrites files; it proves nothing
    "black src/",
]


@pytest.mark.parametrize("cmd", IS_VERIFICATION)
def test_is_verification_command(cmd):
    assert is_verification_command(cmd), cmd


@pytest.mark.parametrize("cmd", NOT_VERIFICATION)
def test_is_not_verification_command(cmd):
    assert not is_verification_command(cmd), cmd


@pytest.mark.parametrize("cmd", IS_VERIFICATION)
def test_every_verification_command_is_allowed(cmd):
    """The gate and the allowlist must agree: a command `done` accepts as proof
    must be one bash will actually run, or the model is boxed in."""
    assert check_command(cmd, ("search", "test", "lint", "git_read")) is None, cmd


# --- done ---

def test_done(ctx):
    r = DoneTool().execute({"summary": "fixed the bug"}, ctx)
    assert r.details["done"] is True
    assert r.output == "fixed the bug"


# --- done's verification gate ---

def _edit(ctx, text="def main():\n    print('hello')\n", new="def main():\n    pass\n"):
    return EditTool().execute({"path": "src/app.py", "old_text": text, "new_text": new}, ctx)


def _verify(ctx, command):
    r = BashTool().execute({"command": command}, ctx)
    ctx.note_tool_result("bash", r)
    return r


def _note_edit(ctx, result):
    ctx.note_tool_result("edit", result)
    return result


@pytest.fixture
def code_ctx(ctx):
    ctx.require_verification = True
    return ctx


def test_done_ungated_when_nothing_was_edited(code_ctx):
    # a read-only session (answered a question, changed nothing) still finishes
    assert not DoneTool().execute({"summary": "explained it"}, code_ctx).is_error


def test_done_blocked_after_an_edit_with_no_check(code_ctx):
    _note_edit(code_ctx, _edit(code_ctx))
    r = DoneTool().execute({"summary": "fixed it"}, code_ctx)
    assert r.is_error
    assert "src/app.py" in r.output
    assert "not run any test" in r.output
    assert any(t == "done_blocked_unverified" for t, _ in code_ctx.events)


def test_done_allowed_after_a_passing_check(code_ctx):
    _note_edit(code_ctx, _edit(code_ctx))
    _verify(code_ctx, "python -m pytest --version")
    r = DoneTool().execute({"summary": "fixed it"}, code_ctx)
    assert not r.is_error
    assert "unverified" not in r.details


def test_done_blocked_when_the_check_failed(code_ctx):
    _note_edit(code_ctx, _edit(code_ctx))
    _verify(code_ctx, "pytest --nonexistent-flag-xyz")
    r = DoneTool().execute({"summary": "fixed it"}, code_ctx)
    assert r.is_error
    assert "failed with exit" in r.output


def test_done_blocked_when_edited_after_the_last_green_check(code_ctx):
    """The 2-of-21 case in the logs: tests ran, then the code kept changing."""
    _note_edit(code_ctx, _edit(code_ctx))
    _verify(code_ctx, "python -m pytest --version")
    _note_edit(code_ctx, _edit(code_ctx, "def main():\n    pass\n", "def main():\n    return 1\n"))
    r = DoneTool().execute({"summary": "fixed it"}, code_ctx)
    assert r.is_error
    assert "ran BEFORE these edits" in r.output


def test_a_formatter_is_not_a_check(code_ctx):
    _note_edit(code_ctx, _edit(code_ctx))
    _verify(code_ctx, "ruff format src/")
    assert DoneTool().execute({"summary": "fixed it"}, code_ctx).is_error


def test_unverified_reason_only_unlocks_after_the_first_block(code_ctx):
    _note_edit(code_ctx, _edit(code_ctx))
    # offered up front it does nothing — otherwise the gate is decorative
    first = DoneTool().execute({"summary": "done", "unverified_reason": "no tests"}, code_ctx)
    assert first.is_error
    second = DoneTool().execute({"summary": "done", "unverified_reason": "no tests"}, code_ctx)
    assert not second.is_error
    assert second.details["unverified"]["paths"] == ["src/app.py"]
    assert any(t == "done_unverified" for t, _ in code_ctx.events)


def test_other_harnesses_are_not_gated(ctx):
    """lead/arch leave require_verification off; done must stay unchanged."""
    _note_edit(ctx, _edit(ctx))
    assert not DoneTool().execute({"summary": "designed it"}, ctx).is_error


# --- schema budget (decision #6: all schemas < ~1200 tokens) ---

# The threshold test_skills.py and test_web.py import: three copies of the
# number had to be edited by hand every time the toolset grew, which is how
# they drift. 1650 covers the 12-tool toolset including WebSearch + WebFetch +
# skill + read_image (~100 tokens added when web tools landed; skill is tiny)
# and done's verification gate (~20: the unverified_reason param and the clause
# naming the gate — the rest of that rule lives in instructions.md and in the
# rejection message, which cost nothing per turn).
# The /4 heuristic is loose; the guardrail exists to keep per-turn schema
# overhead from eating small-model context windows, not as a hard wall.
SCHEMA_TOKEN_BUDGET = 1650


def test_all_schemas_under_token_budget():
    tools = code_harness_tools(with_kg=True)
    assert len(tools) == 12
    wire = json.dumps([t.spec().to_openai() for t in tools])
    approx_tokens = len(wire) / 4
    assert approx_tokens < SCHEMA_TOKEN_BUDGET, (
        f"schemas ≈ {approx_tokens:.0f} tokens, budget is {SCHEMA_TOKEN_BUDGET}"
    )


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
