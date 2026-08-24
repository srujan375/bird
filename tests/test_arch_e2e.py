"""End-to-end: the arch harness over real HTTP — acceptance criteria.

A scripted architect has a design conversation — two rival approaches, one
greyed out with the reason it lost, a box deepened once its branch is reached,
then a handoff — while the test plays the browser page: SSE for events, POSTs
for actions.

What this test no longer contains is the point of the rebuild. There is no
approval gate and no finalize gate, so there are no `permission_request` events
at all: the session ends because the *user said so* in the conversation, and the
architect called `handoff`. The one POST the page still makes is `/mutate`, and
it is the user greying out an approach themselves.
"""

import http.client
import json
import threading
import time

import pytest

from bird.engine.runner import Runner
from bird.engine.session import SessionRecorder
from bird.harnesses.arch import harness as arch_def
from bird.harnesses.arch.run import HANDED_OFF
from bird.harnesses.arch.session import ArchSession
from bird.harnesses.arch.tools import MUTATING_TOOLS, arch_harness_tools
from bird.http_transport import HttpTransport
from bird.llm.registry import ModelSpec, ProviderConfig, Registry
from bird.llm.types import LLMResponse, Message, ToolCall, Usage
from bird.repl import Repl
from bird.serve import Server

SPEC = ModelSpec(
    spec="fake:model",
    provider=ProviderConfig(name="fake", base_url="http://x"),
    model="model",
    context_window=200000,
)

_ids = iter(range(1000))


def tc(name, args):
    return ToolCall(id=f"c{next(_ids)}", name=name, arguments=args,
                    arguments_json=json.dumps(args))


def assistant(content=None, calls=()):
    return Message(role="assistant", content=content, tool_calls=list(calls))


class FakeClient:
    def __init__(self, script):
        self.script = list(script)

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None, on_thinking=None):
        msg = self.script.pop(0)
        if on_delta is not None and msg.content:
            on_delta(msg.content)
            on_delta(None)
        return LLMResponse(message=msg, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


SCRIPT = [
    # turn A: put two rival shapes up, then ask the one question that decides them
    assistant(calls=[
        tc("brief", {"goal": "shorten urls", "actors": ["visitor"]}),
        tc("approach", {"name": "single box", "summary": "sqlite on one host"}),
        tc("approach", {"name": "managed", "summary": "postgres + a load balancer"}),
        tc("canvas", {
            "nodes": [
                {"id": "gw", "label": "HTTP entry", "kind": "api",
                 "responsibility": "takes the long url, hands back a short one"},
                {"id": "db", "label": "URL mappings", "kind": "store"},
                {"id": "lb", "label": "Load balancer", "kind": "infra",
                 "approaches": ["managed"]},
            ],
            "edges": [{"src": "gw", "dst": "db", "label": "lookup"}],
        }),
    ]),
    assistant(content="Single box or managed? I'd take the single box — it's a "
                      "weekend of work and you can move later. Your call."),
    # turn B: the user greyed 'managed' from the page; record the call and go deeper
    assistant(calls=[
        tc("decide", {"topic": "storage", "choice": "sqlite",
                      "against": [{"name": "postgres", "cons": ["a box to operate"]}],
                      "why": "one host is enough at this volume",
                      "pragmatic": "no failover; you restore from backup and lose minutes"}),
        tc("canvas", {"nodes": [{"id": "db", "depth": "sketch",
                                 "detail": "urls(short primary key, long); kept forever"}]}),
    ]),
    assistant(content="Storage settled. The mappings table is the only state — "
                      "anything else you want to walk through?"),
    # turn C: the user says they're done
    assistant(calls=[tc("handoff", {"summary": "single box, sqlite, managed kept as not-taken"})]),
]


class Page:
    """The test's stand-in for the browser page."""

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


def build_stack(tmp_path, script):
    run_dir = tmp_path / ".bird" / "sessions" / "arch-t"
    recorder = SessionRecorder(run_dir)
    registry = Registry(providers={}, models={}, aliases={"default": "fake:model"})
    from bird.tools import ToolContext

    ctx = ToolContext(repo_root=tmp_path, record=recorder.event)
    runner = Runner(
        spec=SPEC, client=FakeClient(script), registry=registry,
        tools=arch_harness_tools(with_kg=False, with_web=False), ctx=ctx,
        instructions_path=arch_def.INSTRUCTIONS_PATH,
        mutating_tools=MUTATING_TOOLS,
        tracker=arch_def.arch_tracker,
        tracker_prefix=arch_def.TRACKER_PREFIX,
        explore_nudge=arch_def.EXPLORE_NUDGE,
        done_tool="handoff",
    )
    repl = Repl(runner, registry, kg=None, recorder=recorder, run_id="arch-t")
    transport = HttpTransport(static_dir=arch_def.STATIC_DIR, stop_when=HANDED_OFF)
    server = Server(repl, transport=transport)
    # no broker: this harness has no gates to block on
    arch = ArchSession(run_dir=run_dir, on_state=transport.emit)
    ctx.arch = arch
    return server, transport, arch, run_dir


def test_a_whole_design_conversation_over_http(tmp_path):
    server, transport, arch, run_dir = build_stack(tmp_path, SCRIPT)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    page = Page(host, port)

    # the static page is served
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/")
    assert b"bird arch" in conn.getresponse().read()
    conn.close()

    # ---- turn A: two rivals go up, and the turn ends as a question ----
    page.post("/input", {"text": "design a url shortener"})
    e = page.wait_for(
        lambda e: e["type"] == "arch_state" and (e.get("changed") or {}).get("kind") == "canvas",
        "the board",
    )
    assert e["state"]["nodes"]["db"]["kind"] == "store"
    assert e["state"]["nodes"]["lb"]["approaches"] == ["managed"]
    assert e["state"]["nodes"]["db"]["approaches"] == [], "the store is shared, drawn once"
    assert "flowchart LR" in e["renders"]["board"]

    end = page.wait_for(lambda e: e["type"] == "turn_end", "turn A end")
    assert end["status"] == "reply", "a question to the user ends the turn; no gate does"
    assert not [x for x in page.events if x["type"] == "permission_request"], \
        "the rebuilt harness has no gates"

    # ---- the user rules, from the page ----
    page.post("/mutate", {"op": "approach", "id": "managed", "status": "greyed",
                          "rejected_reason": "one host is fine for now"})
    e = page.wait_for(
        lambda e: e["type"] == "arch_state"
        and e["state"]["approaches"].get("managed", {}).get("status") == "greyed",
        "the greyed approach",
    )
    assert "not taken: one host is fine for now" in e["renders"]["board"]

    # a greying with no reason is refused, and the page is told why
    page.post("/mutate", {"op": "approach", "id": "single-box", "status": "greyed"}, status=400)

    # ---- turn B: the call gets recorded and the store deepens ----
    page.post("/input", {"text": "go with the single box"})
    page.wait_for(
        lambda e: e["type"] == "arch_state"
        and e["state"]["nodes"].get("db", {}).get("depth") == "sketch",
        "the deepened store",
    )
    page.wait_for(lambda e: e["type"] == "turn_end" and e is not end, "turn B end")

    # ---- turn C: the user says they're done ----
    page.post("/input", {"text": "that's everything, wrap it up"})
    page.wait_for(
        lambda e: e["type"] == "arch_state" and e["status"] == "handed_off", "handoff"
    )
    page.wait_for(lambda e: e["type"] == "turn_end" and e["status"] == "done", "turn C end")

    # stop_when shut the transport down; the pump exits and says bye
    thread.join(timeout=10)
    assert not thread.is_alive()
    page.wait_for(lambda e: e["type"] == "bye", "bye")

    # ---- what survives ----
    assert (run_dir / "arch_state.json").is_file()
    saved = json.loads((run_dir / "arch_state.json").read_text())
    assert saved["handed_off"] is True
    assert saved["approaches"]["managed"]["rejected_reason"] == "one host is fine for now"

    md = (run_dir / "bundle" / "architecture.md").read_text()
    assert "## Approaches not taken" in md
    assert "one host is fine for now" in md
    assert "Deliberately good enough" in md, "the pragmatic call is on the record"
    assert "restore from backup" in md


def test_interrupt_over_http(tmp_path):
    """POST /interrupt cancels the running turn at the next harness event."""
    release = threading.Event()

    class BlockingClient:
        def __init__(self):
            self.calls = 0

        def complete(self, spec, messages, tools=None, temperature=None,
                     max_tokens=None, on_delta=None, on_thinking=None):
            self.calls += 1
            assert release.wait(timeout=10)
            return LLMResponse(
                message=assistant(content="too late"),
                usage=Usage(1, 1), stop_reason="stop", model=spec.spec,
            )

    server, transport, _arch, _ = build_stack(tmp_path, [])
    server.repl.runner.client = BlockingClient()
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    page = Page(host, port)

    page.post("/input", {"text": "start"})
    page.wait_for(
        lambda e: e["type"] == "harness_event" and e["event"] == "run_start", "turn started"
    )
    page.post("/interrupt", {})
    release.set()
    end = page.wait_for(lambda e: e["type"] == "turn_end", "interrupted end")
    assert end["status"] == "interrupted"
    transport.shutdown()
    thread.join(timeout=5)
