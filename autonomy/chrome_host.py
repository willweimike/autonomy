from __future__ import annotations

import json
import struct
import sys
import threading
from typing import Any, BinaryIO, Mapping, Protocol


MAX_NATIVE_MESSAGE_BYTES = 1_000_000

_REQUEST_TYPES = {
    "status",
    "session.start",
    "chat.send",
    "run.inspect",
    "approval.respond",
}


class ChromeHostError(ValueError):
    pass


class ChromeBridge(Protocol):
    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        ...


class NativeMessageWriter:
    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self._lock = threading.Lock()

    def send(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            write_native_message(self.stream, payload)


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
    writer = NativeMessageWriter(output_stream)
    if hasattr(api, "set_event_sink"):
        api.set_event_sink(writer.send)
    workers: list[threading.Thread] = []

    def handle_in_worker(message: dict[str, Any]) -> None:
        try:
            writer.send(api.handle(message))
        except Exception as exc:
            writer.send({"ok": False, "error": str(exc)})

    while True:
        try:
            message = read_native_message(input_stream)
            if message is None:
                for worker in workers:
                    worker.join()
                return 0
            if message["type"] == "chat.send":
                worker = threading.Thread(target=handle_in_worker, args=(message,))
                worker.start()
                workers.append(worker)
                continue
            response = api.handle(message)
        except ChromeHostError as exc:
            try:
                writer.send({"ok": False, "error": str(exc)})
            finally:
                return 1
        except Exception as exc:
            writer.send({"ok": False, "error": str(exc)})
            return 1
        for event in getattr(api, "pop_events", lambda: [])():
            writer.send(event)
        writer.send(response)
