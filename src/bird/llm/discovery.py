"""Model discovery: what can /model actually switch to right now?

Four sources, merged and deduped by spec: models.json entries (always
available), the local Ollama daemon's installed models (each with the context
window the daemon reports, so a pick is recorded at the model's real limit
rather than DEFAULT_CONTEXT_WINDOW), ollama.com's hosted
catalog when OLLAMA_API_KEY is set (listed with the `:cloud` marker), and
OpenRouter's catalog when OPENROUTER_API_KEY is set. Sources that are unreachable or
unconfigured are skipped with a human-readable note instead of an error —
discovery powers an interactive picker, so partial results beat failure.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .ollama import Ollama
from .registry import Registry, add_cloud_marker, ollama_provider_for, split_cloud_marker

CATALOG_TIMEOUT = 10.0

# Context windows for the :cloud models bird ships. ollama.com exposes no
# per-model window (the /api/tags catalog omits it and /api/show is a local
# daemon endpoint, not part of the hosted API), so a cloud spec has no source
# to learn it from — and a wrong window silently breaks compaction, so the
# values are real, sourced from the local/sibling spellings already in
# models.json. deepseek-v4.1-flash has no sibling entry; it matches its
# deepseek-v4-flash:0731 sibling.
CLOUD_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4.1-flash": 1000000,
    "deepseek-v4-flash:0731": 1000000,
    "glm-5.3-flash": 1000000,
    "glm-5.3": 1000000,
    "kimi-k3": 900000,
    "kimi-k2.7-code": 250000,
    "minimax-m3": 500000,
    "gemma4:31b": 200000,
}


@dataclass
class DiscoveredModel:
    spec: str  # full "provider:model"
    source: str  # "configured" | "ollama" | "ollama.com" | "openrouter"
    context_window: int | None = None


def discover_models(
    registry: Registry,
    *,
    http: httpx.Client | None = None,
    ollama: Ollama | None = None,
    ollama_cloud: Ollama | None = None,
) -> tuple[list[DiscoveredModel], list[str]]:
    """Return (models, notes). Notes explain skipped/unreachable sources."""
    models: list[DiscoveredModel] = []
    by_spec: dict[str, DiscoveredModel] = {}
    notes: list[str] = []

    def add(m: DiscoveredModel) -> None:
        if m.spec not in by_spec:
            by_spec[m.spec] = m
            models.append(m)

    for spec, entry in registry.models.items():
        add(DiscoveredModel(spec=spec, source="configured", context_window=entry.get("context_window")))

    provider = registry.providers.get("ollama")
    if provider is not None:
        local = ollama_provider_for(provider, cloud=False)
        client = ollama or Ollama(local.native_url, api_key_env=local.api_key_env)
        try:
            if client.is_up():
                for name in sorted(client.local_models()):
                    # "ornith:latest" is the daemon's spelling of "ornith";
                    # keep the configured spelling so its entry applies
                    spec = f"ollama:{name.removesuffix(':latest')}"
                    known = by_spec.get(spec)
                    if known is not None and known.context_window:
                        continue  # configured with a window: models.json wins
                    window = client.context_length(name)
                    if known is not None:
                        # configured, but the entry never learned its window
                        # (a /think choice alone writes one): the daemon's fills it
                        known.context_window = window
                    else:
                        add(DiscoveredModel(spec=spec, source="ollama", context_window=window))
            else:
                notes.append("ollama: not reachable — start it with `ollama serve`")
        except httpx.HTTPError as e:
            notes.append(f"ollama: listing failed ({e})")
        finally:
            if ollama is None:
                client.close()
        # hosted catalog: listed under the cloud marker so picking one routes
        # to ollama.com (see registry.split_cloud_marker)
        cloud = ollama_provider_for(provider, cloud=True)
        if not cloud.api_key:
            notes.append(f"ollama.com: set {cloud.api_key_env} to list its catalog")
        elif ollama_cloud is not None or ollama is None:
            client = ollama_cloud or Ollama(cloud.native_url, api_key_env=cloud.api_key_env)
            try:
                if client.is_up():
                    for name in sorted(client.local_models()):
                        # the hosted catalog carries no window, so the static
                        # table is the only source — without it a pick is
                        # recorded at DEFAULT_CONTEXT_WINDOW and warned about
                        add(
                            DiscoveredModel(
                                spec=f"ollama:{add_cloud_marker(name)}",
                                source="ollama.com",
                                context_window=CLOUD_CONTEXT_WINDOWS.get(name),
                            )
                        )
                else:
                    notes.append("ollama.com: not reachable")
            except httpx.HTTPError as e:
                notes.append(f"ollama.com: catalog fetch failed ({e})")
            finally:
                if ollama_cloud is None:
                    client.close()

    provider = registry.providers.get("openrouter")
    if provider is not None:
        if not provider.api_key:
            notes.append(
                f"openrouter: set {provider.api_key_env or 'the API key'} to list its catalog"
            )
        else:
            try:
                for spec, ctx in _openrouter_catalog(provider.base_url, provider.api_key, http):
                    add(DiscoveredModel(spec=spec, source="openrouter", context_window=ctx))
            except httpx.HTTPError as e:
                notes.append(f"openrouter: catalog fetch failed ({e})")

    return models, notes


def ollama_context_window(
    registry: Registry, name: str, *, ollama: Ollama | None = None
) -> int | None:
    """The local daemon's context length for an `ollama:` spec (or alias)
    that has no models.json entry — what a direct `/model <spec>` or a
    `--model` at startup records instead of assuming DEFAULT_CONTEXT_WINDOW.
    None when the spec's entry already has a window (models.json wins),
    it is cloud-marked (no /api/show there), another provider's, or the
    daemon can't say. An entry WITHOUT a window is asked about: a /think
    choice writes reasoning_effort alone, which must not hide the gap."""
    spec = registry.aliases.get(name, name)
    if registry.models.get(spec, {}).get("context_window"):
        return None
    provider_name, _, model = spec.partition(":")
    if provider_name != "ollama" or not model:
        return None
    model, cloud = split_cloud_marker(model)
    if cloud:
        # no /api/show for a hosted model: the static table is the only source
        return CLOUD_CONTEXT_WINDOWS.get(model)
    provider = registry.providers.get("ollama")
    if provider is None:
        return None
    local = ollama_provider_for(provider, cloud=False)
    client = ollama or Ollama(local.native_url, api_key_env=local.api_key_env)
    try:
        return client.context_length(model)
    finally:
        if ollama is None:
            client.close()


def _openrouter_catalog(
    base_url: str, api_key: str, http: httpx.Client | None
) -> list[tuple[str, int | None]]:
    client = http or httpx.Client(timeout=CATALOG_TIMEOUT)
    try:
        resp = client.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        out = [
            (f"openrouter:{m['id']}", m.get("context_length"))
            for m in resp.json().get("data", [])
            if m.get("id")
        ]
        return sorted(out)
    finally:
        if http is None:
            client.close()
