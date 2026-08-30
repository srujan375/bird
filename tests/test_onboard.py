"""In-session onboarding: the walkthrough over a scripted IO, key storage,
the first-run stamp, and the doctor report. No network: probe / discovery /
verify are mocked exactly as in test_setup."""

import json
import os

import pytest

import bird.llm.registry as registry_mod
import bird.setup as setup_mod
from bird.llm.discovery import DiscoveredModel
from bird.llm.registry import Registry
from bird.onboard import (
    Choice,
    Prompter,
    TransportIO,
    keys_status,
    mark_setup_done,
    needs_first_run,
    set_key,
    walkthrough,
)
from bird.setup import Probes


class ScriptedIO:
    """Answers prompts from a queue; records everything said."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.said = []
        self.asked = []

    def say(self, text):
        self.said.append(text)

    def ask(self, prompt, default=""):
        self.asked.append(("ask", prompt))
        return self.answers.pop(0) if self.answers else default

    def ask_secret(self, prompt):
        self.asked.append(("secret", prompt))
        return self.answers.pop(0) if self.answers else ""

    def choose(self, title, choices, current=None):
        self.asked.append(("choose", title, [c.value for c in choices], current))
        return self.answers.pop(0) if self.answers else None

    @property
    def transcript(self):
        return "\n".join(self.said)


@pytest.fixture
def user_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no repo .env in sight
    bird_dir = tmp_path / ".bird"
    monkeypatch.setattr(registry_mod, "USER_BIRD_DIR", bird_dir)
    monkeypatch.setattr(registry_mod, "USER_MODELS_JSON", bird_dir / "models.json")
    monkeypatch.setattr(registry_mod, "USER_ENV_FILE", bird_dir / ".env")
    monkeypatch.setattr(setup_mod, "USER_MODELS_JSON", bird_dir / "models.json")
    monkeypatch.setattr(setup_mod, "USER_ENV_FILE", bird_dir / ".env")
    for name in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "BIRD_SKIP_SETUP"):
        monkeypatch.delenv(name, raising=False)
    return bird_dir


@pytest.fixture
def registry(tmp_path):
    data = {
        "providers": {
            "ollama": {"base_url": "http://localhost:11434/v1", "native_url": "http://localhost:11434",
                       "api_key_env": "OLLAMA_API_KEY"},
            "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
        },
        "models": {"ollama:glm-5.3:cloud": {"context_window": 1000000}},
        "aliases": {"default": "ollama:glm-5.3:cloud"},
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(data))
    return Registry.load(p)


def _discovery(monkeypatch, models):
    monkeypatch.setattr(setup_mod, "discover_models", lambda registry: (models, []))


# ---- first-run stamp --------------------------------------------------------

def test_needs_first_run_until_stamped(user_paths):
    assert needs_first_run()
    mark_setup_done()
    assert not needs_first_run()


def test_existing_user_config_counts_as_set_up(user_paths):
    user_paths.mkdir()
    (user_paths / ".env").write_text("OLLAMA_API_KEY=x\n")
    assert not needs_first_run()


def test_skip_env_var(user_paths, monkeypatch):
    monkeypatch.setenv("BIRD_SKIP_SETUP", "1")
    assert not needs_first_run()


# ---- keys -------------------------------------------------------------------

def test_set_key_persists_and_goes_live(user_paths):
    path = set_key("ollama_api_key", " sk-test ")
    assert path == user_paths / ".env"
    assert "OLLAMA_API_KEY=sk-test" in path.read_text()
    assert os.environ["OLLAMA_API_KEY"] == "sk-test"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ("OLLAMA_API_KEY", f"~/.bird/.env ({path})") in keys_status()


def test_set_key_rejects_unknown_or_empty(user_paths):
    with pytest.raises(ValueError):
        set_key("AWS_SECRET", "x")
    with pytest.raises(ValueError):
        set_key("OLLAMA_API_KEY", "   ")


def test_keys_status_not_set(user_paths):
    assert keys_status() == [("OLLAMA_API_KEY", "not set"), ("OPENROUTER_API_KEY", "not set")]


# ---- walkthrough ------------------------------------------------------------

def test_walkthrough_local_only_picks_a_daemon_model(user_paths, registry, monkeypatch):
    monkeypatch.setattr(setup_mod, "probe", lambda r: Probes(local_up=True, local_models=["ornith"]))
    _discovery(monkeypatch, [
        DiscoveredModel(spec="ollama:glm-5.3:cloud", source="configured", context_window=1000000),
        DiscoveredModel(spec="ollama:ornith", source="ollama"),
        DiscoveredModel(spec="ollama:kimi-k3:cloud", source="ollama.com"),
    ])
    verified = []
    monkeypatch.setattr(setup_mod, "verify_model", lambda spec, reg, **kw: (verified.append(spec) or True, "replied 'ok'"))

    io = ScriptedIO(["", "", "ollama:ornith"])  # skip both keys, pick ornith
    chosen = walkthrough(io, registry, first_run=True)

    assert chosen == "ollama:ornith"
    assert [a[0] for a in io.asked] == ["secret", "secret", "choose"]
    # cloud specs need the key we just skipped, so they were not offered;
    # the configured cloud default stays listed only as the current choice
    offered = io.asked[2][2]
    assert "ollama:ornith" in offered and "ollama:kimi-k3:cloud" not in offered
    assert verified == ["ollama:ornith"]
    assert json.loads(registry.path.read_text())["aliases"]["default"] == "ollama:ornith"
    assert not needs_first_run()
    assert "Welcome to bird" in io.transcript and "setup done" in io.transcript


def test_walkthrough_stores_key_and_keeps_cloud_default(user_paths, registry, monkeypatch):
    probes = [Probes(local_up=False), Probes(ollama_key="sk", cloud_ok=True)]
    monkeypatch.setattr(setup_mod, "probe", lambda r: probes.pop(0))
    _discovery(monkeypatch, [
        DiscoveredModel(spec="ollama:glm-5.3:cloud", source="configured", context_window=1000000),
        DiscoveredModel(spec="ollama:glm-5.3:cloud", source="ollama.com"),
    ])
    monkeypatch.setattr(setup_mod, "verify_model", lambda spec, reg, **kw: (True, "replied 'ok'"))

    io = ScriptedIO(["sk", "", None])  # ollama key, skip openrouter, keep default
    chosen = walkthrough(io, registry)

    assert chosen is None  # kept what it had
    assert "OLLAMA_API_KEY=sk" in (user_paths / ".env").read_text()
    assert "verify ollama:glm-5.3:cloud: ok" in io.transcript


def test_walkthrough_with_nothing_reachable_says_so(user_paths, registry, monkeypatch):
    monkeypatch.setattr(setup_mod, "probe", lambda r: Probes())
    _discovery(monkeypatch, [])
    io = ScriptedIO(["", ""])
    assert walkthrough(io, registry) is None
    assert "no model source is reachable" in io.transcript
    assert not needs_first_run()  # asked once; /setup repeats on demand


def test_walkthrough_reports_failed_verify(user_paths, registry, monkeypatch):
    monkeypatch.setattr(setup_mod, "probe", lambda r: Probes(ollama_key="sk", cloud_ok=True))
    _discovery(monkeypatch, [DiscoveredModel(spec="ollama:glm-5.3:cloud", source="ollama.com")])
    monkeypatch.setattr(setup_mod, "verify_model", lambda spec, reg, **kw: (False, "HTTP 401"))
    io = ScriptedIO([None])
    walkthrough(io, registry)
    assert "FAILED — HTTP 401" in io.transcript
    assert "/keys set" in io.transcript


# ---- transport IO -----------------------------------------------------------

def test_transport_io_round_trip():
    import threading

    events = []
    prompter = Prompter(lambda t, **d: events.append((t, d)))
    tio = TransportIO(lambda t, **d: events.append((t, d)), prompter)
    result = {}

    def ask():
        result["secret"] = tio.ask_secret("OLLAMA_API_KEY")
        result["choice"] = tio.choose("default model", [Choice("a", "a"), Choice("b", "b")], current="a")
        result["skipped"] = tio.ask("name", default="dflt")

    t = threading.Thread(target=ask)
    t.start()
    # answer in order, as the UI would, by the ids the events carried
    for expected, value in (("OLLAMA_API_KEY", "sk"), ("default model", "b"), ("name", None)):
        while not any(e[0] == "prompt_request" and e[1]["prompt"] == expected for e in events):
            pass
        req = next(e for e in events if e[0] == "prompt_request" and e[1]["prompt"] == expected)
        prompter.resolve(req[1]["id"], value)
    t.join(timeout=5)
    assert result == {"secret": "sk", "choice": "b", "skipped": "dflt"}
    secret_req = next(e for e in events if e[1].get("prompt") == "OLLAMA_API_KEY")
    assert secret_req[1]["secret"] is True
    choice_req = next(e for e in events if e[1].get("prompt") == "default model")
    assert [c["value"] for c in choice_req[1]["choices"]] == ["a", "b"] and choice_req[1]["current"] == "a"


def test_prompter_cancel_all_unblocks_with_none():
    import threading

    prompter = Prompter(lambda t, **d: None)
    out = []
    t = threading.Thread(target=lambda: out.append(prompter.request({"prompt": "x"})))
    t.start()
    while not prompter._pending:
        pass
    prompter.cancel_all()
    t.join(timeout=5)
    assert out == [None]


def test_openrouter_catalog_is_a_pointer_when_other_models_exist(user_paths, registry, monkeypatch):
    monkeypatch.setattr(setup_mod, "probe", lambda r: Probes(
        ollama_key="sk", openrouter_key="or", cloud_ok=True, openrouter_ok=True, local_up=True, local_models=["ornith"]))
    _discovery(monkeypatch, [
        DiscoveredModel(spec="ollama:glm-5.3:cloud", source="configured"),
        DiscoveredModel(spec="ollama:ornith", source="ollama"),
        DiscoveredModel(spec="ollama:kimi-k3:cloud", source="ollama.com"),
    ] + [DiscoveredModel(spec=f"openrouter:x/m{i}", source="openrouter") for i in range(300)])
    monkeypatch.setattr(setup_mod, "verify_model", lambda spec, reg, **kw: (True, "ok"))
    io = ScriptedIO([None])
    walkthrough(io, registry)
    offered = io.asked[-1][2]
    assert len(offered) == 3 and not any(o.startswith("openrouter:") for o in offered)
    assert "+300 more on OpenRouter" in io.transcript


def test_openrouter_catalog_offered_when_it_is_all_there_is(user_paths, registry, monkeypatch):
    monkeypatch.setattr(setup_mod, "probe", lambda r: Probes(openrouter_key="or", openrouter_ok=True))
    _discovery(monkeypatch, [DiscoveredModel(spec=f"openrouter:x/m{i}", source="openrouter") for i in range(5)])
    monkeypatch.setattr(setup_mod, "verify_model", lambda spec, reg, **kw: (True, "ok"))
    io = ScriptedIO(["", "openrouter:x/m2"])  # skip OLLAMA key, then pick
    assert walkthrough(io, registry) == "openrouter:x/m2"
    assert len(io.asked[-1][2]) == 5


def test_repo_only_key_is_offered_globally(user_paths, registry, monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("OLLAMA_API_KEY=from-repo\n")
    monkeypatch.setenv("OLLAMA_API_KEY", "from-repo")
    monkeypatch.setattr(setup_mod, "probe", lambda r: Probes(ollama_key="from-repo", cloud_ok=True))
    _discovery(monkeypatch, [DiscoveredModel(spec="ollama:glm-5.3:cloud", source="ollama.com")])
    monkeypatch.setattr(setup_mod, "verify_model", lambda spec, reg, **kw: (True, "ok"))
    io = ScriptedIO(["y", "", None])  # store globally, skip openrouter, keep default
    walkthrough(io, registry)
    assert io.asked[0][0] == "ask" and "this repo's .env" in io.asked[0][1]
    assert "OLLAMA_API_KEY=from-repo" in (user_paths / ".env").read_text()
