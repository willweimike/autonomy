from __future__ import annotations

import importlib.util
import json
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ...models import Observation, RiskLevel
from ..redaction import redact_jsonable, redact_sensitive_text

_DEFAULT_SNAPSHOT_CHARS = 12_000
_FULL_SNAPSHOT_CHARS = 50_000
_MAX_SNAPSHOT_CHARS = 50_000
_MAX_AVAILABILITY_REASON_CHARS = 500


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


def _snapshot_limit(arguments: dict) -> int:
    default = _FULL_SNAPSHOT_CHARS if bool(arguments.get("full", False)) else _DEFAULT_SNAPSHOT_CHARS
    limit = int(arguments.get("max_chars", default))
    if limit < 1:
        raise ValueError("max_chars must be at least 1")
    return min(limit, _MAX_SNAPSHOT_CHARS)


def _compact_browser_unavailable_reason(message: str) -> str:
    text = str(message).strip()
    if "Browser logs:" in text:
        text = text.split("Browser logs:", 1)[0].strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = " ".join(lines) if lines else text
    if len(compact) > _MAX_AVAILABILITY_REASON_CHARS:
        compact = compact[: _MAX_AVAILABILITY_REASON_CHARS - 3].rstrip() + "..."
    return compact


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
        compact = _compact_browser_unavailable_reason(message)
        return False, f"playwright chromium unavailable: {compact}"


@dataclass
class BrowserController:
    screenshot_dir: Path | None = None
    _playwright: Any = field(default=None, init=False, repr=False)
    _browser: Any = field(default=None, init=False, repr=False)
    _page: Any = field(default=None, init=False, repr=False)
    _console_messages: list[dict] = field(default_factory=list, init=False, repr=False)
    _page_errors: list[dict] = field(default_factory=list, init=False, repr=False)
    _dialogs: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _pending_dialogs: list[dict] = field(default_factory=list, init=False, repr=False)

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
            page.on("dialog", self._record_dialog)
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

    def _record_dialog(self, dialog) -> None:
        dialog_id = f"dialog_{uuid.uuid4().hex[:12]}"
        payload = {
            "id": dialog_id,
            "type": self._attribute_or_call(dialog, "type", ""),
            "message": self._attribute_or_call(dialog, "message", ""),
            "default_value": self._attribute_or_call(dialog, "default_value", ""),
        }
        self._dialogs[dialog_id] = dialog
        self._pending_dialogs.append(payload)

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

    def snapshot(
        self,
        extra: dict | None = None,
        *,
        full: bool = False,
        max_chars: int = _DEFAULT_SNAPSHOT_CHARS,
    ) -> dict:
        page = self._ensure_page()
        pending_dialogs = list(self._pending_dialogs)
        if pending_dialogs:
            body_text = ""
            elements = []
            truncated = False
            full_text_chars = 0
        else:
            full_text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            full_text_chars = len(full_text)
            truncated = full_text_chars > max_chars
            body_text = full_text[:max_chars]
            elements = self._element_inventory(page)
        payload = {
            "url": page.url,
            "title": page.title(),
            "text": body_text,
            "full": full,
            "max_chars": max_chars,
            "text_chars": full_text_chars,
            "text_truncated": truncated,
            "elements": elements,
            "pending_dialogs": pending_dialogs,
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

    def screenshot(self, *, full_page: bool = True) -> dict:
        page = self._ensure_page()
        screenshot_dir = self.screenshot_dir or Path.cwd() / ".autonomy" / "browser-screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / f"browser_screenshot_{uuid.uuid4().hex}.png"
        page.screenshot(path=str(screenshot_path), full_page=full_page)
        try:
            size = screenshot_path.stat().st_size
        except OSError:
            size = 0
        return {
            "success": screenshot_path.is_file(),
            "url": page.url,
            "title": page.title(),
            "path": str(screenshot_path),
            "bytes": size,
            "full_page": full_page,
            "action": "screenshot",
        }

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

    def dialog(
        self,
        *,
        action: str,
        prompt_text: str = "",
        dialog_id: str = "",
    ) -> dict:
        page = self._ensure_page()
        del page
        dialog_payload = self._select_dialog_payload(dialog_id)
        if dialog_payload is None:
            return {
                "success": False,
                "error": "no pending browser dialog",
                "action": "dialog",
                "pending_dialogs": list(self._pending_dialogs),
            }
        effective_dialog_id = str(dialog_payload["id"])
        dialog = self._dialogs.get(effective_dialog_id)
        if dialog is None:
            return {
                "success": False,
                "error": f"pending dialog is no longer available: {effective_dialog_id}",
                "action": "dialog",
                "pending_dialogs": list(self._pending_dialogs),
            }
        try:
            if action == "accept":
                dialog.accept(prompt_text)
            elif action == "dismiss":
                dialog.dismiss()
            else:
                raise ValueError("action must be accept or dismiss")
        except Exception as exc:
            return {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "action": "dialog",
                "dialog": dialog_payload,
                "pending_dialogs": list(self._pending_dialogs),
            }
        self._dialogs.pop(effective_dialog_id, None)
        self._pending_dialogs = [
            item for item in self._pending_dialogs if item.get("id") != effective_dialog_id
        ]
        return {
            "success": True,
            "action": "dialog",
            "dialog_action": action,
            "dialog": dialog_payload,
            "pending_dialogs": list(self._pending_dialogs),
        }

    def _select_dialog_payload(self, dialog_id: str = "") -> dict | None:
        if not self._pending_dialogs:
            return None
        if dialog_id:
            for item in self._pending_dialogs:
                if item.get("id") == dialog_id:
                    return item
            return None
        return self._pending_dialogs[0]


def _observation(payload: dict, evidence: str) -> Observation:
    redacted_payload, payload_redacted = redact_jsonable(payload)
    redacted_evidence, evidence_redacted = redact_sensitive_text(evidence)
    redacted = payload_redacted or evidence_redacted
    return Observation(
        "",
        bool(payload.get("success", True)),
        output=json.dumps(redacted_payload, sort_keys=True),
        error=str(redacted_payload.get("error", "")),
        evidence=(redacted_evidence, f"browser_redacted:{str(redacted).lower()}"),
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

    def validate_snapshot(arguments: dict) -> None:
        allowed = {"full", "max_chars"}
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise ValueError(f"unexpected arguments: {', '.join(unexpected)}")
        if "full" in arguments and not isinstance(arguments["full"], bool):
            raise ValueError("full must be a boolean")
        _snapshot_limit(arguments)

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

    def validate_screenshot(arguments: dict) -> None:
        allowed = {"full_page"}
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise ValueError(f"unexpected arguments: {', '.join(unexpected)}")
        if "full_page" in arguments and not isinstance(arguments["full_page"], bool):
            raise ValueError("full_page must be a boolean")

    def validate_console(arguments: dict) -> None:
        allowed = {"clear", "expression"}
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise ValueError(f"unexpected arguments: {', '.join(unexpected)}")
        if "expression" in arguments:
            _non_empty(arguments["expression"], "expression")

    def validate_dialog(arguments: dict) -> None:
        allowed = {"action", "prompt_text", "dialog_id"}
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise ValueError(f"unexpected arguments: {', '.join(unexpected)}")
        action = _non_empty(arguments["action"], "action")
        if action not in {"accept", "dismiss"}:
            raise ValueError("action must be accept or dismiss")
        if "prompt_text" in arguments and not isinstance(arguments["prompt_text"], str):
            raise ValueError("prompt_text must be a string")
        if "dialog_id" in arguments and not isinstance(arguments["dialog_id"], str):
            raise ValueError("dialog_id must be a string")

    def navigate(arguments: dict) -> Observation:
        url = _validate_http_url(str(arguments["url"]))
        payload = controller.navigate(url, _timeout_ms(arguments.get("timeout")))
        return _observation(payload, f"browser_navigate:{payload.get('url', url)}")

    def snapshot(arguments: dict) -> Observation:
        validate_snapshot(arguments)
        full = bool(arguments.get("full", False))
        payload = controller.snapshot(
            extra={"action": "snapshot"},
            full=full,
            max_chars=_snapshot_limit(arguments),
        )
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

    def screenshot(arguments: dict) -> Observation:
        validate_screenshot(arguments)
        payload = controller.screenshot(full_page=bool(arguments.get("full_page", True)))
        return _observation(payload, f"browser_screenshot:{payload.get('path', '')}")

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

    def dialog(arguments: dict) -> Observation:
        validate_dialog(arguments)
        payload = controller.dialog(
            action=str(arguments["action"]),
            prompt_text=str(arguments.get("prompt_text", "")),
            dialog_id=str(arguments.get("dialog_id", "")),
        )
        dialog_payload = payload.get("dialog", {})
        dialog_id = dialog_payload.get("id", "") if isinstance(dialog_payload, dict) else ""
        return _observation(payload, f"browser_dialog:{dialog_id}")

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
        validate_snapshot,
        description="Return the current browser page title, URL, bounded visible text, and actionable elements.",
        argument_contract={
            "full": "boolean, use larger default text window when true (optional)",
            "max_chars": "integer max visible text chars, default 12000 compact or 50000 full (optional)",
        },
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
        "browser.screenshot",
        screenshot,
        validate_screenshot,
        description="Capture the current browser page as a PNG file under the workspace .autonomy directory.",
        argument_contract={"full_page": "boolean, default true (optional)"},
        **{**common, "side_effects": ("browser-state", "network-read", "file-write")},
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
    registry.register(
        "browser.dialog",
        dialog,
        validate_dialog,
        description="Accept or dismiss a pending native JavaScript browser dialog reported by browser.snapshot.",
        argument_contract={
            "action": "accept|dismiss",
            "prompt_text": "string (optional)",
            "dialog_id": "string (optional)",
        },
        **common,
    )
