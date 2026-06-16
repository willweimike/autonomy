from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser

from ...models import Observation, RiskLevel
from ..redaction import redact_jsonable, redact_sensitive_text


DUCKDUCKGO_HTML_URL = "https://duckduckgo.com/html/"


def _positive_int(value, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed < 1:
        raise ValueError("value must be positive")
    return min(parsed, maximum)


def _search_url(query: str) -> str:
    return DUCKDUCKGO_HTML_URL + "?" + urllib.parse.urlencode({"q": query})


def _fetch_search_html(query: str, *, timeout: int) -> tuple[str, str, int]:
    url = _search_url(query)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Autonomy/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(500_000)
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), response.geturl(), response.status


def _decode_result_url(href: str) -> str:
    href = unescape(href.strip())
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        query = urllib.parse.parse_qs(parsed.query)
        uddg = query.get("uddg", [""])[0]
        if uddg:
            return urllib.parse.unquote(uddg)
    if href.startswith("//"):
        return "https:" + href
    return href


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = {str(name).lower(): str(value) for name, value in attrs if value is not None}
        classes = set(attrs_dict.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self._finish_current()
            href = _decode_result_url(attrs_dict.get("href", ""))
            self._current = {"url": href, "title": "", "snippet": ""}
            self._capture = "title"
            self._parts = []
        elif self._current is not None and ("result__snippet" in classes or "result__body" in classes):
            self._capture = "snippet"
            self._parts = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._current is not None and self._capture == "title" and tag == "a":
            self._current["title"] = _normalize_text(" ".join(self._parts))
            self._capture = None
            self._parts = []
        elif self._current is not None and self._capture == "snippet" and tag in {"a", "div"}:
            snippet = _normalize_text(" ".join(self._parts))
            if snippet:
                self._current["snippet"] = snippet
            self._capture = None
            self._parts = []

    def handle_data(self, data):
        if self._capture is not None:
            self._parts.append(data)

    def close(self):
        super().close()
        self._finish_current()

    def _finish_current(self) -> None:
        if self._current is None:
            return
        url = self._current.get("url", "")
        title = self._current.get("title", "")
        parsed = urllib.parse.urlparse(url)
        if title and parsed.scheme in {"http", "https"} and parsed.netloc:
            self.results.append(dict(self._current))
        self._current = None
        self._capture = None
        self._parts = []


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _parse_results(html: str, *, limit: int) -> list[dict]:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(html)
    parser.close()
    seen: set[str] = set()
    results: list[dict] = []
    for item in parser.results:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "rank": len(results) + 1,
                "title": item["title"][:300],
                "url": url,
                "snippet": item.get("snippet", "")[:1000],
            }
        )
        if len(results) >= limit:
            break
    return results


def _web_search_observation(payload: dict) -> Observation:
    redacted_payload, payload_redacted = redact_jsonable(payload)
    evidence, evidence_redacted = redact_sensitive_text(
        f"web_search:{redacted_payload.get('query')}:{redacted_payload.get('count')}"
    )
    redacted = payload_redacted or evidence_redacted
    return Observation(
        "",
        True,
        output=json.dumps(redacted_payload, sort_keys=True),
        evidence=(evidence, f"web_redacted:{str(redacted).lower()}"),
        side_effects=("network-read",),
    )


def register_web_tools(registry) -> None:
    def validate_search(arguments: dict) -> None:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        _positive_int(arguments.get("limit"), default=5, maximum=10)
        _positive_int(arguments.get("timeout"), default=20, maximum=120)

    def search(arguments: dict) -> Observation:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = _positive_int(arguments.get("limit"), default=5, maximum=10)
        timeout = _positive_int(arguments.get("timeout"), default=20, maximum=120)
        try:
            html, final_url, status = _fetch_search_html(query, timeout=timeout)
            results = _parse_results(html, limit=limit)
            payload = {
                "query": query,
                "status": status,
                "final_url": final_url,
                "results": results,
                "count": len(results),
            }
            return _web_search_observation(payload)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return Observation(
                "",
                False,
                error=f"web search failed: {exc}",
                evidence=(f"web_search_error:{query}",),
                side_effects=("network-read",),
            )

    registry.register(
        "web.search",
        search,
        validate_search,
        description="Search the web and return ranked result titles, URLs, and snippets.",
        toolset="web",
        argument_contract={
            "query": "string",
            "limit": "integer max results, default 5, max 10 (optional)",
            "timeout": "integer seconds, default 20, max 120 (optional)",
        },
        default_risk=RiskLevel.LOW,
        side_effects=("network-read",),
    )
