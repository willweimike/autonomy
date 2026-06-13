from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from ...models import Observation, RiskLevel


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


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        del attrs
        if tag.lower() in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def _extract_text(html: str, max_chars: int) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()[:max_chars]


def register_web_tools(registry) -> None:
    def validate(arguments: dict) -> None:
        _validate_http_url(str(arguments["url"]))
        _positive_int(arguments.get("timeout"), default=20, maximum=120)
        _positive_int(arguments.get("max_chars"), default=20000, maximum=200000)

    def fetch(arguments: dict) -> Observation:
        url = _validate_http_url(str(arguments["url"]))
        timeout = _positive_int(arguments.get("timeout"), default=20, maximum=120)
        max_chars = _positive_int(arguments.get("max_chars"), default=20000, maximum=200000)
        payload = _fetch_url(url, timeout=timeout, max_chars=max_chars)
        return Observation(
            "",
            200 <= int(payload["status"]) < 400,
            output=json.dumps(payload, sort_keys=True),
            evidence=(f"web_fetch:{payload['status']}:{payload['final_url']}",),
            side_effects=("network-read",),
        )

    def extract(arguments: dict) -> Observation:
        url = _validate_http_url(str(arguments["url"]))
        timeout = _positive_int(arguments.get("timeout"), default=20, maximum=120)
        max_chars = _positive_int(arguments.get("max_chars"), default=20000, maximum=200000)
        payload = _fetch_url(url, timeout=timeout, max_chars=max_chars)
        text = _extract_text(str(payload["body"]), max_chars)
        output = {
            "status": payload["status"],
            "content_type": payload["content_type"],
            "final_url": payload["final_url"],
            "truncated": payload["truncated"] or len(text) >= max_chars,
            "text": text,
        }
        return Observation(
            "",
            200 <= int(payload["status"]) < 400,
            output=json.dumps(output, sort_keys=True),
            evidence=(f"web_extract:{payload['status']}:{payload['final_url']}",),
            side_effects=("network-read",),
        )

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
