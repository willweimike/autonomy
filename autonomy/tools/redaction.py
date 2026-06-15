from __future__ import annotations

import re
from typing import Any


_PREFIX_PATTERNS = (
    r"sk-[A-Za-z0-9_-]{10,}",
    r"sk_[A-Za-z0-9_]{10,}",
    r"github_pat_[A-Za-z0-9_]{10,}",
    r"gh[pousr]_[A-Za-z0-9]{10,}",
    r"AIza[A-Za-z0-9_-]{30,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"hf_[A-Za-z0-9]{10,}",
    r"gsk_[A-Za-z0-9]{10,}",
    r"pypi-[A-Za-z0-9_-]{10,}",
    r"npm_[A-Za-z0-9]{10,}",
    r"AKIA[A-Z0-9]{16}",
    r"xai-[A-Za-z0-9]{30,}",
)
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)
_SECRET_NAME_RE = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Za-z0-9_]{{0,50}}{_SECRET_NAME_RE}[A-Za-z0-9_]{{0,50}})\s*=\s*(['\"]?)([^\s'\"&]+)\2",
    re.IGNORECASE,
)
_JSON_FIELD_RE = re.compile(
    r'("(?:api_?key|token|secret|password|access_token|refresh_token|auth_token|authorization)")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
_JSON_SECRET_KEYS = {
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "authorization",
    "client_secret",
}
_AUTH_HEADER_RE = re.compile(r"(Authorization:\s*Bearer\s+)(\S+)", re.IGNORECASE)
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)
_URL_QUERY_SECRET_RE = re.compile(
    r"([?&](?:access_token|refresh_token|id_token|token|api_key|apikey|client_secret|password|secret|key)=)([^&#\s]+)",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)


def _mask(value: str) -> str:
    if len(value) < 18:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def redact_sensitive_text(text: str) -> tuple[str, bool]:
    """Redact common secrets from command and process output."""
    if not text:
        return text, False
    redacted = text
    redacted = _PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", redacted)
    redacted = _PREFIX_RE.sub(lambda match: _mask(match.group(1)), redacted)
    redacted = _ENV_ASSIGN_RE.sub(
        lambda match: f"{match.group(1)}={match.group(2)}***{match.group(2)}",
        redacted,
    )
    redacted = _JSON_FIELD_RE.sub(lambda match: f'{match.group(1)}: "***"', redacted)
    redacted = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}***", redacted)
    redacted = _DB_CONNSTR_RE.sub(lambda match: f"{match.group(1)}***{match.group(3)}", redacted)
    redacted = _URL_QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}***", redacted)
    return redacted, redacted != text


def redact_jsonable(value: Any) -> tuple[Any, bool]:
    """Redact secret-like strings from JSON-serializable payloads."""
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _JSON_SECRET_KEYS and item not in ("", None):
                redacted_item, item_changed = "***", True
            else:
                redacted_item, item_changed = redact_jsonable(item)
            redacted[key] = redacted_item
            changed = changed or item_changed
        return redacted, changed
    if isinstance(value, list):
        redacted_items = []
        changed = False
        for item in value:
            redacted_item, item_changed = redact_jsonable(item)
            redacted_items.append(redacted_item)
            changed = changed or item_changed
        return redacted_items, changed
    if isinstance(value, tuple):
        redacted_items = []
        changed = False
        for item in value:
            redacted_item, item_changed = redact_jsonable(item)
            redacted_items.append(redacted_item)
            changed = changed or item_changed
        return tuple(redacted_items), changed
    return value, False
