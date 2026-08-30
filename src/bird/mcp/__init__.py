"""bird as an MCP host: stdio MCP client, dynamic tool bridging, mcp.json config.

A configured MCP server is a subprocess speaking JSON-RPC 2.0 over its
stdin/stdout. `config` finds and parses mcp.json; `client` spawns the server
and speaks the wire; `bridge` turns each discovered tool into a bird Tool so
build_runner can mount it next to the native ones, gated like bash.
"""

from .config import McpError, McpServerSpec, load_mcp_servers

__all__ = ["McpError", "McpServerSpec", "load_mcp_servers"]
