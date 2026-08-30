"""The user config layer: ~/.bird/models.json merged over the builtin package
file, and persistence that never touches the builtin.

USER_MODELS_JSON / USER_BIRD_DIR are module-level constants in
bird.llm.registry, so every test here monkeypatches them onto the module —
no test ever reads or writes a real ~/.bird.
"""

import json

import pytest

import bird.llm.registry as registry_mod
from bird.llm.registry import Registry, RegistryError


@pytest.fixture
def user_paths(tmp_path, monkeypatch):
    """Point the module-level user-config constants at tmp_path."""
    bird_dir = tmp_path / ".bird"
    user_json = bird_dir / "models.json"
    monkeypatch.setattr(registry_mod, "USER_BIRD_DIR", bird_dir)
    monkeypatch.setattr(registry_mod, "USER_MODELS_JSON", user_json)
    return bird_dir, user_json


@pytest.fixture
def builtin_override(tmp_path, monkeypatch):
    """A small, fully-known builtin catalog so merge assertions are exact."""
    data = {
        "providers": {
            "ollama": {"base_url": "http://localhost:11434/v1", "native_url": "http://localhost:11434"},
            "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
        },
        "models": {
            "ollama:qwen2.5-coder:14b": {"context_window": 32768},
            "openrouter:anthropic/claude-sonnet-5": {"context_window": 200000},
        },
        "aliases": {
            "default": "ollama:qwen2.5-coder:14b",
            "architect": "openrouter:anthropic/claude-sonnet-5",
            "compactor": "ollama:qwen2.5-coder:14b",
        },
    }
    p = tmp_path / "builtin" / "models.json"
    p.parent.mkdir()
    p.write_text(json.dumps(data))
    monkeypatch.setattr(registry_mod, "_BUILTIN_MODELS_JSON", p)
    return p, data


def test_user_overrides_provider_and_alias_and_adds_model(user_paths, builtin_override):
    """A user file with one provider override, one alias override, and one new
    model: user entries win, every builtin entry survives."""
    _, user_json = user_paths
    builtin_path, _ = builtin_override
    user_json.parent.mkdir()
    user_json.write_text(json.dumps({
        "providers": {
            "ollama": {"base_url": "http://127.0.0.1:11434/v1", "native_url": "http://127.0.0.1:11434"},
        },
        "models": {
            "ollama:my-local:8b": {"context_window": 8192},
        },
        "aliases": {
            "default": "ollama:my-local:8b",
        },
    }))

    reg = Registry.load()

    # user overrides win
    assert reg.providers["ollama"].base_url == "http://127.0.0.1:11434/v1"
    assert reg.providers["ollama"].native_url == "http://127.0.0.1:11434"
    assert reg.aliases["default"] == "ollama:my-local:8b"
    # the new user model is present
    assert "ollama:my-local:8b" in reg.models
    # builtin entries survive the merge
    assert "openrouter" in reg.providers
    assert "ollama:qwen2.5-coder:14b" in reg.models
    assert reg.aliases["architect"] == "openrouter:anthropic/claude-sonnet-5"
    assert reg.aliases["compactor"] == "ollama:qwen2.5-coder:14b"
    # the builtin file itself is untouched
    assert json.loads(builtin_path.read_text())["aliases"]["default"] == "ollama:qwen2.5-coder:14b"


def test_missing_user_file_degrades_to_builtin(user_paths, builtin_override, capsys):
    _, user_json = user_paths
    assert not user_json.exists()
    reg = Registry.load()
    assert reg.aliases["default"] == "ollama:qwen2.5-coder:14b"
    assert "openrouter" in reg.providers
    # nothing printed: a missing file is the normal first-run state
    assert capsys.readouterr().err == ""


def test_corrupt_user_file_degrades_to_builtin_with_warning(user_paths, builtin_override, capsys):
    _, user_json = user_paths
    user_json.parent.mkdir()
    user_json.write_text("{not json")
    reg = Registry.load()
    assert reg.aliases["default"] == "ollama:qwen2.5-coder:14b"
    assert "openrouter" in reg.providers
    err = capsys.readouterr().err
    assert "warning" in err and str(user_json) in err


def test_non_object_user_file_degrades_to_builtin_with_warning(user_paths, builtin_override, capsys):
    _, user_json = user_paths
    user_json.parent.mkdir()
    user_json.write_text(json.dumps(["not", "an", "object"]))
    reg = Registry.load()
    assert reg.aliases["default"] == "ollama:qwen2.5-coder:14b"
    assert "not an object" in capsys.readouterr().err


def test_explicit_path_is_used_exactly_with_no_merge(user_paths, builtin_override, tmp_path):
    """--models-json is a full override: nothing from the builtin or the user
    file leaks in, and nothing is written to the user file."""
    _, user_json = user_paths
    user_json.parent.mkdir()
    user_json.write_text(json.dumps({"aliases": {"default": "ollama:my-local:8b"}}))
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({
        "providers": {"ollama": {"base_url": "http://localhost:11434/v1"}},
        "models": {},
        "aliases": {"default": "ollama:from-explicit"},
    }))

    reg = Registry.load(explicit)

    assert reg.aliases == {"default": "ollama:from-explicit"}
    assert list(reg.providers) == ["ollama"]
    assert reg.path == explicit
    # the user file was not touched by the load
    assert json.loads(user_json.read_text()) == {"aliases": {"default": "ollama:my-local:8b"}}


def test_load_without_user_file_persists_to_user_file_once_created(user_paths, builtin_override):
    """path is the user file even before it exists; set_default seeds it."""
    _, user_json = user_paths
    reg = Registry.load()
    assert reg.path == user_json
    assert not user_json.exists()  # nothing written until a choice is made
    assert reg.set_default("openrouter:anthropic/claude-sonnet-5") is True
    assert user_json.is_file()
    data = json.loads(user_json.read_text())
    assert data["aliases"]["default"] == "openrouter:anthropic/claude-sonnet-5"
    # the seeded user file reproduces the merged session state
    assert "openrouter" in data["providers"]
    assert "ollama:qwen2.5-coder:14b" in data["models"]


def test_set_default_persists_to_user_file_not_builtin(user_paths, builtin_override):
    """The builtin package file is never a write target."""
    builtin_path, _ = builtin_override
    _, user_json = user_paths
    reg = Registry.load()
    assert reg.set_default("openrouter:anthropic/claude-sonnet-5") is True

    reloaded = Registry.load()
    assert reloaded.aliases["default"] == "openrouter:anthropic/claude-sonnet-5"
    # builtin untouched
    assert json.loads(builtin_path.read_text())["aliases"]["default"] == "ollama:qwen2.5-coder:14b"
    # and the user file now drives the merge
    assert user_json.is_file()


def test_set_think_mode_persists_to_user_file_not_builtin(user_paths, builtin_override):
    builtin_path, _ = builtin_override
    _, user_json = user_paths
    reg = Registry.load()
    assert reg.set_think_mode("ollama:qwen2.5-coder:14b", "medium") is True

    data = json.loads(user_json.read_text())
    assert data["models"]["ollama:qwen2.5-coder:14b"]["reasoning_effort"] == "medium"
    builtin_data = json.loads(builtin_path.read_text())
    assert "reasoning_effort" not in builtin_data["models"]["ollama:qwen2.5-coder:14b"]


def test_set_default_with_explicit_path_persists_there(user_paths, builtin_override, tmp_path):
    """An explicit --models-json keeps persisting to that file, as before."""
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({
        "providers": {"ollama": {"base_url": "http://localhost:11434/v1"}},
        "models": {},
        "aliases": {"default": "ollama:old"},
    }))
    reg = Registry.load(explicit)
    assert reg.set_default("ollama:new") is True
    assert json.loads(explicit.read_text())["aliases"]["default"] == "ollama:new"


def test_unreadable_user_file_warns_and_uses_builtin(user_paths, builtin_override, capsys):
    """An OSError (e.g. a directory where the file should be) degrades too."""
    _, user_json = user_paths
    user_json.parent.mkdir()
    user_json.mkdir()  # a directory: read_text raises IsADirectoryError (an OSError)
    reg = Registry.load()
    assert reg.aliases["default"] == "ollama:qwen2.5-coder:14b"
    err = capsys.readouterr().err
    assert "warning" in err and str(user_json) in err


def test_corrupt_user_file_still_allows_persistence(user_paths, builtin_override):
    """A corrupt user file degrades the load, but set_default still seeds a
    fresh user file from the merged state — the user is not stuck."""
    _, user_json = user_paths
    user_json.parent.mkdir()
    user_json.write_text("garbage")
    reg = Registry.load()
    assert reg.set_default("openrouter:anthropic/claude-sonnet-5") is True
    data = json.loads(user_json.read_text())
    assert data["aliases"]["default"] == "openrouter:anthropic/claude-sonnet-5"


def test_unknown_spec_resolves_with_defaults_and_warns_once(user_paths, builtin_override, capsys):
    _, user_json = user_paths
    reg = Registry.load()
    spec = reg.resolve("openrouter:brand/new-model")
    assert spec.context_window == registry_mod.DEFAULT_CONTEXT_WINDOW
    assert "no models.json entry" in capsys.readouterr().err
    # the second resolve of the same spec is silent
    reg.resolve("openrouter:brand/new-model")
    assert capsys.readouterr().err == ""


def test_merge_ignores_non_dict_sections(user_paths, builtin_override):
    """A user file whose sections are the wrong type is skipped per-key, not
    rejected wholesale."""
    _, user_json = user_paths
    user_json.parent.mkdir()
    user_json.write_text(json.dumps({
        "providers": "not a dict",
        "aliases": {"default": "ollama:qwen2.5-coder:14b"},
    }))
    reg = Registry.load()
    assert reg.aliases["default"] == "ollama:qwen2.5-coder:14b"
    # the builtin provider section survived the skipped user section
    assert "openrouter" in reg.providers


def test_unreadable_builtin_raises_registry_error(user_paths, tmp_path, monkeypatch):
    bad = tmp_path / "builtin" / "models.json"
    bad.parent.mkdir()
    bad.write_text("{broken")
    monkeypatch.setattr(registry_mod, "_BUILTIN_MODELS_JSON", bad)
    with pytest.raises(RegistryError, match="unreadable"):
        Registry.load()