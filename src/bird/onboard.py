"""In-session onboarding: the setup walkthrough, key management and the
doctor report, reachable from the REPL and the TUI as slash commands — and
run automatically on the very first launch.

Everything here is parameterised over an `IO` (ask / ask_secret / choose /
say) so the same walkthrough drives a terminal (`ConsoleIO`), the JSON
bridge behind the TUI (`serve.TransportIO`), and a scripted test double.
The writes all go through `setup.py` (keys → ~/.bird/.env, default →
~/.bird/models.json); nothing here touches the builtin package file.
"""

from __future__ import annotations

import getpass
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from dotenv import find_dotenv

from .llm import registry as registry_mod
from .llm.registry import Registry
from . import setup as setup_mod
from .setup import KEY_ENV_VARS, Probes

# Written once a walkthrough has completed (or been skipped) so a fresh
# install asks exactly once. Config files count too: a user who has a
# ~/.bird/models.json or .env has been through setup, by hand or by us.
SETUP_STAMP = "setup-done"


@dataclass
class Choice:
    value: str
    label: str
    description: str = ""


class IO(Protocol):
    def say(self, text: str) -> None: ...
    def ask(self, prompt: str, default: str = "") -> str: ...
    def ask_secret(self, prompt: str) -> str: ...
    def choose(self, title: str, choices: list[Choice], current: str | None = None) -> str | None: ...


class ConsoleIO:
    """Terminal I/O: input(), getpass(), numbered lists."""

    def say(self, text: str) -> None:
        print(text)

    def ask(self, prompt: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            value = input(f"{prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
        return value or default

    def ask_secret(self, prompt: str) -> str:
        try:
            return getpass.getpass(f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""

    MAX_LISTED = 40

    def choose(self, title: str, choices: list[Choice], current: str | None = None) -> str | None:
        print(f"\n{title}:")
        for i, c in enumerate(choices[: self.MAX_LISTED], 1):
            mark = "●" if c.value == current else " "
            desc = f"  — {c.description}" if c.description else ""
            print(f"  {mark} {i:3d}. {c.label}{desc}")
        if len(choices) > self.MAX_LISTED:
            print(f"      … {len(choices) - self.MAX_LISTED} more — type the provider:model spec")
        raw = self.ask("number or provider:model (empty to keep current)")
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1].value
        return raw


# ---- keys -------------------------------------------------------------------


def _file_value(path: Path | str | None, name: str) -> str | None:
    if not path or not Path(path).is_file():
        return None
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith(f"{name}="):
                return line[len(name) + 1:].strip().strip("'\"")
    except OSError:
        pass
    return None


def keys_status() -> list[tuple[str, str]]:
    """(name, where) for each provider key: the file whose value is the live
    one ('repo .env' or '~/.bird/.env'), 'shell environment', or 'not set'."""
    repo_env = find_dotenv(usecwd=True) or None
    out = []
    for name in KEY_ENV_VARS:
        live = os.environ.get(name)
        if not live:
            out.append((name, "not set"))
        elif _file_value(repo_env, name) == live:
            out.append((name, f"repo .env ({repo_env})"))
        elif _file_value(registry_mod.USER_ENV_FILE, name) == live:
            out.append((name, f"~/.bird/.env ({registry_mod.USER_ENV_FILE})"))
        else:
            out.append((name, "shell environment"))
    return out


def set_key(name: str, value: str) -> Path:
    """Persist a provider key to ~/.bird/.env and make it live in this
    process. Raises ValueError for anything but the known key names."""
    name = name.strip().upper()
    if name not in KEY_ENV_VARS:
        raise ValueError(f"unknown key {name!r} (known: {', '.join(KEY_ENV_VARS)})")
    value = value.strip()
    if not value:
        raise ValueError("empty value")
    setup_mod.write_env_keys({name: value})
    os.environ[name] = value
    return registry_mod.USER_ENV_FILE


# ---- doctor -----------------------------------------------------------------


def doctor_report(registry: Registry, repo: str | Path = ".") -> tuple[list[str], int]:
    """The `bird doctor` lines and the number of failing checks."""
    from .doctor import render_checks, run_checks

    checks = run_checks(registry, repo)
    return render_checks(checks), sum(1 for c in checks if not c.ok)


# ---- first run --------------------------------------------------------------


def _stamp_path() -> Path:
    return registry_mod.USER_BIRD_DIR / SETUP_STAMP


def needs_first_run() -> bool:
    """True until a walkthrough has run once, unless the user has already
    configured ~/.bird by other means (a models.json or .env there)."""
    if os.environ.get("BIRD_SKIP_SETUP"):
        return False
    if _stamp_path().is_file():
        return False
    return not (registry_mod.USER_MODELS_JSON.is_file() or registry_mod.USER_ENV_FILE.is_file())


def mark_setup_done() -> None:
    try:
        _stamp_path().parent.mkdir(parents=True, exist_ok=True)
        _stamp_path().write_text("")
    except OSError:
        pass  # unwritable HOME: we'll ask again next time, no worse than before


# ---- the walkthrough --------------------------------------------------------


def _probe_lines(p: Probes) -> list[str]:
    lines = [
        f"  OLLAMA_API_KEY: {'set' if p.ollama_key else 'not set'}",
        f"  OPENROUTER_API_KEY: {'set' if p.openrouter_key else 'not set'}",
    ]
    if p.local_up:
        lines.append(f"  local Ollama at {setup_mod.LOCAL_OLLAMA_URL}: up ({len(p.local_models)} models)")
    else:
        lines.append(f"  local Ollama at {setup_mod.LOCAL_OLLAMA_URL}: down")
    if p.ollama_key:
        lines.append(f"  Ollama Cloud: {'reachable' if p.cloud_ok else 'unreachable — check the key'}")
    if p.openrouter_key:
        lines.append(f"  OpenRouter: {'reachable' if p.openrouter_ok else 'unreachable — check the key'}")
    return lines


def walkthrough(
    io: IO,
    registry: Registry,
    *,
    first_run: bool = False,
    current_model: str | None = None,
) -> str | None:
    """Probe, collect missing keys, pick and verify a default model. Returns
    the spec chosen (persisted as the `default` alias) or None if the user
    kept what they had. Every prompt can be skipped with an empty answer, so
    an interrupted walkthrough leaves a usable state behind."""
    if first_run:
        io.say(
            "Welcome to bird. Let's get you a model that answers — this runs once;\n"
            "/setup repeats it, /doctor re-checks, /keys manages provider keys."
        )
    io.say("checking what's available…")
    probes = setup_mod.probe(registry)
    for line in _probe_lines(probes):
        io.say(line)

    # 1. keys: ask for each one that's missing; empty skips
    missing = [name for name, key in (("OLLAMA_API_KEY", probes.ollama_key),
                                      ("OPENROUTER_API_KEY", probes.openrouter_key)) if not key]
    if missing:
        io.say(
            "\nKeys unlock the hosted catalogs — OLLAMA_API_KEY for ollama.com "
            "(`ollama:<model>:cloud` specs), OPENROUTER_API_KEY for OpenRouter.\n"
            "Skip both to run only on the local Ollama daemon."
        )
    # keys that only a repo .env provides work in that repo alone; offer to
    # make them global so `bird` answers in every directory
    for name, where in keys_status():
        if where.startswith("repo .env") and os.environ.get(name):
            answer = io.ask(f"{name} comes from this repo's .env — store it in ~/.bird/.env for every repo? (y/n)", default="y")
            if answer.lower().startswith("y"):
                io.say(f"  {name} → {set_key(name, os.environ[name])}")
    written = []
    for name in missing:
        value = io.ask_secret(f"{name} (empty to skip)")
        if value:
            try:
                path = set_key(name, value)
            except ValueError as e:
                io.say(f"  {name}: {e}")
                continue
            written.append(name)
            io.say(f"  {name} → {path}")
    if written:
        probes = setup_mod.probe(registry)
        for line in _probe_lines(probes):
            io.say(line)
    if not probes.any_model_source:
        io.say(
            "\nno model source is reachable: start `ollama serve` for local models,\n"
            "or add a key — then run /setup again."
        )
        mark_setup_done()
        return None

    # 2. default model, from what discovery can actually see
    models, notes = setup_mod.discover_models(registry)
    for note in notes:
        io.say(f"  note: {note}")
    default_spec = registry.aliases.get("default")
    local_names = {n.removesuffix(":latest") for n in probes.local_models}

    def usable(m) -> bool:
        """Has something behind it right now: the daemon serves it, or its
        catalog's key is present. Configured entries are judged the same way
        — a builtin cloud entry without a key is not an option today."""
        if m.source == "ollama":
            return True
        if m.source == "ollama.com":
            return bool(probes.ollama_key)
        if m.source == "openrouter":
            return bool(probes.openrouter_key)
        provider, _, name = m.spec.partition(":")
        if provider == "ollama":
            plain, cloud = registry_mod.split_cloud_marker(name)
            return bool(probes.ollama_key) if cloud else plain in local_names
        if provider == "openrouter":
            return bool(probes.openrouter_key)
        return True

    candidates = [m for m in models if usable(m)]
    # OpenRouter's catalog is hundreds of entries: it is a fallback when
    # nothing else is usable, otherwise a pointer (type the spec, or /model
    # later with its filter)
    catalog = [m for m in candidates if m.source == "openrouter"]
    if catalog and len(candidates) - len(catalog) >= 3:
        candidates = [m for m in candidates if m.source != "openrouter"]
        io.say(f"  (+{len(catalog)} more on OpenRouter — type any openrouter:<model> spec, or /model to browse)")
    choices = [
        Choice(
            value=m.spec,
            label=m.spec,
            description=m.source + (f" · {m.context_window // 1024}k ctx" if m.context_window else "")
            + (" · default" if m.spec == default_spec else ""),
        )
        for m in candidates
    ]
    chosen = io.choose("default model", choices, current=current_model or default_spec) if choices else None
    if chosen and chosen != default_spec:
        if registry.set_default(chosen):
            io.say(f"default → {chosen} ({registry_mod.USER_MODELS_JSON})")
        else:
            io.say(f"default → {chosen} (this session only)")
        default_spec = chosen
    elif chosen == default_spec:
        chosen = None

    # 3. one real call
    if default_spec:
        ok, detail = setup_mod.verify_model(default_spec, registry)
        io.say(f"verify {default_spec}: {'ok' if ok else 'FAILED'} — {detail}")
        if not ok:
            io.say("  pick another with /model, add a key with /keys set <NAME>, or /doctor for details")
    mark_setup_done()
    if first_run:
        io.say("setup done — type a task to start, /help lists the commands")
    return chosen


# ---- prompt round-trip for a JSON bridge ------------------------------------


class Prompter:
    """Blocks a worker thread on a question until the UI answers, the same
    shape as permissions.PermissionBroker. `emit` sends prompt_request events;
    resolve() is called from the transport's reader thread."""

    def __init__(self, emit: Callable[..., None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, list[Any]]] = {}

    def request(self, payload: dict[str, Any]) -> str | None:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            done = threading.Event()
            slot: list[Any] = [None]
            self._pending[req_id] = (done, slot)
        self._emit("prompt_request", id=req_id, **payload)
        done.wait()
        with self._lock:
            self._pending.pop(req_id, None)
        return slot[0]

    def resolve(self, req_id: int, value: str | None) -> None:
        with self._lock:
            entry = self._pending.get(req_id)
        if entry:
            done, slot = entry
            slot[0] = value
            done.set()

    def cancel_all(self) -> None:
        with self._lock:
            entries = list(self._pending.values())
        for done, slot in entries:
            slot[0] = None
            done.set()


class TransportIO:
    """The walkthrough's IO over a JSON bridge: text goes out as
    command_output, questions as prompt_request answered by prompt_response."""

    def __init__(self, emit: Callable[..., None], prompter: Prompter) -> None:
        self._emit = emit
        self._prompter = prompter

    def say(self, text: str) -> None:
        self._emit("command_output", text=text)

    def ask(self, prompt: str, default: str = "") -> str:
        value = self._prompter.request({"prompt": prompt, "secret": False, "default": default})
        return (value or "").strip() or default

    def ask_secret(self, prompt: str) -> str:
        value = self._prompter.request({"prompt": prompt, "secret": True})
        return (value or "").strip()

    def choose(self, title: str, choices: list[Choice], current: str | None = None) -> str | None:
        value = self._prompter.request({
            "prompt": title,
            "choices": [{"value": c.value, "label": c.label, "description": c.description} for c in choices],
            "current": current,
        })
        return value or None
