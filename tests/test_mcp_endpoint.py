"""The MCP endpoint: bird's board tools served to an outside model runtime.

Stateless streamable HTTP — one JSON-RPC message in, one JSON body out. What
these tests hold: the four methods a tool server needs, the bearer token as
the only door, and the tool's own result reaching both the caller (as text)
and the embedder (as details)."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from bird.harnesses.arch.session import ArchSession
from bird.harnesses.arch.tools import arch_board_tools
from bird.http_transport import HttpTransport
from bird.mcp.endpoint import METHOD_NOT_FOUND, INVALID_PARAMS, McpEndpoint
from bird.tools import ToolContext

TOKEN = "t0k3n"


@pytest.fixture
def endpoint(tmp_path):
    session = ArchSession(run_dir=tmp_path / "run")
    ctx = ToolContext(repo_root=tmp_path, arch=session)
    ran = []
    ep = McpEndpoint("arch", arch_board_tools(), ctx, TOKEN,
                     on_result=lambda name, args, res: ran.append((name, args, res)))
    return ep, session, ran


def call(ep, method, params=None, req_id=1, token=TOKEN):
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    headers = {"authorization": f"Bearer {token}"} if token else {}
    status, ctype, body = ep.handle("POST", headers, json.dumps(msg).encode())
    return status, (json.loads(body) if body else None)


def test_initialize_negotiates_a_version_we_speak(endpoint):
    ep, _, _ = endpoint
    status, reply = call(ep, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
    assert status == 200
    assert reply["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in reply["result"]["capabilities"]
    # an unknown version gets our newest, not an error
    _, reply = call(ep, "initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_is_the_board_and_nothing_else(endpoint):
    ep, _, _ = endpoint
    _, reply = call(ep, "tools/list")
    names = {t["name"] for t in reply["result"]["tools"]}
    assert names == {"canvas", "approach", "decide", "question", "brief", "handoff", "import_repo"}
    canvas = next(t for t in reply["result"]["tools"] if t["name"] == "canvas")
    assert canvas["inputSchema"]["type"] == "object"
    assert canvas["description"]


def test_tools_call_moves_the_board_and_tells_the_embedder(endpoint):
    ep, session, ran = endpoint
    _, reply = call(ep, "tools/call", {
        "name": "canvas",
        "arguments": {"nodes": [{"label": "API", "kind": "api"}]},
    })
    assert reply["result"]["isError"] is False
    assert "1 node(s) added" in reply["result"]["content"][0]["text"]
    assert "api" in session.state.nodes
    name, args, res = ran[0]
    assert name == "canvas" and res.details["subjects"] == ["api"]


def test_a_tool_refusal_is_an_error_result_not_a_protocol_error(endpoint):
    ep, _, _ = endpoint
    _, reply = call(ep, "tools/call", {"name": "canvas", "arguments": {}})
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "nothing to do" in reply["result"]["content"][0]["text"]


def test_unknown_tool_and_method_are_jsonrpc_errors(endpoint):
    ep, _, _ = endpoint
    _, reply = call(ep, "tools/call", {"name": "nope", "arguments": {}})
    assert reply["error"]["code"] == INVALID_PARAMS
    _, reply = call(ep, "resources/list")
    assert reply["error"]["code"] == METHOD_NOT_FOUND


def test_notifications_are_accepted_and_unanswered(endpoint):
    ep, _, _ = endpoint
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    status, _, out = ep.handle("POST", {"authorization": f"Bearer {TOKEN}"}, body)
    assert status == 202 and out == b""


def test_a_batch_answers_each_request(endpoint):
    ep, _, _ = endpoint
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    status, _, out = ep.handle("POST", {"authorization": f"Bearer {TOKEN}"}, json.dumps(batch).encode())
    replies = json.loads(out)
    assert status == 200 and [r["id"] for r in replies] == [1, 2]


def test_the_token_is_the_only_door(endpoint):
    ep, _, _ = endpoint
    status, _ = call(ep, "ping", token="wrong")
    assert status == 401
    status, _ = call(ep, "ping", token=None)
    assert status == 401


def test_bad_json_is_a_parse_error(endpoint):
    ep, _, _ = endpoint
    status, _, out = ep.handle("POST", {"authorization": f"Bearer {TOKEN}"}, b"{not json")
    assert status == 400 and json.loads(out)["error"]["code"] == -32700


def test_get_and_delete(endpoint):
    ep, _, _ = endpoint
    assert ep.handle("GET", {"authorization": f"Bearer {TOKEN}"}, b"")[0] == 405
    assert ep.handle("DELETE", {"authorization": f"Bearer {TOKEN}"}, b"")[0] == 200


def test_mounted_on_the_transport_it_answers_over_http(tmp_path, endpoint):
    """The route hook: the transport hands the body over and writes the
    reply, and the built-in routes are untouched."""
    ep, session, _ = endpoint
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<title>arch</title>", encoding="utf-8")
    transport = HttpTransport(static_dir=static)
    transport.mount("/mcp", ep.handle)

    class Handlers:
        def on_user_input(self, text, subjects=()): pass

    thread = threading.Thread(target=transport.run, args=(Handlers(),), daemon=True)
    thread.start()
    try:
        host, port = transport._server.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "brief", "arguments": {"goal": "shorten urls"}}}
        conn.request("POST", "/mcp", body=json.dumps(msg),
                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        reply = json.loads(resp.read())
        assert resp.status == 200 and reply["id"] == 7
        assert session.state.brief.goal == "shorten urls"
        # wrong token over the wire
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/mcp", body=json.dumps(msg), headers={"Authorization": "Bearer nope"})
        assert conn.getresponse().status == 401
        # the page is still served
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/")
        assert conn.getresponse().status == 200
    finally:
        transport.shutdown()
        thread.join(timeout=5)
