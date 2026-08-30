"""The ONE wire adapter: OpenAI-compatible /chat/completions over httpx.

Both v1 providers (OpenRouter, Ollama) speak this dialect, so this is the
only place in the codebase that knows any provider wire format. The adapter
owns TRANSPORT retries only (429/5xx backoff, honoring Retry-After); invalid
tool calls and model misbehavior are the runner's problem (decision #7).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable

import httpx

from ..registry import ModelSpec
from ..types import LLMResponse, Message, ToolCall, ToolSpec, Usage

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_TRANSPORT_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0

# bird's internal reasoning_effort vocabulary (see repl.THINK_MODES: off/low/
# medium/high/max, with `off` stored as "none") -> OpenRouter's unified
# `reasoning` object. OpenRouter only accepts effort high|medium|low there —
# it has no "max", so bird's `max` clamps to "high". "none" (think off) maps
# to None: omit the object entirely and leave provider defaults in charge.
# This is the single place that table lives; keep it that way.
OPENROUTER_REASONING_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
    # OpenAI-style vocabulary, mapped to the nearest available effort
    "minimal": "low",
}


class WireError(Exception):
    """Transport or protocol failure after retries were exhausted."""


class WireAborted(WireError):
    """The in-flight request was torn down by `abort()` — an interrupt, not a
    provider failure. Never retried."""


# Streaming callback: called with each assistant-text fragment as it arrives,
# with "" as a cancellation-check heartbeat on chunks that carry no text
# (thinking/tool-call deltas), then once with None after the message's text
# is complete. Callers may raise from it to abort the stream.
OnDelta = Callable[[str | None], None]


class OpenAICompatClient:
    def __init__(self, timeout: float = 300.0):
        self._http = httpx.Client(timeout=timeout)
        # abort() runs on a different thread than the request it kills: the
        # lock guards the in-flight response handle, the flag tells the
        # request thread that whatever error it just saw was self-inflicted
        self._lock = threading.Lock()
        self._inflight: httpx.Response | None = None
        self._aborted = False

    def close(self) -> None:
        self._http.close()

    def abort(self) -> None:
        """Tear down the in-flight streaming request from another thread.

        A cooperative cancel flag is only ever seen when a chunk arrives; a
        provider that has stopped sending leaves the request thread blocked
        in the socket read with nothing to check it. Shutting the socket
        wakes that read immediately, and `_stream_with_retries` reports
        WireAborted instead of retrying. Idempotent; a no-op when idle (the
        flag still sticks until `clear_abort`, so an interrupt that lands
        between requests aborts the next one rather than being lost)."""
        with self._lock:
            self._aborted = True
            resp = self._inflight
        if resp is None:
            return
        try:
            stream = resp.extensions.get("network_stream")
            sock = stream.get_extra_info("socket") if stream is not None else None
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass  # already closed, or a transport without a raw socket
        try:
            resp.close()
        except Exception:
            pass

    def clear_abort(self) -> None:
        """Arm the client for a new turn after an abort."""
        with self._lock:
            self._aborted = False

    @property
    def aborted(self) -> bool:
        return self._aborted

    def _set_inflight(self, resp: httpx.Response | None) -> None:
        with self._lock:
            self._inflight = resp
            aborted = self._aborted
        # abort() raced us: it saw no in-flight response, so it could not
        # close this one — do it ourselves rather than block on a dead read
        if aborted and resp is not None:
            self.abort()

    def complete(
        self,
        spec: ModelSpec,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: OnDelta | None = None,
        on_thinking: OnDelta | None = None,
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
        # Ollama's OpenAI-compatible endpoint controls thinking via
        # `reasoning_effort` (the native `think` flag is ignored there).
        # OpenRouter instead takes the unified nested `reasoning` object
        # ({"effort": high|medium|low}) — its providers (Anthropic among them)
        # can ONLY be enabled that way, and a raw top-level reasoning_effort
        # must never reach its wire. Translate bird's internal vocabulary via
        # OPENROUTER_REASONING_EFFORT; "none" (think off) omits the object so
        # provider defaults apply.
        if "reasoning_effort" in spec.extra:
            if spec.provider.name == "ollama":
                payload["reasoning_effort"] = spec.extra["reasoning_effort"]
            elif spec.provider.name == "openrouter":
                effort = OPENROUTER_REASONING_EFFORT.get(spec.extra["reasoning_effort"])
                if effort is not None:
                    payload["reasoning"] = {"effort": effort}

        headers = {"Content-Type": "application/json"}
        api_key = spec.provider.api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = spec.provider.base_url.rstrip("/") + "/chat/completions"
        if on_delta is not None:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            data = self._stream_with_retries(url, payload, headers, on_delta, on_thinking)
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
        self, url: str, payload: dict[str, Any], headers: dict[str, str], on_delta: OnDelta,
        on_thinking: OnDelta | None = None,
    ) -> dict[str, Any]:
        """SSE streaming POST. Retries only while nothing has been delivered to
        on_delta/on_thinking — a stream that drops mid-response cannot be
        restarted without duplicating already-shown text, so that becomes a
        hard WireError."""
        last_error = ""
        for attempt in range(MAX_TRANSPORT_ATTEMPTS):
            if self._aborted:
                raise WireAborted(f"request to {url} aborted")
            emitted = [False]
            try:
                with self._http.stream("POST", url, json=payload, headers=headers) as resp:
                    self._set_inflight(resp)
                    try:
                        if resp.status_code == 200:
                            return self._consume_sse(resp, on_delta, emitted, on_thinking)
                        resp.read()
                    finally:
                        self._set_inflight(None)
                    last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                    if resp.status_code not in RETRYABLE_STATUS:
                        raise WireError(f"{url} -> {last_error}")
            except WireError:
                if self._aborted:
                    raise WireAborted(f"request to {url} aborted") from None
                raise
            except Exception as e:
                # an aborted socket surfaces as whatever httpx/httpcore was in
                # the middle of (ReadError, StreamClosed, RemoteProtocolError,
                # a bare OSError) — none of it is the provider's doing
                if self._aborted:
                    raise WireAborted(f"request to {url} aborted") from None
                if not isinstance(e, httpx.HTTPError):
                    raise
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
        resp: httpx.Response, on_delta: OnDelta, emitted: list[bool],
        on_thinking: OnDelta | None = None,
    ) -> dict[str, Any]:
        """Fold an SSE chunk stream back into the non-streaming response shape
        so _parse stays the single parser."""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
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
            # reasoning trace (Ollama thinking models): streamed live when an
            # on_thinking callback is wired; otherwise it takes the existing
            # heartbeat path (on_delta(""), nothing accumulated) so an
            # unadapted caller's pre-delivery retry policy is byte-identical
            # to today — emitted[0] is untouched and reasoning is dropped.
            reasoning = delta.get("reasoning")
            if reasoning:
                if on_thinking is not None:
                    reasoning_parts.append(reasoning)
                    emitted[0] = True
                    on_thinking(reasoning)
                else:
                    on_delta("")
            text = delta.get("content")
            if text:
                content_parts.append(text)
                emitted[0] = True
                on_delta(text)
            elif not reasoning:
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
        if reasoning_parts:
            # fold the accumulated reasoning into the synthesized message so
            # _parse stays the single parser and yields Message.thinking
            if on_thinking is not None:
                on_thinking(None)  # reasoning complete — sentinel mirrors on_delta

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
        if reasoning_parts:
            message["reasoning"] = "".join(reasoning_parts)
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
            # reasoning trace (Ollama thinking models): provider-neutral — read
            # the field if present, never gate on provider. Display-only: it
            # never re-enters a model prompt (to_openai excludes it).
            thinking=raw_msg.get("reasoning") or None,
        )
        usage_raw = data.get("usage") or {}
        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens", 0),
            output_tokens=usage_raw.get("completion_tokens", 0),
        )
        stop_reason = choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
        return LLMResponse(message=message, usage=usage, stop_reason=stop_reason, model=spec.spec)
