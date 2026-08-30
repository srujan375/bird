"""`bird doctor`: one line per check, a fix hint per failure, exit 0/1.

The startup check the 401-mid-task experience was missing: run it after
install, after an upgrade, or whenever a session misbehaves, and it says
exactly what is broken and what to do about it. Read-only — doctor never
writes config; that is `bird setup`'s job.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from .llm.ollama import DEFAULT_API_KEY_ENV, Ollama
from .llm.registry import USER_ENV_FILE, USER_MODELS_JSON, Registry
from .setup import LOCAL_OLLAMA_URL, Probes, probe

MIN_PYTHON = (3, 11)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""  # shown only on failure


def run_checks(registry: Registry, repo: str | Path = ".") -> list[Check]:
    """Every check, in display order. Pure: nothing is printed or written."""
    probes = probe(registry)
    return [
        _python_check(),
        _keys_check(probes),
        _local_ollama_check(probes),
        _cloud_check(probes),
        _openrouter_check(probes),
        _default_alias_check(registry),
        _tui_check(),
        _kg_check(repo),
    ]


def render_checks(checks: list[Check]) -> list[str]:
    """One line per check (plus an indented fix hint on failure) and a
    closing summary line."""
    lines: list[str] = []
    for c in checks:
        line = f"{'✓' if c.ok else '✗'} {c.name}"
        if c.detail:
            line += f" — {c.detail}"
        lines.append(line)
        if not c.ok and c.fix:
            lines.append(f"    fix: {c.fix}")
    failed = sum(1 for c in checks if not c.ok)
    lines.append("")
    if failed:
        lines.append(f"{failed} check(s) need attention — `bird setup` (or /setup in a session) fixes most of these")
    else:
        lines.append("all checks passed")
    return lines


def doctor_main(args) -> int:
    """Entry point for `bird doctor`. Exit 0 when everything is healthy, 1
    when at least one check needs attention."""
    registry = Registry.load(getattr(args, "models_json", None))
    checks = run_checks(registry, getattr(args, "repo", "."))
    for line in render_checks(checks):
        print(line)
    return 1 if any(not c.ok for c in checks) else 0


def _python_check() -> Check:
    v = sys.version_info
    ok = v >= MIN_PYTHON
    return Check(
        name=f"python {v.major}.{v.minor}.{v.micro}",
        ok=ok,
        fix=f"bird needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+",
    )


def _keys_check(p: Probes) -> Check:
    set_keys = [name for name, key in ((DEFAULT_API_KEY_ENV, p.ollama_key),
                                       ("OPENROUTER_API_KEY", p.openrouter_key)) if key]
    if set_keys:
        return Check(name="provider keys", ok=True, detail=", ".join(set_keys))
    if p.local_up:
        return Check(
            name="provider keys",
            ok=True,
            detail="none set — fine for local Ollama; cloud-marked and openrouter models need a key",
        )
    return Check(
        name="provider keys",
        ok=False,
        detail="no OLLAMA_API_KEY or OPENROUTER_API_KEY in the environment",
        fix=f"`bird setup` writes them to {USER_ENV_FILE}, or export them in your shell",
    )


def _local_ollama_check(p: Probes) -> Check:
    if p.local_up:
        return Check(
            name=f"local Ollama ({LOCAL_OLLAMA_URL})",
            ok=True,
            detail=f"{len(p.local_models)} models installed",
        )
    # down is only a failure when nothing else can serve a model
    if p.cloud_ok or p.openrouter_ok:
        return Check(
            name=f"local Ollama ({LOCAL_OLLAMA_URL})",
            ok=True,
            detail="not running (cloud providers cover model serving)",
        )
    return Check(
        name=f"local Ollama ({LOCAL_OLLAMA_URL})",
        ok=False,
        detail="not running",
        fix="`ollama serve`, or set OLLAMA_API_KEY / OPENROUTER_API_KEY for a cloud provider",
    )


def _cloud_check(p: Probes) -> Check:
    if not p.ollama_key:
        return Check(
            name="Ollama Cloud",
            ok=True,
            detail=f"no {DEFAULT_API_KEY_ENV} (skipped)",
        )
    if p.cloud_ok:
        return Check(name="Ollama Cloud", ok=True, detail="reachable with key")
    return Check(
        name="Ollama Cloud",
        ok=False,
        detail="key set but ollama.com is unreachable",
        fix="check the key and your network — the key may be revoked",
    )


def _openrouter_check(p: Probes) -> Check:
    if not p.openrouter_key:
        return Check(
            name="OpenRouter",
            ok=True,
            detail="no OPENROUTER_API_KEY (skipped)",
        )
    if p.openrouter_ok:
        return Check(name="OpenRouter", ok=True, detail="reachable with key")
    return Check(
        name="OpenRouter",
        ok=False,
        detail="key set but openrouter.ai is unreachable",
        fix="check the key and your network — the key may be revoked",
    )


def _default_alias_check(registry: Registry) -> Check:
    alias = registry.aliases.get("default")
    if not alias:
        return Check(
            name="default model alias",
            ok=False,
            detail="no 'default' alias in models.json",
            fix="`bird setup` picks one, or /model in a session",
        )
    try:
        spec = registry.resolve(alias)
    except Exception as e:
        return Check(
            name="default model alias",
            ok=False,
            detail=f"'{alias}' does not resolve: {e}",
            fix="fix the alias in ~/.bird/models.json, or `bird setup` to re-pick",
        )
    where = USER_MODELS_JSON if USER_MODELS_JSON.is_file() else "the builtin models.json"
    return Check(name="default model alias", ok=True, detail=f"{spec.spec} ({where})")


def _tui_check() -> Check:
    from .cli import _tui_dir

    tui_dir = _tui_dir()
    if tui_dir is not None:
        return Check(name="TUI", ok=True, detail=str(tui_dir))
    return Check(
        name="TUI",
        ok=False,
        detail="not installed (sessions fall back to the plain REPL)",
        fix="cd tui && npm install",
    )


def _kg_check(repo: str | Path = ".") -> Check:
    from .context.kg import KG

    kg = KG(repo)
    if not kg.is_ready():
        return Check(
            name="knowledge graph",
            ok=True,
            detail="not built yet (built in the background on first run)",
        )
    stale = kg.is_stale()
    if stale:
        return Check(
            name="knowledge graph",
            ok=False,
            detail="stale — the repo has changed since the last build",
            fix="`bird kg update` (or ignore it: sessions refresh it in the background)",
        )
    return Check(name="knowledge graph", ok=True, detail=str(kg.out_dir))