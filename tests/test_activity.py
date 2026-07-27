import json

from ox.activity import MAX_LABEL_LEN, format_activity


def assistant_event(*calls):
    return {"turn": 1, "content": "", "tool_calls": [
        {"name": name, "arguments_json": json.dumps(args) if isinstance(args, dict) else args}
        for name, args in calls
    ]}


def test_tool_call_headers():
    lines = format_activity("assistant", assistant_event(
        ("bash", {"command": "git status"}), ("kg_query", {"question": "where is auth?"}),
    ))
    assert lines == ["  › bash git status", "  › kg_query where is auth?"]


def test_long_detail_truncated_and_flattened():
    cmd = "grep -r foo\n" + "x" * 200
    (line,) = format_activity("assistant", assistant_event(("bash", {"command": cmd})))
    assert "\n" not in line
    assert line.endswith("…")
    assert len(line) <= MAX_LABEL_LEN + len("  › …")


def test_invalid_arguments_json_falls_back_to_name():
    assert format_activity("assistant", assistant_event(("bash", "{not json"))) == ["  › bash"]


def test_tool_result_only_on_error():
    assert format_activity("tool_result", {"name": "read", "is_error": False}) == []
    assert format_activity("tool_result", {"name": "read", "is_error": True}) == ["  ✕ read failed"]


def test_other_events_silent():
    assert format_activity("run_start", {"task": "x"}) == []
