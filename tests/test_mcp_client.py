"""End-to-end tests for McpClient against a real fake stdio MCP server.

The fixture (tests/fixtures/fake_mcp_server.py) is spawned as a subprocess
speaking newline-framed JSON-RPC — the client's risk lives in the wire
(framing, handshake, lifecycle), so only a real subprocess exercises it
honestly. Timeouts are patched down so the failure paths stay fast.
"""

import sys
import time
from pathlib import Path

import pytest

from bird.mcp.client import McpClient
from bird.mcp.config import McpError, McpServerSpec

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _spec(*flags: str, name: str = "fake") -> McpServerSpec:
    return McpServerSpec(name=name, command=sys.executable,
                         args=[str(FIXTURE), *flags])


@pytest.fixture
def client():
    """A started client against the well-behaved fake server; always closed."""
    c = McpClient(_spec())
    c.start()
    yield c
    c.close()


# --- startup: spawn, handshake, tools/list ---

def test_start_handshakes_and_lists_tools(client):
    names = [t["name"] for t in client.tools]
    assert names == ["echo", "fail", "mixed", "hang"]
    echo = client.tools[0]
    assert echo["inputSchema"]["properties"]["message"]["type"] == "string"


def test_start_names_the_server_on_spawn_failure():
    c = McpClient(McpServerSpec(name="ghost", command="definitely-not-a-real-command-xyz"))
    with pytest.raises(McpError, match="mcp server 'ghost': cannot start"):
        c.start()


def test_start_times_out_when_initialize_is_never_answered(monkeypatch):
    monkeypatch.setattr("bird.mcp.client.INITIALIZE_TIMEOUT", 1.0)
    c = McpClient(_spec("--no-init-reply"))
    try:
        with pytest.raises(McpError, match="'initialize' timed out"):
            c.start()
    finally:
        c.close()


def test_failed_start_cleans_up_the_subprocess(monkeypatch):
    """A handshake failure must not leak a running server process."""
    monkeypatch.setattr("bird.mcp.client.INITIALIZE_TIMEOUT", 0.5)
    c = McpClient(_spec("--no-init-reply"))
    with pytest.raises(McpError):
        c.start()
    assert not c._alive()


# --- tools/call ---

def test_call_tool_returns_the_result_dict(client):
    result = client.call_tool("echo", {"message": "hello"})
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": "echo: hello"}]


def test_call_tool_passes_isError_through(client):
    result = client.call_tool("fail", {})
    assert result["isError"] is True


def test_call_tool_surfaces_json_rpc_errors(client):
    with pytest.raises(McpError, match="unknown tool 'nope'"):
        client.call_tool("nope", {})


def test_call_tool_timeout(monkeypatch):
    monkeypatch.setattr("bird.mcp.client.CALL_TIMEOUT", 1.0)
    c = McpClient(_spec("--hang-tool"))
    c.start()
    try:
        with pytest.raises(McpError, match="'tools/call' timed out"):
            c.call_tool("hang", {})
        # a timed-out call must not poison the wire: the next call still works
        result = c.call_tool("echo", {"message": "after"})
        assert result["content"][0]["text"] == "echo: after"
    finally:
        c.close()


def test_non_json_stdout_noise_does_not_break_framing():
    c = McpClient(_spec("--log-stdout"))
    c.start()
    try:
        result = c.call_tool("echo", {"message": "through the noise"})
        assert result["content"][0]["text"] == "echo: through the noise"
    finally:
        c.close()


# --- dead server & reconnect ---

def test_dead_server_fails_the_call_then_reconnects_once():
    """--die-after-init exits right after the handshake, so start() fails at
    tools/list. Drive the reconnect path directly: spawn, handshake, let the
    server die, then the next call gets one respawn attempt and succeeds."""
    c = McpClient(_spec("--die-after-init"))
    c._spawn()
    c._request("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }, timeout=5.0)
    c._notify("notifications/initialized", {})
    deadline = time.time() + 5
    while c._alive() and time.time() < deadline:
        time.sleep(0.05)
    assert not c._alive()
    # the server died between calls: one reconnect attempt, then the call
    # works. Aim the respawn at a healthy server — respawning the same
    # --die-after-init spec would exit again right after its own initialize
    # and could never answer the call.
    c.spec = _spec()
    result = c.call_tool("echo", {"message": "back from the dead"})
    assert result["content"][0]["text"] == "echo: back from the dead"
    c.close()


def test_dead_server_reconnect_failure_is_a_named_error():
    """The one reconnect attempt is against the same spec; when the command
    itself can't start, the error says the reconnect failed."""
    c = McpClient(_spec("--die-after-init"))
    c._spawn()
    c._request("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }, timeout=5.0)
    deadline = time.time() + 5
    while c._alive() and time.time() < deadline:
        time.sleep(0.05)
    # point the respawn at a command that cannot start
    c.spec = McpServerSpec(name="fake", command="definitely-not-a-real-command-xyz")
    with pytest.raises(McpError, match="reconnect failed"):
        c.call_tool("echo", {"message": "x"})
    c.close()


def test_call_on_a_closed_client_fails_fast(client):
    client.close()
    with pytest.raises(McpError, match="client is closed"):
        client.call_tool("echo", {"message": "x"})


# --- shutdown ---

def test_close_stops_the_process(client):
    proc = client._proc
    client.close()
    assert proc.poll() is not None
    assert not client._alive()


def test_close_is_idempotent(client):
    client.close()
    client.close()  # no raise, no hang


def test_close_escalates_to_kill_when_sigterm_is_ignored(monkeypatch):
    monkeypatch.setattr("bird.mcp.client.KILL_GRACE", 0.5)
    c = McpClient(_spec("--ignore-sigterm"))
    c.start()
    proc = c._proc
    start = time.time()
    c.close()
    assert proc.poll() is not None
    assert time.time() - start < 5  # terminate(0.5s grace) -> kill, not a hang


def test_context_manager():
    with McpClient(_spec()) as c:
        assert c.tools
        proc = c._proc
    assert proc.poll() is not None
