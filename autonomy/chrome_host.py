from __future__ import annotations

import json
import struct
import sys
from typing import Any, BinaryIO, Mapping, Protocol


MAX_NATIVE_MESSAGE_BYTES = 1_000_000

_REQUEST_TYPES = {
    "status",
    "session.start",
    "chat.send",
    "run.inspect",
}


class ChromeHostError(ValueError):
    pass


class ChromeBridge(Protocol):
    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        ...


def read_native_message(
    stream: BinaryIO,
    *,
    max_bytes: int = MAX_NATIVE_MESSAGE_BYTES,
) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ChromeHostError("invalid native message header")
    size = struct.unpack("<I", header)[0]
    if size > max_bytes:
        raise ChromeHostError(f"native message exceeds {max_bytes} bytes")
    body = stream.read(size)
    if len(body) != size:
        raise ChromeHostError("truncated native message")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChromeHostError(f"invalid native message payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ChromeHostError("expected JSON object")
    message_type = payload.get("type")
    if message_type is None:
        raise ChromeHostError("missing type")
    if not isinstance(message_type, str) or message_type not in _REQUEST_TYPES:
        raise ChromeHostError(f"unknown type: {message_type}")
    return payload


def write_native_message(stream: BinaryIO, payload: Mapping[str, Any]) -> None:
    body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("<I", len(body)))
    stream.write(body)
    stream.flush()


def run_chrome_host(
    *,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    api: ChromeBridge | None = None,
) -> int:
    from .chrome_api import ChromeSessionBridge

    input_stream = sys.stdin.buffer if input_stream is None else input_stream
    output_stream = sys.stdout.buffer if output_stream is None else output_stream
    api = ChromeSessionBridge() if api is None else api
    while True:
        try:
            message = read_native_message(input_stream)
            if message is None:
                return 0
            response = api.handle(message)
        except ChromeHostError as exc:
            try:
                write_native_message(output_stream, {"ok": False, "error": str(exc)})
            finally:
                return 1
        except Exception as exc:
            write_native_message(output_stream, {"ok": False, "error": str(exc)})
            return 1
        write_native_message(output_stream, response)
