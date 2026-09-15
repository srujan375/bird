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
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import McpError

REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0.1"
REGISTRY_TIMEOUT = 15.0
# 24-hour on-disk cache of fetched registry pages: the catalog stays
# browsable when the network is down, and the header shows the cache age.
CACHE_TTL = 24 * 60 * 60.0
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".bird", "cache", "mcp-registry")

# runtime hints bird can launch over stdio
_SUPPORTED_HINTS = {"npm": "npx", "pypi": "uvx"}
_STDIO_TRANSPORTS = {None, "", "stdio"}


def _launch_hint(pkg: dict[str, Any]) -> str | None:
    """'npm' / 'pypi' when bird can launch this package over stdio, else
    None. The registry's server.json rarely carries a runtimeHint any more
    — most entries are just {registryType: npm, identifier, transport:
    {type: stdio}} — so the registry type is the primary signal; an
    explicit hint (npx/uvx) still counts, and a non-stdio transport
    (streamable-http, sse) never does."""
    transport = pkg.get("transport") or {}
    if isinstance(transport, dict) and transport.get("type") not in _STDIO_TRANSPORTS:
        return None
    hint = pkg.get("runtimeHint")
    if hint in _SUPPORTED_HINTS:
        return hint
    for kind, cmd in _SUPPORTED_HINTS.items():
        if hint == cmd:
            return kind
    kind = pkg.get("registryType")
    return kind if kind in _SUPPORTED_HINTS else None

# registry reason strings rewritten for people — the raw "docker —
# unsupported" tells the user nothing about *why*; these do (prototype copy)
HUMAN_REASONS = {
    "docker": "needs Docker · bird launches local subprocesses only",
    "remote": "remote (SSE/HTTP) endpoint · bird only speaks stdio today",
}


def human_reason(reason: str) -> str:
    """Rewrite a registry reason string for people. 'docker — unsupported'
    becomes 'needs Docker · bird launches local subprocesses only'."""
    kind = reason.split(" — ")[0].strip().lower()
    return HUMAN_REASONS.get(kind, reason)


@dataclass(frozen=True)
class RegistryHit:
    name: str
    description: str
    version: str
    installable: bool  # has at least one stdio-capable package
    reason: str = ""  # why not installable ("remote — unsupported", "docker — unsupported")
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogEntry:
    """One row of the catalog: a registry hit flattened into exactly what
    the catalog view renders. `command`/`args` are the launch spec for
    display (never truncated — the view wraps); `env` is the ordered list of
    required variable NAMES (values are never read into the UI); `repo` is
    the repository link; `url` is the remote endpoint for unsupported
    remote-only servers (the manual mcp.json snippet)."""
    name: str
    description: str
    version: str
    installable: bool
    reason: str = ""          # raw registry reason
    human_reason: str = ""    # rewritten for people
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    repo: str = ""
    url: str = ""


def _cache_path(path: str) -> str:
    key = urllib.parse.quote(path, safe="")
    return os.path.join(CACHE_DIR, key + ".json")


def _cache_read(path: str) -> dict[str, Any] | None:
    """The cached page, or None when absent/corrupt. Age is checked by the
    caller (a stale page is still useful offline, so we return it regardless
    of TTL and let the caller decide)."""
    try:
        with open(_cache_path(path), encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict) or "data" not in blob:
        return None
    return blob


def _cache_write(path: str, data: dict[str, Any]) -> None:
    """Best-effort: a cache write failure must never break a successful
    fetch."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _cache_path(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "data": data}, f)
        os.replace(tmp, _cache_path(path))
    except OSError:
        pass


def cache_age_seconds(path: str) -> float | None:
    """Seconds since the page was fetched, or None when not cached."""
    blob = _cache_read(path)
    if blob is None:
        return None
    return max(0.0, time.time() - float(blob.get("fetched_at", 0)))


def _ssl_context() -> ssl.SSLContext:
    """A python.org macOS build ships no CA bundle until "Install
    Certificates" is run, and then every registry call dies with
    CERTIFICATE_VERIFY_FAILED. If the interpreter has no bundle but
    certifi is importable (it comes with the venv), use that."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        try:
            import certifi  # noqa: PLC0415

            ctx.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001 - no certifi: keep the default, error surfaces as before
            pass
    return ctx


def _get(path: str, use_cache: bool = True) -> dict[str, Any]:
    """GET a registry page. On network failure, fall back to the on-disk
    cache (any age — a stale list beats a blank wall exactly when the user
    is offline); with no cache either, McpError naming the registry."""
    url = f"{REGISTRY_BASE}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REGISTRY_TIMEOUT, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if use_cache:
            _cache_write(path, data)
        return data
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        if use_cache:
            cached = _cache_read(path)
            if cached is not None:
                return cached["data"]
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
        if _launch_hint(pkg) is not None:
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


def _repo_link(server: dict[str, Any]) -> str:
    """Repository link for display: the first http(s) repositoryUrl, else
    the first package repositoryUrl, else the package identifier's prefix
    (e.g. '@scope/name' -> 'npm: @scope/name')."""
    for key in ("repositoryUrl",):
        url = server.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return url
    for pkg in server.get("packages", []):
        url = pkg.get("repositoryUrl")
        if isinstance(url, str) and url.startswith("http"):
            return url
    for pkg in server.get("packages", []):
        ident = pkg.get("identifier")
        if isinstance(ident, str) and ident:
            return f"{pkg.get('registryType', '?')}: {ident}"
    return ""


def _display_command(server: dict[str, Any]) -> tuple[str, list[str]]:
    """The launch spec for display. Installable servers: the exact command
    package_to_entry would write. Unsupported ones: the registry's own launch
    spec (docker run … / the remote URL) so the detail view can show what
    bird *would have had* to run."""
    packages = server.get("packages", [])
    pkg = next((p for p in packages if _launch_hint(p) is not None), None)
    if pkg is not None:
        try:
            entry, _ = package_to_entry(server)
            return entry["command"], list(entry.get("args", []))
        except McpError:
            pass
    # unsupported: docker/oci packages show their docker command shape
    for p in packages:
        hint = p.get("runtimeHint") or ""
        ident = str(p.get("identifier", ""))
        if hint in ("docker", "oci") or p.get("registryType") in ("oci", "docker"):
            args = [str(a.get("value", a.get("name", ""))) for a in p.get("runtimeArguments", [])]
            return "docker", [*args, ident] if ident else args
    remotes = server.get("remotes", [])
    if remotes:
        url = remotes[0].get("url", "")
        return str(url), []
    return "", []


def hit_to_entry(hit: RegistryHit) -> CatalogEntry:
    """Flatten a registry hit into a catalog row (display-only; install goes
    through package_to_entry against the fetched server.json)."""
    server = hit.raw
    command, args = _display_command(server)
    env: list[str] = []
    for pkg in server.get("packages", []):
        if _launch_hint(pkg) is not None:
            for var in pkg.get("environmentVariables", []):
                name = var.get("name")
                if name and name not in env:
                    env.append(str(name))
    url = ""
    remotes = server.get("remotes", [])
    if remotes and isinstance(remotes[0].get("url"), str):
        url = remotes[0]["url"]
    return CatalogEntry(
        name=hit.name,
        description=hit.description,
        version=hit.version,
        installable=hit.installable,
        reason=hit.reason,
        human_reason=human_reason(hit.reason) if hit.reason else "",
        command=command,
        args=args,
        env=env,
        repo=_repo_link(server),
        url=url,
    )


# the registry lists every published version alphabetically, and its first
# pages are mostly remote-only servers — ask for latest versions and a big
# page so the browse view has installable rows on it
PAGE_LIMIT = 100
BROWSE_PATH = f"/servers?version=latest&limit={PAGE_LIMIT}"


def search_path(query: str) -> str:
    return f"/servers?search={urllib.parse.quote(query)}&version=latest&limit={PAGE_LIMIT}"


def catalog_page(query: str = "") -> tuple[list[CatalogEntry], dict[str, Any]]:
    """One page of the catalog: (entries, page info). `query` empty means
    browse (the registry's default listing). Raises McpError when the
    registry is unreachable AND nothing is cached."""
    path = search_path(query) if query else BROWSE_PATH
    data = _get(path)
    servers = data.get("servers", [])
    # one row per server: the registry lists every published version and
    # `version=latest` is best-effort, so drop repeats by name (first wins)
    seen: set[str] = set()
    hits = []
    for s in servers:
        h = _hit(s.get("server", s))
        if h.name in seen:
            continue
        seen.add(h.name)
        hits.append(h)
    hits.sort(key=lambda h: not h.installable)
    entries = [hit_to_entry(h) for h in hits]
    info = {
        # the registry reports only the page count, never a grand total —
        # pass a total through only when it really is one
        "total": data.get("total"),
        "next_cursor": data.get("nextCursor"),
        "cache_age": cache_age_seconds(path),
    }
    return entries, info


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
        (p for p in packages if _launch_hint(p) is not None),
        None,
    )
    if pkg is None:
        raise McpError(
            f"'{server.get('name', '?')}' has no stdio-installable package "
            f"(bird supports npx/uvx launches only)"
        )
    hint = _launch_hint(pkg)
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
