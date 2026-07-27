"""The ONE wire adapter: OpenAI-compatible /chat/completions over httpx.

Both v1 providers (OpenRouter, Ollama) speak this dialect, so this is the
only place in the codebase that knows any provider wire format. The adapter
owns TRANSPORT retries only (429/5xx backoff, honoring Retry-After); invalid
tool calls and model misbehavior are the runner's problem (decision #7).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from ..registry import ModelSpec
from ..types import LLMResponse, Message, ToolCall, ToolSpec, Usage

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_TRANSPORT_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0


class WireError(Exception):
    """Transport or protocol failure after retries were exhausted."""


# Streaming callback: called with each assistant-text fragment as it arrives,
# with "" as a cancellation-check heartbeat on chunks that carry no text
# (thinking/tool-call deltas), then once with None after the message's text
# is complete. Callers may raise from it to abort the stream.
OnDelta = Callable[[str | None], None]


class OpenAICompatClient:
    def __init__(self, timeout: float = 300.0):
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def complete(
        self,
        spec: ModelSpec,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: OnDelta | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": spec.model,
            "messages": [m.to_openai() for m in messages],
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {"Content-Type": "application/json"}
        api_key = spec.provider.api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = spec.provider.base_url.rstrip("/") + "/chat/completions"
        if on_delta is not None:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            data = self._stream_with_retries(url, payload, headers, on_delta)
        else:
            data = self._post_with_retries(url, payload, headers)
        return self._parse(data, spec)

    def _post_with_retries(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        last_error = ""
        for attempt in range(MAX_TRANSPORT_ATTEMPTS):
            try:
                resp = self._http.post(url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                last_error = f"connection error: {e}"
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except json.JSONDecodeError as e:
                        raise WireError(f"non-JSON 200 response from {url}: {e}") from e
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise WireError(f"{url} -> {last_error}")
                retry_after = resp.headers.get("Retry-After")
                if retry_after and attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                    try:
                        time.sleep(min(float(retry_after), 30.0))
                        continue
                    except ValueError:
                        pass
            if attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
        raise WireError(
            f"{url} failed after {MAX_TRANSPORT_ATTEMPTS} attempts; last: {last_error}"
        )

    def _stream_with_retries(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], on_delta: OnDelta
    ) -> dict[str, Any]:
        """SSE streaming POST. Retries only while nothing has been delivered to
        on_delta — a stream that drops mid-response cannot be restarted without
        duplicating already-shown text, so that becomes a hard WireError."""
        last_error = ""
        for attempt in range(MAX_TRANSPORT_ATTEMPTS):
            emitted = [False]
            try:
                with self._http.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code == 200:
                        return self._consume_sse(resp, on_delta, emitted)
                    resp.read()
                    last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                    if resp.status_code not in RETRYABLE_STATUS:
                        raise WireError(f"{url} -> {last_error}")
            except httpx.HTTPError as e:
                if emitted[0]:
                    raise WireError(f"stream from {url} dropped mid-response: {e}") from e
                last_error = f"connection error: {e}"
            if attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
        raise WireError(
            f"{url} failed after {MAX_TRANSPORT_ATTEMPTS} attempts; last: {last_error}"
        )

    @staticmethod
    def _consume_sse(
        resp: httpx.Response, on_delta: OnDelta, emitted: list[bool]
    ) -> dict[str, Any]:
        """Fold an SSE chunk stream back into the non-streaming response shape
        so _parse stays the single parser."""
        content_parts: list[str] = []
        tool_slots: dict[int, dict[str, str | None]] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] = {}

        for line in resp.iter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError as e:
                raise WireError(f"malformed SSE chunk: {data_str[:200]}") from e
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue  # usage-only final chunk
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                content_parts.append(text)
                emitted[0] = True
                on_delta(text)
            else:
                # heartbeat: thinking/tool-call chunks carry no content, but the
                # caller still needs a hook to cancel mid-generation
                on_delta("")
            for tc in delta.get("tool_calls") or []:
                slot = tool_slots.setdefault(
                    tc.get("index", 0), {"id": None, "name": "", "arguments": ""}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

        if emitted[0]:
            on_delta(None)  # text complete

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
        if tool_slots:
            message["tool_calls"] = [
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"]},
                }
                for _, slot in sorted(tool_slots.items())
            ]
        return {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }

    @staticmethod
    def _parse(data: dict[str, Any], spec: ModelSpec) -> LLMResponse:
        try:
            choice = data["choices"][0]
            raw_msg = choice["message"]
        except (KeyError, IndexError) as e:
            raise WireError(f"malformed completion response: {json.dumps(data)[:500]}") from e

        tool_calls = [
            ToolCall.from_raw(
                id=tc.get("id") or f"call_{i}",
                name=tc.get("function", {}).get("name", ""),
                arguments_json=tc.get("function", {}).get("arguments", ""),
            )
            for i, tc in enumerate(raw_msg.get("tool_calls") or [])
        ]
        message = Message(
            role="assistant",
            content=raw_msg.get("content"),
            tool_calls=tool_calls,
        )
        usage_raw = data.get("usage") or {}
        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens", 0),
            output_tokens=usage_raw.get("completion_tokens", 0),
        )
        stop_reason = choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
        return LLMResponse(message=message, usage=usage, stop_reason=stop_reason, model=spec.spec)
