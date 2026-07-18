"""Provider-neutral message types.

These dataclasses are the internal lingua franca of mha. Nothing outside
llm/wire/ may depend on any provider's wire format; conversion happens only
at the adapter boundary via the to_openai() methods. Session JSONL speaks
this schema (pi-style), so a session can survive a mid-run provider swap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolSpec:
    """A tool definition. `parameters` is a hand-written JSON Schema dict —
    it is sent to the model verbatim, so its token weight is our responsibility."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    """A model-requested tool invocation.

    `arguments` is the parsed dict, or None when the model emitted invalid
    JSON — `arguments_json` always preserves the raw string so the runner can
    show the model exactly what it sent when asking for a retry.
    """

    id: str
    name: str
    arguments: dict[str, Any] | None
    arguments_json: str = ""

    @classmethod
    def from_raw(cls, id: str, name: str, arguments_json: str) -> "ToolCall":
        try:
            parsed = json.loads(arguments_json)
            if not isinstance(parsed, dict):
                parsed = None
        except (json.JSONDecodeError, TypeError):
            parsed = None
        return cls(id=id, name=name, arguments=parsed, arguments_json=arguments_json or "")

    def to_openai(self) -> dict[str, Any]:
        args = self.arguments_json or json.dumps(self.arguments or {})
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": args},
        }


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages

    def to_openai(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
            if msg["content"] is None:
                del msg["content"]
        if self.role == "tool":
            msg["tool_call_id"] = self.tool_call_id
            msg["content"] = self.content or ""
        return msg

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "arguments_json": tc.arguments_json,
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content"),
            tool_calls=[
                ToolCall(
                    id=tc["id"],
                    name=tc["name"],
                    arguments=tc.get("arguments"),
                    arguments_json=tc.get("arguments_json", ""),
                )
                for tc in d.get("tool_calls", [])
            ],
            tool_call_id=d.get("tool_call_id"),
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass
class LLMResponse:
    message: Message
    usage: Usage
    stop_reason: str  # "stop" | "tool_calls" | "length" | provider-specific
    model: str  # provider:model spec that actually served the request
