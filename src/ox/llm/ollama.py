"""Ollama lifecycle helper on the NATIVE API (not the /v1 shim).

Health via /api/tags, model pull via /api/pull, and keep_alive warming via
/api/generate so the model stays resident between harness turns instead of
reloading weights on every call.

Auth: when an API key is supplied (either explicitly via ``api_key`` or via
the ``OLLAMA_API_KEY`` env var when ``api_key_env="OLLAMA_API_KEY"``), every
request carries an ``Authorization: Bearer <key>`` header. This is the
programmatic-access path to ollama.com's hosted API at
``https://ollama.com/api`` — local daemons at ``http://localhost:11434``
ignore the header. See https://docs.ollama.com/api/authentication.

Hosted endpoints (any non-localhost ``native_url``) have no /api/pull — the
catalog is fixed server-side and pull returns 401 for everyone — and don't
need keep_alive warming, so ``ensure()`` only verifies the model is in the
catalog there.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

import httpx

DEFAULT_NATIVE_URL = "http://localhost:11434"
DEFAULT_KEEP_ALIVE = "30m"

DEFAULT_API_KEY_ENV = "OLLAMA_API_KEY"


class OllamaError(Exception):
    pass


class Ollama:
    def __init__(
        self,
        native_url: str = DEFAULT_NATIVE_URL,
        timeout: float = 30.0,
        api_key: str | None = None,
        api_key_env: str | None = DEFAULT_API_KEY_ENV,
    ):
        self.native_url = native_url.rstrip("/")
        # Explicit api_key wins; otherwise read from api_key_env if set.
        if api_key is not None:
            self.api_key = api_key
        elif api_key_env:
            self.api_key = os.environ.get(api_key_env) or None
        else:
            self.api_key = None
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    @property
    def is_local(self) -> bool:
        host = urlsplit(self.native_url).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    def _auth_error(self, op: str, status: int) -> OllamaError:
        if self.api_key:
            detail = "authentication failed; the API key was sent but rejected"
        else:
            detail = (
                f"authentication required; set {DEFAULT_API_KEY_ENV} "
                f"or pass api_key= to Ollama(...)"
            )
        return OllamaError(f"{op}: HTTP {status} — {detail}")

    def is_up(self) -> bool:
        try:
            return self._http.get(
                f"{self.native_url}/api/tags", headers=self._headers()
            ).status_code == 200
        except httpx.HTTPError:
            return False

    def local_models(self) -> list[str]:
        resp = self._http.get(f"{self.native_url}/api/tags", headers=self._headers())
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    def has_model(self, name: str) -> bool:
        # Ollama treats "model" and "model:latest" as the same thing.
        names = set(self.local_models())
        return name in names or f"{name}:latest" in names

    def pull(self, name: str, on_progress=None) -> None:
        """Pull a model, streaming progress. Raises OllamaError on failure."""
        with self._http.stream(
            "POST",
            f"{self.native_url}/api/pull",
            json={"model": name},
            headers=self._headers(),
            timeout=None,
        ) as resp:
            if resp.status_code == 401 or resp.status_code == 403:
                raise self._auth_error(f"pull {name}", resp.status_code)
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
            headers=self._headers(),
            timeout=300.0,
        )
        if resp.status_code == 401 or resp.status_code == 403:
            raise self._auth_error(f"warm {name}", resp.status_code)
        if resp.status_code != 200:
            raise OllamaError(f"warm {name}: HTTP {resp.status_code}: {resp.text[:200]}")

    def ensure(self, name: str, keep_alive: str = DEFAULT_KEEP_ALIVE, on_progress=None) -> None:
        """Health-check, pull if missing, warm. The one call `ox code` makes."""
        if not self.is_up():
            hint = (
                f"start it with `ollama serve`, or for ollama.com cloud "
                f"set {DEFAULT_API_KEY_ENV} and point native_url at https://ollama.com"
            )
            raise OllamaError(f"Ollama is not reachable at {self.native_url}. {hint}.")
        if not self.is_local:
            # Hosted catalog: nothing to pull, nothing to warm — but fail
            # loudly if the name isn't served there (e.g. a ":cloud"-suffixed
            # name, which only the local daemon uses for proxied models).
            if not self.has_model(name):
                raise OllamaError(
                    f"{name} is not in the catalog at {self.native_url} "
                    f"(available: {', '.join(sorted(self.local_models()))})"
                )
            return
        if not self.has_model(name):
            self.pull(name, on_progress=on_progress)
        self.warm(name, keep_alive=keep_alive)
