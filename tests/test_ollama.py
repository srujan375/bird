"""Tests for the Ollama native-API helper (auth header + lifecycle)."""

import json

import httpx
import pytest

from bird.llm.ollama import DEFAULT_API_KEY_ENV, Ollama, OllamaError


@pytest.fixture
def transport():
    """Per-test MockTransport so we can introspect requests."""
    captured: dict = {"paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["paths"].append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})
        if request.url.path == "/api/pull":
            return httpx.Response(
                200,
                content="\n".join(
                    [
                        json.dumps({"status": "pulling manifest"}),
                        json.dumps({"status": "success"}),
                    ]
                ).encode(),
            )
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"response": "", "done": True})
        return httpx.Response(404)

    return captured, httpx.MockTransport(handler)


def _client(captured_transport, **kwargs) -> tuple[Ollama, dict[str, httpx.Request]]:
    captured, tr = captured_transport
    o = Ollama(timeout=5.0, **kwargs)
    o._http._transport = tr  # type: ignore[attr-defined]
    return o, captured


def test_no_api_key_sends_no_auth_header(transport):
    o, captured = _client(transport)
    try:
        assert o.is_up() is True
        assert "Authorization" not in captured["request"].headers
        o.local_models()
        assert "Authorization" not in captured["request"].headers
    finally:
        o.close()


def test_explicit_api_key_sends_bearer_header(transport):
    o, captured = _client(transport, api_key="sk-explicit")
    try:
        o.local_models()
        assert captured["request"].headers["Authorization"] == "Bearer sk-explicit"
        o.pull("qwen2.5:7b")
        assert captured["request"].headers["Authorization"] == "Bearer sk-explicit"
    finally:
        o.close()


def test_env_var_api_key_is_read(monkeypatch, transport):
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "sk-from-env")
    o, captured = _client(transport, api_key_env=DEFAULT_API_KEY_ENV)
    try:
        o.is_up()
        assert captured["request"].headers["Authorization"] == "Bearer sk-from-env"
    finally:
        o.close()


def test_explicit_api_key_overrides_env(monkeypatch, transport):
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "sk-from-env")
    o, captured = _client(transport, api_key="sk-explicit", api_key_env=DEFAULT_API_KEY_ENV)
    try:
        o.is_up()
        assert captured["request"].headers["Authorization"] == "Bearer sk-explicit"
    finally:
        o.close()


def test_api_key_env_disabled_when_none(monkeypatch, transport):
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "sk-should-be-ignored")
    o, captured = _client(transport, api_key_env=None)
    try:
        o.is_up()
        assert "Authorization" not in captured["request"].headers
    finally:
        o.close()


def test_ensure_against_cloud_url_skips_pull_and_warm(transport):
    """Hosted endpoint: ensure() only verifies the catalog — ollama.com has no
    /api/pull (it 401s for everyone) and needs no keep_alive warming."""
    o, captured = _client(transport, api_key="sk-cloud")
    try:
        o.native_url = "https://ollama.com"  # type: ignore[misc]
        o.ensure("qwen2.5:7b")
        assert captured["request"].headers["Authorization"] == "Bearer sk-cloud"
        assert "/api/pull" not in captured["paths"]
        assert "/api/generate" not in captured["paths"]
    finally:
        o.close()


def test_ensure_against_cloud_url_rejects_unknown_model(transport):
    """A model missing from the hosted catalog (e.g. a ':cloud'-suffixed name)
    fails with a catalog error instead of a doomed pull."""
    o, captured = _client(transport, api_key="sk-cloud")
    try:
        o.native_url = "https://ollama.com"  # type: ignore[misc]
        with pytest.raises(OllamaError) as exc:
            o.ensure("qwen2.5:7b:cloud")
        assert "not in the catalog" in str(exc.value)
        assert "qwen2.5:7b" in str(exc.value)  # available models are listed
        assert "/api/pull" not in captured["paths"]
    finally:
        o.close()


def test_is_local():
    for url, expected in [
        ("http://localhost:11434", True),
        ("http://127.0.0.1:11434", True),
        ("https://ollama.com", False),
    ]:
        o = Ollama(url)
        try:
            assert o.is_local is expected, url
        finally:
            o.close()


def test_unreachable_mentions_api_key(monkeypatch):
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "sk-test")
    # A port nothing listens on — don't depend on whether the real local
    # daemon happens to be running.
    o = Ollama("http://localhost:9", api_key=__import__("os").environ.get(DEFAULT_API_KEY_ENV))
    try:
        with pytest.raises(OllamaError) as exc:
            o.ensure("whatever")
        msg = str(exc.value)
        assert "ollama.com" in msg
        assert DEFAULT_API_KEY_ENV in msg
    finally:
        o.close()


def test_pull_401_without_key_hints_at_env_var(monkeypatch):
    monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    o = Ollama(timeout=5.0)
    o._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    try:
        with pytest.raises(OllamaError) as exc:
            o.pull("qwen2.5:7b")
        assert "authentication required" in str(exc.value)
        assert DEFAULT_API_KEY_ENV in str(exc.value)
    finally:
        o.close()


def test_pull_401_with_key_says_rejected():
    """When a bearer token WAS sent, don't claim the key is missing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    o = Ollama(api_key="bogus", timeout=5.0)
    o._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    try:
        with pytest.raises(OllamaError) as exc:
            o.pull("qwen2.5:7b")
        assert "rejected" in str(exc.value)
        assert "authentication required" not in str(exc.value)
    finally:
        o.close()
