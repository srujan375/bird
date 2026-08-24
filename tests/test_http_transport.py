"""Tests for HttpTransport — SSE, replay, POST dispatch, static serving."""

import http.client
import json
import socket
import threading
import time

import pytest

from bird.http_transport import HttpTransport


class Handlers:
    """Records inbound dispatches."""

    def __init__(self):
        self.inputs = []
        self.subjects = []
        self.permissions = []
        self.interrupts = 0

    def on_user_input(self, text, subjects=()):
        self.inputs.append(text)
        self.subjects.append(list(subjects))

    def on_permission(self, req_id, approved, feedback):
        self.permissions.append((req_id, approved, feedback))

    def on_interrupt(self):
        self.interrupts += 1

    def on_command(self, line):
        return None


@pytest.fixture
def served(tmp_path):
    """A running transport bound to a free port, with recording handlers."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<title>arch</title>", encoding="utf-8")
    (static / "app.js").write_text("// js", encoding="utf-8")
    transport = HttpTransport(static_dir=static)
    handlers = Handlers()
    thread = threading.Thread(target=transport.run, args=(handlers,), daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    yield transport, handlers, host, port
    transport.shutdown()
    thread.join(timeout=5)


def request(host, port, method, path, body=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


class SseReader:
    """Collects parsed SSE events from /events on a background thread."""

    def __init__(self, host, port):
        self.events = []
        self.cv = threading.Condition()
        self.conn = http.client.HTTPConnection(host, port, timeout=10)
        self.conn.request("GET", "/events")
        # grab the socket now: SSE answers `Connection: close`, so getresponse()
        # hands ownership to the response and leaves conn.sock None
        self.sock = self.conn.sock
        self.resp = self.conn.getresponse()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while True:
                fp = self.resp.fp
                if fp is None:
                    return  # closed under us
                line = fp.readline()
                if not line:
                    return
                line = line.strip()
                if line.startswith(b"data: "):
                    with self.cv:
                        self.events.append(json.loads(line[len(b"data: "):]))
                        self.cv.notify_all()
        except (OSError, ValueError):
            return

    def wait_for(self, type_, timeout=5.0):
        deadline = time.time() + timeout
        with self.cv:
            while True:
                for e in self.events:
                    if e["type"] == type_:
                        return e
                remaining = deadline - time.time()
                assert remaining > 0, f"timed out waiting for {type_}; got {self.events}"
                self.cv.wait(remaining)

    def close(self):
        """Close like a browser tab does — the server must actually notice.

        Order matters: shutdown() unblocks the reader (closing the buffered
        reader first would block on the in-flight read until its socket
        timeout), and only once that thread has unwound can both references to
        the socket be dropped. Closing just `conn` leaves the response holding
        one, and the fd — so the server keeps writing into a live socket.
        """
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        self.thread.join(timeout=2)
        self.resp.close()
        self.conn.close()
        self.sock.close()


def test_static_serving_and_traversal_guard(served):
    _, _, host, port = served
    status, data = request(host, port, "GET", "/")
    assert status == 200 and b"<title>arch</title>" in data
    status, data = request(host, port, "GET", "/app.js")
    assert status == 200 and b"// js" in data
    status, _ = request(host, port, "GET", "/../secret.txt")
    assert status == 404
    status, _ = request(host, port, "GET", "/missing.html")
    assert status == 404


def test_input_carries_what_the_page_had_selected(served):
    """The selection is the page's — the only way the harness learns it is if
    the message says so."""
    transport, handlers, host, port = served
    status, _ = request(host, port, "POST", "/input", {"text": "why this?", "subjects": ["idx"]})
    assert status == 200
    assert handlers.inputs[-1] == "why this?"
    assert handlers.subjects[-1] == ["idx"]


def test_input_without_a_selection_still_works(served):
    """Most messages point at nothing, and must not have to say so."""
    transport, handlers, host, port = served
    status, _ = request(host, port, "POST", "/input", {"text": "hello"})
    assert status == 200
    assert handlers.subjects[-1] == []


def test_a_malformed_selection_is_not_trusted(served):
    """It arrives over HTTP; nothing but the route bounds its shape."""
    transport, handlers, host, port = served
    status, _ = request(host, port, "POST", "/input", {"text": "hi", "subjects": "idx"})
    assert status == 200
    assert handlers.subjects[-1] == [], "a bare string is not a list of ids"
    status, _ = request(host, port, "POST", "/input", {"text": "hi", "subjects": ["a"] * 40})
    assert len(handlers.subjects[-1]) == 8, "and the list is capped"


def test_post_dispatch(served):
    transport, handlers, host, port = served
    status, _ = request(host, port, "POST", "/input", {"text": "hello"})
    assert status == 200
    status, _ = request(
        host, port, "POST", "/permission",
        {"id": 3, "approved": False, "feedback": "drop the cache"},
    )
    assert status == 200
    status, _ = request(host, port, "POST", "/interrupt", {})
    assert status == 200
    status, _ = request(host, port, "POST", "/nope", {})
    assert status == 404
    assert handlers.inputs == ["hello"]
    assert handlers.permissions == [(3, False, "drop the cache")]
    assert handlers.interrupts == 1


def test_live_stream_delivers_events(served):
    transport, _, host, port = served
    reader = SseReader(host, port)
    transport.emit({"type": "ready", "model": "m"})
    transport.emit({"type": "harness_event", "event": "assistant_delta", "data": {"text": "hi"}})
    got = reader.wait_for("harness_event")
    assert got["data"]["text"] == "hi"
    reader.close()


def test_late_joiner_replay_order(served):
    """A refresh gets: ready, buffered transcript events, latest arch_state,
    pending permission_request — in that order."""
    transport, _, host, port = served
    transport.emit({"type": "ready", "model": "m"})
    transport.emit({"type": "harness_event", "event": "run_start", "data": {"task": "t"}})
    transport.emit({"type": "arch_state", "phase": "intake", "state": {}})
    transport.emit({"type": "harness_event", "event": "tool_result", "data": {"name": "brief"}})
    transport.emit({"type": "arch_state", "phase": "propose", "state": {}})
    transport.emit({"type": "turn_end", "status": "reply"})
    transport.emit({"type": "permission_request", "id": 1, "kind": "finalize"})

    reader = SseReader(host, port)
    reader.wait_for("permission_request")
    types = [e["type"] for e in reader.events]
    assert types == ["ready", "harness_event", "harness_event", "turn_end", "arch_state", "permission_request"]
    # only the LATEST arch_state is replayed
    assert [e for e in reader.events if e["type"] == "arch_state"][0]["phase"] == "propose"
    reader.close()


def test_resolved_permission_not_replayed(served):
    transport, handlers, host, port = served
    transport.emit({"type": "ready", "model": "m"})
    transport.emit({"type": "permission_request", "id": 1, "kind": "finalize"})
    request(host, port, "POST", "/permission", {"id": 1, "approved": True})
    reader = SseReader(host, port)
    reader.wait_for("ready")
    time.sleep(0.1)
    assert not any(e["type"] == "permission_request" for e in reader.events)
    reader.close()


def test_stop_when_ends_run(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("x", encoding="utf-8")
    transport = HttpTransport(
        static_dir=static,
        stop_when=lambda e: e.get("type") == "arch_state" and e.get("phase") == "finalized",
    )
    done = threading.Event()

    def run():
        transport.run(Handlers())
        done.set()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.05)
    transport.emit({"type": "arch_state", "phase": "propose", "state": {}})
    assert not done.is_set()
    transport.emit({"type": "arch_state", "phase": "finalized", "state": {}})
    assert done.wait(timeout=5)


def _finalizing(static, **kw):
    return HttpTransport(
        static_dir=static,
        stop_when=lambda e: e.get("type") == "arch_state" and e.get("phase") == "finalized",
        **kw,
    )


def _static(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("x", encoding="utf-8")
    return static


def test_linger_keeps_serving_until_the_page_closes(tmp_path):
    """A finalized design is still worth reading, so stop_when starts a read
    window instead of pulling the plug. It closes when the last page does."""
    transport = _finalizing(_static(tmp_path), linger=30.0)
    done = threading.Event()

    def run():
        transport.run(Handlers())
        done.set()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.05)
    host, port = transport._server.server_address[:2]
    reader = SseReader(host, port)

    transport.emit({"type": "arch_state", "phase": "finalized", "state": {}})
    # a reader is attached, so the server stays up well past the old 0.3s death
    assert not done.wait(timeout=1.5)
    assert request(host, port, "GET", "/")[0] == 200

    reader.close()
    assert done.wait(timeout=10)  # noticed by the linger's poke, not the 15s ping


def test_linger_gives_up_when_nobody_is_reading(tmp_path):
    """No page attached (headless, --no-open) — nothing to linger for."""
    transport = _finalizing(_static(tmp_path), linger=30.0)
    done = threading.Event()

    def run():
        transport.run(Handlers())
        done.set()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.05)
    transport.emit({"type": "arch_state", "phase": "finalized", "state": {}})
    assert done.wait(timeout=5)


def test_replayed_arch_state_is_stamped_so_the_page_can_tell(tmp_path):
    """A late joiner gets the whole design at once. Without a marker the page
    cannot tell that from a design that just arrived, and animates history."""
    transport = HttpTransport(static_dir=_static(tmp_path))
    transport.emit({"type": "ready", "model": "m"})
    transport.emit({"type": "arch_state", "status": "open", "state": {}})

    _q, replay = transport.subscribe()
    arch = [e for e in replay if e["type"] == "arch_state"]
    assert arch and arch[0]["replayed"] is True

    # the live push itself is untouched — it really is new
    assert "replayed" not in transport._arch_state
    transport.shutdown()
