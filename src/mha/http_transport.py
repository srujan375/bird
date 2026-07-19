"""HttpTransport — the arch page's wire. Stdlib only (decision: no new deps).

Carries the same event vocabulary as StdioTransport, over HTTP on localhost:

  GET  /            → the harness's static page (index.html + assets)
  GET  /events      → SSE stream of the pump's JSON events
  POST /input       → {"text": ...}
  POST /permission  → {"id": n, "approved": bool, "feedback": "optional"}
  POST /interrupt   → {}

Late joiners (including a mid-session browser refresh) are replayed: the
latest `ready`, a bounded buffer of transcript events (harness_event /
turn_end / error), the latest `arch_state` (full-replacement semantics make
one enough), and the still-pending permission_request if a gate is open.
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

REPLAY_LIMIT = 500  # transcript events kept for refresh replay
SSE_PING_SECONDS = 15.0
BUFFERED_TYPES = {"harness_event", "turn_end", "error"}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class HttpTransport:
    """Binds 127.0.0.1 on a random free port at construction (so the URL is
    known before run() blocks); serves until shutdown() or stop_when fires."""

    def __init__(
        self,
        static_dir: Path,
        host: str = "127.0.0.1",
        port: int = 0,
        stop_when: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.static_dir = static_dir.resolve()
        self._stop_when = stop_when
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._buffer: deque[dict[str, Any]] = deque(maxlen=REPLAY_LIMIT)
        self._ready: dict[str, Any] | None = None
        self._arch_state: dict[str, Any] | None = None
        self._pending_perm: dict[str, Any] | None = None
        self._handlers: Any = None

        transport = self

        class Handler(_RequestHandler):
            _transport = transport

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    # ---- Transport interface ----

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            kind = event.get("type")
            if kind == "ready":
                self._ready = event
            elif kind == "arch_state":
                self._arch_state = event
            elif kind == "permission_request":
                self._pending_perm = event
            elif kind in BUFFERED_TYPES:
                self._buffer.append(event)
            for q in self._clients:
                q.put(event)
        if self._stop_when is not None and self._stop_when(event):
            self.shutdown()

    def run(self, handlers: Any) -> None:
        self._handlers = handlers
        try:
            self._server.serve_forever(poll_interval=0.1)
        finally:
            self._server.server_close()

    def shutdown(self) -> None:
        """Stop serve_forever; safe from any thread (including emit's)."""
        threading.Thread(target=self._server.shutdown, daemon=True).start()

    # ---- SSE client bookkeeping ----

    def subscribe(self) -> tuple[queue.Queue, list[dict[str, Any]]]:
        """Atomically returns (live queue, replay list) — no missed or
        duplicated events between the replay snapshot and the live stream."""
        with self._lock:
            replay: list[dict[str, Any]] = []
            if self._ready is not None:
                replay.append(self._ready)
            replay.extend(self._buffer)
            if self._arch_state is not None:
                replay.append(self._arch_state)
            if self._pending_perm is not None:
                replay.append(self._pending_perm)
            q: queue.Queue = queue.Queue()
            self._clients.append(q)
            return q, replay

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def _resolve_pending_perm(self) -> None:
        with self._lock:
            self._pending_perm = None


class _RequestHandler(BaseHTTPRequestHandler):
    _transport: HttpTransport
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # localhost tool; server logs would just pollute the CLI

    # ---- GET: static page + SSE ----

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if self.path == "/events":
            self._serve_events()
            return
        self._serve_static()

    def _serve_static(self) -> None:
        rel = self.path.lstrip("/") or "index.html"
        rel = rel.split("?", 1)[0]
        root = self._transport.static_dir
        target = (root / rel).resolve()
        if not str(target).startswith(str(root)) or not target.is_file():
            self._respond(404, {"error": "not found"})
            return
        body = target.read_bytes()
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self) -> None:
        q, replay = self._transport.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            # SSE is an unbounded response; Content-Length can't apply
            self.send_header("Connection", "close")
            self.end_headers()
            for event in replay:
                self._write_event(event)
            while True:
                try:
                    event = q.get(timeout=SSE_PING_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(event)
                if event.get("type") == "bye":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client went away; unsubscribe below
        finally:
            self._transport.unsubscribe(q)

    def _write_event(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False, default=str)
        self.wfile.write(b"data: " + data.encode("utf-8") + b"\n\n")
        self.wfile.flush()

    # ---- POST: user actions ----

    def do_POST(self) -> None:  # noqa: N802 (stdlib API)
        handlers = self._transport._handlers
        if handlers is None:
            self._respond(503, {"error": "not running"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as e:
            self._respond(400, {"error": f"bad JSON: {e}"})
            return
        if self.path == "/input":
            handlers.on_user_input(str(payload.get("text", "")))
        elif self.path == "/permission":
            self._transport._resolve_pending_perm()
            handlers.on_permission(
                int(payload.get("id", 0)),
                bool(payload.get("approved")),
                str(payload.get("feedback", "") or ""),
            )
        elif self.path == "/interrupt":
            handlers.on_interrupt()
        else:
            self._respond(404, {"error": "not found"})
            return
        self._respond(200, {"ok": True})

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
