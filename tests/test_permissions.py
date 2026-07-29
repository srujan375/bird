"""The permission gate: which tools it covers, where it attaches, and which
broker each surface gets.

The regression that motivated this module: gating used to happen in
Server.__init__, so anything that built a Runner without a Server — `ox code`,
the plain REPL, and the sub-session the lead's `code` tool dispatches mid-turn —
ran edit/write raw. bash was never gated anywhere.
"""

import io
from types import SimpleNamespace

import pytest

from ox.harnesses import registry
from ox.harnesses.lead.tools import CodeTool
from ox.llm.registry import ModelSpec, ProviderConfig, Registry
from ox.permissions import (
    AutoApproveBroker,
    ConsoleBroker,
    DenyBroker,
    GatedTool,
    PermissionBroker,
    gate_tools,
    permission_payload,
)
from ox.tools import BashTool, EditTool, ReadTool, ToolContext, WriteTool

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
    import ox.harnesses.registry as reg_mod
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


# ----------------------------------------------------- surface -> broker choice


def test_headless_broker_yes_flag_auto_approves():
    from ox.cli import _headless_broker

    assert isinstance(_headless_broker(SimpleNamespace(yes=True)), AutoApproveBroker)


def test_headless_broker_denies_when_nobody_can_answer(monkeypatch):
    from ox.cli import _headless_broker

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    broker = _headless_broker(SimpleNamespace(yes=False))
    assert isinstance(broker, DenyBroker)
    assert "--yes" in broker.request({"kind": "edit"})[1]


def test_headless_broker_prompts_on_a_tty(monkeypatch):
    from ox.cli import _headless_broker

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert isinstance(_headless_broker(SimpleNamespace(yes=False)), ConsoleBroker)
