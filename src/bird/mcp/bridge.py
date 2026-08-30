"""The McpTool bridge: one bird Tool per discovered MCP tool.

The server's inputSchema becomes the tool's parameters verbatim (it is
already JSON Schema — the model sees exactly what the server declared).
run() forwards tools/call and maps MCP content blocks onto the dual-output
ToolResult: text blocks join into `output` (what the model sees), image and
other blocks land in `details` (the session log), and the MCP `isError` flag
maps to ToolResult.is_error.

Every bridged tool is named mcp__<server>__<tool> (non-alnum -> _) so
provenance is visible to the model and a server tool named `read` or `bash`
can never shadow a native one. requires_permission is True on all of them —
an MCP tool is arbitrary remote code behind a friendly name, so it flows
through the broker like bash. needs_permission stays the base True: an MCP
call is an external action with no read-only category to waive on, so every
call asks.
"""

from __future__ import annotations

import re
from typing import Any

from ..tools.base import Tool, ToolContext, ToolError, ToolResult
from .client import McpClient
from .config import McpError

_NON_ALNUM = re.compile(r"[^A-Za-z0-9_]")


def _sanitize(part: str) -> str:
    return _NON_ALNUM.sub("_", part)


class McpTool(Tool):
    """A Tool instance bound to one tool on one MCP server."""

    requires_permission = True

    def __init__(self, client: McpClient, server: str, tool: dict[str, Any]) -> None:
        self.client = client
        self.server = server
        self.remote_name = tool.get("name", "")
        self.name = f"mcp__{_sanitize(server)}__{_sanitize(self.remote_name)}"
        description = tool.get("description") or self.remote_name
        self.description = f"[{server}] {description}"
        schema = tool.get("inputSchema")
        # the server declares JSON Schema; a missing schema means "no args"
        self.parameters = schema if isinstance(schema, dict) else {
            "type": "object",
            "properties": {},
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = self.client.call_tool(self.remote_name, args)
        except McpError as e:
            raise ToolError(str(e)) from None
        return mcp_result_to_tool_result(result)


def mcp_result_to_tool_result(result: dict[str, Any]) -> ToolResult:
    """Map a tools/call result onto the dual-output ToolResult.

    Text content joins into the model-visible output; anything else (images,
    resources) is structured detail for the session log. A result with no
    content blocks at all still says *something* — silence reads as a hang.
    """
    texts: list[str] = []
    other: list[dict[str, Any]] = []
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        else:
            other.append(block)
    output = "\n".join(t for t in texts if t) or "(no text content in MCP result)"
    details: dict[str, Any] = {}
    if other:
        details["content"] = other
    if "structuredContent" in result:
        details["structuredContent"] = result["structuredContent"]
    return ToolResult(
        output=output,
        details=details,
        is_error=bool(result.get("isError")),
    )


def bridge_server_tools(client: McpClient) -> list[McpTool]:
    """One McpTool per tool the client discovered at start()."""
    return [McpTool(client, client.spec.name, tool) for tool in client.tools]
