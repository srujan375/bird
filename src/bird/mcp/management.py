"""`bird mcp` — the management surface for mcp.json.

add/list/get/remove/search write and read the same config the loader reads —
no second source of truth. Writes are parse-before-write with an atomic
rename: a corrupt existing file is a loud error naming path + line, never
silently overwritten; `bird mcp list` on a corrupt file shows the error, not
an empty list.

`add --from-registry` resolves a name against the official registry,
translates the package metadata into an entry, shows it, and asks for
confirmation before writing — a registry entry is arbitrary code, so install
is always a conscious yes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import McpClient
from .config import McpError, McpServerSpec, load_mcp_servers, parse_servers
from .discover import fetch_server, package_to_entry, search_registry


def _config_path(repo_root: Path, scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".bird" / "mcp.json"
    return repo_root / ".bird" / "mcp.json"


def _read_file(path: Path) -> dict[str, Any]:
    """The raw document, or {} when the file doesn't exist. A file that
    exists but won't parse is a loud error — never silently overwritten."""
    if not path.is_file():
        return {"servers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise McpError(f"{path}: invalid JSON at line {e.lineno}: {e.msg}") from None
    if not isinstance(data, dict):
        raise McpError(f"{path}: top level must be a JSON object")
    data.setdefault("servers", {})
    return data


def _write_file(path: Path, data: dict[str, Any]) -> None:
    """Parse-before-write + atomic rename: the file on disk is either the old
    document or the new one, never a half-written one."""
    rendered = json.dumps(data, indent=2) + "\n"
    json.loads(rendered)  # belt-and-braces: never write unparseable JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    os.replace(tmp, path)


def _entry_to_spec(name: str, entry: dict[str, Any], source: str) -> McpServerSpec:
    return parse_servers({"servers": {name: entry}}, Path("<entry>"), source)[0]


# ------------------------------------------------------------------- commands


def cmd_add(args, repo_root: Path) -> int:
    path = _config_path(repo_root, args.scope)
    data = _read_file(path)
    if args.name in data["servers"]:
        print(f"error: '{args.name}' already exists in {path} "
              f"(remove it first, or pick another name)", file=sys.stderr)
        return 2

    if args.from_registry:
        server = fetch_server(args.name)
        entry, warnings = package_to_entry(server)
        print(f"registry entry for '{args.name}':")
        print(json.dumps({args.name: entry}, indent=2))
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        # installing a registry entry is running arbitrary code — always a
        # conscious yes, even with the entry shown
        try:
            answer = input("install this server? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in ("y", "yes"):
            print("not installed")
            return 1
    else:
        if not args.command:
            print("error: add needs --command (or --from-registry)", file=sys.stderr)
            return 2
        entry = {"command": args.command}
        if args.args:
            entry["args"] = args.args
        env: dict[str, str] = {}
        for pair in args.env or []:
            if "=" not in pair:
                print(f"error: --env expects K=V, got '{pair}'", file=sys.stderr)
                return 2
            k, v = pair.split("=", 1)
            env[k] = v
        if env:
            entry["env"] = env

    data["servers"][args.name] = entry
    _write_file(path, data)
    print(f"added '{args.name}' to {path}")
    return 0


def cmd_list(args, repo_root: Path) -> int:
    try:
        specs = load_mcp_servers(repo_root)
    except McpError as e:
        # a corrupt file shows the error, not an empty list
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not specs:
        print("no MCP servers configured (.bird/mcp.json, ~/.bird/mcp.json)")
        return 0
    for s in specs:
        cmdline = " ".join([s.command, *s.args])
        marker = "  [disabled]" if s.disabled else ""
        print(f"  {s.name:20s} {cmdline}  [{s.source}]{marker}")
    return 0


def cmd_get(args, repo_root: Path) -> int:
    specs = {s.name: s for s in load_mcp_servers(repo_root)}
    spec = specs.get(args.name)
    if spec is None:
        print(f"error: no MCP server named '{args.name}'", file=sys.stderr)
        return 2
    entry: dict[str, Any] = {"command": spec.command, "args": spec.args}
    if spec.env:
        entry["env"] = spec.env
    print(json.dumps({spec.name: entry}, indent=2))
    print(f"source: {spec.source}")
    if spec.disabled:
        print("disabled: true (kept in the config, not launched)")
        return 0
    # connection check: actually start it and list tools
    client = McpClient(spec)
    try:
        tools = client.start()
    except McpError as e:
        print(f"connection: FAILED — {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(f"connection: ok — {len(tools)} tool(s)")
    for t in tools:
        print(f"  {t.get('name', '?')}")
    return 0


def cmd_remove(args, repo_root: Path) -> int:
    path = _config_path(repo_root, args.scope)
    data = _read_file(path)
    if args.name not in data["servers"]:
        print(f"error: no server '{args.name}' in {path}", file=sys.stderr)
        return 2
    del data["servers"][args.name]
    _write_file(path, data)
    print(f"removed '{args.name}' from {path}")
    return 0


def cmd_search(args, repo_root: Path) -> int:
    hits = search_registry(args.query)
    if not hits:
        print(f"no registry matches for '{args.query}'")
        return 0
    for h in hits:
        if h.installable:
            print(f"  {h.name}  (v{h.version})")
            if h.description:
                print(f"      {h.description}")
        else:
            print(f"  {h.name}  — {h.reason}")
    return 0


def mcp_main(args, repo_root: Path) -> int:
    """Dispatch for the `bird mcp` subcommand tree."""
    try:
        if args.mcp_command == "add":
            return cmd_add(args, repo_root)
        if args.mcp_command == "list":
            return cmd_list(args, repo_root)
        if args.mcp_command == "get":
            return cmd_get(args, repo_root)
        if args.mcp_command == "remove":
            return cmd_remove(args, repo_root)
        if args.mcp_command == "search":
            return cmd_search(args, repo_root)
    except McpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("usage: bird mcp add|list|get|remove|search", file=sys.stderr)
    return 2
