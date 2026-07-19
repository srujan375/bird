"""Model discovery: what can /model actually switch to right now?

Three sources, merged and deduped by spec: models.json entries (always
available), the local Ollama daemon's installed models, and OpenRouter's
catalog when OPENROUTER_API_KEY is set. Sources that are unreachable or
unconfigured are skipped with a human-readable note instead of an error —
discovery powers an interactive picker, so partial results beat failure.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .ollama import DEFAULT_NATIVE_URL, Ollama
from .registry import Registry

CATALOG_TIMEOUT = 10.0


@dataclass
class DiscoveredModel:
    spec: str  # full "provider:model"
    source: str  # "configured" | "ollama" | "openrouter"
    context_window: int | None = None


def discover_models(
    registry: Registry,
    *,
    http: httpx.Client | None = None,
    ollama: Ollama | None = None,
) -> tuple[list[DiscoveredModel], list[str]]:
    """Return (models, notes). Notes explain skipped/unreachable sources."""
    models: list[DiscoveredModel] = []
    seen: set[str] = set()
    notes: list[str] = []

    def add(m: DiscoveredModel) -> None:
        if m.spec not in seen:
            seen.add(m.spec)
            models.append(m)

    for spec, entry in registry.models.items():
        add(DiscoveredModel(spec=spec, source="configured", context_window=entry.get("context_window")))

    provider = registry.providers.get("ollama")
    if provider is not None:
        client = ollama or Ollama(
            provider.native_url or DEFAULT_NATIVE_URL,
            api_key_env=provider.api_key_env,
        )
        try:
            if client.is_up():
                for name in sorted(client.local_models()):
                    add(DiscoveredModel(spec=f"ollama:{name}", source="ollama"))
            else:
                notes.append("ollama: not reachable — start it with `ollama serve`")
        except httpx.HTTPError as e:
            notes.append(f"ollama: listing failed ({e})")
        finally:
            if ollama is None:
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
