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


def test_set_think_mode_persists_and_round_trips(registry):
    assert registry.set_think_mode("ollama:qwen2.5-coder:14b", "medium") is True
    reloaded = Registry.load(registry.path)
    assert reloaded.models["ollama:qwen2.5-coder:14b"]["reasoning_effort"] == "medium"
    # resolve() populates spec.extra from leftover keys in the entry
    spec = reloaded.resolve("ollama:qwen2.5-coder:14b")
    assert spec.extra["reasoning_effort"] == "medium"
    # untouched keys survive the rewrite
    assert reloaded.aliases["judge"] == "openrouter:anthropic/claude-sonnet-5"


def test_set_think_mode_creates_entry_for_unknown_model(registry):
    """A model with no prior entry gets one with just the thinking mode."""
    assert registry.set_think_mode("openrouter:some/new-model", "high") is True
    reloaded = Registry.load(registry.path)
    assert reloaded.models["openrouter:some/new-model"]["reasoning_effort"] == "high"
    spec = reloaded.resolve("openrouter:some/new-model")
    assert spec.extra["reasoning_effort"] == "high"


def test_set_think_mode_none_removes_key(registry):
    registry.set_think_mode("ollama:qwen2.5-coder:14b", "medium")
    assert registry.set_think_mode("ollama:qwen2.5-coder:14b", None) is True
    reloaded = Registry.load(registry.path)
    assert "reasoning_effort" not in reloaded.models["ollama:qwen2.5-coder:14b"]
    spec = reloaded.resolve("ollama:qwen2.5-coder:14b")
    assert "reasoning_effort" not in spec.extra


def test_set_think_mode_without_file_is_session_only():
    reg = Registry(providers={}, models={}, aliases={})
    assert reg.set_think_mode("fake:model", "medium") is False
    assert reg.models["fake:model"]["reasoning_effort"] == "medium"
    # clearing on an in-memory registry also returns False
    assert reg.set_think_mode("fake:model", None) is False
    assert "reasoning_effort" not in reg.models["fake:model"]


def test_builtin_models_json_loads():
    # aliases are user-editable config: assert they resolve, not where they point
    reg = Registry.load()
    # `judge` went with the arch critic — the user is the critic now
    for alias in ("default", "architect", "compactor"):
        spec = reg.resolve(alias)
        assert spec.spec and spec.context_window > 0


# --- ollama routing: the model name decides cloud vs local -------------------

def test_cloud_marker_routes_to_ollama_com(registry):
    spec = registry.resolve("ollama:glm-5.3-flash:cloud")
    assert spec.spec == "ollama:glm-5.3-flash:cloud"  # the key stays intact
    assert spec.model == "glm-5.3-flash"  # the hosted catalog knows no marker
    assert spec.provider.native_url == "https://ollama.com"
    assert spec.provider.base_url == "https://ollama.com/v1"
    assert spec.provider.api_key_env == "OLLAMA_API_KEY"


def test_tagged_cloud_marker(registry):
    spec = registry.resolve("ollama:gpt-oss:120b-cloud")
    assert spec.model == "gpt-oss:120b"
    assert spec.provider.native_url == "https://ollama.com"


def test_unmarked_model_stays_local_even_if_provider_points_at_cloud(tmp_path):
    data = {
        "providers": {"ollama": {"base_url": "https://ollama.com/v1", "native_url": "https://ollama.com"}},
        "models": {},
        "aliases": {},
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(data))
    spec = Registry.load(p).resolve("ollama:ornith:35b")
    assert spec.model == "ornith:35b"
    assert spec.provider.native_url == "http://localhost:11434"
    assert spec.provider.base_url == "http://localhost:11434/v1"


def test_unmarked_model_honours_a_custom_local_daemon(tmp_path):
    data = {
        "providers": {"ollama": {"base_url": "http://gpubox:11434/v1", "native_url": "http://gpubox:11434"}},
        "models": {},
        "aliases": {},
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(data))
    spec = Registry.load(p).resolve("ollama:ornith")
    assert spec.provider.native_url == "http://gpubox:11434"


def test_split_and_add_cloud_marker_roundtrip():
    from bird.llm.registry import add_cloud_marker, split_cloud_marker

    for plain in ("glm-5.3", "gpt-oss:120b", "deepseek-v4-flash:0731"):
        marked = add_cloud_marker(plain)
        assert split_cloud_marker(marked) == (plain, True)
        assert add_cloud_marker(marked) == marked
    assert split_cloud_marker("ornith:35b") == ("ornith:35b", False)
    assert split_cloud_marker("cloud") == ("cloud", False)
