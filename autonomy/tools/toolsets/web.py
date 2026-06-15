from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from ...models import Observation, RiskLevel
from ..redaction import redact_jsonable, redact_sensitive_text


def _validate_http_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    if not parsed.netloc:
        raise ValueError("url must include a host")
    return url


def _positive_int(value, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed < 1:
        raise ValueError("value must be positive")
    return min(parsed, maximum)


def _fetch_url(url: str, *, timeout: int, max_chars: int) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Autonomy/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_chars + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw[:max_chars].decode(charset, errors="replace")
            return {
                "status": response.status,
                "content_type": response.headers.get("content-type", ""),
                "final_url": response.geturl(),
                "truncated": len(raw) > max_chars,
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read(max_chars + 1)
        charset = exc.headers.get_content_charset() if exc.headers else None
        body = raw[:max_chars].decode(charset or "utf-8", errors="replace")
        return {
            "status": exc.code,
            "content_type": exc.headers.get("content-type", "") if exc.headers else "",
            "final_url": exc.geturl(),
            "truncated": len(raw) > max_chars,
            "body": body,
        }


class _PageExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self._base_url = base_url
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._parts: list[str] = []
        self._active_link: dict | None = None
        self._links: list[dict] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = {str(name).lower(): str(value) for name, value in attrs if value is not None}
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and not self._skip_depth:
            href = attrs_dict.get("href", "").strip()
            absolute = urllib.parse.urljoin(self._base_url, href)
            parsed = urllib.parse.urlparse(absolute)
            if href and parsed.scheme in {"http", "https"} and parsed.netloc:
                self._active_link = {"url": absolute, "text_parts": []}

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._active_link is not None:
            text = re.sub(r"\s+", " ", " ".join(self._active_link["text_parts"])).strip()
            self._links.append({"url": self._active_link["url"], "text": text})
            self._active_link = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._title_parts.append(text)
        elif self._active_link is not None:
            self._active_link["text_parts"].append(text)
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()

    def title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._title_parts)).strip()

    def links(self, max_links: int) -> list[dict]:
        seen: set[str] = set()
        links: list[dict] = []
        for item in self._links:
            url = str(item["url"])
            if url in seen:
                continue
            seen.add(url)
            links.append({"url": url, "text": str(item.get("text", ""))[:240]})
            if len(links) >= max_links:
                break
        return links


def _extract_text(html: str, max_chars: int) -> str:
    return _extract_page(html, "", max_chars=max_chars, max_links=0)["text"]


def _extract_page(html: str, base_url: str, *, max_chars: int, max_links: int) -> dict:
    parser = _PageExtractor(base_url)
    parser.feed(html)
    text = parser.text()
    links = parser.links(max_links) if max_links > 0 else []
    return {
        "title": parser.title(),
        "text": text[:max_chars],
        "text_truncated": len(text) > max_chars,
        "links": links,
        "links_truncated": max_links > 0 and len(parser.links(max_links + 1)) > max_links,
    }


def _web_observation(payload: dict, *, evidence_prefix: str) -> Observation:
    redacted_payload, payload_redacted = redact_jsonable(payload)
    final_url = str(redacted_payload.get("final_url", ""))
    evidence, evidence_redacted = redact_sensitive_text(
        f"{evidence_prefix}:{redacted_payload.get('status')}:{final_url}"
    )
    if "count" in redacted_payload:
        evidence = f"{evidence}:{redacted_payload['count']}"
    redacted = payload_redacted or evidence_redacted
    return Observation(
        "",
        200 <= int(payload["status"]) < 400,
        output=json.dumps(redacted_payload, sort_keys=True),
        evidence=(evidence, f"web_redacted:{str(redacted).lower()}"),
        side_effects=("network-read",),
    )


def register_web_tools(registry) -> None:
    def validate(arguments: dict) -> None:
        _validate_http_url(str(arguments["url"]))
        _positive_int(arguments.get("timeout"), default=20, maximum=120)
        _positive_int(arguments.get("max_chars"), default=20000, maximum=200000)

    def validate_links(arguments: dict) -> None:
        _validate_http_url(str(arguments["url"]))
        _positive_int(arguments.get("timeout"), default=20, maximum=120)
        _positive_int(arguments.get("max_links"), default=100, maximum=500)

    def fetch(arguments: dict) -> Observation:
        url = _validate_http_url(str(arguments["url"]))
        timeout = _positive_int(arguments.get("timeout"), default=20, maximum=120)
        max_chars = _positive_int(arguments.get("max_chars"), default=20000, maximum=200000)
        payload = _fetch_url(url, timeout=timeout, max_chars=max_chars)
        return _web_observation(payload, evidence_prefix="web_fetch")

    def extract(arguments: dict) -> Observation:
        url = _validate_http_url(str(arguments["url"]))
        timeout = _positive_int(arguments.get("timeout"), default=20, maximum=120)
        max_chars = _positive_int(arguments.get("max_chars"), default=20000, maximum=200000)
        payload = _fetch_url(url, timeout=timeout, max_chars=max_chars)
        page = _extract_page(str(payload["body"]), str(payload["final_url"]), max_chars=max_chars, max_links=0)
        output = {
            "status": payload["status"],
            "content_type": payload["content_type"],
            "final_url": payload["final_url"],
            "truncated": payload["truncated"] or page["text_truncated"],
            "title": page["title"],
            "text": page["text"],
        }
        return _web_observation(output, evidence_prefix="web_extract")

    def links(arguments: dict) -> Observation:
        url = _validate_http_url(str(arguments["url"]))
        timeout = _positive_int(arguments.get("timeout"), default=20, maximum=120)
        max_links = _positive_int(arguments.get("max_links"), default=100, maximum=500)
        payload = _fetch_url(url, timeout=timeout, max_chars=200000)
        page = _extract_page(str(payload["body"]), str(payload["final_url"]), max_chars=0, max_links=max_links)
        output = {
            "status": payload["status"],
            "content_type": payload["content_type"],
            "final_url": payload["final_url"],
            "title": page["title"],
            "links": page["links"],
            "count": len(page["links"]),
            "truncated": payload["truncated"] or page["links_truncated"],
        }
        return _web_observation(output, evidence_prefix="web_links")

    registry.register(
        "web.fetch",
        fetch,
        validate,
        description="Fetch an HTTP or HTTPS URL and return metadata plus body text.",
        toolset="web",
        argument_contract={
            "url": "string",
            "timeout": "integer (optional)",
            "max_chars": "integer (optional)",
        },
        default_risk=RiskLevel.LOW,
        side_effects=("network-read",),
    )
    registry.register(
        "web.extract",
        extract,
        validate,
        description="Fetch an HTTP or HTTPS URL and return extracted page text.",
        toolset="web",
        argument_contract={
            "url": "string",
            "timeout": "integer (optional)",
            "max_chars": "integer (optional)",
        },
        default_risk=RiskLevel.LOW,
        side_effects=("network-read",),
    )
    registry.register(
        "web.links",
        links,
        validate_links,
        description="Fetch an HTTP or HTTPS URL and return page links with absolute URLs and anchor text.",
        toolset="web",
        argument_contract={
            "url": "string",
            "timeout": "integer (optional)",
            "max_links": "integer max links, default 100, max 500 (optional)",
        },
        default_risk=RiskLevel.LOW,
        side_effects=("network-read",),
    )
