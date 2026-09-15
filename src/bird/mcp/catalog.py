"""The MCP server catalog view: the TUI store's state machine.

Pure, testable logic — no I/O in the state functions. The REPL (or any other
host) feeds keys in via `handle_key` and renders the lines `render` returns;
the registry fetch, the confirmation broker, and the install write all live
in the caller. This mirrors the approved prototype (mcp-catalog-tui.html):

  browse    one surface, no mode switch: the query line is always live
            (fzf grammar). Empty query = browse, grouped into
            Connected → Available → Not installable. Any keystroke filters;
            while filtering the groups collapse into a single ranked list
            but each row keeps its status glyph.
  detail    full launch command (never truncated — wrapped at argument
            boundaries with "\\" continuation and a 4-col hanging indent),
            env vars as $VAR with set/unset dots (values never read), repo
            link, and for unsupported servers a "Why bird can't install
            this" block plus a manual mcp.json snippet.
  confirm   the literal key 'y' (not Enter, not space), with a short arming
            delay so a held key can't fall through; the card states that
            auto-approve does not apply.
  result    success: connected + tool count + sample names; failure: why +
            fix suggestions + last server log lines + "kept in mcp.json —
            will retry on next launch", with r retry / x remove.

Degraded states: unreachable-no-cache (error + retry hint + "your
configured servers are unaffected"), stale cache (browsable, install
disabled, header shows cache age), slow network (skeleton rows, connected
group from local config), empty catalog, no matches.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .discover import CatalogEntry

# how long the confirm card must have been on screen before 'y' counts —
# a held key (or a keypress arriving from the same stroke that opened the
# card) must not fall through into an install
ARM_DELAY_SECONDS = 0.35

VIEWS = ("browse", "detail", "confirm", "result")


# ------------------------------------------------------------- pure helpers


def wrap_command(command: str, args: list[str], width: int) -> list[str]:
    """The launch command wrapped at argument boundaries with a trailing
    "\\" continuation and a 4-column hanging indent — the same shape the
    user would paste into a shell. Never truncated: a long argument gets
    its own line even when it alone exceeds `width`."""
    words = [command, *args]
    lines: list[str] = []
    cur = ""
    for word in words:
        piece = (" " if cur else "") + word
        if cur and len(cur) + len(piece) > width - 2:
            lines.append(cur + " \\")
            cur = "    " + word
        else:
            cur += piece
    lines.append(cur)
    return lines


def env_lines(entry: CatalogEntry, env_set: Callable[[str], bool]) -> list[str]:
    """Required env vars by NAME only, as $VAR with a set/unset dot. Values
    are never read into the view — `env_set` answers set/unset, nothing
    more."""
    if not entry.env:
        return ["none"]
    out = []
    for var in entry.env:
        if env_set(var):
            out.append(f"● ${var}  set in your environment · value never shown")
        else:
            out.append(f"○ ${var}  not set · server will start but likely fail to connect")
    return out


def human_cache_age(seconds: float) -> str:
    """'cached 3h ago' / 'cached 2d ago' for the degraded header."""
    if seconds < 90:
        return "cached just now"
    if seconds < 90 * 60:
        return f"cached {int(seconds // 60)}m ago"
    if seconds < 36 * 60 * 60:
        return f"cached {int(seconds // 3600)}h ago"
    return f"cached {int(seconds // 86400)}d ago"


# ------------------------------------------------------------------- state


@dataclass
class ConnectedInfo:
    """What the session knows about one configured server (from the live
    mcp_clients + mcp.json) — the Connected group's data."""
    name: str
    connected: bool
    tools: int = 0
    error: str = ""


@dataclass
class CatalogState:
    """Everything the view needs. `entries`/`page_info` come from
    discover.catalog_page; `connected` from the session; `degraded` is one
    of 'ok' | 'unreachable' | 'stale' | 'slow' | 'empty'."""
    entries: list[CatalogEntry] = field(default_factory=list)
    page_info: dict[str, Any] = field(default_factory=dict)
    connected: list[ConnectedInfo] = field(default_factory=list)
    degraded: str = "ok"
    view: str = "browse"
    query: str = ""
    cursor: int = 0
    target: CatalogEntry | None = None
    # confirm-card arming
    armed: bool = False
    armed_at: float = 0.0
    # the result card (set by the host after install/remove)
    result: dict[str, Any] | None = None
    width: int = 100
    env_set: Callable[[str], bool] = lambda v: False


@dataclass
class Row:
    kind: str  # "head" | "item"
    title: str = ""
    n: int = 0
    entry: CatalogEntry | None = None
    connected: ConnectedInfo | None = None


def _entry_matches(entry: CatalogEntry, q: str) -> bool:
    hay = f"{entry.name} {entry.description}".lower()
    return q in hay


def visible_entries(state: CatalogState) -> list[CatalogEntry]:
    q = state.query.strip().lower()
    if not q:
        return state.entries
    return [e for e in state.entries if _entry_matches(e, q)]


def rows(state: CatalogState) -> list[Row]:
    """The browse rows. Empty query: grouped Connected → Available → Not
    installable (Connected comes from the session, not the registry — it
    stays visible even when the registry is unreachable). While filtering:
    a single flat ranked list, each row keeping its status glyph."""
    out: list[Row] = []
    if state.query.strip():
        for e in visible_entries(state):
            out.append(Row(kind="item", entry=e, connected=_conn_for(state, e.name)))
        return out
    connected_names = {c.name for c in state.connected}
    connected = [Row(kind="item", entry=e, connected=_conn_for(state, e.name))
                 for e in state.entries if e.name in connected_names]
    # configured servers missing from the registry page still belong in the
    # Connected group — they're local facts, not registry facts
    seen = {r.entry.name for r in connected}
    for c in state.connected:
        if c.name not in seen:
            out_stub = CatalogEntry(name=c.name, description="", version="",
                                    installable=False)
            out.append(Row(kind="item", entry=out_stub, connected=c))
    if connected or out:
        out.append(Row(kind="head", title="Connected", n=len(connected) + len(out)))
        out.extend(connected)
    available = [e for e in state.entries
                 if e.name not in connected_names and e.installable]
    if available:
        out.append(Row(kind="head", title="Available", n=len(available)))
        out.extend(Row(kind="item", entry=e) for e in available)
    unsupported = [e for e in state.entries
                   if e.name not in connected_names and not e.installable]
    if unsupported:
        out.append(Row(kind="head", title="Not installable on this machine",
                       n=len(unsupported)))
        out.extend(Row(kind="item", entry=e) for e in unsupported)
    return out


def _conn_for(state: CatalogState, name: str) -> ConnectedInfo | None:
    for c in state.connected:
        if c.name == name:
            return c
    return None


def items(state: CatalogState) -> list[Row]:
    return [r for r in rows(state) if r.kind == "item"]


def current(state: CatalogState) -> Row | None:
    its = items(state)
    if 0 <= state.cursor < len(its):
        return its[state.cursor]
    return None


# ------------------------------------------------------------- key handling


def handle_key(state: CatalogState, key: str, now: float | None = None) -> str:
    """Feed one key. Returns an action for the host to perform:
      'install'  — the user pressed y on an armed confirm card
      'open'    — open the target's repository link
      'retry'   — refetch the catalog
      'remove'  — remove the target server from mcp.json
      'close'   — leave the catalog entirely
      ''        — nothing (internal state change only)
    Pure: no I/O, no clock reads (pass `now` for testability)."""
    now = time.time() if now is None else now
    view = state.view

    if view == "browse":
        return _key_browse(state, key)
    if view == "detail":
        return _key_detail(state, key)
    if view == "confirm":
        return _key_confirm(state, key, now)
    if view == "result":
        return _key_result(state, key)
    return ""


def _key_browse(state: CatalogState, key: str) -> str:
    if state.degraded in ("unreachable", "empty"):
        if key == "r":
            return "retry"
        return ""
    n = len(items(state))
    if key == "down" or key == "j":
        state.cursor = min(n - 1, state.cursor + 1)
    elif key == "up" or key == "k":
        state.cursor = max(0, state.cursor - 1)
    elif key == "pgdown":
        state.cursor = min(n - 1, state.cursor + 10)
    elif key == "pgup":
        state.cursor = max(0, state.cursor - 10)
    elif key == "enter":
        cur = current(state)
        if cur is not None:
            state.target = cur.entry
            state.view = "detail"
    elif key == "esc":
        # esc clears the query before it closes the catalog
        if state.query:
            state.query = ""
            state.cursor = 0
        else:
            return "close"
    elif key == "backspace":
        state.query = state.query[:-1]
        state.cursor = 0
    elif key == "q" and not state.query:
        return "close"
    elif len(key) == 1:
        # single-key actions only when the query is empty; otherwise letters
        # are search input
        if not state.query and key == "i":
            cur = current(state)
            if cur is not None and cur.entry is not None and cur.entry.installable:
                state.target = cur.entry
                state.view = "confirm"
                state.armed = False
                state.armed_at = now_time()
        else:
            state.query += key
            state.cursor = 0
    return ""


def now_time() -> float:
    return time.time()


def _key_detail(state: CatalogState, key: str) -> str:
    if key == "esc":
        state.view = "browse"
    elif key == "i":
        if state.target is not None and state.target.installable:
            state.view = "confirm"
            state.armed = False
            state.armed_at = now_time()
    elif key == "o":
        return "open"
    elif key == "q":
        return "close"
    return ""


def _key_confirm(state: CatalogState, key: str, now: float) -> str:
    if key == "y":
        # arming delay: a held key can't fall through into an install
        if not state.armed:
            state.armed = True
            state.armed_at = now
            return ""
        if now - state.armed_at < ARM_DELAY_SECONDS:
            return ""
        state.view = "result"
        return "install"
    if key in ("n", "esc"):
        state.view = "detail"
    elif key == "q":
        return "close"
    return ""


def _key_result(state: CatalogState, key: str) -> str:
    if key == "enter" or key == "esc":
        state.view = "browse"
    elif key == "r":
        return "retry"
    elif key == "x":
        return "remove"
    elif key == "q":
        return "close"
    return ""


# --------------------------------------------------------------- rendering


def _ellip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut) + "…"


def _status(state: CatalogState, row: Row) -> str:
    if row.connected is not None:
        if row.connected.connected:
            return f"{row.connected.tools} tools"
        return "not connected"
    e = row.entry
    if e is None:
        return ""
    if not e.installable:
        kind = e.reason.split(" — ")[0].strip().lower()
        if kind == "docker" or kind.startswith("oci"):
            return "needs docker"
        if kind == "remote":
            return "remote only"
        return "unsupported"
    if e.env:
        unset = [v for v in e.env if not state.env_set(v)]
        if unset:
            return f"needs ${unset[0]}"
    return f"v{e.version}" if e.version else ""


def _glyph(row: Row) -> str:
    if row.connected is not None:
        return "✓" if row.connected.connected else "✗"
    if row.entry is not None and not row.entry.installable:
        return "–"
    return " "


def render(state: CatalogState) -> list[str]:
    """The current view as plain text lines (the host prints them; colour
    is the host's business)."""
    if state.view == "browse":
        return _render_browse(state)
    if state.view == "detail":
        return _render_detail(state)
    if state.view == "confirm":
        return _render_confirm(state)
    if state.view == "result":
        return _render_result(state)
    return []


def _render_browse(state: CatalogState) -> list[str]:
    W = state.width
    L: list[str] = []
    age = state.page_info.get("cache_age")
    if state.degraded == "stale" and age is not None:
        note = f"offline · {human_cache_age(age)}"
    elif state.degraded == "slow":
        note = "fetching…"
    else:
        note = "registry.modelcontextprotocol.io"
    L.append(f" MCP  Server catalog  {note}")
    L.append("")
    L.append("─" * W)
    hint = "" if state.query else "type to search · esc to clear"
    L.append(f"  > {state.query}{'' if state.query else ' '}{hint}")
    L.append("─" * W)

    if state.degraded == "unreachable":
        L.append("")
        L.append("  ✗ Couldn't reach the registry")
        L.append("    registry.modelcontextprotocol.io · nothing cached yet, so")
        L.append("    there is nothing to browse offline.")
        L.append("")
        L.append("     r  retry     c  show connected servers     q  close")
        L.append("")
        L.append(f"  Your {len(state.connected)} configured server(s) are unaffected — "
                 f"the catalog is only for discovery.")
        return _foot(state, L, "")

    if state.degraded == "slow":
        L.append(f"  Connected · {len(state.connected)}")
        for c in state.connected:
            mark = f"{c.tools} tools" if c.connected else "not connected"
            L.append(f"    {'✓' if c.connected else '✗'} {c.name}  {mark}")
        L.append("")
        L.append("  Available")
        for i in range(6):
            L.append(f"    {'▆' * (28 + (i * 7) % 20)}   {'▆' * (36 + (i * 11) % 24)}")
        L.append("")
        L.append("    Loading page 1…  the list stays usable — connected servers are local.")
        return _foot(state, L, "")

    R = rows(state)
    if not R:
        L.append("")
        if state.degraded == "empty" or not state.query:
            L.append("  The registry returned no servers.")
            L.append("    This usually means a registry-side outage. Your configured servers are unaffected.")
            L.append("")
            L.append("     r  retry     q  close")
        else:
            L.append(f"  No servers match “{state.query}”")
            L.append("    Search covers name and description. Try a vendor name (“sentry”) or a capability (“sql”).")
            L.append("")
            L.append("     esc  clear search     q  close")
        return _foot(state, L, "")

    its = items(state)
    idx = {id(r): i for i, r in enumerate(its)}
    max_rows = 22
    cur_line = next((i for i, r in enumerate(R)
                     if r.kind == "item" and idx.get(id(r)) == state.cursor), 0)
    scroll = max(0, min(cur_line, max(0, len(R) - max_rows)))
    for r in R[scroll: scroll + max_rows]:
        if r.kind == "head":
            L.append("")
            L.append(f"  {r.title} · {r.n}")
            continue
        sel = idx.get(id(r)) == state.cursor
        name = r.entry.name if r.entry else "?"
        desc = r.entry.description if r.entry else ""
        desc_w = max(24, W - 42 - 26)
        line = (f"  {'▸' if sel else ' '} {_glyph(r)} "
                f"{name:<42} {_ellip(desc, desc_w):<{desc_w}} {_status(state, r)}")
        L.append(line.rstrip())
    if scroll + max_rows < len(R):
        L.append(f"    ↓ {len(R) - scroll - max_rows} more on this page")
    total = state.page_info.get("total")
    if not state.query and isinstance(total, int) and total > len(its):
        L.append(f"    ↓ end of page · {total} total in the registry")
    pos = f"{state.cursor + 1}/{len(its)}" + (" matches" if state.query else " shown")
    return _foot(state, L, pos)


def _foot(state: CatalogState, L: list[str], pos: str) -> list[str]:
    while len(L) < 24:
        L.append("")
    L.append("├" + "─" * max(0, state.width - 2) + "┤")
    cur = current(state)
    act = ""
    if cur is not None and cur.entry is not None:
        if cur.connected is not None:
            act = " i  reconnect   x  remove  "
        elif cur.entry.installable:
            act = " i  install  "
        else:
            act = " ⏎  why?  "
    keys = f" ↑↓  move   ⏎  open  {act} esc  back   q  close"
    if pos:
        keys = keys.ljust(max(0, state.width - len(pos) - 2)) + pos
    L.append(keys)
    return L


def _render_detail(state: CatalogState) -> list[str]:
    e = state.target
    if e is None:
        return ["  (no server selected)"]
    W = state.width
    L = [f" MCP  catalog › {e.name}  v{e.version}" if e.version
         else f" MCP  catalog › {e.name}"]
    L.append("")
    L.append(f"  {e.description}")
    L.append("")
    conn = _conn_for(state, e.name)
    if conn is not None:
        if conn.connected:
            L.append(f"  ✓ connected · {conn.tools} tools exposed")
        else:
            why = f" · {conn.error}" if conn.error else ""
            L.append(f"  ✗ installed but not connected{why}")
    elif not e.installable:
        L.append("  – Why bird can't install this")
        L.append(f"    {e.human_reason or e.reason}")
        L.append(f"    registry says: “{e.reason}”")
    else:
        L.append("  not installed")
    L.append("")
    label = "Launch command" if e.installable else "Registry launch spec (not runnable by bird)"
    L.append(f"  {label}")
    for line in wrap_command(e.command, e.args, W - 8):
        L.append(f"    {line}")
    L.append("")
    L.append("  Required environment")
    for line in env_lines(e, state.env_set):
        L.append(f"    {line}")
    L.append("")
    L.append("  Repository")
    L.append(f"    {e.repo}  (o to open)" if e.repo else "    (none listed)")
    if not e.installable:
        L.append("")
        L.append("  Run it yourself · add to mcp.json when bird supports this transport:")
        short = e.name.rsplit("/", 1)[-1]
        if e.url:
            L.append(f'    "{short}": {{ "url": "{e.url}" }}')
        elif e.command:
            L.append(f'    "{short}": {{ "command": "{e.command}", "args": {e.args!r} }}')
    while len(L) < 24:
        L.append("")
    L.append("├" + "─" * max(0, W - 2) + "┤")
    if conn is not None:
        act = " i  reconnect   x  remove"
    elif e.installable:
        act = " i  install"
    else:
        act = " o  open repository"
    L.append(f"{act}   esc  back to catalog")
    return L


def _render_confirm(state: CatalogState) -> list[str]:
    e = state.target
    if e is None:
        return ["  (no server selected)"]
    W = min(84, state.width)
    L = [f" MCP  catalog › {e.name} › confirm", ""]
    L.append(f"  Install {e.name} v{e.version}".rstrip())
    L.append("  This runs third-party code on this machine as your user.")
    L.append("")
    L.append("  bird will run")
    for line in wrap_command(e.command, e.args, W - 8):
        L.append(f"  {line}")
    L.append("")
    L.append("  and pass these from your environment at launch")
    for line in env_lines(e, state.env_set):
        L.append(f"  {line}")
    L.append("")
    L.append("  written to  .bird/mcp.json (project scope)")
    L.append("")
    y = "  [ y  install ]" if state.armed else "   y  install"
    L.append(f"{y}   n  cancel    auto-approve does not apply here · press y explicitly")
    L.append("")
    L.append("  Environment values are expanded by the server process; bird never reads or stores them.")
    while len(L) < 24:
        L.append("")
    L.append("├" + "─" * max(0, state.width - 2) + "┤")
    L.append(" y  install   n / esc  cancel")
    return L


def _render_result(state: CatalogState) -> list[str]:
    e = state.target
    r = state.result or {}
    ok = bool(r.get("ok"))
    L = [f" MCP  catalog › {e.name if e else '?'} › "
         f"{'connected' if ok else 'connection failed'}", ""]
    if ok:
        L.append(f"  ✓ {e.name if e else '?'} connected"
                 + (f"  in {r['ms']} ms" if r.get("ms") is not None else ""))
        tools = r.get("tools", 0)
        sample = r.get("sample") or []
        L.append(f"    exposes {tools} tools · {'  '.join(str(t) for t in sample)}")
        L.append("")
        L.append("  Tools are available to the agent from your next message.")
    else:
        L.append(f"  ✗ {e.name if e else '?'} installed, but didn't connect")
        L.append(f"    {r.get('why', 'unknown error')}")
        L.append("")
        for fix in r.get("fix") or []:
            L.append(f"    → {fix}")
        log = r.get("log") or []
        if log:
            L.append("")
            L.append("    last lines from the server:")
            for line in log:
                L.append(f"    {line}")
        L.append("")
        L.append("    Kept in mcp.json — it will retry on next launch.")
    while len(L) < 24:
        L.append("")
    L.append("├" + "─" * max(0, state.width - 2) + "┤")
    if ok:
        L.append(" ⏎  back to catalog   q  close")
    else:
        L.append(" r  retry   x  remove   ⏎  back to catalog")
    return L


# ------------------------------------------------------- degraded builders


def state_from_fetch(entries: list[CatalogEntry],
                     page_info: dict[str, Any],
                     connected: list[ConnectedInfo],
                     error: str | None = None) -> CatalogState:
    """Build a CatalogState from one catalog_page() attempt (or its
    failure). Decides the degraded mode:

      error + no entries        -> 'unreachable' (blank wall + retry hint)
      error + entries (cache)   -> 'stale' (browsable, install disabled by
                                   the host while degraded == 'stale')
      no error + no entries     -> 'empty'
      no error + entries        -> 'ok'
    """
    if error is not None:
        degraded = "stale" if entries else "unreachable"
    elif not entries and not connected:
        degraded = "empty"
    else:
        degraded = "ok"
    return CatalogState(entries=entries, page_info=page_info,
                        connected=connected, degraded=degraded)