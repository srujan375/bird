#!/usr/bin/env python3
"""A fake MCP server for tests: newline-framed JSON-RPC 2.0 over stdio.

Spawned as a subprocess by the MCP client tests — only a real subprocess
exercises the wire honestly (framing, handshake, lifecycle). Behavior is
driven by argv flags so one script covers every test case:

  (none)            well-behaved server: handshake, tools/list, tools/call
  --no-init-reply   never answers `initialize` (handshake timeout)
  --hang-tool       the `hang` tool never replies (tools/call timeout)
  --die-after-init  exit(0) right after the handshake (dead-server reconnect)
  --ignore-sigterm  ignore SIGTERM (close() must escalate to kill)
  --log-stdout      print a non-JSON line before each reply (framing noise)

Tools exposed: `echo` (text reply), `fail` (isError result), `mixed`
(text + image blocks), `hang` (never replies, with --hang-tool).
"""

import json
import os
import signal
import sys
import time

NO_INIT_REPLY = "--no-init-reply" in sys.argv
HANG_TOOL = "--hang-tool" in sys.argv
DIE_AFTER_INIT = "--die-after-init" in sys.argv
LOG_STDOUT = "--log-stdout" in sys.argv

if "--ignore-sigterm" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the message back",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "fail",
        "description": "Always returns an error result",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mixed",
        "description": "Text plus a non-text block",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hang",
        "description": "Never replies",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(frame):
    if LOG_STDOUT:
        sys.stdout.write("some server log line that is not json\n")
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def reply(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def reply_error(req_id, code, message):
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def call_tool(params, req_id):
    name = params.get("name")
    args = params.get("arguments", {})
    if name == "echo":
        reply(req_id, {
            "content": [{"type": "text", "text": f"echo: {args.get('message', '')}"}],
            "isError": False,
        })
    elif name == "fail":
        reply(req_id, {
            "content": [{"type": "text", "text": "the tool failed"}],
            "isError": True,
        })
    elif name == "mixed":
        reply(req_id, {
            "content": [
                {"type": "text", "text": "here is an image"},
                {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"},
            ],
            "isError": False,
        })
    elif name == "hang" and HANG_TOOL:
        return  # never reply — the client's call timeout is what fires
    elif name == "hang":
        reply(req_id, {"content": [{"type": "text", "text": "done"}], "isError": False})
    else:
        reply_error(req_id, -32602, f"unknown tool '{name}'")


def main():
    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = frame.get("method")
        req_id = frame.get("id")
        if method == "initialize":
            if NO_INIT_REPLY:
                continue
            reply(req_id, {
                "protocolVersion": frame.get("params", {}).get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
            })
            initialized = True
            if DIE_AFTER_INIT:
                sys.exit(0)
        elif method == "notifications/initialized":
            continue  # notifications get no reply
        elif method == "tools/list":
            reply(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            call_tool(frame.get("params", {}), req_id)
        elif req_id is not None:
            reply_error(req_id, -32601, f"unknown method '{method}'")
    # stdin closed: exit promptly so close() doesn't need the kill path
    sys.exit(0)


if __name__ == "__main__":
    main()
