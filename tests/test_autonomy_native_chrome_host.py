import io
import json
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autonomy.cli import build_parser, main
from autonomy.models import ConversationResponse, RunResult, TerminationReason


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
            read_native_message(io.BytesIO(framed({"type": "nope"})))

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


class FakeConversation:
    def __init__(
        self,
        *,
        workspace,
        db_path,
        max_steps,
        agent_loop_factory,
        responder,
        store=None,
        session_id=None,
        interface="tui",
    ):
        del agent_loop_factory, responder, store
        self.workspace = workspace
        self.db_path = db_path
        self.max_steps = max_steps
        self.session_id = session_id
        self.interface = interface
        self.inputs = []

    def handle_user_input(self, text):
        self.inputs.append(text)
        run_result = RunResult(
            run_id="run-1",
            goal=text,
            termination=TerminationReason.ACHIEVED,
            reason="done",
            steps_executed=1,
        )
        return ConversationResponse(
            session_id="session-1",
            user_turn_id="user-1",
            assistant_turn_id="assistant-1",
            run_result=run_result,
            reply="reply text",
            conversation_context="",
            candidate_skills=(),
            action_recipe_candidates=(),
            decision=None,
        )


class FakeStore:
    def __init__(self, db_path):
        self.db_path = db_path

    def inspect_run(self, run_id):
        return {"run_id": run_id, "events": [{"event_type": "run_started"}]}


class AutonomyNativeChromeApiTest(unittest.TestCase):
    def test_chrome_api_starts_session_and_sends_chat(self):
        from autonomy.chrome_api import ChromeSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            bridge = ChromeSessionBridge(
                conversation_factory=FakeConversation,
                store_factory=FakeStore,
            )

            started = bridge.handle(
                {"type": "session.start", "workspace": str(workspace), "max_steps": 3}
            )
            result = bridge.handle(
                {"type": "chat.send", "session_id": started["session_id"], "text": "hello"}
            )

        self.assertTrue(started["ok"])
        self.assertEqual(started["type"], "session.started")
        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "chat.result")
        self.assertEqual(result["reply"], "reply text")
        self.assertEqual(result["run_id"], "run-1")
        self.assertEqual(result["termination"], "achieved")
        self.assertEqual(result["steps_executed"], 1)

    def test_chrome_api_rejects_bad_workspace_and_unknown_session(self):
        from autonomy.chrome_api import ChromeSessionBridge

        bridge = ChromeSessionBridge(
            conversation_factory=FakeConversation,
            store_factory=FakeStore,
        )

        bad_workspace = bridge.handle(
            {"type": "session.start", "workspace": "/path/does/not/exist"}
        )
        unknown_session = bridge.handle(
            {"type": "chat.send", "session_id": "missing", "text": "hello"}
        )

        self.assertFalse(bad_workspace["ok"])
        self.assertIn("workspace must exist", bad_workspace["error"])
        self.assertFalse(unknown_session["ok"])
        self.assertIn("unknown session", unknown_session["error"])

    def test_chrome_api_inspects_run(self):
        from autonomy.chrome_api import ChromeSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            bridge = ChromeSessionBridge(
                conversation_factory=FakeConversation,
                store_factory=FakeStore,
            )
            bridge.handle({"type": "session.start", "workspace": str(workspace)})
            result = bridge.handle({"type": "run.inspect", "run_id": "run-1"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "run.inspect.result")
        self.assertEqual(result["run"]["run_id"], "run-1")
