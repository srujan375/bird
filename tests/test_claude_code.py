"""The Claude Code wire: its stream-json → bird's harness events, and the
spawn line. The event lines here are shaped after a captured `claude -p
--output-format stream-json --include-partial-messages` run (2.1.x)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bird.claude_code import (
    BUILTIN_TOOLS,
    ClaudeCodeProcess,
    StreamMapper,
    build_argv,
    write_mcp_config,
)


def se(ev):
    return json.dumps({"type": "stream_event", "event": ev})


def mapper():
    events, results, inits = [], [], []
    m = StreamMapper(
        emit=lambda e, d: events.append((e, d)),
        details_for=lambda name: {"summary": f"details for {name}"},
        on_result=results.append,
        on_init=inits.append,
    )
    return m, events, results, inits


def test_a_turn_with_a_tool_call_maps_onto_the_harness_events():
    m, events, results, inits = mapper()
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-1",
                    "model": "claude-x", "mcp_servers": [{"name": "arch", "status": "connected"}]}),
        se({"type": "message_start", "message": {"id": "m1"}}),
        se({"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "mcp__arch__canvas", "input": {}}}),
        se({"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{\"nodes\":"}}),
        json.dumps({"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "tu1", "name": "mcp__arch__canvas", "input": {"nodes": []}}],
            "usage": {"input_tokens": 10, "output_tokens": 3}}}),
        se({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
            "usage": {"input_tokens": 10, "output_tokens": 30}}),
        se({"type": "message_stop"}),
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "Board: 1 node(s) added."}]}}),
        se({"type": "message_start", "message": {"id": "m2"}}),
        se({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Queue or "}}),
        se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "not?"}}),
        json.dumps({"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "text", "text": "Queue or not?"}], "usage": {"input_tokens": 8, "output_tokens": 2}}}),
        se({"type": "message_stop"}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                    "session_id": "s-1", "result": "Queue or not?",
                    "usage": {"input_tokens": 18, "cache_read_input_tokens": 100, "output_tokens": 40}}),
    ]
    for line in lines:
        m.feed(line + "\n")

    kinds = [e for e, _ in events]
    assert kinds == ["tool_call_delta", "assistant", "tool_result",
                     "assistant_delta", "assistant_delta", "assistant"]
    # the tool call: prefix stripped, arguments whole, streamed fragment named
    assert events[0][1] == {"index": 0, "name": "canvas", "text": "{\"nodes\":"}
    first = events[1][1]
    assert first["tool_calls"] == [{"name": "canvas", "arguments_json": "{\"nodes\": []}"}]
    assert first["content"] == "" and first["output_tokens"] == 30
    # the result: named by its call, with the embedder's details attached
    assert events[2][1]["name"] == "canvas"
    assert events[2][1]["details"] == {"summary": "details for canvas"}
    # the text turn
    assert events[3][1] == {"text": "Queue or "}
    assert events[5][1]["content"] == "Queue or not?" and events[5][1]["turn"] == 2
    # the end
    assert len(results) == 1
    r = results[0]
    assert r.status == "done" and r.session_id == "s-1" and r.turns == 2
    assert r.input_tokens == 118 and r.output_tokens == 40
    assert inits[0]["model"] == "claude-x" and m.model == "claude-x"


def test_without_partial_messages_the_assistant_still_lands_whole():
    """No message_stop arrives: the next user/result line is the boundary."""
    m, events, results, _ = mapper()
    m.feed(json.dumps({"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "text", "text": "hello"}]}}))
    assert events == []  # not whole yet
    m.feed(json.dumps({"type": "result", "subtype": "success", "session_id": "s", "usage": {}}))
    assert [e for e, _ in events] == ["assistant"] and events[0][1]["content"] == "hello"
    assert results[0].status == "done"


def test_builtin_tool_results_carry_no_details():
    m, events, _, _ = mapper()
    m.feed(json.dumps({"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "g1", "name": "Glob", "input": {"pattern": "*.md"}}]}}))
    m.feed(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "g1", "content": "No files found", "is_error": False}]}}))
    result = [d for e, d in events if e == "tool_result"][0]
    assert result["name"] == "Glob" and result["details"] is None


def test_error_results_and_max_turns():
    m, _, results, _ = mapper()
    m.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "session_id": "s", "usage": {}}))
    m.feed(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                       "session_id": "s", "usage": {}, "result": "boom"}))
    assert [r.status for r in results] == ["max_turns", "error"]
    assert results[1].is_error and results[1].summary == "boom"


def test_junk_lines_are_ignored():
    m, events, results, _ = mapper()
    m.feed("not json\n")
    m.feed("[1,2]\n")
    m.feed(json.dumps({"type": "rate_limit_event"}))
    assert events == [] and results == []


def test_the_spawn_line(tmp_path):
    argv = build_argv(bin_path="/x/claude", mcp_config=tmp_path / "mcp.json", mcp_server="arch",
                      system_prompt="be brief", session_id="abc", model="opus", max_turns=4)
    joined = " ".join(argv)
    assert argv[:2] == ["/x/claude", "-p"]
    for flag in ("--input-format stream-json", "--output-format stream-json",
                 "--include-partial-messages", "--permission-mode dontAsk",
                 "--strict-mcp-config", "--session-id abc", "--model opus", "--max-turns 4",
                 "--disallowedTools AskUserQuestion"):
        assert flag in joined, flag
    builtin = ",".join(BUILTIN_TOOLS)
    assert f"--tools {builtin}" in joined
    assert f"--allowedTools {builtin},mcp__arch" in joined
    assert "--append-system-prompt be brief" in joined
    # a respawn continues the session instead of minting one
    argv = build_argv(bin_path="c", mcp_config=tmp_path / "m", mcp_server="arch",
                      system_prompt="", session_id="abc", resume="abc")
    assert "--resume" in argv and "--session-id" not in argv


def test_the_mcp_config_names_one_http_server_with_the_token(tmp_path):
    path = write_mcp_config(tmp_path / "claude" / "mcp.json", name="arch",
                            url="http://127.0.0.1:5/mcp", token="T")
    cfg = json.loads(path.read_text())
    assert cfg == {"mcpServers": {"arch": {"type": "http", "url": "http://127.0.0.1:5/mcp",
                                           "headers": {"Authorization": "Bearer T"}}}}


FAKE = """\
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") == "control_request":
        print(json.dumps({"type": "control_response", "response": {"subtype": "success",
                          "request_id": msg["request_id"]}}), flush=True)
        continue
    text = msg["message"]["content"][0]["text"]
    print(json.dumps({"type": "result", "subtype": "success", "result": text.upper(),
                      "session_id": "s", "usage": {}}), flush=True)
    print("noise", file=sys.stderr, flush=True)
"""


def test_the_process_speaks_lines_both_ways(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    got, exits = [], []
    proc = ClaudeCodeProcess([sys.executable, str(script)], cwd=tmp_path,
                             on_line=got.append, on_exit=lambda c, tail: exits.append((c, tail)))
    proc.start()
    assert proc.alive
    proc.send_user("hello")
    proc.interrupt()
    proc.send_user("again")
    proc.terminate()
    assert proc.wait_exit(5)
    kinds = [json.loads(l)["type"] for l in got]
    assert kinds == ["result", "control_response", "result"]
    assert json.loads(got[0])["result"] == "HELLO"
    assert exits and exits[0][0] == 0 and "noise" in exits[0][1]
    assert not proc.alive
