"""`bird paxel`: bird's sessions → the Claude Code layout Paxel ingests.

The load-bearing test here is `test_fix_format_pass_is_a_no_op`: the community
bridge's fix-format.py adds a top-level `type` (from `role`) and a
`message.role` (from `type`). If bird's staged lines already carry both, that
pass changes nothing — which is the whole contract, checked without a network
or a Paxel install. The transform is reimplemented below verbatim from
yc-paxel/scripts/fix-format.py so the assertion is against the real thing.

Everything else is synthetic sessions in tmp_path. The git tests run real
`git init` (no network) and skip if git is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import bird.cli as cli_mod
from bird.paxel import (
    DEFAULT_STAGE_DIR,
    STAGE_DIR_ENV,
    encode_dir,
    git_toplevel,
    map_messages,
    paxel_main,
    resolve_stage_dir,
    stage_sessions,
)

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git not on PATH")


# --- fixtures ---------------------------------------------------------------


def write_session(repo: Path, run_id: str, rows: list[dict], *, meta: dict | None = None) -> Path:
    """A synthetic session dir: messages.jsonl (+ optional session.json)."""
    run_dir = repo / ".bird" / "sessions" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "messages.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    if meta is not None:
        (run_dir / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_dir


def simple_rows() -> list[dict]:
    return [
        {"role": "system", "content": "you are bird"},
        {"role": "user", "content": "fix the login redirect"},
        {
            "role": "assistant",
            "content": "looking now",
            "tool_calls": [
                {"id": "call_1", "name": "grep", "arguments": {"pattern": "redirect"},
                 "arguments_json": '{"pattern": "redirect"}'},
            ],
        },
        {"role": "tool", "content": "src/auth.py:12: redirect", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "done"},
    ]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fix_format_transform(lines: list[dict]) -> list[dict]:
    """yc-paxel/scripts/fix-format.py, verbatim, as a pure function.

    Copied rather than imported: the point is to assert bird's output is
    already in the shape that pass produces, and a copy is the only way to
    check that without a Paxel checkout.
    """
    out = []
    for obj in lines:
        obj = json.loads(json.dumps(obj))  # don't mutate the caller's dicts
        if isinstance(obj, dict):
            if "type" not in obj and "role" in obj:
                obj["type"] = obj["role"]
            msg = obj.get("message")
            if isinstance(msg, dict) and "role" not in msg and obj.get("type") in ("user", "assistant"):
                msg["role"] = obj["type"]
        out.append(obj)
    return out


# --- the mapper -------------------------------------------------------------


def test_user_and_assistant_line_shapes():
    lines = map_messages(simple_rows())

    assert lines[0] == {"type": "user", "message": {"role": "user", "content": "fix the login redirect"}}
    assert lines[1]["type"] == "assistant"
    assert lines[1]["message"]["role"] == "assistant"
    assert lines[1]["message"]["content"] == [
        {"type": "text", "text": "looking now"},
        {"type": "tool_use", "id": "call_1", "name": "grep", "input": {"pattern": "redirect"}},
    ]
    assert lines[3] == {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}


def test_system_prompt_is_dropped():
    """The system prompt is bird's instructions plus the repo map — never a turn."""
    lines = map_messages(simple_rows())
    assert all(line["type"] != "system" for line in lines)
    assert "you are bird" not in json.dumps(lines)


def test_tool_result_lands_in_a_user_turn_with_matching_tool_use_id():
    lines = map_messages(simple_rows())

    result_turn = lines[2]
    assert result_turn["type"] == "user"
    assert result_turn["message"]["role"] == "user"
    assert result_turn["message"]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "src/auth.py:12: redirect"}
    ]
    # the pair resolves: the id on the result is the id on the tool_use
    assert result_turn["message"]["content"][0]["tool_use_id"] == lines[1]["message"]["content"][1]["id"]


def test_consecutive_tool_results_coalesce_into_one_user_turn():
    """bird writes one line per result; Claude Code writes one turn with
    several blocks. Two results must not become two user turns."""
    rows = [
        {"role": "user", "content": "check both"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "name": "read", "arguments": {"path": "a"}, "arguments_json": "{}"},
            {"id": "call_b", "name": "read", "arguments": {"path": "b"}, "arguments_json": "{}"},
        ]},
        {"role": "tool", "content": "A", "tool_call_id": "call_a"},
        {"role": "tool", "content": "B", "tool_call_id": "call_b"},
    ]
    lines = map_messages(rows)

    assert [line["type"] for line in lines] == ["user", "assistant", "user"]
    blocks = lines[2]["message"]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["call_a", "call_b"]
    assert [b["content"] for b in blocks] == ["A", "B"]


def test_assistant_with_only_tool_calls_has_a_block_list_not_null():
    rows = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_x", "name": "bash", "arguments": {"command": "ls"}, "arguments_json": "{}"},
        ]},
    ]
    lines = map_messages(rows)

    content = lines[1]["message"]["content"]
    assert content is not None
    assert content == [{"type": "tool_use", "id": "call_x", "name": "bash", "input": {"command": "ls"}}]


def test_thinking_is_dropped():
    """Display-only: it round-trips through persistence but never to a model."""
    rows = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "thinking": "SECRET REASONING"},
    ]
    lines = map_messages(rows)
    assert "SECRET REASONING" not in json.dumps(lines)


def test_malformed_tool_arguments_fall_back_to_the_raw_json():
    """`arguments` is None when the model emitted invalid JSON; the raw string
    is still on the call, so the input block is not silently emptied."""
    rows = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "name": "edit", "arguments": None, "arguments_json": '{"path": "x.py"}'},
        ]},
    ]
    lines = map_messages(rows)
    assert lines[1]["message"]["content"][0]["input"] == {"path": "x.py"}


def test_unparseable_arguments_become_an_empty_object():
    rows = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "name": "edit", "arguments": None, "arguments_json": "{not json"},
        ]},
    ]
    lines = map_messages(rows)
    assert lines[1]["message"]["content"][0]["input"] == {}


def test_fix_format_pass_is_a_no_op():
    """The contract: bird's lines already carry `type` and a nested
    `message.role`, so the community bridge's fix-format.py changes nothing."""
    lines = map_messages(simple_rows())
    assert fix_format_transform(lines) == lines


def test_fix_format_pass_is_a_no_op_on_a_tool_heavy_session():
    rows = [
        {"role": "user", "content": "run the tests"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "name": "bash", "arguments": {"command": "pytest -q"}, "arguments_json": "{}"},
        ]},
        {"role": "tool", "content": "1 failed", "tool_call_id": "c1"},
        {"role": "assistant", "content": "fixing"},
    ]
    lines = map_messages(rows)
    assert fix_format_transform(lines) == lines


# --- stage dir resolution ---------------------------------------------------


def test_stage_dir_precedence_flag_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv(STAGE_DIR_ENV, str(tmp_path / "from-env"))
    assert resolve_stage_dir(str(tmp_path / "from-flag")) == tmp_path / "from-flag"


def test_stage_dir_env_beats_default(monkeypatch, tmp_path):
    monkeypatch.setenv(STAGE_DIR_ENV, str(tmp_path / "from-env"))
    assert resolve_stage_dir(None) == tmp_path / "from-env"


def test_stage_dir_defaults_under_home(monkeypatch):
    monkeypatch.delenv(STAGE_DIR_ENV, raising=False)
    assert resolve_stage_dir(None) == DEFAULT_STAGE_DIR
    assert str(DEFAULT_STAGE_DIR).startswith(str(Path.home()))


def test_encode_dir_matches_the_community_bridge():
    assert encode_dir(Path("/Users/x/Workspace/Personal/bird")) == "-Users-x-Workspace-Personal-bird"


# --- originalPath: the combined-report mechanism ----------------------------


@needs_git
def test_original_path_is_the_git_toplevel(tmp_path):
    """The addendum's whole point: bird's index must declare the same
    originalPath Claude Code declares, or Paxel buckets them as two projects."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-q", str(repo)], check=True)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    write_session(nested, "20260101-000000-aaaaaa", simple_rows())

    rc = stage_sessions(nested, tmp_path / "stage")

    assert rc == 0
    index = json.loads((tmp_path / "stage" / encode_dir(repo) / "sessions-index.json").read_text())
    assert index["version"] == 1
    assert index["entries"][0]["originalPath"] == str(repo)
    # and NOT bird's own session path, which is what would split the project
    assert ".bird" not in index["entries"][0]["originalPath"]


@needs_git
def test_git_toplevel_resolves_from_a_subdirectory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([GIT, "init", "-q", str(repo)], check=True)
    nested = repo / "deep" / "er"
    nested.mkdir(parents=True)

    top, is_git = git_toplevel(nested)

    assert is_git is True
    assert top == repo


def test_non_git_repo_falls_back_to_the_project_root(tmp_path, capsys):
    """A non-git directory is a normal place to run bird — it must not crash,
    and the output has to say the fallback happened."""
    repo = tmp_path / "plain"
    repo.mkdir()
    write_session(repo, "20260101-000000-bbbbbb", simple_rows())

    rc = stage_sessions(repo, tmp_path / "stage")

    assert rc == 0
    index = json.loads((tmp_path / "stage" / encode_dir(repo) / "sessions-index.json").read_text())
    assert index["entries"][0]["originalPath"] == str(repo)
    out = capsys.readouterr().out
    assert "not a git repo" in out


def test_git_toplevel_returns_false_when_git_is_missing(tmp_path, monkeypatch):
    """A missing git binary reads as 'no toplevel', never an exception."""
    def boom(*a, **k):
        raise FileNotFoundError("no git")

    monkeypatch.setattr(subprocess, "run", boom)
    top, is_git = git_toplevel(tmp_path)
    assert (top, is_git) == (tmp_path, False)


# --- staging ----------------------------------------------------------------


def test_staged_layout_and_manifest(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows(), meta={"model": "ollama:x"})
    write_session(repo, "20260102-000000-bbbbbb", [
        {"role": "user", "content": "second session"},
        {"role": "assistant", "content": "ok"},
    ])

    rc = stage_sessions(repo, tmp_path / "stage")

    assert rc == 0
    project = tmp_path / "stage" / encode_dir(repo)
    assert sorted(p.name for p in project.iterdir()) == [
        "20260101-000000-aaaaaa.jsonl",
        "20260102-000000-bbbbbb.jsonl",
        "sessions-index.json",
    ]
    index = json.loads((project / "sessions-index.json").read_text())
    assert [e["sessionId"] for e in index["entries"]] == [
        "20260101-000000-aaaaaa",
        "20260102-000000-bbbbbb",
    ]
    # the audit surface: one line per session, then the path and a total
    out = capsys.readouterr().out
    assert "20260101-000000-aaaaaa" in out
    assert "fix the login redirect" in out
    assert "staged 2 session(s)" in out
    assert str(project) in out


def test_privacy_line_says_nothing_has_left_the_machine(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())

    stage_sessions(repo, tmp_path / "stage")

    out = capsys.readouterr().out
    assert "nothing has left this machine yet" in out
    assert "Claude/GPT" in out
    assert "TRANSCRIPT_DIR" in out


def test_reexport_replaces_birds_project_dir(tmp_path):
    """A stale entry pointing at a transcript that is gone is worse than
    re-running the command."""
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())
    project = tmp_path / "stage" / encode_dir(repo)
    project.mkdir(parents=True)
    (project / "stale.jsonl").write_text("{}\n", encoding="utf-8")

    stage_sessions(repo, tmp_path / "stage")

    assert not (project / "stale.jsonl").exists()
    assert (project / "20260101-000000-aaaaaa.jsonl").is_file()


def test_reexport_does_not_touch_other_agents_staged_dirs(tmp_path):
    """The staging root is shared with the Cursor/Hermes exporters — wiping it
    would delete work that is not bird's to touch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())
    other = tmp_path / "stage" / "-Users-x-other-project"
    other.mkdir(parents=True)
    (other / "cursor.jsonl").write_text("{}\n", encoding="utf-8")

    stage_sessions(repo, tmp_path / "stage")

    assert (other / "cursor.jsonl").is_file()


def test_session_filter_selects_one_run_id(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())
    write_session(repo, "20260102-000000-bbbbbb", [
        {"role": "user", "content": "other"}, {"role": "assistant", "content": "ok"},
    ])

    rc = stage_sessions(repo, tmp_path / "stage", sessions=["20260101-000000-aaaaaa"])

    assert rc == 0
    project = tmp_path / "stage" / encode_dir(repo)
    assert (project / "20260101-000000-aaaaaa.jsonl").is_file()
    assert not (project / "20260102-000000-bbbbbb.jsonl").exists()
    index = json.loads((project / "sessions-index.json").read_text())
    assert [e["sessionId"] for e in index["entries"]] == ["20260101-000000-aaaaaa"]


def test_session_filter_accepts_a_run_id_prefix(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())

    rc = stage_sessions(repo, tmp_path / "stage", sessions=["20260101-000000"])

    assert rc == 0
    assert (tmp_path / "stage" / encode_dir(repo) / "20260101-000000-aaaaaa.jsonl").is_file()


def test_legacy_session_exports_degraded_instead_of_vanishing(tmp_path):
    """No messages.jsonl, only events.jsonl: load_messages() reconstructs the
    turns, so the session lands as plain text rather than disappearing."""
    repo = tmp_path / "repo"
    run_dir = repo / ".bird" / "sessions" / "20260101-000000-cccccc"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "run_start", "data": {"task": "legacy task"}}) + "\n"
        + json.dumps({"type": "assistant", "data": {"content": "legacy answer"}}) + "\n",
        encoding="utf-8",
    )

    rc = stage_sessions(repo, tmp_path / "stage")

    assert rc == 0
    lines = read_jsonl(tmp_path / "stage" / encode_dir(repo) / "20260101-000000-cccccc.jsonl")
    assert lines[0] == {"type": "user", "message": {"role": "user", "content": "legacy task"}}
    assert lines[1]["message"]["content"] == [{"type": "text", "text": "legacy answer"}]


def test_session_with_no_transcript_is_skipped_not_invented(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())
    (repo / ".bird" / "sessions" / "20260102-000000-empty").mkdir(parents=True)

    rc = stage_sessions(repo, tmp_path / "stage")

    assert rc == 0
    index = json.loads((tmp_path / "stage" / encode_dir(repo) / "sessions-index.json").read_text())
    assert [e["sessionId"] for e in index["entries"]] == ["20260101-000000-aaaaaa"]


def test_no_sessions_at_all_exits_nonzero(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()

    rc = stage_sessions(repo, tmp_path / "stage")

    assert rc == 1
    assert "no sessions found" in capsys.readouterr().err


def test_unmatched_session_filter_exits_nonzero(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())

    rc = stage_sessions(repo, tmp_path / "stage", sessions=["nope"])

    assert rc == 1
    assert "no session matched" in capsys.readouterr().err


def test_unwritable_stage_dir_fails_loudly_with_the_path(tmp_path, capsys):
    """A half-written project dir is regenerable, so this fails with the path
    and the errno rather than doing a temp-dir dance."""
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    rc = stage_sessions(repo, blocker / "stage")

    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot write staging dir" in err
    assert "errno" in err


def test_stage_dir_outside_home_warns_but_stages(tmp_path, capsys, monkeypatch):
    """Paxel's uploader refuses a TRANSCRIPT_DIR outside $HOME, but enforcing
    Paxel's constraint is not bird's job — warn, do not refuse."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())

    rc = stage_sessions(repo, tmp_path / "elsewhere")

    assert rc == 0
    assert "outside $HOME" in capsys.readouterr().err


# --- CLI wiring -------------------------------------------------------------


def test_cli_paxel_subcommand_stages(tmp_path, monkeypatch, capsys):
    """The subparser and its dispatch, end to end through cli.main()."""
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())
    stage = tmp_path / "stage"

    rc = cli_mod.main(["paxel", "--repo", str(repo), "--stage-dir", str(stage)])

    assert rc == 0
    assert (stage / encode_dir(repo) / "sessions-index.json").is_file()
    assert "staged 1 session(s)" in capsys.readouterr().out


def test_cli_paxel_honours_the_env_var(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())
    monkeypatch.setenv(STAGE_DIR_ENV, str(tmp_path / "from-env"))

    rc = cli_mod.main(["paxel", "--repo", str(repo)])

    assert rc == 0
    assert (tmp_path / "from-env" / encode_dir(repo) / "sessions-index.json").is_file()


def test_cli_paxel_help_documents_the_combined_report(tmp_path, capsys):
    """The addendum's invocation has to be discoverable from the command."""
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["paxel", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--all-agents" in out
    assert "git toplevel" in out


def test_paxel_main_reads_args_off_the_namespace(tmp_path, monkeypatch):
    """paxel_main is the seam cli.py calls; it must not need a live parser."""
    import argparse

    repo = tmp_path / "repo"
    repo.mkdir()
    write_session(repo, "20260101-000000-aaaaaa", simple_rows())
    args = argparse.Namespace(stage_dir=str(tmp_path / "stage"), session=None)

    rc = paxel_main(args, repo)

    assert rc == 0
    assert (tmp_path / "stage" / encode_dir(repo) / "sessions-index.json").is_file()
