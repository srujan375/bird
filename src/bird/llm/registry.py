"""Model registry: models.json → resolved ModelSpec.

Specs are `provider:model` strings (split on the FIRST colon only — Ollama
model names themselves contain colons, e.g. `ollama:qwen2.5-coder:14b`).
Aliases (`default`, `judge`, `compactor`) resolve to full specs. Unknown but
well-formed specs resolve with conservative defaults so a user can point at
any OpenRouter/Ollama model without editing models.json first.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONTEXT_WINDOW = 32768

_warned_unknown_specs: set[str] = set()

# Package data (see pyproject), so this resolves to one file in both worlds:
# src/bird/models.json in a checkout, bird/models.json in an installed wheel.
# The old path was parents[3] — the repo root, which only exists in a source
# tree. models.json shipped in no wheel at all, so every non-editable install
# raised FileNotFoundError in load(); editable installs hid it by importing
# straight from the source tree.
_BUILTIN_MODELS_JSON = Path(__file__).resolve().parents[1] / "models.json"


class RegistryError(Exception):
    pass


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str | None = None
    native_url: str | None = None

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


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
        p = Path(path) if path else _BUILTIN_MODELS_JSON
        data = json.loads(p.read_text(encoding="utf-8"))
        providers = {
            name: ProviderConfig(name=name, **cfg)
            for name, cfg in data.get("providers", {}).items()
        }
        return cls(
            providers=providers,
            models=data.get("models", {}),
            aliases=data.get("aliases", {}),
            path=p,
        )

    def set_default(self, spec: str, context_window: int | None = None) -> bool:
        """Make `spec` the `default` alias, remembering its context window when
        discovery learned one. Persists by rewriting only the touched keys of
        the loaded models.json; returns False when there is no file to write, or
        when the file is not writable — now reachable, since an installed wheel
        can sit in a root-owned prefix. The in-memory alias applies either way,
        so the current run still honours the choice; only persistence is lost."""
        self.aliases["default"] = spec
        if context_window and spec not in self.models:
            self.models[spec] = {"context_window": context_window}
        if self.path is None:
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data.setdefault("aliases", {})["default"] = spec
            if spec in self.models:
                data.setdefault("models", {}).setdefault(spec, self.models[spec])
            self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            print(
                f"warning: could not persist default to {self.path} ({e}); "
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
