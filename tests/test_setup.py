"""`bird setup`: non-interactive defaults, actionable todos, and the user
.env writer.

probe()/verify_model()/discover_models() are mocked — no test touches the
network. USER_MODELS_JSON / USER_ENV_FILE are monkeypatched on BOTH modules
that bind them (bird.llm.registry reads its constant inside load(); bird.setup
binds them as default arguments), so no test ever touches a real ~/.bird.
"""

import argparse
import json
import os

import httpx
import pytest

import bird.llm.registry as registry_mod
import bird.setup as setup_mod
from bird.llm.discovery import DiscoveredModel
from bird.setup import Probes, setup_main, write_env_keys


@pytest.fixture
def user_paths(tmp_path, monkeypatch):
    """Point every user-config constant at tmp_path."""
    bird_dir = tmp_path / ".bird"
    user_json = bird_dir / "models.json"
    user_env = bird_dir / ".env"
    monkeypatch.setattr(registry_mod, "USER_BIRD_DIR", bird_dir)
    monkeypatch.setattr(registry_mod, "USER_MODELS_JSON", user_json)
    monkeypatch.setattr(setup_mod, "USER_MODELS_JSON", user_json)
    monkeypatch.setattr(setup_mod, "USER_ENV_FILE", user_env)
    return bird_dir, user_json, user_env


@pytest.fixture
def builtin_override(tmp_path, monkeypatch):
    """A small builtin catalog so assertions are exact and nothing real is written."""
    data = {
        "providers": {
            "ollama": {
                "base_url": "https://ollama.com/v1",
                "native_url": "https://ollama.com",
                "api_key_env": "OLLAMA_API_KEY",
            },
            "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
        },
        "models": {"ollama:qwen2.5-coder:14b": {"context_window": 32768}},
        "aliases": {"default": "ollama:qwen2.5-coder:14b"},
    }
    p = tmp_path / "builtin" / "models.json"
    p.parent.mkdir()
    p.write_text(json.dumps(data))
    monkeypatch.setattr(registry_mod, "_BUILTIN_MODELS_JSON", p)
    return p


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """setup_main must never make a real call: verify + discovery mocked here,
    probe() mocked per-test (each test decides what the machine looks like)."""
    monkeypatch.setattr(setup_mod, "verify_model", lambda spec, registry, **kw: (True, "replied 'ok'"))
    monkeypatch.setattr(
        setup_mod, "discover_models",
        lambda registry: ([DiscoveredModel(spec="ollama:qwen2.5-coder:14b", source="ollama",
                                          context_window=32768)], []),
    )
    for name in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _args(yes=True):
    return argparse.Namespace(models_json=None, yes=yes)


def test_yes_with_local_ollama_reports_daemon_and_todo(
    user_paths, builtin_override, monkeypatch, capsys
):
    """Local daemon up, no cloud key: nothing to re-point (unmarked ollama
    specs always go to the daemon); the missing keys are reported as todo."""
    _, user_json, _ = user_paths
    monkeypatch.setattr(
        setup_mod, "probe",
        lambda registry: Probes(local_up=True, local_models=["qwen2.5-coder:14b"]),
    )

    rc = setup_main(_args())

    assert rc == 1  # keys are still missing
    out = capsys.readouterr().out
    assert "local Ollama at http://localhost:11434: serves any ollama:<model>" in out
    assert "native_url →" not in out
    assert "still needs attention" in out
    assert "OLLAMA_API_KEY" in out and "OPENROUTER_API_KEY" in out
    assert not user_json.exists()  # nothing to persist: the rule needs no file

    # builtin untouched
    builtin = json.loads(builtin_override.read_text())
    assert builtin["providers"]["ollama"]["native_url"] == "https://ollama.com"


def test_yes_with_nothing_available_exits_1_with_actionable_lines(
    user_paths, builtin_override, monkeypatch, capsys
):
    _, user_json, _ = user_paths
    monkeypatch.setattr(setup_mod, "probe", lambda registry: Probes())

    rc = setup_main(_args())

    assert rc == 1
    out = capsys.readouterr().out
    assert "still needs attention" in out
    # every todo line says what to do, not just what failed
    assert "set OLLAMA_API_KEY" in out
    assert "set OPENROUTER_API_KEY" in out
    assert "start a model source" in out
    assert "no model source found" in out
    # nothing was written: there was nothing to write
    assert not user_json.exists()


def test_yes_all_healthy_exits_0(user_paths, builtin_override, monkeypatch, capsys):
    """Keys set, local daemon up, both clouds reachable → nothing to do."""
    _, user_json, _ = user_paths
    monkeypatch.setattr(setup_mod, "probe", lambda registry: Probes(
        ollama_key="sk-ollama", openrouter_key="sk-or",
        local_up=True, local_models=["qwen2.5-coder:14b"],
        cloud_ok=True, openrouter_ok=True,
    ))

    rc = setup_main(_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "setup complete" in out
    # the local daemon is up but a cloud key exists, so native_url is left alone
    assert not user_json.exists()


def test_yes_with_local_up_and_cloud_key_leaves_native_url(
    user_paths, builtin_override, monkeypatch, capsys
):
    """A cloud key means the local daemon is not an unambiguous default —
    non-interactive setup must not silently re-point the provider."""
    _, user_json, _ = user_paths
    monkeypatch.setattr(setup_mod, "probe", lambda registry: Probes(
        ollama_key="sk-ollama", local_up=True, local_models=["m"], cloud_ok=True,
    ))

    rc = setup_main(_args())

    assert rc == 1  # OPENROUTER_API_KEY still missing
    out = capsys.readouterr().out
    assert "native_url →" not in out
    assert not user_json.exists()


def test_write_env_keys_creates_file_with_600_perms(user_paths):
    _, _, user_env = user_paths
    write_env_keys({"OLLAMA_API_KEY": "sk-1"})
    assert user_env.is_file()
    assert (os.stat(user_env).st_mode & 0o777) == 0o600
    assert user_env.read_text() == "OLLAMA_API_KEY=sk-1\n"


def test_write_env_keys_merges_preserving_other_lines(user_paths):
    _, _, user_env = user_paths
    user_env.parent.mkdir()
    user_env.write_text(
        "# my keys\n"
        "OPENROUTER_API_KEY=sk-old\n"
        "UNRELATED=keep-me\n"
        "OLLAMA_API_KEY=sk-stale\n"
    )
    write_env_keys({"OLLAMA_API_KEY": "sk-new"})
    text = user_env.read_text()
    lines = text.splitlines()
    assert lines[0] == "# my keys"  # comments survive
    assert "UNRELATED=keep-me" in lines
    assert "OLLAMA_API_KEY=sk-new" in lines
    assert "sk-stale" not in text  # same-key line replaced, not duplicated
    assert text.count("OLLAMA_API_KEY=") == 1
    # perms tightened on merge too
    assert (os.stat(user_env).st_mode & 0o777) == 0o600


def test_write_env_keys_replaces_in_place_keeping_order(user_paths):
    _, _, user_env = user_paths
    user_env.parent.mkdir()
    user_env.write_text("A=1\nB=2\nC=3\n")
    write_env_keys({"B": "2b"})
    assert user_env.read_text() == "A=1\nB=2b\nC=3\n"


def test_setup_written_key_lands_in_the_file_main_loads(user_paths):
    """The point of the user .env: a key written here is what cli.main() loads
    on every start with no shell export and no CWD .env."""
    _, _, user_env = user_paths
    write_env_keys({"OPENROUTER_API_KEY": "sk-or"})
    assert "OPENROUTER_API_KEY=sk-or" in user_env.read_text()


def test_probe_reports_unreachable_as_fields_not_exceptions(
    user_paths, builtin_override, monkeypatch
):
    """probe() on a dead machine returns fields, never raises."""

    class _Down:
        def is_up(self):
            raise httpx.ConnectError("refused")

        def close(self):
            pass

    class _FailingHTTP:
        def get(self, *a, **kw):
            raise httpx.ConnectError("refused")

        def close(self):
            pass

    monkeypatch.setattr(setup_mod, "Ollama", lambda *a, **kw: _Down())
    monkeypatch.setattr(setup_mod.httpx, "Client", lambda **kw: _FailingHTTP())
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")

    p = setup_mod.probe(registry_mod.Registry.load())

    assert p.local_up is False
    assert p.local_models == []
    assert p.openrouter_ok is False
    assert p.cloud_ok is None  # no ollama key → cloud not probed