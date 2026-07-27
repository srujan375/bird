"""Provider-neutral message types.

These dataclasses are the internal lingua franca of ox. Nothing outside
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
class ContentPart:
    """One part of a multimodal (content-parts) message.

    The OpenAI-compatible wire format expresses multimodal messages as a list
    of typed parts: a text part and one or more image parts. This dataclass is
    the internal representation; `Message.to_openai()` serializes it to the
    wire shape. Only the read_image vision sidecar builds list-content
    messages — the main conversation path stays string content, so this type
    never reaches the transcript or session persistence.
    """

    type: str  # "text" | "image_url"
    text: str | None = None
    image_url: dict | None = None  # {"url": "data:image/png;base64,..."}

    @classmethod
    def text_part(cls, text: str) -> "ContentPart":
        return cls(type="text", text=text)

    @classmethod
    def image(cls, data_uri: str) -> "ContentPart":
        return cls(type="image_url", image_url={"url": data_uri})

    def to_openai(self) -> dict[str, Any]:
        part: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            part["text"] = self.text
        if self.image_url is not None:
            part["image_url"] = self.image_url
        return part

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            d["text"] = self.text
        if self.image_url is not None:
            d["image_url"] = self.image_url
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContentPart":
        return cls(
            type=d["type"],
            text=d.get("text"),
            image_url=d.get("image_url"),
        )


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
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages

    def to_openai(self) -> dict[str, Any]:
        # Multimodal messages carry content as a list of typed parts. The
        # main conversation path only ever uses string content; list content
        # is built solely by the read_image vision sidecar for its one-shot
        # call to the vision model and never reaches the transcript.
        if isinstance(self.content, list):
            wire_content = [p.to_openai() for p in self.content]
        else:
            wire_content = self.content
        msg: dict[str, Any] = {"role": self.role, "content": wire_content}
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
            if msg["content"] is None:
                del msg["content"]
        if self.role == "tool":
            msg["tool_call_id"] = self.tool_call_id
            msg["content"] = self.content or ""
        return msg

    def to_dict(self) -> dict[str, Any]:
        if isinstance(self.content, list):
            content = [p.to_dict() for p in self.content]
        else:
            content = self.content
        d: dict[str, Any] = {"role": self.role, "content": content}
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
        raw_content = d.get("content")
        if isinstance(raw_content, list):
            content: str | list[ContentPart] | None = [
                ContentPart.from_dict(p) for p in raw_content
            ]
        else:
            content = raw_content
        return cls(
            role=d["role"],
            content=content,
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
