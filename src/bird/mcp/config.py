"""mcp.json config: which MCP servers to launch, and how.

Mirrors load_skills discovery: the project file (``.bird/mcp.json``) is read
first, then the user file (``~/.bird/mcp.json``); first wins on a name
collision. Schema, deliberately lean:

    {"servers": {"name": {"command": str, "args": [str], "env": {str: str},
                          "disabled": bool}}}

Env values support ``$VAR`` expansion from the parent environment, so a
secret stays in the environment instead of landing in a file that ends up in
git. ``disabled: true`` keeps the entry but skips launching it — the config
stays in the file instead of being deleted and re-added. Unknown keys are
ignored (forward-compat); a missing ``command`` is a config error. A file
that exists but won't parse is a loud error naming the path — never a silent
empty server list.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_CONFIG = ".bird/mcp.json"
USER_CONFIG = ".bird/mcp.json"

_VAR = re.compile(r"\$(\w+)")


class McpError(Exception):
    """Config/connection failure the user must see. cli.py prints it and
    exits 2 — a configured server that won't start is a config bug, not a
    session that silently lacks tools."""


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    source: str = "project"  # "project" | "user" — which file the entry came from
    # the escape hatch for "keep the entry, don't launch it" — a server you
    # only want on some days stays in the file instead of being deleted and
    # re-added. Kept in the loaded list (so `bird mcp list` shows it) and
    # skipped at mount time.
    disabled: bool = False


def _expand(value: str) -> str:
    """Expand $VAR references from the parent environment. An unset variable
    expands to empty — the warning for that lives at write time (management),
    not here, so a legitimately-optional var doesn't spam every startup."""
    return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), value)


def parse_servers(data: dict[str, Any], path: Path, source: str) -> list[McpServerSpec]:
    """Validate one mcp.json document into specs. Raises McpError on a bad
    shape; unknown keys inside an entry are ignored on purpose."""
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        raise McpError(f"{path}: 'servers' must be an object mapping names to entries")
    out: list[McpServerSpec] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise McpError(f"{path}: server '{name}' must be an object")
        command = entry.get("command")
        if not command or not isinstance(command, str):
            raise McpError(f"{path}: server '{name}' is missing a 'command' string")
        args = entry.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise McpError(f"{path}: server '{name}' has a non-string-list 'args'")
        env = entry.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise McpError(f"{path}: server '{name}' has a non-string-map 'env'")
        out.append(
            McpServerSpec(
                name=name,
                command=command,
                args=list(args),
                env={k: _expand(v) for k, v in env.items()},
                source=source,
                disabled=bool(entry.get("disabled", False)),
            )
        )
    return out


def _read(path: Path, source: str) -> list[McpServerSpec]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise McpError(f"{path}: invalid JSON at line {e.lineno}: {e.msg}") from None
    if not isinstance(data, dict):
        raise McpError(f"{path}: top level must be a JSON object")
    return parse_servers(data, path, source)


def load_mcp_servers(repo_root: Path) -> list[McpServerSpec]:
    """Project file first, then user file; first wins on a name collision."""
    seen: set[str] = set()
    out: list[McpServerSpec] = []
    for spec in (
        *_read(repo_root / PROJECT_CONFIG, "project"),
        *_read(Path.home() / USER_CONFIG, "user"),
    ):
        if spec.name in seen:
            continue
        seen.add(spec.name)
        out.append(spec)
    return out
