import json

from mha.llm.types import LLMResponse, Message, ToolCall, ToolSpec, Usage


def test_toolspec_to_openai():
    spec = ToolSpec(
        name="read",
        description="Read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    wire = spec.to_openai()
    assert wire["type"] == "function"
    assert wire["function"]["name"] == "read"
    assert wire["function"]["parameters"]["required"] == ["path"]


def test_toolcall_from_raw_valid_json():
    tc = ToolCall.from_raw("call_1", "read", '{"path": "a.py"}')
    assert tc.arguments == {"path": "a.py"}
    assert tc.arguments_json == '{"path": "a.py"}'


def test_toolcall_from_raw_invalid_json_preserves_raw():
    tc = ToolCall.from_raw("call_1", "read", '{"path": ')
    assert tc.arguments is None
    assert tc.arguments_json == '{"path": '


def test_toolcall_from_raw_non_object_json():
    tc = ToolCall.from_raw("call_1", "read", '["not", "a", "dict"]')
    assert tc.arguments is None


def test_message_roundtrip_dict():
    msg = Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="c1", name="bash", arguments={"cmd": "ls"}, arguments_json='{"cmd": "ls"}')],
    )
    restored = Message.from_dict(msg.to_dict())
    assert restored.role == "assistant"
    assert restored.tool_calls[0].name == "bash"
    assert restored.tool_calls[0].arguments == {"cmd": "ls"}


def test_message_to_openai_tool_result():
    msg = Message(role="tool", content="ok", tool_call_id="c1")
    wire = msg.to_openai()
    assert wire == {"role": "tool", "content": "ok", "tool_call_id": "c1"}


def test_message_to_openai_assistant_tool_calls_drops_null_content():
    msg = Message(role="assistant", tool_calls=[ToolCall(id="c1", name="read", arguments={}, arguments_json="{}")])
    wire = msg.to_openai()
    assert "content" not in wire
    assert json.loads(wire["tool_calls"][0]["function"]["arguments"]) == {}


def test_usage_add():
    total = Usage(10, 5) + Usage(1, 2)
    assert total.input_tokens == 11
    assert total.output_tokens == 7
    assert total.total_tokens == 18
