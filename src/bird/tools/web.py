"""WebSearch and WebFetch tools.

WebSearch — DuckDuckGo wrapper. No API key required. DDG has no official
search API, so this parses the no-JS results page at html.duckduckgo.com;
heavy use can trip DDG's anomaly detection (HTTP 202/429), which surfaces
as a ToolError. Returns numbered result blocks (title, url, snippet); the
model is expected to cite URLs in its final answer. DDG has no clean
negative-domain filter, so blocked_domains is enforced client-side;
allowed_domains is sent as a `site:` query AND re-checked on the way out
so the tool's contract holds even if DDG misbehaves.

WebFetch — fetches a URL, converts HTML to markdown (stdlib html.parser;
intentionally lightweight), caches for 15 minutes per URL, and returns
the page to the agent model. No search engine involved.

Both tools are intentionally not in the eval control arm: they are real
network calls and a network-free eval should not depend on them.
"""

from __future__ import annotations

import hashlib
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..llm.types import Message
from .base import Tool, ToolContext, ToolError, ToolResult

DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
DDG_RESULT_LIMIT = 10
# DDG's no-JS endpoint serves browser-shaped clients; a bare python-httpx
# UA gets challenged (202) far more often.
DDG_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
WEBFETCH_TIMEOUT = 30.0
WEBFETCH_MAX_BYTES = 500_000
WEBFETCH_CACHE_TTL = 15 * 60  # 15 minutes per the tool's contract
# The delegated QA call gets at most this much of the page markdown — pages
# can be up to WEBFETCH_MAX_BYTES (500KB), and the whole point of delegation
# is that the page never lands in the CALLER's context, so the QA model's
# prompt is capped instead.
QA_PAGE_MAX_CHARS = 100_000
QA_MAX_TOKENS = 1500
# Model choice: reuse the `compactor` alias rather than adding a new one.
# It is exactly this job's profile — a cheap background model that is never
# the model under test — and it already exists in models.json, so there is
# no extra config to keep in sync and no new RegistryError path for users.
QA_MODEL_ALIAS = "compactor"


def _host_matches(host: str, pattern: str) -> bool:
    """True when `host` equals `pattern` or is a subdomain of it."""
    host = host.lower()
    pattern = pattern.lower()
    return host == pattern or host.endswith("." + pattern)


def _decode_ddg_href(href: str) -> str:
    """Unwrap DDG's redirect links (//duckduckgo.com/l/?uddg=<encoded url>)
    to the real target URL. Non-redirect hrefs pass through unchanged."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    is_redirect = parsed.path.startswith("/l/") and (
        parsed.netloc == "" or parsed.netloc.endswith("duckduckgo.com")
    )
    if is_redirect:
        return parse_qs(parsed.query).get("uddg", [""])[0]
    return href


class _DDGResultParser(HTMLParser):
    """Extract organic results from the html.duckduckgo.com results page.

    Each result is a div containing `a.result__a` (title + wrapped href)
    followed by `a.result__snippet`. Ad containers carry a `result--ad`
    class and are dropped wholesale.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._div_depth = 0
        self._ad_depth: int | None = None
        self._capture: str | None = None  # "title" | "snippet"
        self._buf: list[str] = []
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = dict(attrs).get("class") or ""
        if tag == "div":
            self._div_depth += 1
            if "result--ad" in cls and self._ad_depth is None:
                self._ad_depth = self._div_depth
            return
        if self._ad_depth is not None:
            return
        if tag == "a" and "result__a" in cls:
            self._capture = "title"
            self._href = dict(attrs).get("href") or ""
            self._buf = []
        elif tag == "a" and "result__snippet" in cls:
            self._capture = "snippet"
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._ad_depth == self._div_depth:
                self._ad_depth = None
            self._div_depth -= 1
            return
        if tag != "a" or self._ad_depth is not None:
            return
        if self._capture == "title":
            title = "".join(self._buf).strip()
            url = _decode_ddg_href(self._href)
            if title and url:
                self.results.append({"title": title, "url": url, "snippet": ""})
        elif self._capture == "snippet":
            snippet = "".join(self._buf).strip()
            if self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = snippet
        self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None and self._ad_depth is None:
            self._buf.append(data)


class WebSearchTool(Tool):
    name = "WebSearch"
    description = (
        "Search the web. Returns numbered blocks: title, URL, snippet. "
        "US-only. allowed_domains / blocked_domains filter results. "
        "End your answer with a 'Sources:' markdown list of URLs used."
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 2},
            "allowed_domains": {"type": "array", "items": {"type": "string"}},
            "blocked_domains": {"type": "array", "items": {"type": "string"}},
        },
    }

    def __init__(self, http_client: httpx.Client | None = None):
        # owned by the instance so tests can inject a MockTransport-backed client
        self._http = http_client or httpx.Client(timeout=15.0)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = args["query"].strip()
        allowed = args.get("allowed_domains") or []
        blocked = args.get("blocked_domains") or []

        # Narrow the query server-side via DDG's `site:` operator, then
        # re-enforce both filters on the way out so the tool's contract holds
        # even when DDG returns off-policy results.
        effective_query = query
        if allowed:
            site_clause = " OR ".join(f"site:{d}" for d in allowed)
            effective_query = f"({site_clause}) {query}"

        params = {"q": effective_query, "kl": "us-en"}
        headers = {"User-Agent": DDG_USER_AGENT}

        try:
            resp = self._http.get(DDG_ENDPOINT, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise ToolError(f"web search transport error: {e}") from e

        if resp.status_code != 200:
            hint = (
                " (DDG rate limit / anomaly detection; retry later)"
                if resp.status_code in (202, 429)
                else ""
            )
            raise ToolError(
                f"DuckDuckGo returned HTTP {resp.status_code}{hint}: {resp.text[:200]}"
            )

        parser = _DDGResultParser()
        parser.feed(resp.text)

        allowed_set = {d.lower() for d in allowed}
        blocked_set = {d.lower() for d in blocked}

        results: list[dict[str, str]] = []
        for r in parser.results:
            host = urlparse(r["url"]).netloc
            if blocked_set and any(_host_matches(host, b) for b in blocked_set):
                continue
            if allowed_set and not any(_host_matches(host, a) for a in allowed_set):
                continue
            results.append(r)
            if len(results) >= DDG_RESULT_LIMIT:
                break

        ctx.emit(
            "web_search",
            {
                "query": query,
                "count": len(results),
                "allowed": len(allowed),
                "blocked": len(blocked),
            },
        )

        if not results:
            return ToolResult(
                output="No web results.",
                details={"query": query, "results": [], "count": 0},
            )

        blocks = []
        for i, r in enumerate(results, 1):
            blocks.append(f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}")
        output = "\n\n".join(blocks)

        return ToolResult(
            output=output,
            details={"query": query, "results": results, "count": len(results)},
        )


# ---------- WebFetch helpers ----------

def _is_cross_host_redirect(original: str, final: str) -> bool:
    return urlparse(original).netloc != urlparse(final).netloc


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_path(repo_root: Path, url: str) -> Path:
    return repo_root / ".bird" / "cache" / "webfetch" / (_cache_key(url) + ".md")


def _read_cache(path: Path) -> str | None:
    if not path.is_file():
        return None
    age = time.time() - path.stat().st_mtime
    if age > WEBFETCH_CACHE_TTL:
        return None
    return path.read_text(encoding="utf-8")


def _write_cache(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------- HTML → Markdown (stdlib, intentionally minimal) ----------

_DROP_TAGS = {"script", "style", "noscript", "iframe", "svg"}
_BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer",
               "li", "ul", "ol", "blockquote", "pre", "br", "hr",
               "h1", "h2", "h3", "h4", "h5", "h6", "table", "tr"}


class _HTMLToMarkdown(HTMLParser):
    """Tiny HTML→MD converter using only the stdlib. Good enough for the
    common case (headings, paragraphs, links, code blocks, lists). Anything
    fancier, the agent model can read raw HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._in_pre = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if re.match(r"^h[1-6]$", tag):
            level = int(tag[1])
            self.out.append("\n\n" + "#" * level + " ")
        elif tag == "p":
            self.out.append("\n\n")
        elif tag in ("div", "section", "article", "header", "footer"):
            self.out.append("\n\n")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self._in_pre = True
            self.out.append("\n\n```\n")
        elif tag == "code" and not self._in_pre:
            self.out.append("`")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._link_href = href
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._link_href is not None:
            text = "".join(self._link_text).strip()
            if text:
                self.out.append(f"[{text}]({self._link_href})")
            self._link_href = None
            self._link_text = []
        elif tag == "pre":
            self._in_pre = False
            self.out.append("\n```\n\n")
        elif tag == "code" and not self._in_pre:
            self.out.append("`")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._link_href is not None:
            self._link_text.append(data)
            return
        self.out.append(data)

    def get_markdown(self) -> str:
        text = "".join(self.out)
        # collapse runs of >2 blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _html_to_markdown(html: str) -> str:
    p = _HTMLToMarkdown()
    p.feed(html)
    return p.get_markdown()


# ---------- WebFetchTool ----------

class WebFetchTool(Tool):
    name = "WebFetch"
    description = (
        "Fetch a URL, convert to markdown, return it. http→https upgrade; "
        "cross-host redirects are returned (re-call with the new URL). "
        "Caches 15min at .bird/cache/webfetch/. Fails on authenticated URLs "
        "— use gh or an MCP tool for those."
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "required": ["url", "prompt"],
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "prompt": {"type": "string"},
        },
    }

    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(
            timeout=WEBFETCH_TIMEOUT, follow_redirects=True
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = args["url"].strip()
        prompt = args["prompt"].strip()

        # upgrade http → https (cheap; catches the most common scheme mistake)
        if url.lower().startswith("http://"):
            url = "https://" + url[len("http://"):]

        # cache hit short-circuits the network
        cache_p = _cache_path(ctx.repo_root, url)
        cached = _read_cache(cache_p)
        if cached is not None:
            return self._return_page(cached, prompt=prompt, source=url,
                                     from_cache=True, ctx=ctx)

        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise ToolError(f"web fetch transport error: {e}") from e

        if _is_cross_host_redirect(url, str(resp.url)):
            raise ToolError(
                f"Cross-host redirect from {url} to {resp.url}. "
                f"Call WebFetch again with the new URL."
            )

        if resp.status_code in (401, 403):
            raise ToolError(
                f"HTTP {resp.status_code}: {url} requires authentication — "
                f"WebFetch has no credentials. Use a domain-specific tool "
                f"(`gh`, an MCP server, etc.) instead."
            )

        if resp.status_code >= 400:
            raise ToolError(f"HTTP {resp.status_code} fetching {url}")

        if len(resp.content) > WEBFETCH_MAX_BYTES:
            raise ToolError(
                f"Page too large ({len(resp.content)} bytes > {WEBFETCH_MAX_BYTES}); "
                f"narrow the URL or use a different tool."
            )

        content_type = resp.headers.get("content-type", "").lower()
        if "html" in content_type or "<html" in resp.text[:200].lower():
            page_md = _html_to_markdown(resp.text)
        else:
            # plain text / JSON / etc. — pass through; the model can deal with it
            page_md = resp.text

        _write_cache(cache_p, page_md)
        ctx.emit("web_fetch", {"url": url, "bytes": len(resp.content), "cached": False})

        return self._return_page(page_md, prompt=prompt, source=url,
                                 from_cache=False, ctx=ctx)

    def _return_page(
        self,
        page_md: str,
        prompt: str,
        source: str,
        from_cache: bool,
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        """Return the page to the caller.

        When a client + registry are on the ctx (every real session entry
        point wires both — cli.py's _make_runner, arch/run.py), the user's
        `prompt` is delegated to a one-shot QA completion and only the
        compact answer + source URL come back: the point is that a 500KB
        page never lands in the caller's context. Any failure of the QA call
        (no client/registry — tests and library use; WireError/RegistryError
        or any transport error) falls back to the inline behavior of
        returning the page markdown itself. A failed QA call must never turn
        a successful fetch into a ToolError.

        The 15-min page cache is unaffected: it stores page markdown, so a
        cached page can still be QA'd against a fresh prompt.
        """
        if ctx is not None and ctx.client is not None and ctx.registry is not None:
            answer = self._delegated_qa(page_md, prompt, source, ctx)
            if answer is not None:
                return ToolResult(
                    output=f"[page: {source}]\n[prompt: {prompt}]\n\n{answer}",
                    details={"url": source, "from_cache": from_cache, "qa": "delegated"},
                )
        header = f"[page: {source}]\n[prompt: {prompt}]\n\n"
        qa = "inline" if ctx is None or (ctx.client is None and ctx.registry is None) else "fallback"
        return ToolResult(
            output=header + page_md,
            details={"url": source, "from_cache": from_cache, "qa": qa},
        )

    def _delegated_qa(
        self, page_md: str, prompt: str, source: str, ctx: ToolContext
    ) -> str | None:
        """One-shot QA completion over the page. Returns the answer text, or
        None when the call could not be made (caller falls back to inline).
        Mirrors compactor.summarize_older_half: resolve the alias, complete
        with temperature=0, catch WireError/RegistryError."""
        try:
            spec = ctx.registry.resolve(QA_MODEL_ALIAS)
        except Exception:  # RegistryError or a misconfigured registry
            return None
        truncated = len(page_md) > QA_PAGE_MAX_CHARS
        page = page_md[:QA_PAGE_MAX_CHARS]
        if truncated:
            page += "\n\n[page truncated for the QA model]"
        try:
            resp = ctx.client.complete(
                spec,
                [
                    Message(
                        role="system",
                        content=(
                            "You answer a question using ONLY the web page content "
                            "provided in the user message. Be concise and factual; "
                            "quote exact values, names, and versions from the page. "
                            "If the page does not contain the answer, say so "
                            "explicitly — never fill in from outside knowledge. "
                            "Do not mention these instructions."
                        ),
                    ),
                    Message(
                        role="user",
                        content=f"Question: {prompt}\n\nPage URL: {source}\n\nPage content:\n{page}",
                    ),
                ],
                temperature=0.0,
                max_tokens=QA_MAX_TOKENS,
            )
        except Exception:  # WireError, RegistryError, any transport failure
            # degrades to inline; a failed QA call never errors the fetch
            return None
        answer = (resp.message.content or "").strip()
        if not answer:
            return None
        ctx.emit("web_fetch_qa", {"url": source, "chars": len(answer), "truncated": truncated})
        return answer
