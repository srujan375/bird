from mha.llm.types import ToolCall, ToolSpec
from mha.llm.validate import validate_tool_call

READ = ToolSpec(
    name="read",
    description="Read a file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)
SPECS = {"read": READ}


def test_valid_call_returns_none():
    call = ToolCall.from_raw("c1", "read", '{"path": "a.py"}')
    assert validate_tool_call(call, SPECS) is None


def test_unknown_tool():
    call = ToolCall.from_raw("c1", "grep", '{"pattern": "x"}')
    err = validate_tool_call(call, SPECS)
    assert "Unknown tool 'grep'" in err
    assert "read" in err


def test_invalid_json():
    call = ToolCall.from_raw("c1", "read", '{"path": ')
    err = validate_tool_call(call, SPECS)
    assert "invalid JSON" in err
    assert "path (string, required)" in err


def test_missing_required():
    call = ToolCall.from_raw("c1", "read", '{"offset": 3}')
    err = validate_tool_call(call, SPECS)
    assert "Missing: ['path']" in err
    assert "Provided: ['offset']" in err


def test_wrong_type():
    call = ToolCall.from_raw("c1", "read", '{"path": 42}')
    err = validate_tool_call(call, SPECS)
    assert "path" in err
    assert "42" in err


def test_unexpected_property():
    call = ToolCall.from_raw("c1", "read", '{"path": "a.py", "bogus": 1}')
    err = validate_tool_call(call, SPECS)
    assert err is not None and "bogus" in err
