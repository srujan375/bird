"""Ollama lifecycle helper on the NATIVE API (not the /v1 shim).

Health via /api/tags, model pull via /api/pull, and keep_alive warming via
/api/generate so the model stays resident between harness turns instead of
reloading weights on every call.
"""

from __future__ import annotations

import json

import httpx

DEFAULT_NATIVE_URL = "http://localhost:11434"
DEFAULT_KEEP_ALIVE = "30m"


class OllamaError(Exception):
    pass


class Ollama:
    def __init__(self, native_url: str = DEFAULT_NATIVE_URL, timeout: float = 30.0):
        self.native_url = native_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def is_up(self) -> bool:
        try:
            return self._http.get(f"{self.native_url}/api/tags").status_code == 200
        except httpx.HTTPError:
            return False

    def local_models(self) -> list[str]:
        resp = self._http.get(f"{self.native_url}/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    def has_model(self, name: str) -> bool:
        # Ollama treats "model" and "model:latest" as the same thing.
        names = set(self.local_models())
        return name in names or f"{name}:latest" in names

    def pull(self, name: str, on_progress=None) -> None:
        """Pull a model, streaming progress. Raises OllamaError on failure."""
        with self._http.stream(
            "POST", f"{self.native_url}/api/pull", json={"model": name}, timeout=None
        ) as resp:
            if resp.status_code != 200:
                raise OllamaError(f"pull {name}: HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if "error" in event:
                    raise OllamaError(f"pull {name}: {event['error']}")
                if on_progress:
                    on_progress(event)

    def warm(self, name: str, keep_alive: str = DEFAULT_KEEP_ALIVE) -> None:
        """Load the model into memory and keep it resident."""
        resp = self._http.post(
            f"{self.native_url}/api/generate",
            json={"model": name, "prompt": "", "keep_alive": keep_alive},
            timeout=300.0,
        )
        if resp.status_code != 200:
            raise OllamaError(f"warm {name}: HTTP {resp.status_code}: {resp.text[:200]}")

    def ensure(self, name: str, keep_alive: str = DEFAULT_KEEP_ALIVE, on_progress=None) -> None:
        """Health-check, pull if missing, warm. The one call `mha code` makes."""
        if not self.is_up():
            raise OllamaError(
                f"Ollama is not reachable at {self.native_url}. Start it with `ollama serve`."
            )
        if not self.has_model(name):
            self.pull(name, on_progress=on_progress)
        self.warm(name, keep_alive=keep_alive)
