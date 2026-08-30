"""`bird setup`: the one command between install and first success.

Probes what is reachable (provider keys after all .env loading, the local
Ollama daemon, Ollama Cloud, OpenRouter), then — interactively — prompts for
what is missing and writes it to user-level files the user never has to touch
again: keys to ~/.bird/.env (chmod 600), the local daemon and the default
model to ~/.bird/models.json. With --yes, or when stdin is not a tty, it
applies the defaults it can detect and prints what still needs manual
attention instead of hanging on a prompt.

Only user-level files are ever written here — never the builtin package file.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .llm.discovery import discover_models
from .llm.ollama import DEFAULT_API_KEY_ENV, Ollama
from .llm.registry import OLLAMA_CLOUD_URL, USER_ENV_FILE, USER_MODELS_JSON, Registry
from .llm.types import Message
from .llm.wire.openai_compat import OpenAICompatClient

LOCAL_OLLAMA_URL = "http://localhost:11434"
LOCAL_OLLAMA_BASE_URL = LOCAL_OLLAMA_URL + "/v1"
KEY_ENV_VARS = ("OLLAMA_API_KEY", "OPENROUTER_API_KEY")
VERIFY_PROMPT = "Reply with the single word: ok"


@dataclass
class Probes:
    """What the machine looks like right now, after all .env loading."""

    ollama_key: str | None = None
    openrouter_key: str | None = None
    local_up: bool = False
    local_models: list[str] = field(default_factory=list)
    cloud_ok: bool | None = None  # None = not probed (no key)
    openrouter_ok: bool | None = None

    @property
    def any_key(self) -> bool:
        return bool(self.ollama_key or self.openrouter_key)

    @property
    def any_model_source(self) -> bool:
        return self.local_up or self.cloud_ok is True or self.openrouter_ok is True


def probe(registry: Registry, *, ollama: Ollama | None = None, http: httpx.Client | None = None) -> Probes:
    """Probe keys in the environment, the local daemon, and both cloud
    endpoints. Unreachable things become False/None fields, never exceptions —
    setup must work on a machine with nothing installed yet."""
    p = Probes(
        ollama_key=os.environ.get(DEFAULT_API_KEY_ENV),
        openrouter_key=os.environ.get("OPENROUTER_API_KEY"),
    )
    local = ollama or Ollama(LOCAL_OLLAMA_URL, api_key_env=None)
    try:
        p.local_up = local.is_up()
        if p.local_up:
            p.local_models = sorted(local.local_models())
    except httpx.HTTPError:
        p.local_up = False
    finally:
        if ollama is None:
            local.close()

    provider = registry.providers.get("ollama")
    if p.ollama_key and provider is not None:
        client = Ollama(OLLAMA_CLOUD_URL, api_key=p.ollama_key)  # cloud is always ollama.com
        try:
            p.cloud_ok = client.is_up()
        except httpx.HTTPError:
            p.cloud_ok = False
        finally:
            client.close()

    provider = registry.providers.get("openrouter")
    if p.openrouter_key and provider is not None:
        own_http = http or httpx.Client(timeout=10.0)
        try:
            resp = own_http.get(
                f"{provider.base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {p.openrouter_key}"},
            )
            p.openrouter_ok = resp.status_code == 200
        except httpx.HTTPError:
            p.openrouter_ok = False
        finally:
            if http is None:
                own_http.close()
    return p


def write_env_keys(keys: dict[str, str], path: Path | None = None) -> None:
    """Merge KEY=value pairs into the user .env, creating it with 600 perms.
    Existing lines for the same key are replaced in place; other lines and
    comments survive. The path is read from the registry module at call time
    so tests can monkeypatch it."""
    if path is None:
        path = USER_ENV_FILE
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    for key, value in keys.items():
        prefix = f"{key}="
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def set_local_native_url(registry: Registry, path: Path | None = None) -> None:
    """Point the ollama provider's native_url at the local daemon in the user
    models.json (creating it from the merged state if needed). base_url moves
    with it — the OpenAI-compat endpoint lives on the same daemon. The path is
    read from the registry module at call time so tests can monkeypatch it."""
    if path is None:
        path = USER_MODELS_JSON
    if path.is_file():
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = _user_models_data(registry)
    provider = data.setdefault("providers", {}).setdefault("ollama", {})
    provider["native_url"] = LOCAL_OLLAMA_URL
    provider["base_url"] = LOCAL_OLLAMA_BASE_URL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps(data), encoding="utf-8")


def _user_models_data(registry: Registry) -> dict:
    """Seed a user models.json from the merged in-memory state — the same
    shape Registry._persist writes, so the first setup write is a complete
    file rather than a fragment."""
    return {
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
            for name, p in registry.providers.items()
        },
        "models": dict(registry.models),
        "aliases": dict(registry.aliases),
    }


def _dumps(data: dict) -> str:
    import json

    return json.dumps(data, indent=2) + "\n"


def verify_model(spec_str: str, registry: Registry, *, client: OpenAICompatClient | None = None) -> tuple[bool, str]:
    """One real test call. Returns (ok, detail) — the proof the setup worked,
    not a config file that merely looks right."""
    own = client or OpenAICompatClient(timeout=60.0)
    try:
        spec = registry.resolve(spec_str)
        resp = own.complete(spec, [Message(role="user", content=VERIFY_PROMPT)], max_tokens=200)
        text = (resp.message.content or "").strip()
        return True, f"replied {text[:60]!r}" if text else "replied (empty)"
    except Exception as e:  # any failure — auth, network, bad spec — is a failed verify
        return False, str(e)[:200]
    finally:
        if client is None:
            own.close()


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def _ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def setup_main(args) -> int:
    """Entry point for `bird setup`. Returns 0 when a default model was picked
    and verified, 1 when something still needs manual attention."""
    registry = Registry.load(args.models_json)
    interactive = sys.stdin.isatty() and not getattr(args, "yes", False)

    print("checking what's available…")
    probes = probe(registry)
    _print_probes(probes)

    todo: list[str] = []  # what still needs manual attention

    # 1. keys: prompt interactively, report otherwise
    missing = [name for name, key in (("OLLAMA_API_KEY", probes.ollama_key),
                                      ("OPENROUTER_API_KEY", probes.openrouter_key)) if not key]
    to_write: dict[str, str] = {}
    for name in missing:
        if interactive:
            value = _ask(f"{name} (empty to skip)")
            if value:
                to_write[name] = value
        else:
            todo.append(f"set {name} (export it, or add it to {USER_ENV_FILE})")
    if to_write:
        write_env_keys(to_write)
        for name, value in to_write.items():
            os.environ[name] = value
        print(f"wrote {', '.join(to_write)} → {USER_ENV_FILE}")
        # re-probe with the new keys so the rest of setup sees them
        probes = probe(registry)

    # 2. local Ollama: nothing to configure — an `ollama:<model>` spec
    # without the cloud marker always goes to the daemon (registry rule)
    if probes.local_up:
        print(f"local Ollama at {LOCAL_OLLAMA_URL}: serves any ollama:<model> spec without a cloud marker")
    elif not interactive:
        todo.append("start a model source: `ollama serve`, or set OLLAMA_API_KEY / OPENROUTER_API_KEY")

    # 3. pick a default alias from what discovery can actually see
    models, notes = discover_models(registry)
    for note in notes:
        print(f"  note: {note}")
    default_spec = registry.aliases.get("default")
    candidates = [m for m in models if m.source != "configured" or m.spec == default_spec]
    if not candidates:
        candidates = models

    chosen: str | None = None
    if interactive and candidates:
        print("\navailable models:")
        for i, m in enumerate(candidates, 1):
            ctx = f" ({m.context_window:,} ctx)" if m.context_window else ""
            print(f"  {i:3d}. {m.spec}{ctx}")
        raw = _ask("default model (number or provider:model, empty to keep current)")
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            chosen = candidates[int(raw) - 1].spec
        elif raw:
            chosen = raw
    elif not interactive:
        if probes.local_up and not probes.ollama_key:
            # only what the daemon actually serves: a cloud-marked default is
            # spelled `ollama:` too but needs the key we just found missing
            local_specs = [m.spec for m in candidates if m.source == "ollama"]
            chosen = local_specs[0] if local_specs else None
        if chosen is None and not probes.any_model_source:
            todo.append("no model source found — run `bird setup` interactively to configure one")

    # 4. verify the choice with one real call
    if chosen and chosen != default_spec:
        if registry.set_default(chosen):
            print(f"default → {chosen} ({USER_MODELS_JSON})")
        else:
            print(f"default → {chosen} (this session only — {USER_MODELS_JSON} not writable)")
        default_spec = chosen
    if default_spec:
        ok, detail = verify_model(default_spec, registry)
        mark = "✓" if ok else "✗"
        print(f"verify {default_spec}: {'ok' if ok else 'FAILED'} — {detail}")
        if not ok:
            todo.append(f"the default model '{default_spec}' did not answer: {detail}")
    elif not interactive:
        todo.append("no default model chosen — run `bird setup` interactively, or /model in a session")

    from .onboard import mark_setup_done

    mark_setup_done()  # the first-launch walkthrough need not ask again
    if todo:
        print("\nstill needs attention:")
        for item in todo:
            print(f"  - {item}")
        return 1
    print("\nsetup complete — `bird` to start a session, `bird doctor` to re-check")
    return 0


def _print_probes(p: Probes) -> None:
    def key_line(name: str, value: str | None) -> str:
        return f"  {name}: {'set' if value else 'not set'}"

    print(key_line("OLLAMA_API_KEY", p.ollama_key))
    print(key_line("OPENROUTER_API_KEY", p.openrouter_key))
    if p.local_up:
        print(f"  local Ollama at {LOCAL_OLLAMA_URL}: up ({len(p.local_models)} models)")
    else:
        print(f"  local Ollama at {LOCAL_OLLAMA_URL}: down")
    print(f"  Ollama Cloud: {'reachable' if p.cloud_ok else 'unreachable'}"
          + ("" if p.ollama_key else " (no OLLAMA_API_KEY)"))
    print(f"  OpenRouter: {'reachable' if p.openrouter_ok else 'unreachable'}"
          + ("" if p.openrouter_key else " (no OPENROUTER_API_KEY)"))