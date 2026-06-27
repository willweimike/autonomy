import io
import json
import struct
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from autonomy.cli import build_parser, main


def framed(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(body)) + body


class AutonomyNativeChromeHostTest(unittest.TestCase):
    def test_native_message_round_trips_json_object(self):
        from autonomy.chrome_host import read_native_message, write_native_message

        incoming = io.BytesIO(framed({"type": "status"}))
        self.assertEqual(read_native_message(incoming), {"type": "status"})

        outgoing = io.BytesIO()
        write_native_message(outgoing, {"ok": True, "type": "status.result"})

        size = struct.unpack("<I", outgoing.getvalue()[:4])[0]
        payload = json.loads(outgoing.getvalue()[4 : 4 + size].decode("utf-8"))
        self.assertEqual(payload, {"ok": True, "type": "status.result"})

    def test_native_message_rejects_non_object_and_oversized_payload(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        with self.assertRaisesRegex(ChromeHostError, "expected JSON object"):
            read_native_message(io.BytesIO(framed(["bad"])))

        with self.assertRaisesRegex(ChromeHostError, "exceeds"):
            read_native_message(io.BytesIO(framed({"type": "status"})), max_bytes=2)

    def test_native_message_rejects_missing_type(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        with self.assertRaisesRegex(ChromeHostError, "missing type"):
            read_native_message(io.BytesIO(framed({"status": "ok"})))

    def test_native_message_rejects_unknown_type(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        with self.assertRaisesRegex(ChromeHostError, "unknown type"):
            read_native_message(io.BytesIO(framed({"type": "session.start"})))

    def test_native_message_rejects_malformed_json(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        body = b"{not json"
        incoming = io.BytesIO(struct.pack("<I", len(body)) + body)

        with self.assertRaisesRegex(ChromeHostError, "invalid native message payload"):
            read_native_message(incoming)

    def test_native_message_rejects_invalid_utf8(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        body = b"\xff"
        incoming = io.BytesIO(struct.pack("<I", len(body)) + body)

        with self.assertRaisesRegex(ChromeHostError, "invalid native message payload"):
            read_native_message(incoming)

    def test_native_message_rejects_truncated_header(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        with self.assertRaisesRegex(ChromeHostError, "invalid native message header"):
            read_native_message(io.BytesIO(b"\x01\x00"))

    def test_native_message_rejects_truncated_body(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        incoming = io.BytesIO(struct.pack("<I", 4) + b"abc")

        with self.assertRaisesRegex(ChromeHostError, "truncated native message"):
            read_native_message(incoming)

    def test_chrome_host_parser_and_main_delegate_to_host(self):
        args = build_parser().parse_args(["chrome-host"])
        self.assertEqual(args.command, "chrome-host")

        with (
            patch("autonomy.chrome_host.run_chrome_host", return_value=0) as run_host,
            redirect_stdout(io.StringIO()),
        ):
            result = main(["chrome-host"])

        self.assertEqual(result, 0)
        run_host.assert_called_once()
