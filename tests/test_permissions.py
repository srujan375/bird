"""The permission gate: which tools it covers, where it attaches, and which
broker each surface gets.

The regression that motivated this module: gating used to happen in
Server.__init__, so anything that built a Runner without a Server — `bird code`,
the plain REPL, and the sub-session the lead's `code` tool dispatches mid-turn —
ran edit/write raw. bash was never gated anywhere.
"""

import io
from types import SimpleNamespace

import pytest

from bird.harnesses import registry
from bird.harnesses.lead.tools import CodeTool
from bird.llm.registry import ModelSpec, ProviderConfig, Registry
from bird.permissions import (
    AUTO_MODES,
    AutoApproveBroker,
    ConsoleBroker,
    DenyBroker,
    GatedTool,
    NEXT_MODE,
    PermissionBroker,
    auto_approves,
    gate_tools,
    permission_payload,
)
from bird.tools import (
    BashTool,
    EditTool,
    ReadTool,
    Tool,
    ToolContext,
    ToolResult,
    WriteTool,
)

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


class StubBroker:
    def __init__(self, answer=True, feedback=""):
        self.answer, self.feedback = answer, feedback
        self.seen = []

    def request(self, payload):
        self.seen.append(payload)
        return self.answer, self.feedback


# ------------------------------------------------ which tools declare the gate


def test_repo_mutating_tools_declare_permission():
    assert EditTool().requires_permission
    assert WriteTool().requires_permission
    # bash can write anywhere (sed -i, >, cp); a gate it can walk around is not a gate
    assert BashTool().requires_permission


def test_read_only_tools_do_not():
    assert ReadTool().requires_permission is False


# ------------------------------------------------------------- attachment point


def test_build_runner_gates_when_ctx_has_a_broker(tmp_path):
    ctx = ToolContext(repo_root=tmp_path, registry=REG, broker=StubBroker())
    r = registry.build_runner("code", spec=SPEC, client=None, registry=REG, ctx=ctx,
                              with_kg=False, with_web=False)
    for name in ("edit", "write", "bash"):
        assert isinstance(r.tools[name], GatedTool), f"{name} was not gated"
    assert not isinstance(r.tools["read"], GatedTool)


def test_build_runner_ungated_without_a_broker(tmp_path):
    """None = ungated is the library/test default; cli.py always supplies one."""
    ctx = ToolContext(repo_root=tmp_path, registry=REG)
    r = registry.build_runner("code", spec=SPEC, client=None, registry=REG, ctx=ctx,
                              with_kg=False, with_web=False)
    assert not isinstance(r.tools["edit"], GatedTool)


def test_gate_tools_does_not_double_wrap():
    once = gate_tools([EditTool()], StubBroker())
    twice = gate_tools(once, StubBroker())
    assert isinstance(twice[0], GatedTool)
    assert not isinstance(twice[0].inner, GatedTool)


# ---------------------------------------- the bug: dispatched code was ungated


def test_lead_dispatched_code_session_inherits_the_gate(tmp_path):
    """The lead mounts nothing gated, but `code` builds a fresh Runner
    mid-session. Before the broker rode on ToolContext, that sub-runner was
    born ungated even inside a fully gated TUI session."""
    broker = StubBroker()
    built = {}

    def fake_run(task):
        return SimpleNamespace(status="done", summary="built", turns=1)

    real_build = registry.build_runner

    def spy(name, **kw):
        r = real_build(name, **kw)
        built["runner"] = r
        return SimpleNamespace(run=fake_run)

    ctx = ToolContext(repo_root=tmp_path, registry=REG, run_dir=tmp_path,
                      client=None, broker=broker)
    import bird.harnesses.registry as reg_mod
    orig = reg_mod.build_runner
    reg_mod.build_runner = spy
    try:
        CodeTool().run({"task": "build it"}, ctx)
    finally:
        reg_mod.build_runner = orig

    sub = built["runner"]
    for name in ("edit", "write", "bash"):
        assert isinstance(sub.tools[name], GatedTool), f"sub-session {name} ungated"


# -------------------------------------------------------------------- payloads


def test_edit_payload_carries_a_diff(tmp_path):
    p = permission_payload("edit", {"path": "a.py", "old_text": "x = 1",
                                    "new_text": "x = 2"}, ToolContext(repo_root=tmp_path))
    assert p["kind"] == "edit" and p["file"] == "a.py"
    kinds = {line["kind"] for line in p["lines"]}
    assert "add" in kinds and "del" in kinds


def test_write_payload_marks_a_new_file(tmp_path):
    ctx = ToolContext(repo_root=tmp_path)
    fresh = permission_payload("write", {"path": "new.py", "content": "hi"}, ctx)
    assert fresh["new_file"] is True
    (tmp_path / "old.py").write_text("before")
    over = permission_payload("write", {"path": "old.py", "content": "after"}, ctx)
    assert over["new_file"] is False


def test_bash_payload_carries_the_command(tmp_path):
    p = permission_payload("bash", {"command": "pytest -q"}, ToolContext(repo_root=tmp_path))
    assert p == {"kind": "bash", "cmd": "pytest -q"}


def test_edit_payload_falls_back_when_path_is_null(tmp_path):
    p = permission_payload("edit", {"path": None, "old_text": "x", "new_text": "y"},
                           ToolContext(repo_root=tmp_path))
    assert p["kind"] == "edit"
    assert p["file"] == "?"  # not None — must not reach the TUI as null


def test_write_payload_falls_back_when_path_is_null(tmp_path):
    p = permission_payload("write", {"path": None, "content": "hi"},
                           ToolContext(repo_root=tmp_path))
    assert p["kind"] == "write"
    assert p["file"] == "?"  # not None — must not reach the TUI as null
    p = permission_payload("bash", {"command": None}, ToolContext(repo_root=tmp_path))
    assert p["kind"] == "bash"
    assert p["cmd"] == "bash"


def test_bash_payload_falls_back_when_command_is_missing(tmp_path):
    p = permission_payload("bash", {}, ToolContext(repo_root=tmp_path))
    assert p["kind"] == "bash"
    assert p["cmd"] == "bash"


# --------------------------------------------------------------------- denial


def test_denial_feedback_reaches_the_model(tmp_path):
    gated = GatedTool(EditTool(), StubBroker(False, "use the config file instead"))
    res = gated.execute({"path": "a.py", "old_text": "x", "new_text": "y"},
                        ToolContext(repo_root=tmp_path))
    assert res.is_error and "DENIED" in res.output
    assert "use the config file instead" in res.output


def test_denied_edit_does_not_touch_the_file(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1")
    gated = GatedTool(EditTool(), StubBroker(False))
    gated.execute({"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
                  ToolContext(repo_root=tmp_path))
    assert target.read_text() == "x = 1"


# --------------------------------------------------------------------- brokers


def test_auto_approve_broker_says_yes():
    assert AutoApproveBroker().request({"kind": "bash"}) == (True, "")


def test_deny_broker_explains_itself():
    approved, why = DenyBroker("no tty").request({"kind": "edit"})
    assert approved is False and why == "no tty"


def test_unbound_permission_broker_refuses():
    """A UI broker with nothing listening must refuse, not hang or auto-yes."""
    approved, why = PermissionBroker().request({"kind": "edit"})
    assert approved is False and "no UI" in why


@pytest.mark.parametrize("answer,expected", [
    ("y", True), ("yes", True), ("n", False), ("no", False), ("", False),
])
def test_console_broker_reads_the_answer(answer, expected):
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: answer)
    assert b.request({"kind": "edit", "file": "a.py", "lines": []})[0] is expected


@pytest.mark.parametrize("answer,expected", [
    ("Y", True), ("Yes", True), ("YES", True), ("yES", True),
    ("N", False), ("No", False), ("NO", False), ("nO", False),
])
def test_console_broker_accepts_mixed_case_yes_no(answer, expected):
    """`.lower()` was dropped to distinguish 'a' from 'A', but the accept-sets
    became exact-match only — "Yes"/"No" used to work and must keep working."""
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: answer)
    assert b.request({"kind": "edit", "file": "a.py", "lines": []})[0] is expected


def test_console_broker_a_turns_on_auto_approve_for_edits():
    answers = iter(["a"])
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: next(answers))
    assert b.request({"kind": "edit", "file": "a.py", "lines": []})[0] is True
    # the ask callable is exhausted; a second edit must not consult it again
    assert b.request({"kind": "write", "file": "b.py", "lines": []})[0] is True


def test_console_auto_approve_still_asks_for_bash():
    """'a' means auto-accept *edits*. A shell that can write anywhere keeps
    asking, or the edit gate is bypassable by heredoc."""
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: "a")
    b.request({"kind": "edit", "file": "a.py", "lines": []})
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "n"

    b.ask = ask
    approved, _ = b.request({"kind": "bash", "cmd": "rm -rf build"})
    assert asked, "bash was auto-approved by edit-scoped auto-approve"
    assert approved is False


def test_console_broker_interrupt_denies():
    def ask(_):
        raise KeyboardInterrupt

    b = ConsoleBroker(out=io.StringIO(), ask=ask)
    assert b.request({"kind": "edit", "file": "a.py", "lines": []})[0] is False


# ----------------------------------------------------- three-state mode contract


def test_next_mode_cycles_three_states():
    assert NEXT_MODE["normal"] == "auto_edits"
    assert NEXT_MODE["auto_edits"] == "full_auto"
    assert NEXT_MODE["full_auto"] == "normal"


def test_auto_modes_covered_kinds():
    # normal covers nothing; auto_edits covers edits + reads, NOT bash;
    # full_auto adds bash + mcp (an MCP tool is arbitrary remote code behind
    # a friendly name, so it gates like bash, never like a repo-local edit).
    # offer is never covered in any mode.
    assert AUTO_MODES["normal"] == frozenset()
    assert AUTO_MODES["auto_edits"] == frozenset({"edit", "write", "read_outside_repo"})
    assert AUTO_MODES["full_auto"] == frozenset(
        {"edit", "write", "read_outside_repo", "bash", "mcp"}
    )
    for kinds in AUTO_MODES.values():
        assert "offer" not in kinds


def test_auto_approves_helper_matches_mode():
    assert not auto_approves("normal", {"kind": "edit"})
    assert auto_approves("auto_edits", {"kind": "edit"})
    assert auto_approves("auto_edits", {"kind": "read_outside_repo"})
    assert not auto_approves("auto_edits", {"kind": "bash"})
    assert auto_approves("full_auto", {"kind": "bash"})
    assert not auto_approves("full_auto", {"kind": "offer"})


def test_console_broker_default_mode_is_normal():
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: "n")
    assert b.mode == "normal"
    # back-compat boolean view: normal means auto_edits is False
    assert b.auto_edits is False


def test_console_broker_auto_edits_property_back_compat():
    """The old auto_edits bool attribute still reads/writes through the mode."""
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: "n")
    b.auto_edits = True
    assert b.mode == "auto_edits"
    assert b.auto_edits is True
    b.auto_edits = False
    assert b.mode == "normal"


def test_console_broker_A_escalates_to_full_auto_and_approves():
    """'A' approves the current request AND flips the session to full_auto."""
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: "A")
    approved, _ = b.request({"kind": "bash", "cmd": "git rebase main"})
    assert approved is True
    assert b.mode == "full_auto"


def test_console_full_auto_auto_approves_bash_without_asking():
    """In full_auto, bash is auto-approved and the ask callable is not
    consulted — the audit line is the only trace."""
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: "should-not-be-called")
    b.mode = "full_auto"
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "n"

    b.ask = ask
    approved, _ = b.request({"kind": "bash", "cmd": "pytest -q"})
    assert approved is True
    assert not asked, "full_auto should not prompt for bash"


def test_console_full_auto_prints_audit_line_for_bash():
    out = io.StringIO()
    b = ConsoleBroker(out=out, ask=lambda _: "n")
    b.mode = "full_auto"
    b.request({"kind": "bash", "cmd": "pytest -q"})
    assert "FULL AUTO ran bash: pytest -q" in out.getvalue()


def test_console_auto_edits_audit_line_shows_read_path():
    """read_outside_repo payloads carry `path`, not `file`/`cmd`. The audit
    line used to print a trailing space with no target — the read must be
    named so execute-without-review leaves a useful trace."""
    out = io.StringIO()
    b = ConsoleBroker(out=out, ask=lambda _: "should-not-be-called")
    b.mode = "auto_edits"
    b.request({"kind": "read_outside_repo", "tool": "read", "path": "/etc/hosts"})
    text = out.getvalue()
    assert "/etc/hosts" in text
    assert "read_outside_repo" in text


def test_console_full_auto_audit_line_shows_read_path():
    """Same trace requirement in full_auto for a read_outside_repo payload."""
    out = io.StringIO()
    b = ConsoleBroker(out=out, ask=lambda _: "should-not-be-called")
    b.mode = "full_auto"
    b.request({"kind": "read_outside_repo", "tool": "read", "path": "/etc/hosts"})
    text = out.getvalue()
    assert "/etc/hosts" in text
    assert "read_outside_repo" in text


def test_console_full_auto_auto_approves_edits_too():
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: "should-not-be-called")
    b.mode = "full_auto"
    assert b.request({"kind": "edit", "file": "a.py", "lines": []})[0] is True
    assert b.request({"kind": "write", "file": "b.py", "lines": []})[0] is True


def test_console_full_auto_still_prompts_for_offers():
    """Offers fail closed under auto-answer — the answer IS the feedback, so an
    auto-approved offer with no feedback is a corrupted answer."""
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "1"

    b = ConsoleBroker(out=io.StringIO(), ask=ask)
    b.mode = "full_auto"
    approved, feedback = b.request(
        {"kind": "offer", "question": "which?", "options": ["a", "b"]}
    )
    assert asked, "full_auto must still prompt for offers"
    assert approved is True and feedback == "a"


def test_console_auto_edits_covers_read_outside_repo():
    """A read request during auto_edits is auto-approved — the user has no
    reason to deny a read once they've opted into apply-without-asking."""
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: "should-not-be-called")
    b.mode = "auto_edits"
    assert b.request({"kind": "read_outside_repo", "tool": "read", "path": "/etc/hosts"})[
        0
    ] is True


def test_console_full_auto_warning_printed_on_escalation():
    out = io.StringIO()
    b = ConsoleBroker(out=out, ask=lambda _: "A")
    b.request({"kind": "bash", "cmd": "rm -rf build"})
    text = out.getvalue()
    assert "FULL AUTO" in text and "WITHOUT asking" in text


def test_console_broker_mode_propagates_to_subharness_via_shared_instance():
    """The mode lives on the broker object, not per-runner. A sub-harness that
    forks the ToolContext and re-gates on the same broker inherits the mode."""
    b = ConsoleBroker(out=io.StringIO(), ask=lambda _: "A")
    b.request({"kind": "bash", "cmd": "x"})  # escalate
    assert b.mode == "full_auto"
    # the same instance — what a forked ctx would re-gate on — stays full_auto
    # and auto-approves a later bash without consulting ask
    asked = []
    b.ask = lambda prompt: asked.append(prompt) or "n"
    assert b.request({"kind": "bash", "cmd": "y"})[0] is True
    assert not asked


# ----------------------------------------------------- surface -> broker choice


def test_headless_broker_yes_flag_auto_approves():
    from bird.cli import _headless_broker

    assert isinstance(_headless_broker(SimpleNamespace(yes=True)), AutoApproveBroker)


def test_headless_broker_denies_when_nobody_can_answer(monkeypatch):
    from bird.cli import _headless_broker

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    broker = _headless_broker(SimpleNamespace(yes=False))
    assert isinstance(broker, DenyBroker)
    assert "--yes" in broker.request({"kind": "edit"})[1]


def test_headless_broker_prompts_on_a_tty(monkeypatch):
    from bird.cli import _headless_broker

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert isinstance(_headless_broker(SimpleNamespace(yes=False)), ConsoleBroker)


# --- per-call waiver: search-only bash runs unprompted ---

def test_search_only_bash_is_not_prompted(tmp_path):
    """~50 prompts per session were spent on grep/cat/find that the category
    allowlist had already proven read-only, and users started denying them out
    of fatigue. A command that cannot write is a question with one answer."""
    broker = StubBroker(True)
    gated = GatedTool(BashTool(), broker)
    ctx = ToolContext(repo_root=tmp_path, record=lambda t, d: None)
    result = gated.execute({"command": "grep -rn needle ."}, ctx)
    assert not broker.seen, "a read-only search should not have asked"
    assert not result.details.get("denied")


def test_waived_calls_are_still_logged(tmp_path):
    events = []
    gated = GatedTool(BashTool(), StubBroker(True))
    ctx = ToolContext(repo_root=tmp_path, record=lambda t, d: events.append((t, d)))
    gated.execute({"command": "ls src"}, ctx)
    assert any(t == "auto_approved" for t, _ in events), "unprompted runs must stay auditable"


@pytest.mark.parametrize("command", [
    "pytest -q",                     # a test run can rewrite fixtures
    "git log --oneline",             # a git read can be widened
    "ruff check .",
    "grep -rn x . > out.txt",        # redirection writes
    "sed -i 's/a/b/' f.py",          # in-place edit
    "find . -name '*.py' -delete",   # find that deletes
    "xargs rm < list.txt",           # xargs delegating out of the search set
])
def test_non_search_commands_still_prompt(tmp_path, command):
    broker = StubBroker(True)
    gated = GatedTool(BashTool(), broker)
    ctx = ToolContext(repo_root=tmp_path, record=lambda t, d: None)
    gated.execute({"command": command}, ctx)
    assert broker.seen, f"{command!r} was waived but is not a read-only search"


def test_waiver_is_off_when_search_is_not_an_enabled_category(tmp_path):
    broker = StubBroker(True)
    gated = GatedTool(BashTool(), broker)
    ctx = ToolContext(
        repo_root=tmp_path, bash_categories=("test",), record=lambda t, d: None
    )
    gated.execute({"command": "grep -rn x ."}, ctx)
    assert broker.seen, "a harness that disabled search has already said no"


def test_a_wrapped_tool_that_forgot_the_flag_is_still_gated(tmp_path):
    """`requires_permission` decides whether a tool gets WRAPPED; once wrapped,
    the default is to ask. Deriving the per-call answer from the flag would let
    a directly-wrapped tool silently lose its gate."""
    class Forgetful(Tool):
        name = "forgetful"
        description = "x"
        parameters = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            return ToolResult(output="ran")

    broker = StubBroker(False)
    gated = GatedTool(Forgetful(), broker)
    result = gated.execute({}, ToolContext(repo_root=tmp_path))
    assert broker.seen and result.is_error and "DENIED" in result.output
