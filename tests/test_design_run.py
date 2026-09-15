import inspect
import json
from types import SimpleNamespace

import pytest

from bird.harnesses.design import run as run_mod
from bird.harnesses.design.run import (
    CLOSE_WHEN_EMPTY_SECONDS,
    FINALIZED,
    STATIC_DIR,
    run_design_interactive,
)
from bird.harnesses.design.session import DesignSession
from bird.harnesses.registry import get
from bird.llm.registry import ProviderConfig, Registry

REGISTRY = Registry(
    providers={"fake": ProviderConfig(name="fake", base_url="http://x")},
    models={}, aliases={"designer": "fake:model"},
)


@pytest.fixture
def session_cls(monkeypatch):
    """run.py constructs DesignSession with repo_root= (the session owns theme
    lookups); tolerate a DesignSession that has not grown the kwarg yet."""
    params = inspect.signature(DesignSession.__init__).parameters
    if "repo_root" in params:
        return DesignSession

    class Tolerant(DesignSession):
        def __init__(self, *a, repo_root=None, **kw):
            super().__init__(*a, **kw)

    monkeypatch.setattr(run_mod, "DesignSession", Tolerant)
    return Tolerant


class FakeTransport:
    url = "http://127.0.0.1:9999/"
    made: list["FakeTransport"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.emitted = []
        FakeTransport.made.append(self)

    def emit(self, event):
        self.emitted.append(event)

    def shutdown(self):
        pass


class FakeServer:
    made: list["FakeServer"] = []

    def __init__(self, repl, transport=None, broker=None):
        self.repl = repl
        self.inputs = []
        self.ran = False
        FakeServer.made.append(self)

    def on_user_input(self, text):
        self.inputs.append(text)

    def run(self):
        self.ran = True


@pytest.fixture
def wired(monkeypatch, session_cls):
    """The bring-up with the network and the model faked out."""
    FakeTransport.made.clear()
    FakeServer.made.clear()
    monkeypatch.setattr("bird.http_transport.HttpTransport", FakeTransport)
    monkeypatch.setattr("bird.serve.Server", FakeServer)
    monkeypatch.setattr("bird.repl.Repl", lambda *a, **k: SimpleNamespace(messages=[]))
    monkeypatch.setattr(run_mod, "build_runner",
                        lambda _harness, **kw: SimpleNamespace(ctx=kw["ctx"]))
    monkeypatch.setattr("time.sleep", lambda _s: None)  # the SSE drain wait
    return session_cls


def test_design_run_wiring():
    assert STATIC_DIR.name == "static"
    assert FINALIZED({"type": "design_state", "status": "finalized"})
    assert not FINALIZED({"type": "design_state", "status": "ready"})
    d = get("design")
    assert d.done_tool == "design_finalize" and d.interactive
    assert [t.name for t in d.tools(with_kg=False, with_web=False)] == [
        "design_create", "design_read", "design_edit",
        "design_undo", "design_delete_artboard", "design_finalize", "design_status",
        "design_themes", "design_set_theme", "design_plan",
        "design_critique", "design_look", "design_showcase", "design_polish", "skill",
    ]

def test_run_design_interactive_asks_before_it_runs_the_model(tmp_path, wired):
    """`design.start` is what gives the Workbench its first design_state. Without
    it the page sits on an empty status pill with Finalize disabled no matter
    what the model does, and the handoff reports no prompt.

    With a person at the workbench that first push is the intake question, and
    the brief stays parked behind it: the designer does not get a turn until
    the direction is settled, or it designs in a fidelity nobody chose."""
    session = run_design_interactive(
        repo_root=tmp_path, prompt="a landing page", registry=REGISTRY,
        client=None, run_dir=tmp_path / "design-run", no_open=True,
    )
    transport, server = FakeTransport.made[0], FakeServer.made[0]
    assert session.state.prompt == "a landing page"
    assert transport.emitted[0].get("status") == "asking", "the page is told before the model runs"
    assert session.pending_ask().id == "fidelity"
    assert server.inputs == [], "the brief waits for the answer"
    assert server.ran
    # the page closing is how a dispatched session ends: the empty-room close
    # travels with every design transport, and there is no linger to fight it
    assert transport.kwargs["close_when_empty"] == CLOSE_WHEN_EMPTY_SECONDS > 0
    assert "linger" not in transport.kwargs


# ----------------------------------------------------------------- resume

def _saved_session(tmp_path, *, turns: int):
    """An earlier session's run dir: the board file and a saved transcript."""
    old = tmp_path / "design-old"
    old.mkdir()
    (old / "design_state.json").write_text("{}", encoding="utf-8")
    rows = [{"role": "system", "content": "stale prompt"}, {"role": "user", "content": "Direction: High-fidelity. Design system: apple. Brief: a landing page"}]
    for i in range(turns):
        rows.append({"role": "assistant", "content": f"turn {i}"})
    if rows[1:]:
        (old / "messages.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return old


def _fake_load(monkeypatch, session_cls, seen):
    """DesignSession.load as the session module will provide it: a board
    restored from disk. Here: a settled, memory-only board with a brief."""
    def load(cls, run_dir, **kwargs):
        seen["load"] = (run_dir, kwargs)
        s = DesignSession()  # no intake: settled, nothing pending
        s.start("a landing page")
        return s

    monkeypatch.setattr(session_cls, "load", classmethod(load), raising=False)


def test_resume_reopens_the_board_and_the_transcript_without_re_briefing(tmp_path, monkeypatch, wired):
    """A page closed by accident used to be gone for good. Resume restores the
    board through DesignSession.load, hands the saved transcript back to the
    Repl (stale system prompt dropped), pushes the board to the page, and does
    NOT send the opening brief again — the designer already had its turns."""
    old = _saved_session(tmp_path, turns=2)
    seen = {}
    _fake_load(monkeypatch, wired, seen)

    session = run_design_interactive(
        repo_root=tmp_path, prompt="", registry=REGISTRY, client=None,
        run_dir=tmp_path / "ignored", no_open=True, resume=old,
    )
    transport, server = FakeTransport.made[0], FakeServer.made[0]
    assert seen["load"][0] == old and seen["load"][1].get("intake_gate") is True
    assert session.state.prompt == "a landing page"
    assert [m.role for m in server.repl.messages] == ["user", "assistant", "assistant"]
    assert transport.emitted and transport.emitted[0]["type"] == "design_state"
    assert transport.emitted[0]["prompt"] == "a landing page"
    assert server.inputs == [], "the transcript already has turns: no second brief"
    assert server.ran
    # the recorder wrote into the OLD dir and said so
    rows = [json.loads(l) for l in (old / "events.jsonl").read_text().splitlines() if l.strip()]
    resumed = [r for r in rows if r["type"] == "resume"]
    assert resumed and resumed[0]["data"]["messages"] == 3
    assert not (tmp_path / "ignored").exists()


def test_resume_of_a_session_that_never_got_a_turn_sends_the_composed_opening(tmp_path, monkeypatch, wired):
    """The plan-step deaths left boards with a brief and no designer turn.
    Reopening one sends the same Direction/Design system/Brief line the
    intake would have composed, not the raw prompt."""
    old = _saved_session(tmp_path, turns=0)
    (old / "messages.jsonl").unlink()
    seen = {}
    _fake_load(monkeypatch, wired, seen)

    run_design_interactive(
        repo_root=tmp_path, prompt="", registry=REGISTRY, client=None,
        run_dir=tmp_path / "ignored", no_open=True, resume=old,
    )
    server = FakeServer.made[0]
    assert len(server.inputs) == 1
    assert server.inputs[0].startswith("Direction: ")
    assert server.inputs[0].endswith("Brief: a landing page")


def test_resume_refuses_a_dir_with_no_board(tmp_path, wired):
    with pytest.raises(FileNotFoundError, match="nothing to resume"):
        run_design_interactive(
            repo_root=tmp_path, prompt="", registry=REGISTRY, client=None,
            run_dir=tmp_path / "x", no_open=True, resume=tmp_path / "nope",
        )
