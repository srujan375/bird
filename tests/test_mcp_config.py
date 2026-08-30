"""Tests for mcp.json config: discovery precedence, $VAR expansion, errors."""

import json
from pathlib import Path

import pytest

from bird.mcp.config import McpError, load_mcp_servers, parse_servers


# --- helpers ---

def _write_config(root: Path, servers: dict) -> Path:
    d = root / ".bird"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "mcp.json"
    p.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return p


# --- loading & precedence ---

def test_no_config_files_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    assert load_mcp_servers(tmp_path) == []


def test_project_file_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    _write_config(tmp_path, {"alpha": {"command": "npx", "args": ["-y", "alpha-mcp"]}})
    specs = load_mcp_servers(tmp_path)
    assert len(specs) == 1
    s = specs[0]
    assert s.name == "alpha"
    assert s.command == "npx"
    assert s.args == ["-y", "alpha-mcp"]
    assert s.env == {}
    assert s.source == "project"


def test_user_file_loads_when_no_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_config(home, {"beta": {"command": "uvx", "args": ["beta-mcp"]}})
    specs = load_mcp_servers(tmp_path / "repo")
    assert len(specs) == 1
    assert specs[0].name == "beta"
    assert specs[0].source == "user"


def test_project_wins_on_name_collision(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_config(tmp_path, {"dup": {"command": "project-cmd"}})
    _write_config(home, {"dup": {"command": "user-cmd"}, "other": {"command": "uvx"}})
    specs = {s.name: s for s in load_mcp_servers(tmp_path)}
    assert specs["dup"].command == "project-cmd"
    assert specs["dup"].source == "project"
    assert specs["other"].source == "user"


# --- $VAR expansion ---

def test_env_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setenv("MY_API_KEY", "sekret")
    _write_config(tmp_path, {"s": {"command": "npx", "env": {"KEY": "$MY_API_KEY"}}})
    specs = load_mcp_servers(tmp_path)
    assert specs[0].env == {"KEY": "sekret"}


def test_unset_var_expands_to_empty(tmp_path, monkeypatch):
    """An unset var is empty at load time — the unset warning lives at write
    time (bird mcp add), not on every startup."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)
    _write_config(tmp_path, {"s": {"command": "npx", "env": {"KEY": "$DEFINITELY_UNSET_VAR"}}})
    specs = load_mcp_servers(tmp_path)
    assert specs[0].env == {"KEY": ""}


def test_expansion_embedded_in_value(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setenv("HOST", "example.com")
    _write_config(tmp_path, {"s": {"command": "npx", "env": {"URL": "https://$HOST/api"}}})
    specs = load_mcp_servers(tmp_path)
    assert specs[0].env == {"URL": "https://example.com/api"}


# --- corrupt files & bad shapes: loud errors, never silent ---

def test_corrupt_json_is_a_loud_error(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    d = tmp_path / ".bird"
    d.mkdir()
    (d / "mcp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(McpError, match=r"invalid JSON at line 1"):
        load_mcp_servers(tmp_path)


def test_error_names_the_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    d = tmp_path / ".bird"
    d.mkdir()
    p = d / "mcp.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(McpError, match="top level must be a JSON object") as e:
        load_mcp_servers(tmp_path)
    assert str(p) in str(e.value)


def test_missing_command_is_a_config_error(tmp_path):
    with pytest.raises(McpError, match="missing a 'command'"):
        parse_servers({"servers": {"s": {"args": []}}}, Path("x"), "project")


def test_non_dict_servers_is_a_config_error(tmp_path):
    with pytest.raises(McpError, match="'servers' must be an object"):
        parse_servers({"servers": ["npx"]}, Path("x"), "project")


def test_non_string_args_is_a_config_error(tmp_path):
    with pytest.raises(McpError, match="non-string-list 'args'"):
        parse_servers({"servers": {"s": {"command": "npx", "args": [1]}}}, Path("x"), "project")


def test_non_string_env_is_a_config_error(tmp_path):
    with pytest.raises(McpError, match="non-string-map 'env'"):
        parse_servers({"servers": {"s": {"command": "npx", "env": {"K": 1}}}}, Path("x"), "project")


def test_unknown_keys_are_ignored(tmp_path):
    """Forward-compat: a newer field must not break an older bird."""
    specs = parse_servers(
        {"servers": {"s": {"command": "npx", "transport": "stdio", "future": True}}},
        Path("x"), "project",
    )
    assert specs[0].command == "npx"
