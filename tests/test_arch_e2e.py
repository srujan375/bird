"""End-to-end: the arch harness over real HTTP — acceptance criteria.

A scripted model walks brief → top level → approval gate → risk-ordered
expansion → finalize (rejected with feedback, then approved) while the test
plays the browser page: SSE for events, POSTs for actions.
"""

import http.client
import json
import threading
import time

import pytest

from bird.engine.runner import Runner
from bird.engine.session import SessionRecorder
from bird.harnesses.arch import harness as arch_def
from bird.harnesses.arch.render import TRACKER_PREFIX
from bird.harnesses.arch.session import ArchSession
from bird.harnesses.arch.tools import arch_harness_tools
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

    def complete(self, spec, messages, tools=None, temperature=None, max_tokens=None, on_delta=None):
        msg = self.script.pop(0)
        if on_delta is not None and msg.content:
            on_delta(msg.content)
            on_delta(None)
        return LLMResponse(message=msg, usage=Usage(10, 5), stop_reason="stop", model=spec.spec)


SCRIPT = [
    # turn A: brief -> top level -> request approval -> expand -> finalize rejected
    assistant(calls=[tc("brief", {"goal": "shorten urls", "actors": ["visitor"],
                                  "scope": "internal"})]),
    assistant(calls=[
        tc("component", {"id": "gw", "kind": "gateway", "responsibility": "http entry",
                         "trace": ["shorten urls"]}),
        tc("component", {"id": "db", "kind": "store", "responsibility": "url mappings",
                         "trace": ["shorten urls"], "data_owned": "short->long map"}),
        tc("connect", {"src": "gw", "dst": "db", "label": "lookup", "kind": "sync"}),
        tc("flow", {"id": "shorten", "name": "shorten", "kind": "happy",
                    "steps": [{"src": "gw", "dst": "db", "action": "INSERT mapping"}]}),
        tc("decide", {"topic": "Storage", "category": "storage",
                      "options": [{"name": "sqlite"}, {"name": "postgres"}],
                      "choice": "sqlite", "rationale": "single box"}),
    ]),
    assistant(content="Requesting approval.", calls=[tc("done", {"summary": "top level ready"})]),
    assistant(calls=[tc("expand", {"component_id": "db",
                                   "entities": [{"name": "urls", "keys": "short"}],
                                   "access_patterns": ["short -> long"],
                                   "retention": "forever"})]),
    assistant(calls=[tc("expand", {"component_id": "gw",
                                   "endpoints": [{"route": "/s", "method": "POST",
                                                  "request": "{url}", "response": "{short}",
                                                  "auth": "none"}]})]),
    assistant(calls=[tc("done", {"summary": "expanded; finalize?"})]),
    assistant(content="Noted — I'll record the rate limit decision. Anything else?"),
    # turn B: address feedback, finalize approved
    assistant(calls=[tc("decide", {"topic": "Rate limiting", "category": "integration",
                                   "options": [{"name": "token bucket"}, {"name": "none"}],
                                   "choice": "token bucket", "rationale": "abuse control"})]),
    assistant(calls=[tc("done", {"summary": "final"})]),
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
        mutating_tools=arch_def.MUTATING_TOOLS,
        tracker=arch_def.arch_tracker,
        tracker_prefix=TRACKER_PREFIX,
        explore_nudge=arch_def.EXPLORE_NUDGE,
    )
    repl = Repl(runner, registry, kg=None, recorder=recorder, run_id="arch-t")
    transport = HttpTransport(
        static_dir=arch_def.STATIC_DIR,
        stop_when=lambda e: e.get("type") == "arch_state" and e.get("phase") == "finalized",
    )
    server = Server(repl, transport=transport)
    arch = ArchSession(run_dir=run_dir, broker=server.broker, on_state=transport.emit)
    ctx.arch = arch
    return server, transport, arch, run_dir


def test_full_arch_session_over_http(tmp_path):
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

    page.post("/input", {"text": "design a url shortener"})

    # arch_state streams mid-turn with changed markers
    e = page.wait_for(
        lambda e: e["type"] == "arch_state" and (e.get("changed") or {}).get("id") == "db",
        "db arch_state",
    )
    assert e["state"]["components"]["db"]["kind"] == "store"
    assert "flowchart TD" in e["renders"]["toplevel"]
    assert e["tracker"].startswith(TRACKER_PREFIX)

    # gate 1: top-level approval
    req = page.wait_for(
        lambda e: e["type"] == "permission_request" and e.get("kind") == "toplevel_approval",
        "toplevel_approval",
    )
    page.post("/permission", {"id": req["id"], "approved": True})

    # gate 2: finalize — reject with feedback; the turn continues and ends as a reply
    req = page.wait_for(
        lambda e: e["type"] == "permission_request" and e.get("kind") == "finalize",
        "finalize request",
    )
    assert req["artifacts"]
    page.post("/permission", {"id": req["id"], "approved": False,
                              "feedback": "record a rate limiting decision first"})
    end = page.wait_for(lambda e: e["type"] == "turn_end", "turn A end")
    assert end["status"] == "reply"
    # a rejected finalize drops back into the working phase, not a dead end
    assert arch.state.phase == "expand"

    # turn B: feedback addressed, finalize approved
    page.post("/input", {"text": "record it and finalize"})
    req = page.wait_for(
        lambda e: e["type"] == "permission_request" and e.get("kind") == "finalize"
        and e["id"] != req["id"],
        "second finalize request",
    )
    page.post("/permission", {"id": req["id"], "approved": True})

    page.wait_for(
        lambda e: e["type"] == "arch_state" and e["phase"] == "finalized", "finalized state"
    )
    end = page.wait_for(
        lambda e: e["type"] == "turn_end" and e["status"] == "done", "turn B end"
    )
    # stop_when shut the transport down; the pump exits and says bye
    thread.join(timeout=10)
    assert not thread.is_alive()
    page.wait_for(lambda e: e["type"] == "bye", "bye")

    # bundle + persisted state on disk
    assert (run_dir / "bundle" / "architecture.json").is_file()
    assert (run_dir / "bundle" / "architecture.md").is_file()
    assert (run_dir / "arch_state.json").is_file()
    saved = json.loads((run_dir / "arch_state.json").read_text())
    assert saved["phase"] == "finalized"
    assert {d["topic"] for d in saved["decisions"]} == {"Storage", "Rate limiting"}


def test_interrupt_over_http(tmp_path):
    """POST /interrupt cancels the running turn at the next harness event."""
    release = threading.Event()

    class BlockingClient:
        def __init__(self):
            self.calls = 0

        def complete(self, spec, messages, tools=None, temperature=None,
                     max_tokens=None, on_delta=None):
            self.calls += 1
            assert release.wait(timeout=10)
            return LLMResponse(
                message=assistant(content="too late"),
                usage=Usage(1, 1), stop_reason="stop", model=spec.spec,
            )

    server, transport, arch, _ = build_stack(tmp_path, [])
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
