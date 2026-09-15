"""Tests for model discovery (/model picker sources)."""

import httpx
import pytest

from bird.llm.discovery import discover_models, ollama_context_window
from bird.llm.registry import ProviderConfig, Registry


def make_registry(providers=None, models=None):
    return Registry(providers=providers or {}, models=models or {}, aliases={})


class FakeOllama:
    def __init__(self, up=True, models=(), context=None):
        self.up = up
        self.models = list(models)
        self.context = dict(context or {})  # name -> what /api/show would say
        self.show_calls: list[str] = []

    def context_length(self, name):
        self.show_calls.append(name)
        return self.context.get(name)

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


def test_ollama_models_carry_the_daemons_context_window(monkeypatch):
    """A local model the picker lists must come with its real window — a
    pick without one is recorded at DEFAULT_CONTEXT_WINDOW and warned about."""
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    reg = make_registry(
        providers={"ollama": OLLAMA},
        models={"ollama:ornith": {"context_window": 4096}},
    )
    fake = FakeOllama(
        models=["ornith:latest", "qwen3.8:27b-mlx", "tiny"],
        context={"ornith:latest": 262144, "qwen3.8:27b-mlx": 262144},
    )
    models, notes = discover_models(reg, ollama=fake)
    by_spec = {m.spec: m for m in models}
    assert by_spec["ollama:qwen3.8:27b-mlx"].context_window == 262144
    assert by_spec["ollama:tiny"].context_window is None  # daemon couldn't say
    assert by_spec["ollama:ornith"].context_window == 4096  # configured wins, unasked
    assert fake.show_calls == ["qwen3.8:27b-mlx", "tiny"]
    assert notes == []


def test_ollama_context_window_only_asks_for_unconfigured_local_specs():
    reg = make_registry(
        providers={"ollama": OLLAMA},
        models={
            "ollama:ornith": {"context_window": 4096},
            # a /think choice alone: an entry that never learned its window
            "ollama:thinky": {"reasoning_effort": "low"},
        },
    )
    reg.aliases["default"] = "ollama:ornith"
    fake = FakeOllama(context={"qwen3.8:27b-mlx": 262144, "thinky": 8192})
    assert ollama_context_window(reg, "ollama:qwen3.8:27b-mlx", ollama=fake) == 262144
    assert ollama_context_window(reg, "ollama:thinky", ollama=fake) == 8192
    assert ollama_context_window(reg, "ollama:unknown", ollama=fake) is None
    # configured, aliased, and foreign specs never reach the daemon; a cloud
    # spec is answered from the static table (or None when it isn't in it)
    assert ollama_context_window(reg, "ollama:ornith", ollama=fake) is None
    assert ollama_context_window(reg, "default", ollama=fake) is None
    assert ollama_context_window(reg, "ollama:qwen3.8:cloud", ollama=fake) is None
    assert ollama_context_window(reg, "ollama:gpt-oss:120b-cloud", ollama=fake) is None
    assert ollama_context_window(reg, "openrouter:qwen/qwen3.8", ollama=fake) is None
    assert fake.show_calls == ["qwen3.8:27b-mlx", "thinky", "unknown"]


def test_ollama_context_window_reads_the_cloud_table():
    """A :cloud spec has no /api/show to ask, so its window comes from the
    static table — the value a pick persists instead of the 32k default."""
    reg = make_registry(providers={"ollama": OLLAMA})
    fake = FakeOllama()
    assert ollama_context_window(reg, "ollama:glm-5.3:cloud", ollama=fake) == 1000000
    assert ollama_context_window(reg, "ollama:gpt-oss:120b-cloud", ollama=fake) is None
    # a cloud spec whose entry already learned its window is left alone
    reg.models["ollama:kimi-k3:cloud"] = {"context_window": 900000}
    assert ollama_context_window(reg, "ollama:kimi-k3:cloud", ollama=fake) is None
    assert fake.show_calls == []  # the daemon is never asked about a cloud model


def test_cloud_catalog_models_carry_the_tables_window(monkeypatch):
    """The hosted catalog lists no window, so the picker must fill it from the
    table — otherwise picking a cloud model records 32k and warns."""
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    reg = make_registry(providers={"ollama": OLLAMA})
    models, notes = discover_models(
        reg,
        ollama=FakeOllama(models=[]),
        ollama_cloud=FakeOllama(models=["glm-5.3", "kimi-k3", "gpt-oss:120b"]),
    )
    by_spec = {m.spec: m.context_window for m in models}
    assert by_spec["ollama:glm-5.3:cloud"] == 1000000
    assert by_spec["ollama:kimi-k3:cloud"] == 900000
    assert by_spec["ollama:gpt-oss:120b-cloud"] is None  # not in the table
    assert notes == []


def test_ollama_context_window_without_an_ollama_provider():
    reg = make_registry(providers={"openrouter": OPENROUTER})
    assert ollama_context_window(reg, "ollama:qwen3.8", ollama=FakeOllama()) is None


def test_configured_entry_without_a_window_gets_the_daemons(monkeypatch):
    """/think on a freshly picked local model writes reasoning_effort alone;
    that entry must not hide the window the daemon can report."""
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    reg = make_registry(
        providers={"ollama": OLLAMA},
        models={"ollama:qwen3.8:27b-mlx": {"reasoning_effort": "none"}},
    )
    fake = FakeOllama(models=["qwen3.8:27b-mlx"], context={"qwen3.8:27b-mlx": 262144})
    models, _ = discover_models(reg, ollama=fake)
    (m,) = models
    assert (m.spec, m.source, m.context_window) == ("ollama:qwen3.8:27b-mlx", "configured", 262144)
