from dataclasses import replace
from pathlib import Path

from bird.context.store import MAX_FINDINGS, ContextStore
from bird.tools.base import ToolContext, ToolResult


def repo(tmp_path: Path, **files: str) -> Path:
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    return tmp_path


def test_finding_renders_with_its_source_harness(tmp_path):
    root = repo(tmp_path, **{"dom.py": "def op_move(new_parent): ..."})
    s = ContextStore()
    s.note(root, "dom.py", "code", "op_move reads `new_parent`, not `parent`")
    out = s.render(root)
    assert "dom.py [code] — op_move reads `new_parent`, not `parent`" in out


def test_a_finding_dies_when_its_file_changes(tmp_path):
    root = repo(tmp_path, **{"dom.py": "v1"})
    s = ContextStore()
    s.note(root, "dom.py", "code", "takes two args")
    assert "takes two args" in s.render(root)

    (root / "dom.py").write_text("v2")  # the note may no longer be true
    out = s.render(root) or ""
    assert "takes two args" not in out


def test_a_finding_about_a_deleted_file_drops_out(tmp_path):
    root = repo(tmp_path, **{"gone.py": "x"})
    s = ContextStore()
    s.note(root, "gone.py", "lead", "holds the seam")
    (root / "gone.py").unlink()
    assert "holds the seam" not in (s.render(root) or "")


def test_a_newer_note_replaces_the_old_one_for_that_file(tmp_path):
    root = repo(tmp_path, **{"a.py": "x"})
    s = ContextStore()
    s.note(root, "a.py", "lead", "first take")
    s.note(root, "a.py", "code", "corrected take")
    out = s.render(root)
    assert "first take" not in out
    assert "corrected take" in out


def test_render_is_none_when_nothing_is_known(tmp_path):
    assert ContextStore().render(tmp_path) is None


def test_findings_are_capped(tmp_path):
    root = repo(tmp_path, **{f"f{i}.py": "x" for i in range(MAX_FINDINGS + 10)})
    s = ContextStore()
    for i in range(MAX_FINDINGS + 10):
        s.note(root, f"f{i}.py", "code", f"note {i}")
    assert len(s.findings) == MAX_FINDINGS
    assert "note 0" not in s.render(root)  # oldest fell off
    assert f"note {MAX_FINDINGS + 9}" in s.render(root)


def test_render_still_demands_a_real_read_before_editing(tmp_path):
    # The store saves the SEARCH, never the read: old_text has to be copied
    # verbatim from real read output. If this instruction is ever dropped, a
    # model can reconstruct old_text from a note and silently patch the wrong
    # text — so the wording is pinned here on purpose.
    root = repo(tmp_path, **{"a.py": "x"})
    s = ContextStore()
    s.note(root, "a.py", "code", "anything")
    out = s.render(root).lower()
    assert "verbatim" in out
    assert "must still read any file you intend to edit" in out


def test_ruled_out_items_survive_and_dedupe(tmp_path):
    s = ContextStore()
    s.rule_out("ToolContext has no `design` field")
    s.rule_out("ToolContext has no `design` field")
    out = s.render(tmp_path)
    assert out.count("no `design` field") == 1


def test_seen_index_is_separate_from_findings(tmp_path):
    root = repo(tmp_path, **{"a.py": "x", "b.py": "y"})
    s = ContextStore()
    s.note(root, "a.py", "code", "the interesting one")
    s.mark_seen(["a.py", "b.py"], "lead")
    out = s.render(root)
    # a.py has a finding, so it is not repeated in the bare index
    assert "no finding recorded: b.py" in out


def test_reads_land_in_the_seen_index_without_model_cooperation(tmp_path):
    s = ContextStore()
    ctx = ToolContext(repo_root=tmp_path, store=s, harness="lead")
    ctx.note_tool_result("read", ToolResult(output="...", details={"path": "a.py"}))
    ctx.note_tool_result(
        "read", ToolResult(output="...", details={"paths": ["b.py", "c.py"]})
    )
    assert s.seen == {"a.py": "lead", "b.py": "lead", "c.py": "lead"}


def test_a_failed_read_is_not_recorded_as_seen(tmp_path):
    s = ContextStore()
    ctx = ToolContext(repo_root=tmp_path, store=s)
    ctx.note_tool_result(
        "read", ToolResult(output="nope", details={"path": "a.py"}, is_error=True)
    )
    assert s.seen == {}


def test_the_dispatch_fork_shares_the_parent_store(tmp_path):
    # The whole seam fix rests on replace() being shallow: the code fork must
    # write into the SAME store the lead reads back.
    s = ContextStore()
    parent = ToolContext(repo_root=tmp_path, store=s, harness="lead")
    child = replace(parent, plan=None, arch=None, last_bundle=None, pending_input=None)
    assert child.store is parent.store

    (tmp_path / "a.py").write_text("x")
    child.store.note(tmp_path, "a.py", "code", "learned in the fork")
    assert "learned in the fork" in parent.store.render(tmp_path)
