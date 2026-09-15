"""Tests for the MCP server catalog: discover.py's cached registry surface,
the catalog view state machine, and the management write path."""

import json
import time
from pathlib import Path

import pytest

from bird.mcp import catalog as cat
from bird.mcp.catalog import (
    ARM_DELAY_SECONDS,
    CatalogEntry,
    CatalogState,
    ConnectedInfo,
    handle_key,
    rows,
    wrap_command,
)
from bird.mcp.config import McpError
from bird.mcp import discover
from bird.mcp import management as mgmt


# ------------------------------------------------------------------ helpers


def _server(name, hint="npm", ident=None, env=None, remotes=None, desc="a server"):
    """A registry server.json shape."""
    pkg = {
        "registryType": "npm" if hint == "npm" else "pypi" if hint == "pypi" else hint,
        "identifier": ident or name,
        "runtimeHint": hint,
        "runtimeArguments": [{"value": "-y"}],
        "packageArguments": [],
        "environmentVariables": [{"name": v, "isRequired": True} for v in env or []],
        "repositoryUrl": f"https://github.com/example/{name}",
    }
    s = {"name": name, "description": desc, "version": "1.0.0", "packages": [pkg]}
    if remotes:
        s["remotes"] = remotes
    return s


def _page(*servers, total=None):
    return {"servers": [{"server": s} for s in servers],
            "total": total if total is not None else len(servers)}


def _entry(name="alpha", installable=True, **kw):
    return CatalogEntry(name=name, description=kw.get("description", "does things"),
                        version=kw.get("version", "1.0.0"),
                        installable=installable,
                        reason=kw.get("reason", ""),
                        human_reason=kw.get("human_reason", ""),
                        command=kw.get("command", "npx"),
                        args=kw.get("args", ["-y", name]),
                        env=kw.get("env", []),
                        repo=kw.get("repo", ""),
                        url=kw.get("url", ""))


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    monkeypatch.setattr(discover, "CACHE_DIR", str(d))
    return d


@pytest.fixture
def fake_registry(monkeypatch, cache_dir):
    """A fake registry network layer: urlopen returns canned pages (or
    fails), so discover's real _get — including its stale-cache fallback —
    runs against the tmp cache dir."""
    import urllib.error

    state = {"pages": {}, "fail": False, "calls": []}

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        state["calls"].append(req.full_url)
        if state["fail"]:
            raise urllib.error.URLError("connection timed out")
        path = req.full_url.replace(discover.REGISTRY_BASE, "")
        if path not in state["pages"]:
            raise urllib.error.URLError(f"404: {path}")
        return FakeResp(state["pages"][path])

    monkeypatch.setattr(discover.urllib.request, "urlopen", fake_urlopen)
    return state


# ------------------------------------------------- discover: cache & catalog


def test_cache_write_then_read_roundtrip(fake_registry, cache_dir):
    discover._cache_write("/servers", {"servers": []})
    blob = discover._cache_read("/servers")
    assert blob is not None and blob["data"] == {"servers": []}
    assert "fetched_at" in blob


def test_cache_read_missing_or_corrupt_is_none(cache_dir):
    assert discover._cache_read("/nope") is None
    p = discover._cache_path("/servers")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text("{not json", encoding="utf-8")
    assert discover._cache_read("/servers") is None
    # a non-dict blob is also rejected
    Path(p).write_text('["x"]', encoding="utf-8")
    assert discover._cache_read("/servers") is None


def test_cache_age_seconds(fake_registry, cache_dir):
    assert discover.cache_age_seconds("/servers") is None
    discover._cache_write("/servers", {"servers": []})
    age = discover.cache_age_seconds("/servers")
    assert age is not None and age < 5
    # backdate the entry: age grows accordingly
    p = discover._cache_path("/servers")
    blob = json.loads(Path(p).read_text())
    blob["fetched_at"] = time.time() - 3600
    Path(p).write_text(json.dumps(blob), encoding="utf-8")
    assert 3500 < discover.cache_age_seconds("/servers") < 3700


def test_stale_cache_fallback_on_network_failure(fake_registry, cache_dir):
    fake_registry["pages"][discover.BROWSE_PATH] = _page(_server("alpha"))
    entries, info = discover.catalog_page()
    assert [e.name for e in entries] == ["alpha"]
    # network dies — the stale cache keeps the catalog browsable
    fake_registry["fail"] = True
    entries, info = discover.catalog_page()
    assert [e.name for e in entries] == ["alpha"]
    assert info["cache_age"] is not None


def test_unreachable_and_uncached_is_mcperror(fake_registry, cache_dir):
    fake_registry["fail"] = True
    with pytest.raises(McpError, match="cannot reach the MCP registry"):
        discover.catalog_page()


def test_catalog_page_groups_installable_first(fake_registry):
    fake_registry["pages"][discover.BROWSE_PATH] = _page(
        _server("docker-one", hint="docker"),
        _server("good-one"),
        _server("remote-one", hint="docker", remotes=[{"url": "https://x/sse"}]),
    )
    entries, info = discover.catalog_page()
    assert entries[0].installable and entries[0].name == "good-one"
    assert not entries[-1].installable
    assert info["total"] == 3


def test_catalog_page_query_uses_search_path(fake_registry):
    fake_registry["pages"][discover.search_path("sql")] = _page(_server("sqlite"))
    entries, _ = discover.catalog_page("sql")
    assert [e.name for e in entries] == ["sqlite"]
    assert any("search=sql" in c for c in fake_registry["calls"])


def test_hit_to_entry_flattens_display_fields(fake_registry):
    fake_registry["pages"][discover.BROWSE_PATH] = _page(
        _server("github", env=["GITHUB_TOKEN"], ident="@scope/github"))
    entries, _ = discover.catalog_page()
    e = entries[0]
    assert e.command == "npx"
    assert e.args == ["-y", "@scope/github"]
    assert e.env == ["GITHUB_TOKEN"]
    assert e.repo == "https://github.com/example/github"
    assert e.installable is True


def test_hit_to_entry_unsupported_remote(fake_registry):
    remote = {"name": "linear", "description": "issues", "version": "1.0.0",
              "packages": [], "remotes": [{"url": "https://mcp.linear.app/sse"}]}
    fake_registry["pages"][discover.BROWSE_PATH] = _page(remote)
    entries, _ = discover.catalog_page()
    e = entries[0]
    assert e.installable is False
    assert "remote" in e.reason
    assert e.url == "https://mcp.linear.app/sse"
    assert e.human_reason  # rewritten for people


def test_human_reason_rewrites_known_kinds():
    assert "Docker" in discover.human_reason("docker — unsupported")
    assert "stdio" in discover.human_reason("remote — unsupported")
    # unknown reasons pass through untouched
    assert discover.human_reason("weird — unsupported") == "weird — unsupported"


def test_display_command_installable_matches_package_to_entry(fake_registry):
    s = _server("alpha", ident="alpha-pkg")
    cmd, args = discover._display_command(s)
    entry, _ = discover.package_to_entry(s)
    assert cmd == entry["command"] and args == entry["args"]


def test_display_command_docker_shape():
    s = _server("graf", hint="docker")
    s["packages"][0]["runtimeArguments"] = [{"value": "run"}, {"value": "--rm"}]
    cmd, args = discover._display_command(s)
    assert cmd == "docker"
    assert args[:2] == ["run", "--rm"]


def test_display_command_remote_url():
    s = {"name": "lin", "packages": [], "remotes": [{"url": "https://mcp.lin.app/sse"}]}
    cmd, args = discover._display_command(s)
    assert cmd == "https://mcp.lin.app/sse" and args == []


# ------------------------------------------------- catalog state machine


def _state(entries=None, connected=None, **kw):
    return CatalogState(entries=entries if entries is not None else [],
                        connected=connected or [], **kw)


def test_grouping_connected_available_unsupported():
    s = _state(
        entries=[_entry("a"), _entry("b", installable=False, reason="docker — unsupported")],
        connected=[ConnectedInfo("c", True, 12)],
    )
    r = rows(s)
    heads = [x.title for x in r if x.kind == "head"]
    assert heads == ["Connected", "Available", "Not installable on this machine"]
    names = [x.entry.name for x in r if x.kind == "item"]
    assert names == ["c", "a", "b"]


def test_connected_server_missing_from_registry_still_shown():
    s = _state(entries=[_entry("a")], connected=[ConnectedInfo("local-only", True, 3)])
    names = [x.entry.name for x in rows(s) if x.kind == "item"]
    assert "local-only" in names


def test_filtering_collapses_groups_into_ranked_list():
    s = _state(entries=[_entry("sentry", description="issues and events"),
                         _entry("sqlite", description="sql database"),
                         _entry("github", description="pull requests")])
    handle_key(s, "s")
    handle_key(s, "q")  # letters are search input while the query is live
    r = rows(s)
    assert all(x.kind == "item" for x in r)
    assert {x.entry.name for x in r} == {"sqlite"}


def test_esc_clears_query_before_closing():
    s = _state(entries=[_entry("a")])
    handle_key(s, "a")
    assert s.query == "a"
    assert handle_key(s, "esc") == ""  # cleared, not closed
    assert s.query == ""
    assert handle_key(s, "esc") == "close"


def test_backspace_edits_query():
    s = _state(entries=[_entry("a")])
    handle_key(s, "x")
    handle_key(s, "y")
    handle_key(s, "backspace")
    assert s.query == "x"


def test_cursor_movement_clamped():
    s = _state(entries=[_entry("a"), _entry("b"), _entry("c")])
    assert handle_key(s, "down") == ""
    assert s.cursor == 1
    handle_key(s, "pgdown")
    assert s.cursor == 2
    handle_key(s, "down")
    assert s.cursor == 2  # clamped at the end
    handle_key(s, "up")
    assert s.cursor == 1
    handle_key(s, "pgup")
    assert s.cursor == 0
    handle_key(s, "up")
    assert s.cursor == 0


def test_enter_opens_detail_and_esc_returns():
    s = _state(entries=[_entry("a")])
    handle_key(s, "enter")
    assert s.view == "detail" and s.target.name == "a"
    handle_key(s, "esc")
    assert s.view == "browse"


def test_i_opens_confirm_only_for_installable():
    s = _state(entries=[_entry("bad", installable=False, reason="docker — unsupported")])
    handle_key(s, "i")
    assert s.view == "browse"  # unsupported: no confirm card
    s2 = _state(entries=[_entry("good")])
    handle_key(s2, "i")
    assert s2.view == "confirm" and s2.target.name == "good"


def test_confirm_requires_armed_y_with_delay():
    """The literal 'y' — twice, with the arming delay between: a held key
    can't fall through into an install."""
    s = _state(entries=[_entry("a")])
    handle_key(s, "i")
    t = 1000.0
    assert handle_key(s, "y", now=t) == ""  # first y arms, never installs
    assert s.armed is True
    assert handle_key(s, "y", now=t + 0.01) == ""  # inside the delay: no
    assert s.view == "confirm"
    assert handle_key(s, "y", now=t + ARM_DELAY_SECONDS + 0.1) == "install"
    assert s.view == "result"


def test_confirm_n_and_esc_cancel():
    s = _state(entries=[_entry("a")])
    handle_key(s, "i")
    handle_key(s, "y", now=1.0)
    assert handle_key(s, "n", now=2.0) == ""
    assert s.view == "detail"
    handle_key(s, "i")
    handle_key(s, "esc")
    assert s.view == "detail"


def test_result_view_keys():
    s = _state(entries=[_entry("a")])
    s.target = s.entries[0]
    s.result = {"ok": False, "why": "boom", "fix": ["retry"], "log": ["line"]}
    s.view = "result"
    assert handle_key(s, "r") == "retry"
    assert handle_key(s, "x") == "remove"
    assert handle_key(s, "enter") == ""
    assert s.view == "browse"


def test_degraded_unreachable_retry_key():
    s = _state(degraded="unreachable")
    assert handle_key(s, "r") == "retry"
    assert handle_key(s, "down") == ""  # nothing to move in a blank wall


def test_state_from_fetch_modes():
    ok = cat.state_from_fetch([_entry("a")], {"total": 1}, [])
    assert ok.degraded == "ok"
    stale = cat.state_from_fetch([_entry("a")], {"cache_age": 3600}, [], error="down")
    assert stale.degraded == "stale"
    unreachable = cat.state_from_fetch([], {}, [], error="down")
    assert unreachable.degraded == "unreachable"
    empty = cat.state_from_fetch([], {"total": 0}, [])
    assert empty.degraded == "empty"


def test_wrap_command_continuation_and_hanging_indent():
    lines = wrap_command("npx", ["-y", "@sentry/mcp-server@0.12.0",
                                 "--access-token=$SENTRY_AUTH_TOKEN",
                                 "--organization-slug=$SENTRY_ORG"], 40)
    assert lines[0].endswith(" \\")
    assert all(l.startswith("    ") or l is lines[0] for l in lines[1:])
    joined = " ".join(l.rstrip(" \\").strip() for l in lines)
    assert "$SENTRY_AUTH_TOKEN" in joined  # never truncated
    assert "--organization-slug=$SENTRY_ORG" in joined


def test_wrap_command_short_stays_one_line():
    assert wrap_command("npx", ["-y", "alpha"], 60) == ["npx -y alpha"]


def test_env_lines_names_only_never_values():
    e = _entry("a", env=["TOKEN"])
    lines = cat.env_lines(e, lambda v: True)
    assert lines == ["● $TOKEN  set in your environment · value never shown"]
    lines = cat.env_lines(e, lambda v: False)
    assert "not set" in lines[0]
    assert cat.env_lines(_entry("b"), lambda v: True) == ["none"]


def test_render_browse_shows_groups_and_cache_age():
    s = _state(entries=[_entry("a"), _entry("b", installable=False,
                                           reason="docker — unsupported",
                                           human_reason="needs Docker")],
               connected=[ConnectedInfo("c", True, 12)])
    s.page_info = {"total": 2}
    out = "\n".join(cat.render(s))
    assert "Connected · 1" in out
    assert "Available · 1" in out
    assert "Not installable" in out
    assert "12 tools" in out


def test_render_browse_stale_header_shows_cache_age():
    s = _state(entries=[_entry("a")], degraded="stale")
    s.page_info = {"cache_age": 3 * 3600}
    out = "\n".join(cat.render(s))
    assert "offline" in out and "cached 3h ago" in out


def test_render_browse_unreachable_mentions_configured_servers():
    s = _state(connected=[ConnectedInfo("c", True, 2)], degraded="unreachable")
    out = "\n".join(cat.render(s))
    assert "Couldn't reach the registry" in out
    assert "unaffected" in out


def test_render_browse_no_matches():
    s = _state(entries=[_entry("a")])
    handle_key(s, "z")
    out = "\n".join(cat.render(s))
    assert "No servers match" in out


def test_render_detail_unsupported_block_and_snippet():
    s = _state(entries=[])
    s.target = _entry("mcp/grafana", installable=False,
                      reason="docker — unsupported",
                      human_reason="needs Docker · bird launches local subprocesses only",
                      command="docker", args=["run", "mcp/grafana"],
                      url="https://g/sse", repo="github.com/grafana/mcp-grafana")
    s.view = "detail"
    out = "\n".join(cat.render(s))
    assert "Why bird can't install this" in out
    assert "needs Docker" in out
    assert '"url": "https://g/sse"' in out  # manual mcp.json snippet


def test_render_confirm_states_auto_approve_does_not_apply():
    s = _state(entries=[_entry("a", env=["TOKEN"])])
    s.target = s.entries[0]
    s.view = "confirm"
    out = "\n".join(cat.render(s))
    assert "auto-approve does not apply" in out
    assert "$TOKEN" in out


def test_render_result_failure_kept_in_mcpjson():
    s = _state(entries=[_entry("a")])
    s.target = s.entries[0]
    s.view = "result"
    s.result = {"ok": False, "why": "$TOKEN is not set", "fix": ["export TOKEN=…"],
                "log": ["Error: TOKEN required"]}
    out = "\n".join(cat.render(s))
    assert "didn't connect" in out
    assert "Kept in mcp.json" in out
    assert "Error: TOKEN required" in out


def test_render_result_success_tool_count_and_samples():
    s = _state(entries=[_entry("a")])
    s.target = s.entries[0]
    s.view = "result"
    s.result = {"ok": True, "ms": 340, "tools": 12,
                "sample": ["list", "read", "search", "+9"]}
    out = "\n".join(cat.render(s))
    assert "connected" in out and "12 tools" in out and "+9" in out


# ------------------------------------------------------- management writes


def _repo(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    return tmp_path


def test_install_refuses_without_confirm(tmp_path, monkeypatch, fake_registry):
    root = _repo(tmp_path, monkeypatch)
    fake_registry["pages"]["/servers/alpha/versions/latest"] = {"server": _server("alpha")}
    with pytest.raises(McpError, match="without explicit confirmation"):
        mgmt.install_from_registry("alpha", root)


def test_install_writes_var_references_not_values(tmp_path, monkeypatch,
                                                   fake_registry, monkeypatch_env):
    root = _repo(tmp_path, monkeypatch)
    fake_registry["pages"]["/servers/alpha/versions/latest"] = {
        "server": _server("alpha", env=["SECRET_KEY"])}
    entry, warnings = mgmt.install_from_registry("alpha", root, confirm=True)
    assert entry["env"] == {"SECRET_KEY": "$SECRET_KEY"}
    assert warnings  # the var is unset in the test env
    on_disk = json.loads((root / ".bird" / "mcp.json").read_text())
    assert on_disk["servers"]["alpha"]["env"]["SECRET_KEY"] == "$SECRET_KEY"


@pytest.fixture
def monkeypatch_env(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    return monkeypatch


def test_install_duplicate_name_is_mcperror(tmp_path, monkeypatch, fake_registry):
    root = _repo(tmp_path, monkeypatch)
    d = root / ".bird"
    d.mkdir()
    (d / "mcp.json").write_text(json.dumps({"servers": {"alpha": {"command": "npx"}}}))
    fake_registry["pages"]["/servers/alpha/versions/latest"] = {"server": _server("alpha")}
    with pytest.raises(McpError, match="already exists"):
        mgmt.install_from_registry("alpha", root, confirm=True)


def test_remove_server(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    d = root / ".bird"
    d.mkdir()
    (d / "mcp.json").write_text(json.dumps({"servers": {"a": {"command": "npx"},
                                                        "b": {"command": "uvx"}}}))
    mgmt.remove_server("a", root)
    on_disk = json.loads((d / "mcp.json").read_text())
    assert list(on_disk["servers"]) == ["b"]


def test_remove_missing_is_mcperror(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    with pytest.raises(McpError, match="no server"):
        mgmt.remove_server("ghost", root)


def test_corrupt_file_is_never_silently_overwritten(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    d = root / ".bird"
    d.mkdir()
    (d / "mcp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(McpError, match="invalid JSON"):
        mgmt.remove_server("a", root)
    assert (d / "mcp.json").read_text() == "{not json"  # untouched


def test_install_on_corrupt_file_is_loud(tmp_path, monkeypatch, fake_registry):
    root = _repo(tmp_path, monkeypatch)
    d = root / ".bird"
    d.mkdir()
    (d / "mcp.json").write_text("[1]", encoding="utf-8")
    fake_registry["pages"]["/servers/alpha/versions/latest"] = {"server": _server("alpha")}
    with pytest.raises(McpError, match="top level must be a JSON object"):
        mgmt.install_from_registry("alpha", root, confirm=True)


def test_cmd_add_from_registry_requires_yes(tmp_path, monkeypatch, fake_registry):
    """The CLI confirm flow: anything but y/yes leaves the file alone."""
    from types import SimpleNamespace

    root = _repo(tmp_path, monkeypatch)
    fake_registry["pages"]["/servers/alpha/versions/latest"] = {"server": _server("alpha")}
    args = SimpleNamespace(mcp_command="add", name="alpha", command=None,
                           args=None, env=None, scope="project",
                           from_registry=True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert mgmt.cmd_add(args, root) == 1
    assert not (root / ".bird" / "mcp.json").exists()
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert mgmt.cmd_add(args, root) == 0
    assert "alpha" in json.loads((root / ".bird" / "mcp.json").read_text())["servers"]


def test_cmd_list_on_corrupt_file_shows_error(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    d = root / ".bird"
    d.mkdir()
    (d / "mcp.json").write_text("{oops", encoding="utf-8")
    from types import SimpleNamespace
    rc = mgmt.cmd_list(SimpleNamespace(), root)
    assert rc == 2
    assert "invalid JSON" in capsys.readouterr().err


def test_cmd_get_roundtrip(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    d = root / ".bird"
    d.mkdir()
    (d / "mcp.json").write_text(json.dumps(
        {"servers": {"alpha": {"command": "npx", "args": ["-y", "alpha"]}}}))
    from types import SimpleNamespace
    # connection check would spawn a process; stub test path via disabled? No —
    # cmd_get starts the server. Use a command that fails fast instead.
    (d / "mcp.json").write_text(json.dumps(
        {"servers": {"alpha": {"command": "definitely-not-a-command-xyz"}}}))
    rc = mgmt.cmd_get(SimpleNamespace(name="alpha"), root)
    assert rc == 1  # configured, but the launch fails — reported, not crashed
    out = capsys.readouterr().out
    assert "alpha" in out