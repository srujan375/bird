"""HttpTransport — the arch page's wire. Stdlib only (decision: no new deps).

Carries the same event vocabulary as StdioTransport, over HTTP on localhost:

  GET  /            → the harness's static page (index.html + assets)
  GET  /events      → SSE stream of the pump's JSON events
  POST /input       → {"text": ...}
  POST /permission  → {"id": n, "approved": bool, "feedback": "optional"}
  POST /interrupt   → {}
  POST /mutate      → {"op": ..., ...} — a UI edit; 200 {"ok": true} or 400
  POST /answer      → {"id": ..., "value": ...} — a row taken in a picker
  POST /capture     → {"id": ..., "png"|"error": ...} — the page's answer to a
                      capture_request; 200 {"ok": true} or 400
  POST /upload      → {"name": ..., "data": <base64>} — an image the composer
                      pasted or dropped; 200 {"ok": true, "path": ..., "size":
                      n} or 400. `path` is the reference the page puts in the
                      message text so the pump's ingest flow recognizes it.
  POST /board       → {} — send what the user drew; starts a turn
                      {"ok": false, "error": ...}. The resulting state arrives
                      on /events like every other change, so the reply carries
                      only the verdict.

Anything else is a mounted route: `mount(path, fn)` hands a path to an
embedder-owned handler that speaks its own protocol (the arch MCP endpoint
lives this way). The transport reads the body and writes the reply; it never
looks inside either.

Late joiners (including a mid-session browser refresh) are replayed: the
latest `ready`, a bounded buffer of transcript events (harness_event /
turn_end / error), the latest `arch_state` (full-replacement semantics make
one enough), the still-pending permission_request if a gate is open, and the
still-pending capture_request if a screenshot is being taken — the same
replay-on-refresh contract, because a page that reloads mid-capture is the
one that has to answer it.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

REPLAY_LIMIT = 500  # transcript events kept for refresh replay
# A page can only select one box today; this bounds a malformed client,
# not the UI.
MAX_SUBJECTS = 8
SSE_PING_SECONDS = 15.0
BUFFERED_TYPES = {"harness_event", "turn_end", "error"}
# Live-only: a replay would only repeat what the next event in the buffer
# already carries whole (`assistant` has the text; `assistant` and the
# tool_result have the call), and a page joining late has nothing half-drawn
# to append a fragment to.
LIVE_ONLY_EVENTS = {"assistant_delta", "tool_call_delta"}
LINGER_POLL_SECONDS = 0.5

# Queued to a client to force a write without sending it anything: it comes out
# as an SSE comment, which is how a gone-away tab gets noticed.
_PING = object()

# what a mounted route is: (method, headers, body) -> (status, content type, body)
RouteHandler = Callable[[str, dict[str, str], bytes], tuple[int, str, bytes]]

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
    known before run() blocks); serves until shutdown() or stop_when fires.

    `linger` is what stands between "the session is over" and "the page the
    user is reading went dead under them". With it set, stop_when starts a
    countdown instead of pulling the plug: the server keeps serving until the
    last page closes (detected within one SSE ping) or `linger` seconds pass,
    whichever comes first. Leave it at 0 when a caller is waiting on the return
    — the lead's dispatch path, tests — and the old behaviour is unchanged.

    `close_when_empty` is the other direction: the page closing IS the user
    leaving. Once a first page has subscribed, a room that stays empty for
    that many seconds shuts the server down, so a caller blocked on run() (a
    dispatched design session) gets its return instead of hanging on a tab
    nobody will reopen. Zero, the default, keeps today's behaviour: only
    stop_when ends the run.
    """

    def __init__(
        self,
        static_dir: Path,
        host: str = "127.0.0.1",
        port: int = 0,
        stop_when: Callable[[dict[str, Any]], bool] | None = None,
        linger: float = 0.0,
        close_when_empty: float = 0.0,
    ) -> None:
        self.static_dir = static_dir.resolve()
        self._stop_when = stop_when
        self._linger = max(0.0, linger)
        self._close_when_empty = max(0.0, close_when_empty)
        self._ever_subscribed = False  # arms close_when_empty
        self._closed = False  # run() has returned; background watchers exit
        self._stopping = False
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._buffer: deque[dict[str, Any]] = deque(maxlen=REPLAY_LIMIT)
        self._ready: dict[str, Any] | None = None
        self._arch_state: dict[str, Any] | None = None
        self._design_state: dict[str, Any] | None = None
        self._scribe: dict[str, Any] | None = None
        self._pending_perm: dict[str, Any] | None = None
        # the capture_request still waiting for the page's png, if any —
        # replayed to a late joiner like _pending_perm, cleared when the
        # answer POSTs back
        self._pending_capture: dict[str, Any] | None = None
        self._handlers: Any = None
        # path -> fn(method, headers, body) -> (status, content_type, body).
        # Checked before the built-in routes, so an embedder can serve a
        # protocol of its own without this file learning it.
        self._routes: dict[str, RouteHandler] = {}

        transport = self

        class Handler(_RequestHandler):
            _transport = transport

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def mount(self, path: str, handler: "RouteHandler") -> None:
        """Serve `path` (every method) with `handler`. Whole-path match only;
        the handler owns the wire format on both sides."""
        self._routes[path] = handler

    # ---- Transport interface ----

    def emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            kind = event.get("type")
            if kind == "ready":
                self._ready = event
            elif kind == "arch_state":
                self._arch_state = event
            elif kind == "design_state":
                self._design_state = event
            elif kind == "scribe":
                # the board's "drawing / up to date" strip; one is enough to replay
                self._scribe = event
            elif kind == "permission_request":
                self._pending_perm = event
            elif kind == "capture_request":
                self._pending_capture = event
            elif kind in BUFFERED_TYPES:
                if event.get("event") not in LIVE_ONLY_EVENTS:
                    self._buffer.append(event)
            for q in self._clients:
                q.put(event)
        if self._stop_when is not None and self._stop_when(event):
            self._begin_stop()

    def _begin_stop(self) -> None:
        """stop_when fired. Either pull the plug or start the read window."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        if self._linger <= 0:
            self.shutdown()
            return
        threading.Thread(target=self._linger_then_stop, daemon=True, name="http-linger").start()

    def _linger_then_stop(self) -> None:
        deadline = time.monotonic() + self._linger
        while time.monotonic() < deadline:
            time.sleep(LINGER_POLL_SECONDS)
            with self._lock:
                if not self._clients:
                    break  # the last page closed; nothing left to stay open for
                # No events flow after finalize, so a closed tab would otherwise
                # go unnoticed until the 15s keepalive. Poke each client: the
                # write fails, that thread unsubscribes, and the next poll sees
                # an empty room.
                for q in self._clients:
                    q.put(_PING)
        self.shutdown()

    def _watch_empty_room(self) -> None:
        """close_when_empty's watcher. Nothing else can end a dispatched
        design session — the caller is blocked on run() with no linger and no
        timeout — so a closed tab used to leave that turn hung forever: every
        one of the 16 logged design dispatches ended exactly that way, and the
        lead never got to say so. Armed by the first subscriber, so a page that
        was never opened (--no-open, a blocked auto-open) still gets its
        chance; from then on an empty room for the configured window shuts
        the server down. The poke is the linger's trick: a write to a dead
        socket is how a closed tab is noticed inside one poll instead of at
        the 15 s keepalive."""
        empty_since: float | None = None
        last_poke = 0.0
        poke_every = max(0.1, min(2.0, self._close_when_empty / 4))
        while not self._closed:
            time.sleep(LINGER_POLL_SECONDS)
            with self._lock:
                if self._stopping:
                    return  # stop_when got there first; the linger owns the exit
                if not self._ever_subscribed:
                    continue
                clients = list(self._clients)
            now = time.monotonic()
            if clients:
                empty_since = None
                if now - last_poke >= poke_every:
                    last_poke = now
                    for q in clients:
                        q.put(_PING)
                continue
            if empty_since is None:
                empty_since = now
            elif now - empty_since >= self._close_when_empty:
                self.shutdown()
                return

    def run(self, handlers: Any) -> None:
        self._handlers = handlers
        if self._close_when_empty > 0:
            threading.Thread(
                target=self._watch_empty_room, daemon=True, name="http-empty-room"
            ).start()
        try:
            self._server.serve_forever(poll_interval=0.1)
        finally:
            self._closed = True
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
                # Stamped so the page can tell "this design was already here when
                # I opened" from "this just arrived". A refresh mid-session
                # delivers the whole board at once, and a page that animates it
                # claims eleven boxes were created a moment ago.
                replay.append({**self._arch_state, "replayed": True})
            if self._design_state is not None:
                # Same stamp, same reason: the design page must tell a board it
                # is joining mid-session from one that just changed.
                replay.append({**self._design_state, "replayed": True})
            if self._scribe is not None:
                replay.append(self._scribe)
            if self._pending_perm is not None:
                replay.append(self._pending_perm)
            if self._pending_capture is not None:
                # a page that reloads mid-capture is the one that has to
                # answer it — same replay contract as the open gate above
                replay.append(self._pending_capture)
            q: queue.Queue = queue.Queue()
            self._clients.append(q)
            self._ever_subscribed = True
            return q, replay

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def _resolve_pending_perm(self) -> None:
        with self._lock:
            self._pending_perm = None

    def _resolve_pending_capture(self) -> None:
        """The capture round trip completed — the request must not replay to
        the next late joiner, or a refreshed page would re-capture and POST a
        second png the waiter would only drop."""
        with self._lock:
            self._pending_capture = None


class _RequestHandler(BaseHTTPRequestHandler):
    _transport: HttpTransport
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # localhost tool; server logs would just pollute the CLI

    # ---- GET: static page + SSE ----

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if self._serve_route("GET"):
            return
        if self.path == "/events":
            self._serve_events()
            return
        self._serve_static()

    def do_DELETE(self) -> None:  # noqa: N802 (stdlib API)
        if not self._serve_route("DELETE"):
            self._respond(404, {"error": "not found"})

    def _serve_route(self, method: str) -> bool:
        """A mounted route, if the path names one. True when it answered."""
        route = self._transport._routes.get(self.path.split("?", 1)[0])
        if route is None:
            return False
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            status, ctype, out = route(method, headers, body)
        except Exception as e:  # the route's bug must not kill the thread silently
            status, ctype, out = 500, "application/json", json.dumps({"error": str(e)}).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if out:
            self.wfile.write(out)
        return True

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
                    event = _PING
                if event is _PING:
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
        if self._serve_route("POST"):
            return
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
            # `subjects` is what the page had selected. It arrives over HTTP,
            # so nothing but this bounds its shape — the session decides what
            # the ids mean and drops the ones it cannot resolve.
            raw = payload.get("subjects")
            subjects = [str(s) for s in raw][:MAX_SUBJECTS] if isinstance(raw, list) else []
            handlers.on_user_input(str(payload.get("text", "")), subjects)
        elif self.path == "/permission":
            self._transport._resolve_pending_perm()
            handlers.on_permission(
                int(payload.get("id", 0)),
                bool(payload.get("approved")),
                str(payload.get("feedback", "") or ""),
            )
        elif self.path == "/interrupt":
            handlers.on_interrupt("/interrupt")
        elif self.path == "/board":
            # "send what I drew". Deliberately an explicit act: inferring it
            # from a pause spends a model call on an unfinished thought and
            # asks the architect to respond to something the user had not
            # finished saying. getattr, like /mutate: not every embedder
            # serves it.
            on_submit = getattr(handlers, "on_board_submit", None)
            if on_submit is None:
                self._respond(404, {"error": "not found"})
                return
            on_submit()
        elif self.path == "/answer":
            # A picker answer. Same getattr deal as /mutate: the harness that
            # asked the question settles it, and one that asks nothing serves
            # no route. The reply is the verdict only — the resulting state,
            # and the turn the answer may have unblocked, arrive on /events.
            on_answer = getattr(handlers, "on_answer", None)
            if on_answer is None:
                self._respond(404, {"error": "not found"})
                return
            result = on_answer(payload)
            self._respond(200 if result.get("ok") else 400, result)
            return
        elif self.path == "/mutate":
            # getattr, not a hard call: a transport shouldn't require every
            # embedder's Handlers to grow a method for a route it never serves
            on_mutate = getattr(handlers, "on_mutate", None)
            if on_mutate is None:
                self._respond(404, {"error": "not found"})
                return
            result = on_mutate(payload)
            self._respond(200 if result.get("ok") else 400, result)
            return
        elif self.path == "/capture":
            # The page's answer to a capture_request — the same shape as
            # /mutate: getattr, verdict-only reply, state changes on /events.
            # The pending request is retired here, where the round trip ends.
            on_capture = getattr(handlers, "on_capture", None)
            if on_capture is None:
                self._respond(404, {"error": "not found"})
                return
            self._transport._resolve_pending_capture()
            result = on_capture(payload)
            self._respond(200 if result.get("ok") else 400, result)
            return
        elif self.path == "/upload":
            # An image the composer pasted or dropped, as base64. Same shape
            # as /mutate: getattr, verdict-only reply — the attachment_saved
            # event, and the turn the message starts, arrive on /events.
            on_upload = getattr(handlers, "on_upload", None)
            if on_upload is None:
                self._respond(404, {"error": "not found"})
                return
            result = on_upload(payload)
            self._respond(200 if result.get("ok") else 400, result)
            return
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
