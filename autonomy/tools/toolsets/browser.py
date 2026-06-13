from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from ...models import Observation, RiskLevel


def _validate_http_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must use http or https")
    if not parsed.netloc:
        raise ValueError("url must include a host")
    return url


def _timeout_ms(value) -> int:
    timeout = int(value if value is not None else 30)
    if timeout < 1:
        raise ValueError("timeout must be at least 1")
    return min(timeout, 120) * 1000


def _non_empty(value, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


@lru_cache(maxsize=1)
def browser_tools_available() -> tuple[bool, str]:
    try:
        spec = importlib.util.find_spec("playwright.sync_api")
    except ModuleNotFoundError:
        spec = None
    if spec is None:
        return False, "playwright package is not installed"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True, ""
    except Exception as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            return (
                False,
                "chromium browser runtime is not installed; run: python3.13 -m playwright install chromium",
            )
        return False, f"playwright chromium unavailable: {message}"


@dataclass
class BrowserController:
    _playwright: Any = field(default=None, init=False, repr=False)
    _browser: Any = field(default=None, init=False, repr=False)
    _page: Any = field(default=None, init=False, repr=False)
    _console_messages: list[dict] = field(default_factory=list, init=False, repr=False)
    _page_errors: list[dict] = field(default_factory=list, init=False, repr=False)

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("playwright package is not installed") from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._page = self._browser.new_page()
        self._attach_page_listeners(self._page)
        return self._page

    def _attach_page_listeners(self, page) -> None:
        try:
            page.on("console", self._record_console_message)
            page.on("pageerror", self._record_page_error)
        except Exception:
            return

    def _record_console_message(self, message) -> None:
        self._console_messages.append(
            {
                "type": self._attribute_or_call(message, "type", "log"),
                "text": self._attribute_or_call(message, "text", ""),
                "location": self._json_safe_value(
                    self._attribute_or_call(message, "location", {})
                ),
            }
        )

    def _record_page_error(self, error) -> None:
        self._page_errors.append(
            {
                "message": str(error),
                "source": "pageerror",
            }
        )

    @staticmethod
    def _attribute_or_call(instance, name: str, default):
        try:
            value = getattr(instance, name)
        except Exception:
            return default
        try:
            return value() if callable(value) else value
        except Exception:
            return default

    @staticmethod
    def _json_safe_value(value):
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return str(value)

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            self._page = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                finally:
                    self._playwright = None

    def navigate(self, url: str, timeout_ms: int) -> dict:
        page = self._ensure_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return self.snapshot(
            extra={
                "status": response.status if response else None,
                "action": "navigate",
            }
        )

    def snapshot(self, extra: dict | None = None) -> dict:
        page = self._ensure_page()
        body_text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
        payload = {
            "url": page.url,
            "title": page.title(),
            "text": body_text[:12000],
            "elements": self._element_inventory(page),
        }
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _element_inventory(page) -> list[dict]:
        try:
            raw = page.evaluate(
                """() => {
                    const cssEscape = window.CSS && window.CSS.escape
                        ? window.CSS.escape
                        : (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&");
                    const uniqueSelector = (element) => {
                        const tag = element.tagName.toLowerCase();
                        const attrSelector = (name) => {
                            const value = element.getAttribute(name);
                            return value ? `${tag}[${name}=${JSON.stringify(value)}]` : "";
                        };
                        if (element.id) {
                            return `#${cssEscape(element.id)}`;
                        }
                        for (const name of ["data-testid", "data-test", "data-cy", "name", "aria-label"]) {
                            const selector = attrSelector(name);
                            if (selector) {
                                return selector;
                            }
                        }
                        let selector = tag;
                        const type = element.getAttribute("type");
                        if (type) {
                            selector += `[type=${JSON.stringify(type)}]`;
                        }
                        let current = element;
                        while (current && current.parentElement) {
                            const siblings = Array.from(current.parentElement.children)
                                .filter((candidate) => candidate.tagName === current.tagName);
                            if (siblings.length > 1) {
                                selector += `:nth-of-type(${siblings.indexOf(current) + 1})`;
                            }
                            break;
                        }
                        return selector;
                    };
                    const candidates = Array.from(document.querySelectorAll(
                        "a[href],button,input,textarea,select,[role='button'],[role='link'],[contenteditable='true']"
                    ));
                    return candidates
                        .filter((element) => {
                            const rect = element.getBoundingClientRect();
                            const style = window.getComputedStyle(element);
                            return rect.width > 0
                                && rect.height > 0
                                && style.visibility !== "hidden"
                                && style.display !== "none";
                        })
                        .slice(0, 50)
                        .map((element) => ({
                            selector: uniqueSelector(element),
                            tag: element.tagName.toLowerCase(),
                            text: (element.innerText || element.value || "").trim().slice(0, 160),
                            role: element.getAttribute("role") || "",
                            aria_label: element.getAttribute("aria-label") || "",
                            name: element.getAttribute("name") || "",
                            placeholder: element.getAttribute("placeholder") || "",
                            type: element.getAttribute("type") || "",
                            href: element.getAttribute("href") || "",
                            disabled: Boolean(element.disabled || element.getAttribute("aria-disabled") === "true")
                        }));
                }"""
            )
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        elements: list[dict] = []
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("selector"), str):
                elements.append(
                    {
                        "selector": str(item.get("selector", "")),
                        "tag": str(item.get("tag", "")),
                        "text": str(item.get("text", "")),
                        "role": str(item.get("role", "")),
                        "aria_label": str(item.get("aria_label", "")),
                        "name": str(item.get("name", "")),
                        "placeholder": str(item.get("placeholder", "")),
                        "type": str(item.get("type", "")),
                        "href": str(item.get("href", "")),
                        "disabled": bool(item.get("disabled", False)),
                    }
                )
        return elements

    def click(self, selector: str, timeout_ms: int) -> dict:
        page = self._ensure_page()
        page.locator(selector).first.click(timeout=timeout_ms)
        return self.snapshot(extra={"action": "click", "selector": selector})

    def type_text(self, selector: str, text: str, timeout_ms: int) -> dict:
        page = self._ensure_page()
        locator = page.locator(selector).first
        locator.fill(text, timeout=timeout_ms)
        return self.snapshot(extra={"action": "type", "selector": selector})

    def scroll(self, direction: str) -> dict:
        page = self._ensure_page()
        delta = -700 if direction == "up" else 700
        page.evaluate("(amount) => window.scrollBy(0, amount)", delta)
        return self.snapshot(extra={"action": "scroll", "direction": direction})

    def back(self) -> dict:
        page = self._ensure_page()
        page.go_back(wait_until="domcontentloaded")
        return self.snapshot(extra={"action": "back"})

    def press(self, key: str) -> dict:
        page = self._ensure_page()
        page.keyboard.press(key)
        return self.snapshot(extra={"action": "press", "key": key})

    def get_images(self) -> dict:
        page = self._ensure_page()
        raw = page.evaluate(
            """() => {
                const cssEscape = window.CSS && window.CSS.escape
                    ? window.CSS.escape
                    : (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&");
                const selectorFor = (image) => {
                    if (image.id) {
                        return `#${cssEscape(image.id)}`;
                    }
                    const alt = image.getAttribute("alt");
                    if (alt) {
                        return `img[alt=${JSON.stringify(alt)}]`;
                    }
                    const src = image.getAttribute("src");
                    if (src) {
                        return `img[src=${JSON.stringify(src)}]`;
                    }
                    return "img";
                };
                return Array.from(document.images)
                    .map((image) => ({
                        src: image.currentSrc || image.src || "",
                        alt: image.alt || "",
                        width: Number(image.naturalWidth || image.width || 0),
                        height: Number(image.naturalHeight || image.height || 0),
                        selector: selectorFor(image),
                    }))
                    .filter((image) => image.src && !image.src.startsWith("data:"))
                    .slice(0, 100);
            }"""
        )
        images: list[dict] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("src", "")).strip()
                if not src or src.startswith("data:"):
                    continue
                images.append(
                    {
                        "src": src,
                        "alt": str(item.get("alt", "")),
                        "width": int(item.get("width") or 0),
                        "height": int(item.get("height") or 0),
                        "selector": str(item.get("selector", "")),
                    }
                )
        return {
            "url": page.url,
            "title": page.title(),
            "images": images,
            "count": len(images),
            "action": "get_images",
        }

    def console(self, *, clear: bool = False, expression: str | None = None) -> dict:
        page = self._ensure_page()
        if expression is not None:
            try:
                result = page.evaluate(expression)
                safe_result = self._json_safe_value(result)
                payload = {
                    "success": True,
                    "url": page.url,
                    "expression": expression,
                    "result": safe_result,
                    "result_type": type(safe_result).__name__,
                    "action": "console",
                }
            except Exception as exc:
                payload = {
                    "success": False,
                    "url": page.url,
                    "expression": expression,
                    "error": f"{type(exc).__name__}: {exc}",
                    "action": "console",
                }
            if clear:
                self._clear_console_buffers()
            return payload

        payload = {
            "success": True,
            "url": page.url,
            "console_messages": list(self._console_messages),
            "page_errors": list(self._page_errors),
            "total_messages": len(self._console_messages),
            "total_errors": len(self._page_errors),
            "action": "console",
        }
        if clear:
            self._clear_console_buffers()
        return payload

    def _clear_console_buffers(self) -> None:
        self._console_messages.clear()
        self._page_errors.clear()


def _observation(payload: dict, evidence: str) -> Observation:
    return Observation(
        "",
        bool(payload.get("success", True)),
        output=json.dumps(payload, sort_keys=True),
        error=str(payload.get("error", "")),
        evidence=(evidence,),
        side_effects=("browser-state", "network-read"),
    )


def register_browser_tools(registry, controller: BrowserController, *, availability_check) -> None:
    def validate_navigate(arguments: dict) -> None:
        _validate_http_url(str(arguments["url"]))
        _timeout_ms(arguments.get("timeout"))

    def validate_no_args(arguments: dict) -> None:
        if arguments:
            unexpected = ", ".join(sorted(arguments))
            raise ValueError(f"unexpected arguments: {unexpected}")

    def validate_selector(arguments: dict) -> None:
        _non_empty(arguments["selector"], "selector")
        _timeout_ms(arguments.get("timeout"))

    def validate_type(arguments: dict) -> None:
        _non_empty(arguments["selector"], "selector")
        if "text" not in arguments:
            raise ValueError("text is required")
        _timeout_ms(arguments.get("timeout"))

    def validate_scroll(arguments: dict) -> None:
        direction = str(arguments.get("direction", "down")).strip().lower()
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")

    def validate_press(arguments: dict) -> None:
        _non_empty(arguments["key"], "key")

    def validate_console(arguments: dict) -> None:
        allowed = {"clear", "expression"}
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise ValueError(f"unexpected arguments: {', '.join(unexpected)}")
        if "expression" in arguments:
            _non_empty(arguments["expression"], "expression")

    def navigate(arguments: dict) -> Observation:
        url = _validate_http_url(str(arguments["url"]))
        payload = controller.navigate(url, _timeout_ms(arguments.get("timeout")))
        return _observation(payload, f"browser_navigate:{payload.get('url', url)}")

    def snapshot(arguments: dict) -> Observation:
        validate_no_args(arguments)
        payload = controller.snapshot(extra={"action": "snapshot"})
        return _observation(payload, f"browser_snapshot:{payload.get('url', '')}")

    def click(arguments: dict) -> Observation:
        selector = _non_empty(arguments["selector"], "selector")
        payload = controller.click(selector, _timeout_ms(arguments.get("timeout")))
        return _observation(payload, f"browser_click:{selector}")

    def type_text(arguments: dict) -> Observation:
        selector = _non_empty(arguments["selector"], "selector")
        payload = controller.type_text(
            selector,
            str(arguments["text"]),
            _timeout_ms(arguments.get("timeout")),
        )
        return _observation(payload, f"browser_type:{selector}")

    def scroll(arguments: dict) -> Observation:
        direction = str(arguments.get("direction", "down")).strip().lower()
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        payload = controller.scroll(direction)
        return _observation(payload, f"browser_scroll:{direction}")

    def back(arguments: dict) -> Observation:
        validate_no_args(arguments)
        payload = controller.back()
        return _observation(payload, f"browser_back:{payload.get('url', '')}")

    def press(arguments: dict) -> Observation:
        key = _non_empty(arguments["key"], "key")
        payload = controller.press(key)
        return _observation(payload, f"browser_press:{key}")

    def get_images(arguments: dict) -> Observation:
        validate_no_args(arguments)
        payload = controller.get_images()
        return _observation(payload, f"browser_get_images:{payload.get('url', '')}")

    def console(arguments: dict) -> Observation:
        validate_console(arguments)
        expression = arguments.get("expression")
        payload = controller.console(
            clear=bool(arguments.get("clear", False)),
            expression=str(expression) if expression is not None else None,
        )
        return _observation(payload, f"browser_console:{payload.get('url', '')}")

    common = {
        "toolset": "browser",
        "default_risk": RiskLevel.MEDIUM,
        "side_effects": ("browser-state", "network-read"),
        "availability_check": availability_check,
    }
    registry.register(
        "browser.navigate",
        navigate,
        validate_navigate,
        description="Navigate a headless Chromium page to an HTTP or HTTPS URL.",
        argument_contract={"url": "string", "timeout": "integer (optional)"},
        **common,
    )
    registry.register(
        "browser.snapshot",
        snapshot,
        validate_no_args,
        description="Return the current browser page title, URL, and visible text.",
        argument_contract={},
        **common,
    )
    registry.register(
        "browser.click",
        click,
        validate_selector,
        description="Click the first element matching a CSS selector.",
        argument_contract={"selector": "string", "timeout": "integer (optional)"},
        **common,
    )
    registry.register(
        "browser.type",
        type_text,
        validate_type,
        description="Fill the first element matching a CSS selector with text.",
        argument_contract={"selector": "string", "text": "string", "timeout": "integer (optional)"},
        **common,
    )
    registry.register(
        "browser.scroll",
        scroll,
        validate_scroll,
        description="Scroll the current browser page up or down.",
        argument_contract={"direction": "up|down (optional)"},
        **common,
    )
    registry.register(
        "browser.back",
        back,
        validate_no_args,
        description="Navigate the current browser page back.",
        argument_contract={},
        **common,
    )
    registry.register(
        "browser.press",
        press,
        validate_press,
        description="Press a keyboard key in the current browser page.",
        argument_contract={"key": "string"},
        **common,
    )
    registry.register(
        "browser.get_images",
        get_images,
        validate_no_args,
        description="Return image URLs, alt text, dimensions, and selectors from the current browser page.",
        argument_contract={},
        **common,
    )
    registry.register(
        "browser.console",
        console,
        validate_console,
        description="Return browser console messages and page errors, or evaluate a diagnostic JavaScript expression.",
        argument_contract={"clear": "boolean (optional)", "expression": "string (optional)"},
        **common,
    )
