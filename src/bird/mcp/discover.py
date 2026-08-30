"""Official MCP registry discovery: search, fetch, translate to mcp.json entries.

The registry (registry.modelcontextprotocol.io, API v0.1 frozen) returns
server.json metadata: packages (registryType, identifier, runtimeHint,
runtimeArguments, packageArguments, environmentVariables) and remotes. The
hard part is not the search, it's the translation: a registry Package becomes

    {command: runtimeHint, args: runtimeArguments + [identifier]
           + packageArguments, env: environmentVariables}

Only stdio-capable packages (npm/pypi with npx/uvx hints) are installable —
bird speaks stdio only. oci/docker and remote-only entries are shown in
search results but marked unsupported, keeping the gap visible rather than
silently dropping them.

Secrets: required environmentVariables are written as "$VAR" references (the
config loader expands them from the parent environment); the caller warns if
a var is unset. Secret VALUES are never written into mcp.json — plaintext
secrets in a config file are how they end up in git.

Stdlib urllib only. Registry unreachable -> McpError naming the registry and
suggesting manual `bird mcp add`; no cache, no retry.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import McpError

REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0.1"
REGISTRY_TIMEOUT = 15.0

# runtime hints bird can launch over stdio
_SUPPORTED_HINTS = {"npm": "npx", "pypi": "uvx"}


@dataclass(frozen=True)
class RegistryHit:
    name: str
    description: str
    version: str
    installable: bool  # has at least one stdio-capable package
    reason: str = ""  # why not installable ("remote — unsupported", "docker — unsupported")
    raw: dict[str, Any] = field(default_factory=dict)


def _get(path: str) -> dict[str, Any]:
    url = f"{REGISTRY_BASE}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REGISTRY_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise McpError(
            f"cannot reach the MCP registry ({REGISTRY_BASE}): {e}. "
            f"Add the server manually with `bird mcp add <name> --command <cmd>`."
        ) from None


def _hit(server: dict[str, Any]) -> RegistryHit:
    """One search result. Installable iff some package has a stdio runtime
    hint bird supports; otherwise the reason says what it would take."""
    name = server.get("name", "?")
    description = server.get("description", "")
    version = server.get("version", "")
    packages = server.get("packages", [])
    remotes = server.get("remotes", [])
    for pkg in packages:
        if pkg.get("runtimeHint") in _SUPPORTED_HINTS:
            return RegistryHit(name, description, version, True, raw=server)
    if packages:
        kinds = ", ".join(
            sorted({p.get("registryType", "?") for p in packages})
        )
        return RegistryHit(name, description, version, False,
                           reason=f"{kinds} — unsupported (bird speaks stdio only)",
                           raw=server)
    if remotes:
        return RegistryHit(name, description, version, False,
                           reason="remote — unsupported (bird speaks stdio only)",
                           raw=server)
    return RegistryHit(name, description, version, False,
                       reason="no packages or remotes listed", raw=server)


def search_registry(query: str) -> list[RegistryHit]:
    """GET /v0.1/servers?search=<q> -> hits, installable first."""
    data = _get(f"/servers?search={urllib.parse.quote(query)}")
    servers = data.get("servers", [])
    hits = [_hit(s.get("server", s)) for s in servers]
    hits.sort(key=lambda h: not h.installable)
    return hits


def fetch_server(name: str) -> dict[str, Any]:
    """GET /v0.1/servers/{name}/versions/latest -> the server.json dict."""
    data = _get(f"/servers/{urllib.parse.quote(name, safe='')}/versions/latest")
    return data.get("server", data)


def package_to_entry(server: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Translate a registry server.json into an mcp.json entry.

    Returns (entry, warnings): entry is {command, args, env} ready to write;
    warnings names required env vars that are unset in the parent environment
    (they are written as "$VAR" references regardless — never values).

    Raises McpError when no package is stdio-installable.
    """
    packages = server.get("packages", [])
    pkg = next(
        (p for p in packages if p.get("runtimeHint") in _SUPPORTED_HINTS),
        None,
    )
    if pkg is None:
        raise McpError(
            f"'{server.get('name', '?')}' has no stdio-installable package "
            f"(bird supports npx/uvx launches only)"
        )
    hint = pkg["runtimeHint"]
    command = _SUPPORTED_HINTS[hint]
    args: list[str] = []
    for arg in pkg.get("runtimeArguments", []):
        args.append(str(arg.get("value", arg.get("name", ""))))
    identifier = pkg.get("identifier")
    if identifier:
        args.append(str(identifier))
    for arg in pkg.get("packageArguments", []):
        args.append(str(arg.get("value", arg.get("name", ""))))

    env: dict[str, str] = {}
    warnings: list[str] = []
    for var in pkg.get("environmentVariables", []):
        var_name = var.get("name")
        if not var_name:
            continue
        # a $VAR reference keeps the secret in the environment; the value is
        # never written into mcp.json
        env[var_name] = f"${var_name}"
        if var.get("isRequired") and var_name not in os.environ:
            warnings.append(
                f"required environment variable {var_name} is not set — "
                f"the server will likely fail until you export it"
            )
    entry: dict[str, Any] = {"command": command, "args": args}
    if env:
        entry["env"] = env
    return entry, warnings
