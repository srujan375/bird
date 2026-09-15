"""End-to-end: the workbench answers its questions over real HTTP, and only
then does the designer run.

The acceptance criterion for the whole change is the order of events here. The
page connects, gets a question and no model turn; answers it, gets the next
question and still no model turn; answers that, and only now does a turn start
— carrying the direction and the design system the user actually picked.
"""

import http.client
import json
import threading
import time

import pytest

from bird.engine.session import SessionRecorder
from bird.harnesses.design import intake
from bird.harnesses.design.run import FINALIZED, STATIC_DIR
from bird.harnesses.design.session import DesignSession
from bird.harnesses.registry import build_runner
from bird.http_transport import HttpTransport
from bird.llm.registry import ModelSpec, ProviderConfig, Registry
from bird.llm.types import LLMResponse, Message, ToolCall, Usage
from bird.repl import Repl
from bird.serve import Server
from bird.tools import ToolContext

PROVIDER = ProviderConfig(name="fake", base_url="http://x")
SPEC = ModelSpec(spec="fake:model", provider=PROVIDER, model="model", context_window=200000)

HERO = "<!doctype html><html><head><title>Hero</title></head><body><h1>Hello</h1></body></html>"

_ids = iter(range(1000))


def tc(name, args):
    return ToolCall(id=f"c{next(_ids)}", name=name, arguments=args,
                    arguments_json=json.dumps(args))


def assistant(content=None, calls=()):
    return Message(role="assistant", content=content, tool_calls=list(calls))


class FakeClient:
    def __init__(self, script):
        self.script = list(script)

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None,
                 on_delta=None, on_thinking=None, **kwargs):
        msg = self.script.pop(0)
        if on_delta is not None and msg.content:
            on_delta(msg.content)
            on_delta(None)
        return LLMResponse(message=msg, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


SCRIPT = [
    assistant(calls=[tc("design_create", {"html": HERO, "title": "Hero"})]),
    assistant(content="One take up — a centred hero. Want a denser one beside it?"),
]


class Page:
    """The test's stand-in for the workbench page."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.events = []
        self.cv = threading.Condition()
        self.conn = http.client.HTTPConnection(host, port, timeout=10)
        self.conn.request("GET", "/events")
        self.resp = self.conn.getresponse()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.resp.fp.readline()
                if not line:
                    return
                if line.startswith(b"data: "):
                    with self.cv:
                        self.events.append(json.loads(line[6:]))
                        self.cv.notify_all()
        except (OSError, ValueError):
            return

    def wait_for(self, pred, what, timeout=10.0):
        deadline = time.time() + timeout
        seen = 0
        with self.cv:
            while True:
                for e in self.events[seen:]:
                    if pred(e):
                        return e
                seen = len(self.events)
                remaining = deadline - time.time()
                assert remaining > 0, f"timed out waiting for {what}; got {[x['type'] for x in self.events]}"
                self.cv.wait(remaining)

    def post(self, path, body, status=200):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request("POST", path, body=json.dumps(body))
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        assert resp.status == status, f"POST {path} -> {resp.status}: {payload!r}"
        return json.loads(payload or b"{}")

    def turns_started(self):
        return [e for e in self.events
                if e.get("type") == "harness_event" and e.get("event") == "run_start"]


def build_stack(tmp_path, script, prompt="a landing page for a podcast"):
    run_dir = tmp_path / ".bird" / "sessions" / "design-t"
    recorder = SessionRecorder(run_dir)
    registry = Registry(providers={"fake": PROVIDER}, models={}, aliases={"designer": "fake:model"})
    ctx = ToolContext(repo_root=tmp_path, record=recorder.event)
    runner = build_runner("design", spec=SPEC, client=FakeClient(script),
                          registry=registry, ctx=ctx, with_kg=False)
    repl = Repl(runner, registry, kg=None, recorder=recorder, run_id="design-t")
    transport = HttpTransport(static_dir=STATIC_DIR, stop_when=FINALIZED)
    server = Server(repl, transport=transport)
    design = DesignSession(run_dir=run_dir, intake_gate=True, on_state=transport.emit)
    ctx.design = design
    # run.py's order: open the board, then hold the brief behind the question
    design.start(prompt)
    return server, transport, design


def test_the_designer_does_not_run_until_its_questions_are_answered(tmp_path):
    server, transport, design = build_stack(tmp_path, SCRIPT)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    page = Page(host, port)

    # ---- the first thing the page is told is the question ----
    first = page.wait_for(lambda e: e["type"] == "design_state", "the opening push")
    assert first["status"] == "asking"
    assert first["ask"]["id"] == intake.FIDELITY
    assert first["ask"]["variant"] == "option-cards"
    assert [o["label"] for o in first["ask"]["options"]] == ["Wireframe", "High-fidelity"]
    assert page.turns_started() == [], "nothing is being designed behind the question"

    # ---- answering one brings the next, and still no turn ----
    out = page.post("/answer", {"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    assert out == {"ok": True, "answered": intake.FIDELITY,
                   "value": intake.HIGH_FIDELITY, "label": "High-fidelity"}
    second = page.wait_for(
        lambda e: e["type"] == "design_state" and (e.get("ask") or {}).get("id") == intake.DESIGN_SYSTEM,
        "the design-system question",
    )
    assert second["intake"][0]["answered"] is True, "the first is on the record, answered"
    assert second["intake_locked"] is False, "and still theirs to change"
    assert page.turns_started() == [], "one question at a time, and no work behind either"

    theme = second["ask"]["options"][0]["value"]

    # ---- the last answer is what starts the designer ----
    page.post("/answer", {"id": intake.DESIGN_SYSTEM, "value": theme})
    start = page.wait_for(
        lambda e: e.get("type") == "harness_event" and e.get("event") == "run_start",
        "the opening turn",
    )
    assert start["data"]["task"] == (
        f"Direction: High-fidelity. Design system: {theme}. "
        "Brief: a landing page for a podcast"
    ), "the designer is told what the user picked, not a default nobody read"

    # ---- and the artboard it draws lands on the board ----
    drawn = page.wait_for(
        lambda e: e["type"] == "design_state" and e.get("artboards"),
        "the first artboard",
    )
    assert drawn["artboards"][0]["id"] == "hero"
    assert drawn["ask"] is None and drawn["intake_locked"] is True, (
        "the answers are history once the designer has acted on them"
    )

    # ---- the user finalizes, which is what ends the session ----
    page.wait_for(lambda e: e["type"] == "turn_end", "the turn to finish")
    page.post("/mutate", {"op": "finalize", "artboard": "hero"})
    thread.join(timeout=10)
    assert not thread.is_alive(), "finalize is the transport's stop condition"
    assert design.state.finalized_artboard == "hero"


def test_a_refused_answer_leaves_the_question_on_the_table(tmp_path):
    server, transport, design = build_stack(tmp_path, SCRIPT)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    page = Page(host, port)
    page.wait_for(lambda e: e["type"] == "design_state", "the opening push")

    out = page.post("/answer", {"id": intake.FIDELITY, "value": "sketchy"}, status=400)
    assert out["ok"] is False and "no option" in out["error"]
    assert design.pending_ask().id == intake.FIDELITY
    assert page.turns_started() == []

    transport.shutdown()
    thread.join(timeout=10)


class BlockingClient:
    """The designer mid-generation: complete() does not come back until the
    test releases it, so the interrupt has a running turn to land on."""

    def __init__(self, release):
        self.release = release

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None,
                 on_delta=None, on_thinking=None, **kwargs):
        assert self.release.wait(timeout=10)
        return LLMResponse(message=assistant(content="too late"),
                           usage=Usage(1, 1), stop_reason="stop", model=spec.spec)


def test_an_interrupt_over_http_stops_the_running_turn(tmp_path):
    """POST /interrupt cancels the running turn — the page's Stop control
    rides this route, and the turn_end(status=interrupted) it produces is
    what un-sticks the page instead of leaving it on a spinner."""
    release = threading.Event()
    server, transport, _design = build_stack(tmp_path, [])
    server.repl.runner.client = BlockingClient(release)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    page = Page(host, port)

    # the intake stands between the page and a running turn: answer both
    page.wait_for(lambda e: e["type"] == "design_state", "the opening push")
    page.post("/answer", {"id": intake.FIDELITY, "value": intake.HIGH_FIDELITY})
    second = page.wait_for(
        lambda e: e["type"] == "design_state" and (e.get("ask") or {}).get("id") == intake.DESIGN_SYSTEM,
        "the design-system question",
    )
    page.post("/answer", {"id": intake.DESIGN_SYSTEM,
                          "value": second["ask"]["options"][0]["value"]})
    page.wait_for(lambda e: e["type"] == "harness_event" and e["event"] == "run_start",
                  "the opening turn")

    # the composer stays open mid-turn: input during a turn is injected at
    # the safe seam, not refused — the nudge path the open composer feeds
    page.post("/input", {"text": "lean quieter"})

    page.post("/interrupt", {})
    release.set()
    end = page.wait_for(lambda e: e["type"] == "turn_end", "the interrupted end")
    assert end["status"] == "interrupted"
    # the log answers "who stopped it": the /interrupt route names itself as
    # the source, so a session log can tell a page Stop from anything else
    # that tears a turn down
    rows = [
        json.loads(line)
        for line in (tmp_path / ".bird" / "sessions" / "design-t" / "events.jsonl")
        .read_text()
        .splitlines()
        if line.strip()
    ]
    hits = [r for r in rows if r["type"] == "interrupt"]
    assert hits, "the interrupt never reached the session log"
    assert hits[0]["data"] == {"source": "/interrupt"}
    transport.shutdown()
    thread.join(timeout=5)


@pytest.mark.skipif(
    not (STATIC_DIR / "index.html").is_file(),
    reason="the workbench page is being rebuilt as a vite target; the contract "
           "below stands for whatever lands in design/static",
)
def test_the_workbench_page_asks_rather_than_defaulting():
    """The direction and the design system are questions now, not two selects
    in the chat bar that a session could start on without anybody reading them.

    Port guide for the rebuilt page: design-workbench/pickers/README.md.
    """
    markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "pick-direction" not in markup
    assert "pick-theme" not in markup
