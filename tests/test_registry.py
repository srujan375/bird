import json

import pytest

from bird.llm.registry import Registry, RegistryError


@pytest.fixture
def registry(tmp_path):
    data = {
        "providers": {
            "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
            "ollama": {"base_url": "http://localhost:11434/v1", "native_url": "http://localhost:11434", "api_key_env": None},
        },
        "models": {
            "ollama:qwen2.5-coder:14b": {"context_window": 32768, "supports_tools": True, "constrained_decoding": False}
        },
        "aliases": {"default": "ollama:qwen2.5-coder:14b", "judge": "openrouter:anthropic/claude-sonnet-5"},
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(data))
    return Registry.load(p)


def test_resolve_alias(registry):
    spec = registry.resolve("default")
    assert spec.spec == "ollama:qwen2.5-coder:14b"
    assert spec.model == "qwen2.5-coder:14b"  # colon inside model name survives
    assert spec.provider.name == "ollama"
    assert spec.context_window == 32768


def test_resolve_full_spec_with_colons(registry):
    spec = registry.resolve("ollama:qwen2.5-coder:14b")
    assert spec.model == "qwen2.5-coder:14b"


def test_resolve_unknown_model_gets_defaults(registry):
    spec = registry.resolve("openrouter:some/new-model")
    assert spec.model == "some/new-model"
    assert spec.context_window == 32768
    assert spec.supports_tools is True


def test_resolve_bad_name_raises(registry):
    with pytest.raises(RegistryError):
        registry.resolve("not-an-alias")


def test_resolve_unknown_provider_raises(registry):
    with pytest.raises(RegistryError):
        registry.resolve("nope:model")


def test_api_key_env(registry, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    spec = registry.resolve("judge")
    assert spec.provider.api_key == "sk-test"
    assert registry.resolve("default").provider.api_key is None


def test_set_default_persists(registry):
    assert registry.set_default("openrouter:some/new-model", context_window=131072) is True
    reloaded = Registry.load(registry.path)
    assert reloaded.aliases["default"] == "openrouter:some/new-model"
    spec = reloaded.resolve("default")
    assert spec.context_window == 131072
    # untouched keys survive the rewrite
    assert reloaded.aliases["judge"] == "openrouter:anthropic/claude-sonnet-5"
    assert "ollama:qwen2.5-coder:14b" in reloaded.models


def test_set_default_without_file_is_session_only():
    reg = Registry(providers={}, models={}, aliases={})
    assert reg.set_default("fake:model") is False
    assert reg.aliases["default"] == "fake:model"


def test_builtin_models_json_loads():
    # aliases are user-editable config: assert they resolve, not where they point
    reg = Registry.load()
    for alias in ("default", "judge", "compactor"):
        spec = reg.resolve(alias)
        assert spec.spec and spec.context_window > 0
