"""Tests for WebSearch and WebFetch tools.

These tests do NOT hit the network. WebSearch is stubbed via httpx.MockTransport
inside an httpx.Client we inject; WebFetch gets a tiny in-process server
served via a custom transport too. We exercise:
- schema smoke (both tools expose well-formed ToolSpecs)
- happy path for both (mocked HTTP): redirect-href unwrapping, ads dropped
- domain filtering (allowed / blocked)
- WebFetch: http→https upgrade, auth failure, oversize, cross-host redirect,
  cache hit on second call within TTL
- DDG rate limiting (HTTP 202) surfaces as a ToolError
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
import pytest

from bird.harnesses.code import code_harness_tools
from bird.tools import WebFetchTool, WebSearchTool
from bird.tools.base import ToolContext, ToolError


# ---------- fixtures ----------

@pytest.fixture
def repo(tmp_path):
    return tmp_path


@pytest.fixture
def ctx(repo):
    events = []

    def record(event_type, data):
        events.append((event_type, data))

    c = ToolContext(repo_root=repo, record=record)
    c.events = events
    return c


def _mock_client(handler) -> httpx.Client:
    """Build an httpx.Client whose transport is fully controlled by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ddg_page(results, with_ad: bool = False) -> str:
    """Render an html.duckduckgo.com-shaped results page. Hrefs are wrapped
    in DDG's /l/?uddg= redirect the way the live site serves them."""
    blocks = []
    if with_ad:
        blocks.append(
            '<div class="result result--ad">'
            '<a rel="nofollow" class="result__a" href="https://ads.ddg/click">Sponsored</a>'
            '<a class="result__snippet">buy now</a>'
            "</div>"
        )
    for r in results:
        wrapped = "//duckduckgo.com/l/?uddg=" + quote(r["url"], safe="") + "&rut=abc"
        blocks.append(
            '<div class="result results_links results_links_deep web-result">'
            f'<a rel="nofollow" class="result__a" href="{wrapped}">{r["title"]}</a>'
            f'<a class="result__snippet" href="{wrapped}">{r["snippet"]}</a>'
            "</div>"
        )
    return "<html><body>" + "".join(blocks) + "</body></html>"


def _ddg_ok(results, with_ad: bool = False):
    """Handler factory: return a DDG-shaped 200 with the given results."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "html.duckduckgo.com"
        assert request.url.params.get("q")
        return httpx.Response(200, text=_ddg_page(results, with_ad=with_ad))
    return handler


# ---------- WebSearch ----------

def test_websearch_happy_path(ctx):
    results = [
        {"title": "First", "url": "https://example.com/a", "snippet": "alpha"},
        {"title": "Second", "url": "https://example.org/b", "snippet": "beta"},
    ]
    tool = WebSearchTool(http_client=_mock_client(_ddg_ok(results, with_ad=True)))
    r = tool.execute({"query": "hello world"}, ctx)

    assert not r.is_error
    assert "[1] First" in r.output
    assert "[2] Second" in r.output
    # redirect hrefs are unwrapped back to the real target URLs
    assert "https://example.com/a" in r.output
    assert "https://example.org/b" in r.output
    assert "uddg=" not in r.output
    # the result--ad block is dropped wholesale
    assert "Sponsored" not in r.output
    assert r.details["count"] == 2
    assert any(t == "web_search" for t, _ in ctx.events)


def test_websearch_allowed_domains_filter(ctx):
    results = [
        {"title": "Wanted", "url": "https://docs.python.org/3/x", "snippet": "py"},
        {"title": "Unwanted", "url": "https://nope.com/y", "snippet": "no"},
    ]
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(request.url.params.get("q") or "")
        return httpx.Response(200, text=_ddg_page(results))

    tool = WebSearchTool(http_client=_mock_client(handler))
    r = tool.execute({"query": "python", "allowed_domains": ["docs.python.org"]}, ctx)

    assert not r.is_error
    # narrowed server-side via site: AND re-filtered client-side
    assert "site:docs.python.org" in seen_queries[0]
    assert "docs.python.org" in r.output
    assert "nope.com" not in r.output
    assert r.details["count"] == 1


def test_websearch_blocked_domains_filter(ctx):
    results = [
        {"title": "Bad", "url": "https://ads.example.com/x", "snippet": "ad"},
        {"title": "Good", "url": "https://good.com/y", "snippet": "ok"},
    ]
    tool = WebSearchTool(http_client=_mock_client(_ddg_ok(results)))
    r = tool.execute({"query": "x", "blocked_domains": ["ads.example.com"]}, ctx)

    assert not r.is_error
    assert "good.com" in r.output
    assert "ads.example.com" not in r.output
    assert r.details["count"] == 1


def test_websearch_http_error_surfaces(ctx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    tool = WebSearchTool(http_client=_mock_client(handler))
    r = tool.execute({"query": "x"}, ctx)
    assert r.is_error
    assert "500" in r.output


def test_websearch_rate_limit_surfaces(ctx):
    """DDG answers 202 (anomaly detection) when it rate-limits a client."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, text="anomaly")

    tool = WebSearchTool(http_client=_mock_client(handler))
    r = tool.execute({"query": "x"}, ctx)
    assert r.is_error
    assert "202" in r.output
    assert "rate limit" in r.output.lower()


def test_websearch_empty_results(ctx):
    tool = WebSearchTool(http_client=_mock_client(_ddg_ok([])))
    r = tool.execute({"query": "obscure thing"}, ctx)
    assert not r.is_error
    assert "No web results." in r.output
    assert r.details["count"] == 0


# ---------- WebFetch ----------

FETCH_HTML = """
<!doctype html>
<html><head><title>Hi</title></head>
<body><h1>Heading</h1>
<p>Hello <b>world</b>.</p>
<a href="https://example.com">link</a>
<script>alert('x')</script>
</body></html>
""".strip()


def test_webfetch_happy_path_html_to_md(ctx, repo):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=FETCH_HTML,
        )

    tool = WebFetchTool(http_client=_mock_client(handler))
    r = tool.execute(
        {"url": "https://example.com/page", "prompt": "summarize this"},
        ctx,
    )

    assert not r.is_error
    # markdown conversion drops the script tag and keeps the heading
    assert "Heading" in r.output
    assert "Hello world" in r.output
    assert "alert(" not in r.output
    # the prompt and source are surfaced in the output framing
    assert "[page: https://example.com/page]" in r.output
    assert "[prompt: summarize this]" in r.output
    assert r.details["from_cache"] is False
    # no client/registry on this ctx → the inline path, exactly as before
    assert r.details["qa"] == "inline"
    # cache file is written on first call
    cache_file = repo / ".bird" / "cache" / "webfetch"
    assert cache_file.is_dir()
    assert any(cache_file.iterdir())


def test_webfetch_second_call_hits_cache(ctx, repo):
    """Second call within TTL should not reach the transport at all."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="<html><body><p>v1</p></body></html>")

    tool = WebFetchTool(http_client=_mock_client(handler))
    url = "https://example.com/cached"
    r1 = tool.execute({"url": url, "prompt": "q"}, ctx)
    r2 = tool.execute({"url": url, "prompt": "q"}, ctx)

    assert not r1.is_error and not r2.is_error
    assert r1.details["from_cache"] is False
    assert r2.details["from_cache"] is True
    # transport was hit exactly once across both calls
    assert len(calls) == 1


def test_webfetch_http_upgraded_to_https(ctx):
    """Bare http:// URLs are upgraded silently."""
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, text="<html><body><p>ok</p></body></html>")

    tool = WebFetchTool(http_client=_mock_client(handler))
    r = tool.execute({"url": "http://example.com/up", "prompt": "q"}, ctx)
    assert not r.is_error
    assert seen_urls == ["https://example.com/up"]


def test_webfetch_auth_required_surfaces_clean_error(ctx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="login required")

    tool = WebFetchTool(http_client=_mock_client(handler))
    r = tool.execute({"url": "https://example.com/private", "prompt": "q"}, ctx)
    assert r.is_error
    assert "401" in r.output
    assert "WebFetch has no credentials" in r.output


def test_webfetch_oversize_page_rejected(ctx):
    big = "x" * 600_000  # > WEBFETCH_MAX_BYTES (500_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big)

    tool = WebFetchTool(http_client=_mock_client(handler))
    r = tool.execute({"url": "https://example.com/huge", "prompt": "q"}, ctx)
    assert r.is_error
    assert "too large" in r.output.lower()


def test_webfetch_cross_host_redirect_surfaces(ctx):
    """A redirect to a different host is reported so the model can re-call."""

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx follows the redirect under the hood; we compare URLs after
        # the request. Simulate by making the final URL a different host.
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>x</body></html>",
        )

    # Build a client that reports a different final URL. Easiest: a custom
    # transport that sets request.url to the redirect target.
    class RedirectTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            request.url = httpx.URL("https://other-host.example/x")
            return httpx.Response(200, text="<html><body>x</body></html>")

    tool = WebFetchTool(http_client=httpx.Client(transport=RedirectTransport()))
    r = tool.execute({"url": "https://example.com/start", "prompt": "q"}, ctx)
    assert r.is_error
    assert "Cross-host redirect" in r.output
    assert "other-host.example" in r.output


# ---------- WebFetch delegated QA ----------

class _FakeQAClient:
    """Stands in for OpenAICompatClient.complete() in the delegated-QA path.
    Same shape as _FakeVisionClient in test_tools.py."""

    def __init__(self, answer="The page says: 42", error=None):
        self._answer = answer
        self._error = error
        self.calls = []

    def complete(self, spec, messages, tools=None, **kw):
        self.calls.append({"spec": spec, "messages": messages, "kw": kw})
        if self._error is not None:
            raise self._error
        from bird.llm.types import LLMResponse, Message, Usage
        return LLMResponse(
            message=Message(role="assistant", content=self._answer),
            usage=Usage(),
            stop_reason="stop",
            model=spec.spec,
        )


class _FakeQARegistry:
    def __init__(self, spec_str="ollama:gemma4:31b"):
        from bird.llm.registry import ModelSpec, ProviderConfig
        self._spec = ModelSpec(
            spec=spec_str,
            provider=ProviderConfig(name="ollama", base_url="http://x"),
            model=spec_str.split(":", 1)[1],
        )

    def resolve(self, name):
        if name != "compactor":
            from bird.llm.registry import RegistryError
            raise RegistryError(f"no alias {name}")
        return self._spec


def _qa_ctx(repo, client, registry):
    events = []
    c = ToolContext(
        repo_root=repo,
        record=lambda t, d: events.append((t, d)),
        client=client,
        registry=registry,
    )
    c.events = events
    return c


def _ok_page_handler():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=FETCH_HTML,
        )
    return handler


def test_webfetch_delegated_qa_returns_answer_not_page(ctx, repo):
    """With client+registry on the ctx, the caller gets the compact QA
    answer, not the whole page markdown."""
    client = _FakeQAClient(answer="The heading is 'Heading' and the body greets the world.")
    qctx = _qa_ctx(repo, client=client, registry=_FakeQARegistry())
    tool = WebFetchTool(http_client=_mock_client(_ok_page_handler()))
    r = tool.execute({"url": "https://example.com/page", "prompt": "what does it say?"}, qctx)

    assert not r.is_error
    assert r.details["qa"] == "delegated"
    # the answer is there; the raw page markdown is NOT in the caller's output
    assert "The heading is 'Heading'" in r.output
    assert "Hello world" not in r.output
    # source URL and prompt framing are preserved
    assert "[page: https://example.com/page]" in r.output
    assert "[prompt: what does it say?]" in r.output
    # the QA call used the compactor alias, temperature 0, no tools
    call = client.calls[0]
    assert call["spec"].spec == "ollama:gemma4:31b"
    assert call["kw"].get("temperature") == 0.0
    assert call["kw"].get("max_tokens") is not None
    # system prompt says answer from the page only; user message carries the page
    assert call["messages"][0].role == "system"
    assert "ONLY" in call["messages"][0].content
    assert call["messages"][1].role == "user"
    assert "what does it say?" in call["messages"][1].content
    assert "Heading" in call["messages"][1].content


def test_webfetch_delegated_qa_truncates_huge_page(ctx, repo):
    """A page near WEBFETCH_MAX_BYTES is capped before it goes to the QA model."""
    from bird.tools.web import QA_PAGE_MAX_CHARS

    huge = "word " * 60_000  # ~300KB of markdown

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=huge)  # no content-type → passthrough

    client = _FakeQAClient(answer="summary")
    qctx = _qa_ctx(repo, client=client, registry=_FakeQARegistry())
    tool = WebFetchTool(http_client=_mock_client(handler))
    r = tool.execute({"url": "https://example.com/big", "prompt": "q"}, qctx)

    assert not r.is_error
    assert r.details["qa"] == "delegated"
    sent_page = client.calls[0]["messages"][1].content
    assert len(sent_page) < len(huge)
    assert "[page truncated for the QA model]" in sent_page
    assert sent_page.count("word") <= QA_PAGE_MAX_CHARS // 4 + 10


def test_webfetch_wire_error_falls_back_to_inline(ctx, repo):
    """A failed QA call must never turn a successful fetch into an error:
    WireError from the client degrades to the inline page markdown."""
    from bird.llm.wire.openai_compat import WireError

    client = _FakeQAClient(error=WireError("connection refused"))
    qctx = _qa_ctx(repo, client=client, registry=_FakeQARegistry())
    tool = WebFetchTool(http_client=_mock_client(_ok_page_handler()))
    r = tool.execute({"url": "https://example.com/page", "prompt": "q"}, qctx)

    assert not r.is_error
    assert r.details["qa"] == "fallback"
    # the full page markdown came back, as before delegation existed
    assert "Heading" in r.output
    assert "Hello world" in r.output
    assert len(client.calls) == 1  # the QA call was attempted exactly once


def test_webfetch_registry_error_falls_back_to_inline(ctx, repo):
    """A missing/misconfigured compactor alias degrades to inline too."""
    from bird.llm.registry import RegistryError

    class _NoCompactorRegistry:
        def resolve(self, name):
            raise RegistryError(f"no alias {name}")

    client = _FakeQAClient()
    qctx = _qa_ctx(repo, client=client, registry=_NoCompactorRegistry())
    tool = WebFetchTool(http_client=_mock_client(_ok_page_handler()))
    r = tool.execute({"url": "https://example.com/page", "prompt": "q"}, qctx)

    assert not r.is_error
    assert r.details["qa"] == "fallback"
    assert "Heading" in r.output
    assert client.calls == []  # resolve failed → complete never called


def test_webfetch_qa_runs_on_cached_page(ctx, repo):
    """The cache stores page markdown, so a cached page is still QA'd
    against a fresh prompt (and the second prompt's answer is what returns)."""
    client = _FakeQAClient(answer="first answer")
    qctx = _qa_ctx(repo, client=client, registry=_FakeQARegistry())
    tool = WebFetchTool(http_client=_mock_client(_ok_page_handler()))
    url = "https://example.com/cached-qa"

    r1 = tool.execute({"url": url, "prompt": "q1"}, qctx)
    assert r1.details["from_cache"] is False
    assert r1.details["qa"] == "delegated"
    assert "first answer" in r1.output

    client._answer = "second answer"
    r2 = tool.execute({"url": url, "prompt": "q2"}, qctx)
    assert r2.details["from_cache"] is True
    assert r2.details["qa"] == "delegated"
    assert "second answer" in r2.output
    assert "q2" in client.calls[1]["messages"][1].content
    assert len(client.calls) == 2


def test_webfetch_inline_ctx_records_inline_path(ctx, repo):
    """The existing no-client ctx (tests, library use) records qa=inline."""
    tool = WebFetchTool(http_client=_mock_client(_ok_page_handler()))
    r = tool.execute({"url": "https://example.com/page", "prompt": "q"}, ctx)
    assert not r.is_error
    assert r.details["qa"] == "inline"
    assert "Heading" in r.output


# ---------- schema / wiring smoke ----------

def test_both_tools_present_in_code_harness():
    names = [t.name for t in code_harness_tools(with_kg=True)]
    assert "WebSearch" in names
    assert "WebFetch" in names


def test_both_tools_have_required_schema_fields():
    for tool in (WebSearchTool(), WebFetchTool()):
        spec = tool.spec()
        assert spec.name in ("WebSearch", "WebFetch")
        assert spec.parameters.get("type") == "object"
        assert "properties" in spec.parameters
        assert spec.parameters.get("additionalProperties") is False
        for required in spec.parameters.get("required", []):
            assert required in spec.parameters["properties"]


def test_schemas_under_token_budget_after_adding_web_tools():
    """Regression guard: even with the two web tools, the full toolset stays
    under the wire budget. The threshold is imported from test_tools.py, which
    is its single source of truth."""
    from .test_tools import SCHEMA_TOKEN_BUDGET

    wire = json.dumps([t.spec().to_openai() for t in code_harness_tools(with_kg=True)])
    approx_tokens = len(wire) / 4
    assert approx_tokens < SCHEMA_TOKEN_BUDGET, (
        f"schemas ≈ {approx_tokens:.0f} tokens, budget is {SCHEMA_TOKEN_BUDGET}"
    )
