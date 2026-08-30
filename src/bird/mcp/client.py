"""The stdio MCP client: spawn a server subprocess, speak JSON-RPC 2.0.

One client per configured server. The wire is newline-framed JSON-RPC over
the child's stdin/stdout; a reader thread drains stdout line-by-line into a
pending-request map (id -> queue), and writes are serialized with a lock so
concurrent tool calls can't interleave a frame.

Lifecycle: start() spawns, does the initialize handshake, and runs tools/list
so the bridge knows what to mount. call_tool() has a 60s default timeout; a
dead pipe mid-call fails that call, and the NEXT call triggers one reconnect
attempt before erroring. close() is stdin close -> terminate -> kill after a
5s grace.

Stdlib only — the risk this class carries is framing, handshake, and
lifecycle, and none of that needs a dependency.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Any

from .config import McpError, McpServerSpec

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "bird", "version": "0.2.0"}

INITIALIZE_TIMEOUT = 10.0
LIST_TIMEOUT = 10.0
CALL_TIMEOUT = 60.0
KILL_GRACE = 5.0


class McpClient:
    """One MCP server subprocess. Not thread-safe to start/close, safe to
    call tools on from many threads (writes are locked, replies are routed
    by request id)."""

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.tools: list[dict[str, Any]] = []  # raw tool dicts from tools/list
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 0
        self._closed = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> list[dict[str, Any]]:
        """Spawn, handshake, list tools. Raises McpError naming the server on
        any failure — startup is fail-hard by design."""
        self._spawn()
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                timeout=INITIALIZE_TIMEOUT,
            )
            self._notify("notifications/initialized", {})
            result = self._request("tools/list", {}, timeout=LIST_TIMEOUT)
        except McpError:
            self.close()
            raise
        self.tools = list(result.get("tools", []))
        return self.tools

    def close(self) -> None:
        """stdin close -> terminate -> kill(5s). Idempotent."""
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for q in pending:
            q.put(McpError(f"mcp server '{self.spec.name}' is shutting down"))
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=KILL_GRACE)

    def _spawn(self) -> None:
        env = {**os.environ, **self.spec.env}
        try:
            self._proc = subprocess.Popen(
                [self.spec.command, *self.spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
        except OSError as e:
            raise McpError(
                f"mcp server '{self.spec.name}': cannot start "
                f"'{self.spec.command}': {e}"
            ) from None
        # Each connection gets its own pending map, handed to its reader
        # thread. When this process dies, that reader drains *its* map on
        # EOF — never the map of a connection spawned later (a respawn's
        # in-flight initialize must not be killed by the old reader).
        pending: dict[int, queue.Queue] = {}
        self._pending = pending
        self._reader = threading.Thread(target=self._read_loop, args=(pending,), daemon=True)
        self._reader.start()

    # ------------------------------------------------------------ tool calls

    def call_tool(self, name: str, args: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """tools/call. Returns the raw result dict (content blocks, isError).
        A dead pipe fails this call; the next call gets one reconnect attempt.
        timeout defaults to CALL_TIMEOUT resolved at call time so the module
        constant stays patchable (an import-time default would freeze it)."""
        if timeout is None:
            timeout = CALL_TIMEOUT
        try:
            return self._request("tools/call", {"name": name, "arguments": args}, timeout=timeout)
        except McpError:
            if self._closed or self._alive():
                raise
            # the server died between calls — one reconnect attempt, then give up
            self._respawn()
            return self._request("tools/call", {"name": name, "arguments": args}, timeout=timeout)

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _respawn(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=KILL_GRACE)
            except OSError:
                pass
        try:
            self._spawn()
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                timeout=INITIALIZE_TIMEOUT,
            )
            self._notify("notifications/initialized", {})
        except McpError as e:
            raise McpError(
                f"mcp server '{self.spec.name}': reconnect failed: {e}"
            ) from None

    # ------------------------------------------------------------ the wire

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._closed:
            raise McpError(f"mcp server '{self.spec.name}': client is closed")
        if not self._alive():
            raise McpError(f"mcp server '{self.spec.name}': process is not running")
        with self._write_lock:
            self._next_id += 1
            req_id = self._next_id
            q: queue.Queue = queue.Queue(maxsize=1)
            with self._pending_lock:
                self._pending[req_id] = q
            try:
                self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            except McpError:
                with self._pending_lock:
                    self._pending.pop(req_id, None)
                raise
        try:
            reply = q.get(timeout=timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise McpError(
                f"mcp server '{self.spec.name}': '{method}' timed out after {timeout:.0f}s"
            ) from None
        if isinstance(reply, McpError):
            raise reply
        if "error" in reply:
            err = reply["error"]
            raise McpError(
                f"mcp server '{self.spec.name}': '{method}' failed: "
                f"{err.get('message', err)}"
            )
        return reply.get("result", {})

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._write_lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, frame: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError(f"mcp server '{self.spec.name}': process is not running")
        try:
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise McpError(f"mcp server '{self.spec.name}': broken pipe: {e}") from None

    def _read_loop(self, pending: dict[int, queue.Queue]) -> None:
        """Drain stdout line-by-line into this connection's pending map. A
        response with an unknown id (a stray notification, a late reply after
        timeout) is dropped. EOF kills every waiter on THIS connection so a
        dead server never hangs a call — and never touches a newer one."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue  # a server that logs to stdout must not kill the wire
            req_id = frame.get("id")
            if req_id is None:
                continue  # notification, not a response
            with self._pending_lock:
                q = pending.pop(req_id, None)
            if q is not None:
                q.put(frame)
        # EOF: the process exited or closed stdout. Clear only the map this
        # reader owns — self._pending may already point at a respawn's map.
        with self._pending_lock:
            waiters = list(pending.values())
            pending.clear()
        for q in waiters:
            q.put(McpError(f"mcp server '{self.spec.name}': connection closed"))

    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
