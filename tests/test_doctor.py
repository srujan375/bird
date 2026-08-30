"""`bird doctor`: one line per check, a fix hint per failure, exit 0/1.

probe() is mocked per-test (each test decides what the machine looks like);
the registry comes from a small builtin catalog via the same monkeypatched
module constants test_user_config.py uses, so no test touches a real ~/.bird.
The KG check is exercised for real on a tmp repo with no graph — the normal
first-run state — and the TUI check is pinned via bird.cli._tui_dir so the
result does not depend on whether node_modules happens to exist in CI.
"""

import argparse
import json
from pathlib import Path

import pytest

import bird.cli as cli_mod
import bird.llm.registry as registry_mod
import bird.doctor as doctor_mod
from bird.doctor import doctor_main
from bird.setup import Probes


@pytest.fixture
def user_paths(tmp_path, monkeypatch):
    """Point every user-config constant at tmp_path — including doctor's own
    imported bindings (it reads USER_MODELS_JSON for a detail line)."""
    bird_dir = tmp_path / ".bird"
    user_json = bird_dir / "models.json"
    monkeypatch.setattr(registry_mod, "USER_BIRD_DIR", bird_dir)
    monkeypatch.setattr(registry_mod, "USER_MODELS_JSON", user_json)
    monkeypatch.setattr(doctor_mod, "USER_MODELS_JSON", user_json)
    monkeypatch.setattr(doctor_mod, "USER_ENV_FILE", bird_dir / ".env")
    return bird_dir, user_json


@pytest.fixture
def builtin_override(tmp_path, monkeypatch):
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


@pytest.fixture
def tui_installed(monkeypatch):
    """Pin the TUI check so it passes regardless of CI node_modules."""
    fake = Path("/tmp/fake-tui")
    monkeypatch.setattr(cli_mod, "_tui_dir", lambda: fake)
    return fake


def _args(repo) -> argparse.Namespace:
    return argparse.Namespace(models_json=None, repo=str(repo))


def test_all_healthy_exits_0(user_paths, builtin_override, tui_installed,
                             tmp_path, monkeypatch, capsys):
    """Keys set, local daemon up, clouds reachable, default alias resolves,
    graph simply not built yet → exit 0."""
    monkeypatch.setattr(doctor_mod, "probe", lambda registry: Probes(
        ollama_key="sk-ollama", openrouter_key="sk-or",
        local_up=True, local_models=["qwen2.5-coder:14b"],
        cloud_ok=True, openrouter_ok=True,
    ))

    rc = doctor_main(_args(tmp_path))

    assert rc == 0
    out = capsys.readouterr().out
    assert "all checks passed" in out
    assert "✗" not in out
    # the not-built graph is a pass with an explanation, not a failure
    assert "not built yet" in out


def test_missing_keys_and_down_daemon_exit_1_with_fix_hints(
    user_paths, builtin_override, tui_installed, tmp_path, monkeypatch, capsys
):
    """Nothing configured: the keys check and the local-daemon check fail,
    each with a fix line that says what to do, and the exit code is 1."""
    monkeypatch.setattr(doctor_mod, "probe", lambda registry: Probes())

    rc = doctor_main(_args(tmp_path))

    assert rc == 1
    out = capsys.readouterr().out
    assert "✗ provider keys" in out
    assert "✗ local Ollama" in out
    assert "2 check(s) need attention" in out
    # every failure carries an actionable fix
    assert out.count("fix:") >= 2
    assert "bird setup" in out
    assert "ollama serve" in out


def test_down_local_daemon_passes_when_cloud_covers_it(
    user_paths, builtin_override, tui_installed, tmp_path, monkeypatch, capsys
):
    """Local Ollama down is only a failure when nothing else can serve."""
    monkeypatch.setattr(doctor_mod, "probe", lambda registry: Probes(
        openrouter_key="sk-or", openrouter_ok=True,
    ))

    rc = doctor_main(_args(tmp_path))

    assert rc == 0
    out = capsys.readouterr().out
    assert "not running (cloud providers cover model serving)" in out


def test_kg_check_on_tmp_repo_without_graph_passes(tmp_path, capsys):
    """A repo with no graph yet is 'not built yet', a pass — exercised for
    real, no mock, because that is exactly the first-run state."""
    from bird.doctor import _kg_check

    check = _kg_check(tmp_path)
    assert check.ok is True
    assert "not built yet" in check.detail


def test_tui_check_missing_reports_fallback_and_fix(user_paths, builtin_override,
                                                    tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "_tui_dir", lambda: None)
    monkeypatch.setattr(doctor_mod, "probe", lambda registry: Probes(
        ollama_key="sk", local_up=True, local_models=["m"], cloud_ok=True,
    ))

    rc = doctor_main(_args(tmp_path))

    assert rc == 1  # only the TUI check fails
    out = capsys.readouterr().out
    assert "✗ TUI" in out
    assert "plain REPL" in out
    assert "cd tui && npm install" in out


def test_default_alias_check_reports_where_it_resolved(
    user_paths, builtin_override, tmp_path
):
    from bird.doctor import _default_alias_check

    registry = registry_mod.Registry.load()
    check = _default_alias_check(registry)
    assert check.ok is True
    assert "ollama:qwen2.5-coder:14b" in check.detail


def test_missing_default_alias_fails_with_fix(user_paths, builtin_override, tmp_path):
    from bird.doctor import _default_alias_check

    registry = registry_mod.Registry.load()
    registry.aliases = {}
    check = _default_alias_check(registry)
    assert check.ok is False
    assert "bird setup" in check.fix
