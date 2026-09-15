"""End-to-end: the arch Workbench on Claude Code's loop, over real HTTP.

A fake `claude` — a script that speaks the stream-json wire and really calls
the board tools over the MCP endpoint bird serves — plays the engine; the
test plays the page. What has to hold: the page sees the same events it sees
from bird's own engine (run_start, assistant, tool_result with details,
arch_state, turn_end), the [arch] note rides on every message, edits made on
the page reach the next turn, the picker round-trips, an interrupt lands, a
dead process is respawned with --resume, and a handoff ends the session.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time

import pytest

from bird.engine.session import SessionRecorder
from bird.harnesses.arch import harness as arch_def
from bird.harnesses.arch.claude import (
    CLAUDE_SESSION_FILENAME,
    ClaudeArchServer,
    load_claude_session,
    system_prompt,
)
from bird.harnesses.arch.run import HANDED_OFF
from bird.harnesses.arch.session import ArchSession
from bird.http_transport import HttpTransport

# The engine stand-in. Reads the spawn line for --mcp-config / --session-id /
# --resume, logs its argv, and answers each user line by what it says:
#   "#draw"    → tools/call canvas over MCP, then a text reply
#   "#ask"     → tools/call question (with options)
#   "#handoff" → tools/call handoff
#   "#hang"    → wait for an interrupt control_request, then finish
#   "#die"     → answer, then exit
# Tokens, not words: the [arch] note rides on every message and "changed"
# contains "hang".
# Every result echoes the full message it received, so the test can read what
# the architect was actually sent.
FAKE_CLAUDE = r'''#!%(python)s
import json, sys, urllib.request

argv = sys.argv[1:]
def opt(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None
cfg = json.load(open(opt("--mcp-config")))["mcpServers"]["arch"]
session = opt("--resume") or opt("--session-id")
with open(opt("--mcp-config") + ".argv", "a") as f:
    f.write(json.dumps(argv) + "\n")

def out(obj):
    print(json.dumps(obj), flush=True)

def mcp(method, params, rid=1):
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
    req = urllib.request.Request(cfg["url"], data=body, method="POST",
                                 headers={**cfg["headers"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)

def tool(name, args, tid):
    full = "mcp__arch__" + name
    out({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": tid, "name": full, "input": {}}}})
    reply = mcp("tools/call", {"name": name, "arguments": args})
    out({"type": "assistant", "message": {"id": "m-" + tid, "content": [
        {"type": "tool_use", "id": tid, "name": full, "input": args}], "usage": {"input_tokens": 5, "output_tokens": 1}}})
    out({"type": "stream_event", "event": {"type": "message_stop"}})
    out({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": reply["result"]["content"][0]["text"],
         "is_error": reply["result"]["isError"]}]}})
    return reply

def say(text, mid):
    out({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
    out({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}}})
    for piece in (text[: len(text) // 2], text[len(text) // 2:]):
        out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": piece}}})
    out({"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": text}],
         "usage": {"input_tokens": 7, "output_tokens": 3}}})
    out({"type": "stream_event", "event": {"type": "message_stop"}})

init = mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
status = "connected" if "result" in init else "failed"
out({"type": "system", "subtype": "init", "session_id": session, "model": "fake-claude",
     "tools": ["Read"], "mcp_servers": [{"name": "arch", "status": status}]})

n = 0
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") != "user":
        continue
    n += 1
    text = msg["message"]["content"][0]["text"]
    if "#draw" in text:
        tool("canvas", {"nodes": [{"id": "api", "label": "API", "kind": "api"}]}, "tu%%d" %% n)
        say("Drawn. Queue or not?", "m%%d" %% n)
    elif "#ask" in text:
        tool("question", {"question": "Queue?", "recommendation": "no",
                          "options": [{"label": "queue", "cost": "retries for free"},
                                      {"label": "in-process", "cost": "simpler", "rec": True}]},
             "tu%%d" %% n)
        say("Parked.", "m%%d" %% n)
    elif "#handoff" in text:
        tool("handoff", {"summary": "one box"}, "tu%%d" %% n)
        say("Done.", "m%%d" %% n)
    elif "#hang" in text:
        for more in sys.stdin:
            if json.loads(more).get("type") == "control_request":
                out({"type": "control_response", "response": {"subtype": "success",
                     "request_id": json.loads(more)["request_id"]}})
                break
        say("(stopped)", "m%%d" %% n)
    else:
        say("Noted.", "m%%d" %% n)
    out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
         "session_id": session, "result": text,
         "usage": {"input_tokens": 10, "output_tokens": 4}})
    if "#die" in text:
        sys.exit(0)
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


@pytest.fixture
def stack(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE % {"python": sys.executable})
    fake.chmod(0o755)
    run_dir = tmp_path / ".bird" / "sessions" / "arch-c"
    recorder = SessionRecorder(run_dir)
    arch = ArchSession(run_dir=run_dir)
    transport = HttpTransport(static_dir=arch_def.STATIC_DIR, stop_when=HANDED_OFF)
    server = ClaudeArchServer(
        arch=arch, transport=transport, recorder=recorder, repo_root=tmp_path,
        run_dir=run_dir, run_id="arch-c", bin_path=str(fake),
        with_scribe=False,  # the single-agent path; the scribe has its own file
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


def test_a_turn_draws_on_the_board_through_mcp(stack):
    server, arch, page, run_dir = stack
    page.post("/input", {"text": "please #draw the api"})

    started = page.wait_for(lambda e: harness(e, "run_start"), "run_start")
    assert started["data"]["task"] == "please #draw the api"  # the note is not the user's words
    # the process said which model it is; the page learns it from a second ready
    page.wait_for(lambda e: e["type"] == "ready" and e["model"] == "fake-claude", "model")
    # the tool call landed on the board, and the board was pushed
    pushed = page.wait_for(lambda e: e["type"] == "arch_state" and "api" in e["state"]["nodes"], "board")
    assert pushed["status"] == "open"
    # the page gets the same tool_result it gets from bird's engine: named,
    # with the tool's own details attached
    result = page.wait_for(lambda e: harness(e, "tool_result"), "tool_result")
    assert result["data"]["name"] == "canvas"
    assert result["data"]["details"]["subjects"] == ["api"]
    # streamed text, then the whole message, then the end of the turn
    page.wait_for(lambda e: harness(e, "assistant_delta"), "delta")
    said = page.wait_for(lambda e: harness(e, "assistant") and e["data"]["content"], "assistant")
    assert said["data"]["content"] == "Drawn. Queue or not?"
    end = page.wait_for(lambda e: e["type"] == "turn_end", "turn_end")
    assert end["status"] == "done" and end["input_tokens"] == 10 and end["output_tokens"] == 4
    # the architect was sent the [arch] note along with the words
    assert "[arch]" in end["summary"] and end["summary"].startswith("please #draw the api")
    assert "frontier:" in end["summary"]
    # the claude session is remembered for a later bird --resume
    saved = load_claude_session(run_dir)
    assert saved == server.session_id and saved


def test_page_edits_reach_the_next_turn(stack):
    server, arch, page, _ = stack
    page.post("/input", {"text": "#draw"})
    page.wait_for(lambda e: e["type"] == "turn_end", "first turn")
    # the user renames the box on the page, then talks
    page.post("/mutate", {"op": "node", "id": "api", "label": "Gateway"})
    page.wait_for(lambda e: e["type"] == "arch_state" and e["state"]["nodes"]["api"]["label"] == "Gateway",
                  "renamed")
    page.post("/input", {"text": "thoughts?"})
    end = page.wait_for(lambda e: e["type"] == "turn_end" and "thoughts?" in e["summary"], "second turn")
    sent = end["summary"]
    assert sent.startswith("[the user changed the board]")
    assert "Gateway" in sent and "thoughts?" in sent


def test_the_picker_round_trips(stack):
    server, arch, page, _ = stack
    page.post("/input", {"text": "#ask me"})
    asked = page.wait_for(lambda e: e["type"] == "arch_state" and e.get("ask"), "ask")
    qid = asked["ask"]["id"]
    rows = asked["ask"]["options"]
    assert [r["label"] for r in rows] == ["queue", "in-process"]
    page.wait_for(lambda e: e["type"] == "turn_end", "turn")
    # the user takes a row: the state settles it and a turn carries the pick
    page.post("/answer", {"id": qid, "value": rows[1]["value"]})
    page.wait_for(lambda e: e["type"] == "arch_state" and e.get("ask") is None, "settled")
    started = page.wait_for(lambda e: harness(e, "run_start") and "[the user picked]" in e["data"]["task"], "pick turn")
    assert "in-process" in started["data"]["task"]
    assert arch.state.questions[0].status == "answered"


def test_an_interrupt_stops_the_turn(stack):
    server, arch, page, _ = stack
    page.post("/input", {"text": "#hang on this"})
    page.wait_for(lambda e: harness(e, "run_start"), "run_start")
    time.sleep(0.3)
    page.post("/interrupt", {})
    end = page.wait_for(lambda e: e["type"] == "turn_end", "turn_end")
    assert end["status"] == "interrupted" and end["reason"] == "user"
    # and the process is still usable
    page.post("/input", {"text": "still there?"})
    ends = lambda: [e for e in page.events if e["type"] == "turn_end"]
    page.wait_for(lambda e: e["type"] == "turn_end" and len(ends()) == 2, "second turn")
    assert ends()[1]["status"] == "done"


def test_a_dead_process_is_respawned_with_resume(stack):
    server, arch, page, run_dir = stack
    page.post("/input", {"text": "#draw then #die"})
    page.wait_for(lambda e: e["type"] == "turn_end", "first turn")
    first_session = server.session_id
    time.sleep(0.3)  # let the exit land
    page.post("/input", {"text": "back?"})
    page.wait_for(lambda e: e["type"] == "turn_end" and "back?" in e.get("summary", ""), "second turn")
    argv_log = (run_dir / "claude" / "mcp.json.argv").read_text().splitlines()
    assert len(argv_log) == 2
    first, second = (json.loads(l) for l in argv_log)
    assert "--session-id" in first and first[first.index("--session-id") + 1] == first_session
    assert "--resume" in second and second[second.index("--resume") + 1] == first_session
    assert server.session_id == first_session
    # the spawn line is the real thing, not a test-only one
    assert "--strict-mcp-config" in first and "--permission-mode" in first
    assert first[first.index("--append-system-prompt") + 1] == system_prompt()


def test_handoff_ends_the_session(stack):
    server, arch, page, _ = stack
    page.post("/input", {"text": "#draw"})  # handoff refuses an empty board
    page.wait_for(lambda e: e["type"] == "turn_end", "first turn")
    page.post("/input", {"text": "#handoff now"})
    done = page.wait_for(lambda e: e["type"] == "arch_state" and e["status"] == "handed_off", "handed off")
    assert done["state"]["handed_off"]
    page.wait_for(lambda e: e["type"] == "bye", "bye")
    assert arch.state.handed_off


def test_the_system_prompt_is_the_instructions_plus_the_claude_addendum():
    text = system_prompt()
    assert text.startswith("# Architecture harness")
    assert "mcp__arch__canvas" in text and "AskUserQuestion" in text
