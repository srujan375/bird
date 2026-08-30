import httpx
import pytest
from pytest_httpx import IteratorStream

from bird.llm.registry import ModelSpec, ProviderConfig
from bird.llm.types import Message, ToolSpec
from bird.llm.wire.openai_compat import MAX_TRANSPORT_ATTEMPTS, OpenAICompatClient, WireError


@pytest.fixture
def spec():
    return ModelSpec(
        spec="ollama:test-model",
        provider=ProviderConfig(name="ollama", base_url="http://localhost:11434/v1"),
        model="test-model",
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("bird.llm.wire.openai_compat.time.sleep", lambda s: None)
    c = OpenAICompatClient()
    yield c
    c.close()


def completion_body(message, finish_reason="stop"):
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_complete_text_response(client, spec, httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        json=completion_body({"role": "assistant", "content": "hi"}),
    )
    resp = client.complete(spec, [Message(role="user", content="hello")])
    assert resp.message.content == "hi"
    assert resp.stop_reason == "stop"
    assert resp.usage.input_tokens == 10
    assert resp.model == "ollama:test-model"


def test_complete_tool_call_response(client, spec, httpx_mock):
    httpx_mock.add_response(
        json=completion_body(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}}
                ],
            },
            finish_reason="tool_calls",
        )
    )
    resp = client.complete(spec, [Message(role="user", content="read a.py")])
    assert resp.stop_reason == "tool_calls"
    assert resp.message.tool_calls[0].arguments == {"path": "a.py"}


def test_tools_and_auth_in_request(client, httpx_mock, monkeypatch):
    monkeypatch.setenv("OR_KEY", "sk-or-test")
    or_spec = ModelSpec(
        spec="openrouter:foo/bar",
        provider=ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key_env="OR_KEY"),
        model="foo/bar",
    )
    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    tool = ToolSpec(name="read", description="d", parameters={"type": "object", "properties": {}})
    client.complete(or_spec, [Message(role="user", content="x")], tools=[tool])
    req = httpx_mock.get_requests()[0]
    assert req.headers["Authorization"] == "Bearer sk-or-test"
    import json

    payload = json.loads(req.content)
    assert payload["model"] == "foo/bar"
    assert payload["tools"][0]["function"]["name"] == "read"


def test_reasoning_effort_forwarded_for_ollama(client, httpx_mock):
    """When the provider is ollama and spec.extra carries reasoning_effort,
    the wire adapter must forward it into the /chat/completions payload."""
    import json

    thinking_spec = ModelSpec(
        spec="ollama:test-model",
        provider=ProviderConfig(name="ollama", base_url="http://localhost:11434/v1"),
        model="test-model",
        extra={"reasoning_effort": "medium"},
    )
    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    client.complete(thinking_spec, [Message(role="user", content="x")])
    payload = json.loads(httpx_mock.get_requests()[0].content)
    assert payload["reasoning_effort"] == "medium"


def test_reasoning_translated_for_openrouter(client, httpx_mock, monkeypatch):
    """OpenRouter takes the unified nested `reasoning` object, not a raw
    top-level reasoning_effort: spec.extra's value must be translated into
    payload["reasoning"]["effort"], and the raw key must NEVER appear."""
    import json

    monkeypatch.setenv("OR_KEY", "sk-or-test")
    or_spec = ModelSpec(
        spec="openrouter:foo/bar",
        provider=ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key_env="OR_KEY"),
        model="foo/bar",
        extra={"reasoning_effort": "medium"},
    )
    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    client.complete(or_spec, [Message(role="user", content="x")])
    payload = json.loads(httpx_mock.get_requests()[0].content)
    assert payload["reasoning"] == {"effort": "medium"}
    assert "reasoning_effort" not in payload


def test_reasoning_omitted_for_openrouter_when_off(client, httpx_mock, monkeypatch):
    """`/think off` stores reasoning_effort 'none'; OpenRouter has no off
    switch, so the adapter must omit the reasoning object entirely (provider
    defaults apply) rather than send exclude:true (which still spends
    reasoning tokens)."""
    import json

    monkeypatch.setenv("OR_KEY", "sk-or-test")
    or_spec = ModelSpec(
        spec="openrouter:foo/bar",
        provider=ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key_env="OR_KEY"),
        model="foo/bar",
        extra={"reasoning_effort": "none"},
    )
    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    client.complete(or_spec, [Message(role="user", content="x")])
    payload = json.loads(httpx_mock.get_requests()[0].content)
    assert "reasoning" not in payload
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize(
    ("internal", "effort"),
    [
        ("low", "low"),
        ("high", "high"),
        ("max", "high"),  # OpenRouter has no max; clamped to high
        ("minimal", "low"),  # OpenAI-style vocabulary -> nearest effort
    ],
)
def test_reasoning_vocabulary_mapped_for_openrouter(
    client, httpx_mock, monkeypatch, internal, effort
):
    """Every bird think-mode value maps onto OpenRouter's high|medium|low
    vocabulary; unknown values fall back to omitting the object."""
    import json

    monkeypatch.setenv("OR_KEY", "sk-or-test")
    or_spec = ModelSpec(
        spec="openrouter:foo/bar",
        provider=ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", api_key_env="OR_KEY"),
        model="foo/bar",
        extra={"reasoning_effort": internal},
    )
    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    client.complete(or_spec, [Message(role="user", content="x")])
    payload = json.loads(httpx_mock.get_requests()[0].content)
    assert payload["reasoning"] == {"effort": effort}
    assert "reasoning_effort" not in payload


def test_reasoning_effort_absent_when_not_set(client, spec, httpx_mock):
    """Current behavior preserved: no reasoning_effort in the payload when
    spec.extra doesn't carry it (Ollama auto-enables thinking on its own)."""
    import json

    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    client.complete(spec, [Message(role="user", content="x")])
    payload = json.loads(httpx_mock.get_requests()[0].content)
    assert "reasoning_effort" not in payload


def test_retries_on_500_then_succeeds(client, spec, httpx_mock):
    httpx_mock.add_response(status_code=500)
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    resp = client.complete(spec, [Message(role="user", content="x")])
    assert resp.message.content == "ok"
    assert len(httpx_mock.get_requests()) == 3


def test_no_retry_on_400(client, spec, httpx_mock):
    httpx_mock.add_response(status_code=400, text="bad request")
    with pytest.raises(WireError, match="HTTP 400"):
        client.complete(spec, [Message(role="user", content="x")])
    assert len(httpx_mock.get_requests()) == 1


def test_exhausts_retries(client, spec, httpx_mock):
    for _ in range(MAX_TRANSPORT_ATTEMPTS):
        httpx_mock.add_response(status_code=503)
    with pytest.raises(WireError, match="failed after"):
        client.complete(spec, [Message(role="user", content="x")])


def test_connection_error_retried(client, spec, httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    resp = client.complete(spec, [Message(role="user", content="x")])
    assert resp.message.content == "ok"


# ---------- streaming ----------

def sse(*chunks, done=True):
    import json as _json

    lines = [f"data: {_json.dumps(c)}\n\n".encode() for c in chunks]
    if done:
        lines.append(b"data: [DONE]\n\n")
    return IteratorStream(lines)


def delta_chunk(delta, finish_reason=None):
    return {"choices": [{"delta": delta, "finish_reason": finish_reason}]}


def test_stream_text_response(client, spec, httpx_mock):
    httpx_mock.add_response(
        stream=sse(
            delta_chunk({"role": "assistant", "content": "he"}),
            delta_chunk({"content": "llo"}),
            delta_chunk({}, finish_reason="stop"),
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
        ),
        headers={"content-type": "text/event-stream"},
    )
    deltas = []
    resp = client.complete(spec, [Message(role="user", content="x")], on_delta=deltas.append)
    # "" is the cancel heartbeat for the textless finish chunk; None marks end of text
    assert deltas == ["he", "llo", "", None]
    assert resp.message.content == "hello"
    assert resp.stop_reason == "stop"
    assert resp.usage.input_tokens == 7
    req = httpx_mock.get_requests()[0]
    import json

    payload = json.loads(req.content)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


def test_stream_tool_call_assembled_across_chunks(client, spec, httpx_mock):
    httpx_mock.add_response(
        stream=sse(
            delta_chunk(
                {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read", "arguments": '{"pa'}}]}
            ),
            delta_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}),
            delta_chunk({}, finish_reason="tool_calls"),
        ),
        headers={"content-type": "text/event-stream"},
    )
    deltas = []
    resp = client.complete(spec, [Message(role="user", content="x")], on_delta=deltas.append)
    # no text → only cancel heartbeats (one per chunk), no end marker
    assert deltas == ["", "", ""]
    assert resp.stop_reason == "tool_calls"
    assert resp.message.tool_calls[0].name == "read"
    assert resp.message.tool_calls[0].arguments == {"path": "a.py"}


def test_stream_retries_before_first_delta(client, spec, httpx_mock):
    httpx_mock.add_response(status_code=503)
    httpx_mock.add_response(
        stream=sse(delta_chunk({"content": "ok"}, finish_reason="stop")),
        headers={"content-type": "text/event-stream"},
    )
    deltas = []
    resp = client.complete(spec, [Message(role="user", content="x")], on_delta=deltas.append)
    assert resp.message.content == "ok"
    assert len(httpx_mock.get_requests()) == 2


def test_stream_drop_after_delta_is_fatal(client, spec, httpx_mock):
    import json as _json

    def body():
        yield f"data: {_json.dumps(delta_chunk({'content': 'par'}))}\n\n".encode()
        raise httpx.ReadError("connection reset")

    httpx_mock.add_response(stream=IteratorStream(body()), headers={"content-type": "text/event-stream"})
    deltas = []
    with pytest.raises(WireError, match="dropped mid-response"):
        client.complete(spec, [Message(role="user", content="x")], on_delta=deltas.append)
    assert deltas == ["par"]  # partial text was delivered, but never retried/duplicated
    assert len(httpx_mock.get_requests()) == 1


def test_abort_wakes_a_silent_stream_and_does_not_retry():
    """A provider that accepts the request and then never sends a byte leaves
    the client blocked in a socket read. abort() from another thread must
    surface as WireAborted promptly, without burning the retry budget."""
    import socket as _socket
    import threading
    import time as _time

    from bird.llm.wire.openai_compat import WireAborted

    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted = []

    def silent_server():
        conn, _ = srv.accept()
        accepted.append(conn)
        # swallow the request headers+body, then send headers and go quiet
        conn.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(65536)
        head, _, body = buf.partition(b"\r\n\r\n")
        length = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
        while len(body) < length:
            body += conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        _time.sleep(30)

    threading.Thread(target=silent_server, daemon=True).start()

    client = OpenAICompatClient(timeout=60.0)
    spec = ModelSpec(
        spec="ollama:m", provider=ProviderConfig(name="ollama", base_url=f"http://127.0.0.1:{port}/v1"), model="m",
    )
    outcome = {}

    def request():
        try:
            client.complete(spec, [Message(role="user", content="hi")], on_delta=lambda c: None)
        except BaseException as e:  # noqa: BLE001
            outcome["exc"] = e

    t = threading.Thread(target=request, daemon=True)
    t.start()
    deadline = _time.time() + 5
    while not accepted and _time.time() < deadline:
        _time.sleep(0.01)
    _time.sleep(0.3)  # let the request thread reach the blocking read
    started = _time.time()
    client.abort()
    t.join(timeout=5)
    assert not t.is_alive(), "request thread still blocked after abort()"
    assert isinstance(outcome.get("exc"), WireAborted), outcome
    assert _time.time() - started < 2.0
    client.close()


def test_abort_flag_sticks_until_cleared(client, spec, httpx_mock):
    from bird.llm.wire.openai_compat import WireAborted

    client.abort()  # nothing in flight: the flag is remembered
    with pytest.raises(WireAborted):
        client.complete(spec, [Message(role="user", content="hi")], on_delta=lambda c: None)
    client.clear_abort()
    httpx_mock.add_response(json=completion_body({"role": "assistant", "content": "ok"}))
    assert client.complete(spec, [Message(role="user", content="hi")]).message.content == "ok"
