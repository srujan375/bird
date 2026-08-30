"""Model registry: models.json → resolved ModelSpec.

Specs are `provider:model` strings (split on the FIRST colon only — Ollama
model names themselves contain colons, e.g. `ollama:qwen2.5-coder:14b`).
Aliases (`default`, `architect`, `compactor`) resolve to full specs. Unknown but
well-formed specs resolve with conservative defaults so a user can point at
any OpenRouter/Ollama model without editing models.json first.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_CONTEXT_WINDOW = 32768

_warned_unknown_specs: set[str] = set()

# Package data (see pyproject), so this resolves to one file in both worlds:
# src/bird/models.json in a checkout, bird/models.json in an installed wheel.
# The old path was parents[3] — the repo root, which only exists in a source
# tree. models.json shipped in no wheel at all, so every non-editable install
# raised FileNotFoundError in load(); editable installs hid it by importing
# straight from the source tree.
_BUILTIN_MODELS_JSON = Path(__file__).resolve().parents[1] / "models.json"

# The user config layer: ~/.bird/. The builtin file belongs to the package
# (site-packages under pipx/uv — possibly root-owned, overwritten on every
# upgrade), so nothing of the user's is ever written there. /model and /think
# persist to the user file, which survives upgrades; keys live in ~/.bird/.env
# so they survive changing directories (cli.py loads it after the CWD .env).
USER_BIRD_DIR = Path.home() / ".bird"
USER_MODELS_JSON = USER_BIRD_DIR / "models.json"
USER_ENV_FILE = USER_BIRD_DIR / ".env"


class RegistryError(Exception):
    pass


def _providers_from(data: dict) -> dict[str, "ProviderConfig"]:
    return {
        name: ProviderConfig(name=name, **cfg)
        for name, cfg in data.get("providers", {}).items()
    }


def _merge_user_over_builtin() -> dict:
    """The user file ~/.bird/models.json merged over the builtin package file.

    Per top-level key (providers, models, aliases), user entries add or
    override by name/spec and the builtin supplies the base catalog — so a
    user file with one local model still gets every shipped provider and
    alias. A missing or unreadable user file degrades to the builtin alone;
    a corrupt one is reported and skipped rather than breaking every run.
    """
    try:
        builtin = json.loads(_BUILTIN_MODELS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RegistryError(f"builtin models.json is unreadable: {e}") from e
    try:
        user_text = USER_MODELS_JSON.read_text(encoding="utf-8")
    except FileNotFoundError:
        return builtin
    except OSError as e:
        print(f"warning: could not read {USER_MODELS_JSON} ({e}); using builtin", file=sys.stderr)
        return builtin
    try:
        user = json.loads(user_text)
    except json.JSONDecodeError as e:
        print(
            f"warning: {USER_MODELS_JSON} is not valid JSON ({e}); using builtin",
            file=sys.stderr,
        )
        return builtin
    if not isinstance(user, dict):
        print(f"warning: {USER_MODELS_JSON} is not an object; using builtin", file=sys.stderr)
        return builtin

    merged = dict(builtin)
    for key in ("providers", "models", "aliases"):
        user_section = user.get(key)
        if not isinstance(user_section, dict):
            continue
        section = dict(merged.get(key, {}))
        section.update(user_section)
        merged[key] = section
    return merged


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str | None = None
    native_url: str | None = None

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


# Ollama routing: the model NAME decides where an `ollama:` spec goes, using
# Ollama's own convention for hosted models — `name:cloud` for an untagged
# model, `name:tag-cloud` for a tagged one (gpt-oss:120b-cloud). A marked
# model is served straight from ollama.com (the marker stripped: the hosted
# catalog lists plain names) and needs OLLAMA_API_KEY; an unmarked one is
# served by the local daemon, which pulls it if missing. The configured
# provider URLs only ever customise the LOCAL side (a daemon on another box);
# pointing them at ollama.com does not turn unmarked models into cloud ones.
OLLAMA_CLOUD_URL = "https://ollama.com"
OLLAMA_LOCAL_URL = "http://localhost:11434"
OLLAMA_CLOUD_MARKER = "cloud"


def split_cloud_marker(model: str) -> tuple[str, bool]:
    """'glm-5.3-flash:cloud' -> ('glm-5.3-flash', True);
    'gpt-oss:120b-cloud' -> ('gpt-oss:120b', True); 'ornith:35b' -> unchanged."""
    name, sep, tag = model.rpartition(":")
    if not sep:
        return model, False
    if tag == OLLAMA_CLOUD_MARKER:
        return name, True
    if tag.endswith("-" + OLLAMA_CLOUD_MARKER):
        return f"{name}:{tag[: -len(OLLAMA_CLOUD_MARKER) - 1]}", True
    return model, False


def add_cloud_marker(model: str) -> str:
    """Inverse of split_cloud_marker for a plain hosted-catalog name."""
    if split_cloud_marker(model)[1]:
        return model
    return f"{model}-{OLLAMA_CLOUD_MARKER}" if ":" in model else f"{model}:{OLLAMA_CLOUD_MARKER}"


def _is_ollama_cloud_url(url: str | None) -> bool:
    return bool(url) and "ollama.com" in url


def ollama_provider_for(base: ProviderConfig, cloud: bool) -> ProviderConfig:
    """The provider endpoints an ollama model actually talks to."""
    if cloud:
        return ProviderConfig(
            name=base.name,
            base_url=OLLAMA_CLOUD_URL + "/v1",
            api_key_env=base.api_key_env or "OLLAMA_API_KEY",
            native_url=OLLAMA_CLOUD_URL,
        )
    native = base.native_url if not _is_ollama_cloud_url(base.native_url) else None
    base_url = base.base_url if not _is_ollama_cloud_url(base.base_url) else None
    if native is None and base_url is None:
        native, base_url = OLLAMA_LOCAL_URL, OLLAMA_LOCAL_URL + "/v1"
    elif native is None:
        native = base_url.rstrip("/").removesuffix("/v1")
    elif base_url is None:
        base_url = native.rstrip("/") + "/v1"
    return ProviderConfig(
        name=base.name, base_url=base_url, api_key_env=base.api_key_env, native_url=native
    )


@dataclass
class ModelSpec:
    spec: str  # full "provider:model"
    provider: ProviderConfig
    model: str  # provider-side model id
    context_window: int = DEFAULT_CONTEXT_WINDOW
    supports_tools: bool = True
    constrained_decoding: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Registry:
    providers: dict[str, ProviderConfig]
    models: dict[str, dict[str, Any]]
    aliases: dict[str, str]
    path: Path | None = None  # where to persist alias changes; None = in-memory only

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Registry":
        """Load the model catalog.

        Precedence: an explicit `path` is used exactly as given (no merge —
        it is a full override). Otherwise the user file ~/.bird/models.json is
        merged over the builtin package file: per top-level key, user entries
        add or override by name/spec, and the builtin supplies the base
        catalog. The registry persists to the file the user data came from
        (the user file, or the explicit path) — never to the builtin.
        """
        if path is not None:
            p = Path(path)
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls(
                providers=_providers_from(data),
                models=data.get("models", {}),
                aliases=data.get("aliases", {}),
                path=p,
            )
        data = _merge_user_over_builtin()
        # path is the user file even when it doesn't exist yet: _persist seeds
        # it from the merged state on the first /model or /think choice, and
        # nothing writes until then.
        return cls(
            providers=_providers_from(data),
            models=data.get("models", {}),
            aliases=data.get("aliases", {}),
            path=USER_MODELS_JSON,
        )

    def set_default(self, spec: str, context_window: int | None = None) -> bool:
        """Make `spec` the `default` alias, remembering its context window when
        discovery learned one. Persists to the user file ~/.bird/models.json
        (creating it if needed) so the choice survives upgrades and root-owned
        prefixes — the builtin package file is never written. An explicit
        --models-json keeps persisting to that file, as before. Returns False
        when there is no file to write, or when the file is not writable. The
        in-memory alias applies either way, so the current run still honours
        the choice; only persistence is lost."""
        self.aliases["default"] = spec
        if context_window and spec not in self.models:
            self.models[spec] = {"context_window": context_window}

        def apply(data: dict) -> None:
            data.setdefault("aliases", {})["default"] = spec
            if spec in self.models:
                data.setdefault("models", {}).setdefault(spec, self.models[spec])

        return self._persist(apply, what="default")

    def set_think_mode(self, spec: str, reasoning_effort: str | None) -> bool:
        """Persist `reasoning_effort` into the model's models.json entry so it
        survives across sessions and /reload respawns. None clears it (removes
        the key). Writes the user file ~/.bird/models.json (creating it if
        needed) — never the builtin package file; an explicit --models-json
        keeps persisting to that file. Returns False when there is no file to
        write or it isn't writable — the in-memory update applies either way."""
        entry = self.models.setdefault(spec, {})
        if reasoning_effort is None:
            entry.pop("reasoning_effort", None)
        else:
            entry["reasoning_effort"] = reasoning_effort

        def apply(data: dict) -> None:
            models = data.setdefault("models", {})
            if reasoning_effort is None:
                models.get(spec, {}).pop("reasoning_effort", None)
            else:
                models.setdefault(spec, {})["reasoning_effort"] = reasoning_effort

        return self._persist(apply, what="thinking mode")

    def _persist(self, mutate: Callable[[dict], None], what: str) -> bool:
        """Rewrite only the touched keys of the persistence file. The target is
        the user file unless an explicit --models-json was loaded; the builtin
        package file is never a write target. When the registry was loaded
        from the builtin alone (no user file yet), the user file is created
        from the merged in-memory state, so the first /model or /think choice
        seeds it with everything needed to reproduce the session."""
        if self.path is None:
            return False
        try:
            if self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    # a corrupt persistence file must not break the session:
                    # reseed it from the merged in-memory state, like load()
                    # degrades to the builtin
                    print(
                        f"warning: {self.path} is not valid JSON ({e}); "
                        f"rewriting it from the current session state",
                        file=sys.stderr,
                    )
                    data = {}
                mutate(data)
            else:
                # no file yet: seed it from the full in-memory (merged) state
                data = {
                    "providers": {
                        name: {
                            k: v
                            for k, v in {
                                "base_url": p.base_url,
                                "api_key_env": p.api_key_env,
                                "native_url": p.native_url,
                            }.items()
                            if v is not None
                        }
                        for name, p in self.providers.items()
                    },
                    "models": dict(self.models),
                    "aliases": dict(self.aliases),
                }
                mutate(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            print(
                f"warning: could not persist {what} to {self.path} ({e}); "
                f"pass --models-json to use a writable config",
                file=sys.stderr,
            )
            return False
        return True

    def resolve(self, name: str) -> ModelSpec:
        """Resolve an alias or provider:model spec to a ModelSpec."""
        spec = self.aliases.get(name, name)
        if ":" not in spec:
            raise RegistryError(
                f"'{name}' is not a known alias ({sorted(self.aliases)}) and is not "
                f"a 'provider:model' spec"
            )
        provider_name, model = spec.split(":", 1)
        provider = self.providers.get(provider_name)
        if provider is None:
            raise RegistryError(
                f"unknown provider '{provider_name}' in '{spec}' "
                f"(known: {sorted(self.providers)})"
            )
        if provider_name == "ollama":
            model, cloud = split_cloud_marker(model)
            provider = ollama_provider_for(provider, cloud)
        entry = dict(self.models.get(spec, {}))
        if not entry and spec not in _warned_unknown_specs:
            # a wrong context window silently breaks compaction, so be loud
            _warned_unknown_specs.add(spec)
            print(
                f"warning: no models.json entry for '{spec}'; assuming "
                f"context_window={DEFAULT_CONTEXT_WINDOW}",
                file=sys.stderr,
            )
        return ModelSpec(
            spec=spec,
            provider=provider,
            model=model,
            context_window=entry.pop("context_window", DEFAULT_CONTEXT_WINDOW),
            supports_tools=entry.pop("supports_tools", True),
            constrained_decoding=entry.pop("constrained_decoding", False),
            extra=entry,
        )
