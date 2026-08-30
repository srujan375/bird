"""Tests for model discovery (/model picker sources)."""

import httpx
import pytest

from bird.llm.discovery import discover_models
from bird.llm.registry import ProviderConfig, Registry


def make_registry(providers=None, models=None):
    return Registry(providers=providers or {}, models=models or {}, aliases={})


class FakeOllama:
    def __init__(self, up=True, models=()):
        self.up = up
        self.models = list(models)

    def is_up(self):
        return self.up

    def local_models(self):
        return self.models

    def close(self):
        pass


OLLAMA = ProviderConfig(name="ollama", base_url="http://localhost:11434/v1")
OPENROUTER = ProviderConfig(
    name="openrouter", base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY"
)


def test_configured_models_always_listed():
    reg = make_registry(models={"fake:model": {"context_window": 32768}})
    models, notes = discover_models(reg)
    assert [(m.spec, m.source, m.context_window) for m in models] == [
        ("fake:model", "configured", 32768)
    ]
    assert notes == []


def test_ollama_models_listed_and_deduped(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "k")  # no cloud fake given: catalog skipped, no note
    reg = make_registry(
        providers={"ollama": OLLAMA},
        models={"ollama:ornith": {"context_window": 262144}},
    )
    models, notes = discover_models(reg, ollama=FakeOllama(models=["ornith", "qwen3:8b"]))
    by_spec = {m.spec: m for m in models}
    assert by_spec["ollama:ornith"].source == "configured"  # configured wins over discovered
    assert by_spec["ollama:qwen3:8b"].source == "ollama"
    assert len(models) == 2
    assert notes == []


def test_ollama_down_becomes_note():
    reg = make_registry(providers={"ollama": OLLAMA})
    models, notes = discover_models(reg, ollama=FakeOllama(up=False))
    assert models == []
    assert any("ollama" in n for n in notes)


def test_openrouter_needs_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    reg = make_registry(providers={"openrouter": OPENROUTER})
    models, notes = discover_models(reg)
    assert models == []
    assert any("OPENROUTER_API_KEY" in n for n in notes)


@pytest.fixture
def openrouter_http():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "anthropic/claude-sonnet-5", "context_length": 200000},
                    {"id": "some/tiny-model"},
                ]
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openrouter_catalog_listed(monkeypatch, openrouter_http):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    reg = make_registry(providers={"openrouter": OPENROUTER})
    models, notes = discover_models(reg, http=openrouter_http)
    assert notes == []
    by_spec = {m.spec: m for m in models}
    assert by_spec["openrouter:anthropic/claude-sonnet-5"].context_window == 200000
    assert by_spec["openrouter:some/tiny-model"].context_window is None


def test_openrouter_failure_becomes_note(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    reg = make_registry(providers={"openrouter": OPENROUTER})
    models, notes = discover_models(reg, http=http)
    assert models == []
    assert any("openrouter" in n for n in notes)


def test_cloud_catalog_listed_with_marker(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    reg = make_registry(providers={"ollama": OLLAMA})
    models, notes = discover_models(
        reg,
        ollama=FakeOllama(models=["ornith"]),
        ollama_cloud=FakeOllama(models=["glm-5.3", "gpt-oss:120b"]),
    )
    by_spec = {m.spec: m.source for m in models}
    assert by_spec == {
        "ollama:ornith": "ollama",
        "ollama:glm-5.3:cloud": "ollama.com",
        "ollama:gpt-oss:120b-cloud": "ollama.com",
    }
    assert notes == []


def test_cloud_catalog_needs_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    reg = make_registry(providers={"ollama": OLLAMA})
    models, notes = discover_models(reg, ollama=FakeOllama(models=[]))
    assert models == []
    assert any("OLLAMA_API_KEY" in n for n in notes)
