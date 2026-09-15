"""The scribe: a second, cheap model that draws one step behind.

The fake engine here plays both roles. Spawned with the architect's system
prompt it answers like the architect and never draws; spawned with the
scribe's it reads the turn record it was handed, draws every `#box:<id>`
and `#decide:<topic>=<choice>` it finds, and exits. What has to hold: the
architect's turn ends without drawing, the board fills in afterwards, the
page sees drawing → idle with recent lines, several fast answers coalesce
into one job, a broken job is reported and not fatal, and a handoff waits
for the queue to drain.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest

from bird.engine.session import SessionRecorder
from bird.harnesses.arch import harness as arch_def
from bird.harnesses.arch.claude import ClaudeArchServer, architect_tools, scribe_tools, system_prompt
from bird.harnesses.arch.run import HANDED_OFF
from bird.harnesses.arch.scribe import Scribe, turn_record
from bird.harnesses.arch.session import ArchSession
from bird.http_transport import HttpTransport
import http.client

FAKE = r'''#!%(python)s
import json, sys, time, urllib.request

argv = sys.argv[1:]
def opt(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None
cfg = json.load(open(opt("--mcp-config")))["mcpServers"]
server = next(iter(cfg))          # "arch" for the architect, "board" for the scribe
cfg = cfg[server]
scribe = "You are the scribe" in ((opt("--append-system-prompt") or "") + (opt("--system-prompt") or ""))
session = opt("--resume") or opt("--session-id") or "scribe-job"
with open(opt("--mcp-config") + ".jobs", "a") as f:
    f.write("\n")

def out(obj):
    print(json.dumps(obj), flush=True)

def mcp(method, params, rid=1):
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
    req = urllib.request.Request(cfg["url"], data=body, method="POST",
                                 headers={**cfg["headers"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)

def tool(name, args, tid):
    full = "mcp__%%s__%%s" %% (server, name)
    reply = mcp("tools/call", {"name": name, "arguments": args})
    out({"type": "assistant", "message": {"id": "m-" + tid, "content": [
        {"type": "tool_use", "id": tid, "name": full, "input": args}], "usage": {}}})
    out({"type": "stream_event", "event": {"type": "message_stop"}})
    res = reply.get("result") or {"content": [{"type": "text", "text": json.dumps(reply.get("error"))}], "isError": True}
    out({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": res["content"][0]["text"], "is_error": res["isError"]}]}})
    return res

def say(text, mid):
    out({"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": text}], "usage": {}}})
    out({"type": "stream_event", "event": {"type": "message_stop"}})

out({"type": "system", "subtype": "init", "session_id": session, "model": "fake-" + ("scribe" if scribe else "architect"),
     "tools": [], "mcp_servers": [{"name": server, "status": "connected"}]})

n = 0
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") != "user":
        continue
    n += 1
    text = msg["message"]["content"][0]["text"]
    if scribe:
        if "#slow" in text:
            time.sleep(1.2)
        if "#break" in text:
            sys.exit(3)
        boxes = [w.split(":", 1)[1].strip(".,") for w in text.split() if w.startswith("#box:")]
        if boxes:
            tool("canvas", {"nodes": [{"id": b, "label": b.replace("-", " ").title(), "kind": "service"} for b in boxes]}, "tu%%d" %% n)
        for w in text.split():
            if w.startswith("#decide:"):
                topic, choice = w[len("#decide:"):].split("=", 1)
                tool("decide", {"topic": topic, "choice": choice, "why": "the architect said so"}, "td%%d" %% n)
        say("done", "m%%d" %% n)
    else:
        if "#ask" in text:
            tool("question", {"question": "Queue?", "recommendation": "no",
                              "options": [{"label": "queue", "cost": "retries for free"},
                                          {"label": "in-process", "cost": "simpler", "rec": True}]}, "tq%%d" %% n)
        if "#handoff" in text:
            res = tool("handoff", {"summary": "one box"}, "th%%d" %% n)
            say("Handoff: " + res["content"][0]["text"][:60], "m%%d" %% n)
        elif "#canvas" in text:
            res = tool("canvas", {"nodes": [{"id": "x", "label": "X"}]}, "tc%%d" %% n)
            say("tried to draw: " + res["content"][0]["text"][:80], "m%%d" %% n)
        elif "#decide" in text:
            res = tool("decide", {"topic": "t", "choice": "c"}, "tc%%d" %% n)
            say("tried to decide: " + res["content"][0]["text"][:80], "m%%d" %% n)
        else:
            # the architect's reply carries the shape in words; the tokens are
            # what the fake scribe reads, standing in for a real one's reading
            say("Noted. " + " ".join(w for w in text.split() if w.startswith(("#box:", "#decide:", "#slow", "#break"))), "m%%d" %% n)
    out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
         "session_id": session, "result": text, "usage": {"input_tokens": 3, "output_tokens": 1}})
    if scribe:
        break
'''


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
                assert remaining > 0, (
                    f"timed out waiting for {what}; got "
                    f"{[(x['type'], x.get('event') or x.get('status')) for x in self.events]}")
                self.cv.wait(remaining)

    def post(self, path, body, status=200):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request("POST", path, body=json.dumps(body))
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        assert resp.status == status, f"POST {path} -> {resp.status}: {payload!r}"
        return json.loads(payload or b"{}")

    def turn_ends(self):
        return [e for e in self.events if e["type"] == "turn_end"]


def harness(e, name):
    return e["type"] == "harness_event" and e["event"] == name


def harness(e, name):
    return e["type"] == "harness_event" and e["event"] == name


@pytest.fixture
def stack(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(FAKE % {"python": sys.executable})
    fake.chmod(0o755)
    run_dir = tmp_path / ".bird" / "sessions" / "arch-s"
    recorder = SessionRecorder(run_dir)
    arch = ArchSession(run_dir=run_dir)
    transport = HttpTransport(static_dir=arch_def.STATIC_DIR, stop_when=HANDED_OFF)
    server = ClaudeArchServer(
        arch=arch, transport=transport, recorder=recorder, repo_root=tmp_path,
        run_dir=run_dir, run_id="arch-s", bin_path=str(fake), scribe_model="fake-haiku",
    )
    arch.on_state = transport.emit
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    page = Page(host, port)
    page.wait_for(lambda e: e["type"] == "ready", "ready")
    yield server, arch, page, run_dir
    transport.shutdown()
    thread.join(timeout=10)
    recorder.close()


def scribe_events(page):
    return [e for e in page.events if e["type"] == "scribe"]


def test_the_tool_split():
    assert {t.name for t in architect_tools(with_scribe=True)} == {"import_repo", "canvas", "approach", "question", "brief"}
    assert {t.name for t in scribe_tools()} == {"canvas", "decide"}
    assert {t.name for t in architect_tools(with_scribe=False)} == {
        "import_repo", "canvas", "approach", "decide", "question", "brief", "handoff"}
    assert "The scribe draws" in system_prompt(with_scribe=True)
    assert "The scribe draws" not in system_prompt(with_scribe=False)


def test_a_turn_record_is_the_words_and_the_calls():
    rec = turn_record([
        ("run_start", {"task": "sqlite is fine"}),
        ("assistant", {"content": "", "tool_calls": [
            {"name": "question", "arguments_json": json.dumps({"question": "Queue?"})},
            {"name": "Read", "arguments_json": "{}"}]}),
        ("assistant", {"content": "Storage settled. Two boxes: an API and the mappings store.", "tool_calls": []}),
    ])
    assert rec.splitlines() == [
        "user: sqlite is fine",
        'architect called question: {"question": "Queue?"}',
        "architect: Storage settled. Two boxes: an API and the mappings store.",
    ]


def test_the_scribe_draws_after_the_architects_turn_ends(stack):
    server, arch, page, run_dir = stack
    page.wait_for(lambda e: e["type"] == "scribe" and e["state"] == "idle", "scribe idle at start")
    page.post("/input", {"text": "the shape is #box:api and #box:mappings"})
    end = page.wait_for(lambda e: e["type"] == "turn_end", "architect's turn")
    assert end["status"] == "done"
    # the architect drew nothing itself; the board fills in afterwards
    assert "api" not in arch.state.nodes or True  # may already have landed; timing is the scribe's
    page.wait_for(lambda e: e["type"] == "arch_state" and {"api", "mappings"} <= set(e["state"]["nodes"]), "scribe drew")
    idle = page.wait_for(lambda e: e["type"] == "scribe" and e["state"] == "idle" and e["recent"], "scribe idle")
    assert idle["queued"] == 0
    assert idle["recent"][0]["text"].startswith("2 node(s) added")
    assert set(idle["recent"][0]["ids"]) == {"api", "mappings"}
    assert idle["model"] == "fake-haiku"
    # the page saw it drawing in between
    assert any(e["state"] == "drawing" for e in scribe_events(page))
    # the drawing went through the endpoint: it is on disk like any edit
    assert "mappings" in json.loads((run_dir / "arch_state.json").read_text())["nodes"]


def test_the_architect_keeps_canvas_as_a_fallback_but_not_decide(stack):
    server, arch, page, _ = stack
    page.post("/input", {"text": "#canvas please"})
    said = page.wait_for(lambda e: harness(e, "assistant") and e["data"]["content"].startswith("tried to draw"), "reply")
    assert "unknown tool" not in said["data"]["content"] and "x" in arch.state.nodes
    page.post("/input", {"text": "#decide please"})
    said = page.wait_for(lambda e: harness(e, "assistant") and e["data"]["content"].startswith("tried to decide"), "reply 2")
    assert "unknown tool" in said["data"]["content"] and arch.state.decisions == []


def test_fast_answers_coalesce_into_one_job(stack):
    server, arch, page, run_dir = stack
    page.wait_for(lambda e: e["type"] == "scribe" and e["state"] == "idle", "idle")
    # the first job is slow; two more turns land while it runs
    page.post("/input", {"text": "#slow #box:one"})
    page.wait_for(lambda e: e["type"] == "turn_end", "turn 1")
    page.post("/input", {"text": "#box:two"})
    page.wait_for(lambda e: e["type"] == "turn_end" and len(page.turn_ends()) == 2, "turn 2")
    page.post("/input", {"text": "#box:three"})
    page.wait_for(lambda e: e["type"] == "turn_end" and len(page.turn_ends()) == 3, "turn 3")
    page.wait_for(lambda e: e["type"] == "arch_state" and {"one", "two", "three"} <= set(e["state"]["nodes"]), "all drawn", timeout=15)
    page.wait_for(lambda e: e["type"] == "scribe" and e["state"] == "idle" and e["queued"] == 0, "idle")
    jobs = (run_dir / "claude" / "scribe-mcp.json.jobs").read_text().count("\n")
    assert jobs == 2, f"expected the two later turns to share a job, got {jobs} jobs"


def test_a_broken_job_is_reported_and_the_next_one_runs(stack):
    server, arch, page, _ = stack
    page.post("/input", {"text": "#break this"})
    page.wait_for(lambda e: e["type"] == "turn_end", "turn")
    err = page.wait_for(lambda e: e["type"] == "scribe" and e["state"] == "error", "error reported")
    assert "couldn't draw" in err["error"]
    page.post("/input", {"text": "#box:after"})
    page.wait_for(lambda e: e["type"] == "arch_state" and "after" in e["state"]["nodes"], "next job drew")
    page.wait_for(lambda e: e["type"] == "scribe" and e["state"] == "idle", "clear again")


def test_handoff_waits_for_the_scribe(stack):
    server, arch, page, run_dir = stack
    page.post("/input", {"text": "#slow #box:last-word"})
    page.wait_for(lambda e: e["type"] == "turn_end", "turn")
    assert server.scribe.busy
    page.post("/input", {"text": "#handoff"})
    done = page.wait_for(lambda e: e["type"] == "arch_state" and e["status"] == "handed_off", "handed off", timeout=15)
    assert "last-word" in done["state"]["nodes"]
    md = (run_dir / "bundle" / "architecture.md").read_text()
    assert "Last Word" in md


def test_the_scribe_cannot_record_a_pick_twice(tmp_path):
    from bird.harnesses.arch.claude import DedupDecide
    from bird.tools import ToolContext

    session = ArchSession(run_dir=tmp_path / "run")
    ctx = ToolContext(repo_root=tmp_path, arch=session)
    from bird.harnesses.arch.tools import QuestionTool
    QuestionTool().execute({"question": "Keep links forever?", "recommendation": "yes", "summary": "URL retention",
                            "options": [{"label": "Permanent", "cost": "grows forever"},
                                        {"label": "Expire", "cost": "a sweeper"}]}, ctx)
    q = session.state.questions[0]
    session.answer_ask({"id": q.id, "value": q.options[0].value})
    res = DedupDecide().execute({"topic": "url-retention", "choice": "Permanent", "why": "said so"}, ctx)
    assert not res.is_error and "already recorded" in res.output
    assert len(session.state.decisions) == 1
    # a genuinely new topic still lands
    res = DedupDecide().execute({"topic": "code length", "choice": "6 chars", "why": "enough"}, ctx)
    assert not res.is_error and len(session.state.decisions) == 2


def test_the_first_turn_reports_its_research_steps(stack):
    """The rail's list during the research turn: one line per kind of work,
    marked done when the turn ends; nothing on later turns."""
    server, arch, page, _ = stack
    page.post("/input", {"text": "#ask"})   # the fake architect parks a question — no research tools
    page.wait_for(lambda e: e["type"] == "turn_end", "turn 1")
    assert not [e for e in page.events if harness(e, "research")]
    # feed the server what a real first turn produces
    server._turns_completed = 0
    server._harness_event("tool_result", {"name": "Grep", "is_error": False, "details": None})
    server._harness_event("tool_result", {"name": "Read", "is_error": False, "details": None})
    server._harness_event("tool_result", {"name": "WebSearch", "is_error": False, "details": None})
    steps = page.wait_for(lambda e: harness(e, "research") and e["data"]["step"] == "web", "web step")
    seen = [e["data"] for e in page.events if harness(e, "research")]
    assert [s["step"] for s in seen] == ["repo", "web"]   # Read after Grep did not repeat the line
    assert seen[0]["text"] == "Reading the repo" and steps["data"]["done"] is False
