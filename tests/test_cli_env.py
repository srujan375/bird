"""Env precedence in cli.main(): shell export > CWD .env > ~/.bird/.env.

main() calls load_dotenv() (CWD, no override) then load_dotenv(USER_ENV_FILE,
override=False), so a shell export always wins, the CWD .env fills gaps, and
the user file only fills gaps the CWD file left. USER_ENV_FILE is a
module-level constant, so tests monkeypatch it onto bird.cli — no test ever
reads a real ~/.bird.
"""

import json

import pytest

import bird.cli as cli_mod
from bird.llm.registry import Registry


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Keep the real environment out: every test starts with the keys unset."""
    for name in ("BIRD_TEST_KEY", "OLLAMA_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def user_env(tmp_path, monkeypatch):
    """Point USER_ENV_FILE at a tmp_path file and return its path."""
    p = tmp_path / ".bird" / ".env"
    monkeypatch.setattr(cli_mod, "USER_ENV_FILE", p)
    return p


def _run_main(monkeypatch, tmp_path, capsys):
    """Run cli.main() far enough to load .env files, then bail out.

    `bird doctor` is the cheapest subcommand to reach the env loading: it
    parses args, runs the checks, and returns an exit code without ever
    building a runner. probe() is mocked so no network is touched.
    """
    from bird import doctor

    monkeypatch.setattr(
        doctor, "probe",
        lambda registry: doctor.Probes(),  # nothing available, everywhere down
    )
    monkeypatch.chdir(tmp_path)
    return cli_mod.main(["doctor", "--repo", str(tmp_path)])


def test_shell_export_beats_cwd_dotenv_and_user_file(monkeypatch, tmp_path, user_env, capsys):
    (tmp_path / ".env").write_text("BIRD_TEST_KEY=from-cwd\n")
    user_env.parent.mkdir()
    user_env.write_text("BIRD_TEST_KEY=from-user\n")
    monkeypatch.setenv("BIRD_TEST_KEY", "from-shell")

    _run_main(monkeypatch, tmp_path, capsys)

    import os
    assert os.environ["BIRD_TEST_KEY"] == "from-shell"


def test_cwd_dotenv_beats_user_file(monkeypatch, tmp_path, user_env, capsys):
    (tmp_path / ".env").write_text("BIRD_TEST_KEY=from-cwd\n")
    user_env.parent.mkdir()
    user_env.write_text("BIRD_TEST_KEY=from-user\n")

    _run_main(monkeypatch, tmp_path, capsys)

    import os
    assert os.environ["BIRD_TEST_KEY"] == "from-cwd"


def test_user_env_file_fills_gaps(monkeypatch, tmp_path, user_env, capsys):
    user_env.parent.mkdir()
    user_env.write_text("BIRD_TEST_KEY=from-user\n")

    _run_main(monkeypatch, tmp_path, capsys)

    import os
    assert os.environ["BIRD_TEST_KEY"] == "from-user"


def test_cwd_dotenv_does_not_override_shell(monkeypatch, tmp_path, user_env, capsys):
    """load_dotenv() without override=True never clobbers a shell export."""
    (tmp_path / ".env").write_text("BIRD_TEST_KEY=from-cwd\n")
    monkeypatch.setenv("BIRD_TEST_KEY", "from-shell")

    _run_main(monkeypatch, tmp_path, capsys)

    import os
    assert os.environ["BIRD_TEST_KEY"] == "from-shell"


def test_missing_env_files_are_silent(monkeypatch, tmp_path, user_env, capsys):
    """No .env anywhere is the normal state — no error, no crash."""
    rc = _run_main(monkeypatch, tmp_path, capsys)
    assert rc == 1  # doctor reports nothing configured, but does not crash


def test_setup_writes_keys_user_env_file_is_loaded_by_main(monkeypatch, tmp_path, user_env, capsys):
    """The point of the user .env: a key written by `bird setup` is seen by
    main() with no shell export and no CWD .env."""
    user_env.parent.mkdir()
    user_env.write_text("OLLAMA_API_KEY=sk-from-setup\n")

    _run_main(monkeypatch, tmp_path, capsys)

    import os
    assert os.environ["OLLAMA_API_KEY"] == "sk-from-setup"


def test_doctor_sees_key_from_user_env_file(monkeypatch, tmp_path, user_env, capsys):
    """End to end: the key from ~/.bird/.env reaches doctor's key check."""
    user_env.parent.mkdir()
    user_env.write_text("OPENROUTER_API_KEY=sk-user-file\n")

    rc = _run_main(monkeypatch, tmp_path, capsys)

    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY" in out  # the keys check names the set key
    # local ollama is down and no other source is up → still needs attention
    assert rc == 1


def test_registry_load_still_works_after_env_loading(monkeypatch, tmp_path, user_env, capsys):
    """main() loads .env before anything else; the registry must not be
    affected by the cwd change or the env files."""
    (tmp_path / ".env").write_text("BIRD_TEST_KEY=x\n")
    user_env.parent.mkdir()
    user_env.write_text("OTHER_KEY=y\n")

    rc = _run_main(monkeypatch, tmp_path, capsys)

    assert rc == 1
    # and a registry load in the same process is unaffected
    reg = Registry.load()
    assert isinstance(reg.aliases, dict)


# --- first-run hint ---------------------------------------------------------
# An interactive session that boots with no provider key in the environment and
# no local Ollama daemon up prints ONE non-blocking line pointing at `bird
# setup`. The decision lives in _model_source_missing(); the print in
# _print_first_run_hint(). Both are tested directly — driving a full REPL boot
# just to capture one line would mock half of cli.py.


class _FakeOllamaDown:
    def __init__(self, *a, **k):
        pass

    def is_up(self):
        return False


class _FakeOllamaUp:
    def __init__(self, *a, **k):
        pass

    def is_up(self):
        return True


def test_hint_shown_when_no_key_and_no_daemon(monkeypatch, capsys):
    """No keys, daemon down → the hint line prints."""
    monkeypatch.setattr(cli_mod, "Ollama", _FakeOllamaDown)
    cli_mod._print_first_run_hint()
    out = capsys.readouterr().out
    assert out == "no model source configured — run `bird setup`\n"


def test_hint_silent_when_provider_key_set(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(cli_mod, "Ollama", _FakeOllamaDown)
    cli_mod._print_first_run_hint()
    assert capsys.readouterr().out == ""


def test_hint_silent_when_local_daemon_up(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "Ollama", _FakeOllamaUp)
    cli_mod._print_first_run_hint()
    assert capsys.readouterr().out == ""


def test_model_source_missing_tolerates_probe_crash(monkeypatch):
    """A daemon probe that raises must read as 'down', never propagate."""
    class _Boom:
        def __init__(self, *a, **k):
            pass

        def is_up(self):
            raise RuntimeError("network on fire")

    monkeypatch.setattr(cli_mod, "Ollama", _Boom)
    assert cli_mod._model_source_missing() is True


def test_model_source_missing_false_when_key_set(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    assert cli_mod._model_source_missing() is False