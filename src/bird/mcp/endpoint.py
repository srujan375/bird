"""An MCP server endpoint, served from inside a bird process.

The other files in this package make bird an MCP *client*. This one is the
reverse: it hands a list of bird Tools to an outside model runtime over the
Model Context Protocol, so a Claude Code process can call `canvas` and
`decide` on a board that lives in this process. Streamable HTTP, stateless:
every request is one JSON-RPC message (or a batch) answered with one JSON
body, and nothing about a session is kept between calls — the state the
tools mutate is the caller's (ArchSession), not the protocol's.

Stdlib only, like the transport it mounts on. The protocol surface a tool
server needs is four methods, and the `mcp` package would be a dependency
for two hundred lines.

Auth is a bearer token minted per run. The endpoint sits on a localhost port
any process on the machine can reach; the token is what makes "the Claude Code
process bird spawned" the only caller that can move boxes.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..tools import Tool, ToolContext, ToolResult

# Newest first. A client asks for a version; we answer with it when we speak
# it, else with the newest we do — that is the negotiation the spec asks for.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_VERSION = "0.2.0"

JSON = "application/json"

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

ResultHook = Callable[[str, dict[str, Any], ToolResult], None]


class McpEndpoint:
    """`handle(method, headers, body)` is the whole interface: mount it on an
    HttpTransport path and it answers as an MCP server.

    `on_result` is called after every tools/call with (tool name, arguments,
    result) — the embedder's chance to narrate the call to its own UI, since
    the outside runtime only sees the text the model sees."""

    def __init__(
        self,
        name: str,
        tools: list[Tool],
        ctx: ToolContext,
        token: str,
        on_result: ResultHook | None = None,
    ) -> None:
        self.name = name
        self.tools = {t.name: t for t in tools}
        self.ctx = ctx
        self.token = token
        self.on_result = on_result

    # ---- the route ----

    def handle(self, method: str, headers: dict[str, str], body: bytes) -> tuple[int, str, bytes]:
        if not self._authorized(headers):
            return 401, JSON, _dump({"error": "unauthorized"})
        if method == "DELETE":
            return 200, JSON, b""  # nothing to tear down: no sessions
        if method == "GET":
            # no server-initiated stream: the tools push nothing on their own
            return 405, JSON, _dump({"error": "no server stream"})
        if method != "POST":
            return 405, JSON, _dump({"error": "method not allowed"})
        try:
            message = json.loads(body or b"")
        except (ValueError, json.JSONDecodeError) as e:
            return 400, JSON, _dump(_error(None, PARSE_ERROR, f"parse error: {e}"))

        if isinstance(message, list):
            replies = [r for r in (self._dispatch(m) for m in message) if r is not None]
            if not replies:
                return 202, JSON, b""  # notifications only
            return 200, JSON, _dump(replies)
        reply = self._dispatch(message)
        if reply is None:
            return 202, JSON, b""  # a notification: accepted, nothing to say
        return 200, JSON, _dump(reply)

    def _authorized(self, headers: dict[str, str]) -> bool:
        auth = headers.get("authorization", "")
        return auth == f"Bearer {self.token}"

    # ---- JSON-RPC ----

    def _dispatch(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(method, str):
            return _error(req_id, INVALID_REQUEST, "missing method")
        if req_id is None:
            return None  # a notification (initialized, cancelled, progress): nothing to answer
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._tools_list()
            elif method == "tools/call":
                result = self._tools_call(params)
            else:
                return _error(req_id, METHOD_NOT_FOUND, f"unknown method {method!r}")
        except _Invalid as e:
            return _error(req_id, INVALID_PARAMS, str(e))
        except Exception as e:  # a tool's own crash is the model's problem, not the wire's
            return _error(req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}")
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = str(params.get("protocolVersion") or "")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": self.name, "version": SERVER_VERSION},
        }

    def _tools_list(self) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.parameters or {"type": "object", "properties": {}},
                }
                for t in self.tools.values()
            ]
        }

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise _Invalid(f"unknown tool {name!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _Invalid("arguments must be an object")
        result = tool.execute(args, self.ctx)
        if self.on_result is not None:
            self.on_result(tool.name, args, result)
        return {
            "content": [{"type": "text", "text": result.output}],
            "isError": bool(result.is_error),
        }


class _Invalid(Exception):
    pass


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
