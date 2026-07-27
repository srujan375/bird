import httpx
import pytest
from pytest_httpx import IteratorStream

from ox.llm.registry import ModelSpec, ProviderConfig
from ox.llm.types import Message, ToolSpec
from ox.llm.wire.openai_compat import MAX_TRANSPORT_ATTEMPTS, OpenAICompatClient, WireError


@pytest.fixture
def spec():
    return ModelSpec(
        spec="ollama:test-model",
        provider=ProviderConfig(name="ollama", base_url="http://localhost:11434/v1"),
        model="test-model",
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("ox.llm.wire.openai_compat.time.sleep", lambda s: None)
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
