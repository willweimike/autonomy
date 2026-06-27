import io
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autonomy.cli import build_parser, main
from autonomy.models import ConversationResponse, RunResult, TerminationReason


def framed(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def decode_framed_messages(data: bytes) -> list[dict]:
    messages = []
    offset = 0
    while offset < len(data):
        size = struct.unpack("<I", data[offset : offset + 4])[0]
        offset += 4
        messages.append(json.loads(data[offset : offset + size].decode("utf-8")))
        offset += size
    return messages


def read_framed_message(stream) -> dict:
    header = stream.read(4)
    if len(header) != 4:
        raise EOFError("missing frame header")
    size = struct.unpack("<I", header)[0]
    body = stream.read(size)
    if len(body) != size:
        raise EOFError("truncated frame body")
    return json.loads(body.decode("utf-8"))


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

    def test_native_message_accepts_approval_response(self):
        from autonomy.chrome_host import read_native_message

        incoming = io.BytesIO(
            framed(
                {
                    "type": "approval.respond",
                    "approval_id": "abc123",
                    "decision": "allow",
                }
            )
        )

        self.assertEqual(
            read_native_message(incoming),
            {"type": "approval.respond", "approval_id": "abc123", "decision": "allow"},
        )

    def test_native_message_rejects_non_object_and_oversized_payload(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        with self.assertRaisesRegex(ChromeHostError, "expected JSON object"):
            read_native_message(io.BytesIO(framed(["bad"])))

        with self.assertRaisesRegex(ChromeHostError, "exceeds"):
            read_native_message(io.BytesIO(framed({"type": "status"})), max_bytes=2)

    def test_write_native_message_rejects_oversized_payload(self):
        from autonomy.chrome_host import ChromeHostError, write_native_message

        with self.assertRaisesRegex(ChromeHostError, "exceeds"):
            write_native_message(io.BytesIO(), {"ok": True, "body": "x" * 1_000_000})

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

    def test_chrome_host_emits_queued_events_before_response(self):
        from autonomy.chrome_host import run_chrome_host

        class FakeApi:
            def handle(self, message):
                self.message = message
                return {"ok": True, "type": "chat.result"}

            def pop_events(self):
                return [{"ok": True, "type": "approval.requested", "approval_id": "a1"}]

        incoming = io.BytesIO(framed({"type": "status"}))
        outgoing = io.BytesIO()

        result = run_chrome_host(input_stream=incoming, output_stream=outgoing, api=FakeApi())

        self.assertEqual(result, 0)
        first, second = decode_framed_messages(outgoing.getvalue())
        self.assertEqual(first["type"], "approval.requested")
        self.assertEqual(second["type"], "chat.result")

    def test_chrome_host_processes_approval_while_chat_send_is_blocked(self):
        from autonomy.chrome_host import run_chrome_host

        class FakeApi:
            def __init__(self):
                self._writer = None
                self._approved = threading.Event()

            def set_event_sink(self, writer):
                self._writer = writer

            def handle(self, message):
                if message["type"] == "chat.send":
                    self._writer(
                        {"ok": True, "type": "approval.requested", "approval_id": "a1"}
                    )
                    self._approved.wait(timeout=0.2)
                    return {"ok": True, "type": "chat.result", "reply": "done"}
                if message["type"] == "approval.respond":
                    self._approved.set()
                    return {"ok": True, "type": "approval.result", "approval_id": "a1"}
                return {"ok": True, "type": "status.result"}

        incoming = io.BytesIO(
            framed({"type": "chat.send"}) + framed({"type": "approval.respond"})
        )
        outgoing = io.BytesIO()

        result = run_chrome_host(input_stream=incoming, output_stream=outgoing, api=FakeApi())

        self.assertEqual(result, 0)
        messages = decode_framed_messages(outgoing.getvalue())
        self.assertEqual(
            [message["type"] for message in messages],
            ["approval.requested", "approval.result", "chat.result"],
        )


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


class BlockingConversation:
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
        del workspace, db_path, max_steps, agent_loop_factory, responder, store, session_id, interface
        self.entered = threading.Event()
        self.release = threading.Event()

    def handle_user_input(self, text):
        self.entered.set()
        self.release.wait(timeout=1.0)
        run_result = RunResult(
            run_id=f"run-{text}",
            goal=text,
            termination=TerminationReason.ACHIEVED,
            reason="done",
            steps_executed=1,
        )
        return ConversationResponse(
            session_id="blocking-session",
            user_turn_id="user-blocking",
            assistant_turn_id="assistant-blocking",
            run_result=run_result,
            reply="reply text",
            conversation_context="",
            candidate_skills=(),
            action_recipe_candidates=(),
            decision=None,
        )


class ApprovalConversation:
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
        del responder, store, session_id, interface, max_steps
        self.workspace = workspace
        self.db_path = db_path
        self.agent_loop = agent_loop_factory(workspace, db_path)

    def handle_user_input(self, text):
        allowed = self.agent_loop.action_gateway.approval.prompt(text)
        run_result = RunResult(
            run_id="run-approval",
            goal=text,
            termination=TerminationReason.ACHIEVED,
            reason="allow" if allowed else "deny",
            steps_executed=1,
        )
        return ConversationResponse(
            session_id="session-approval",
            user_turn_id="user-approval",
            assistant_turn_id="assistant-approval",
            run_result=run_result,
            reply="approved" if allowed else "denied",
            conversation_context="",
            candidate_skills=(),
            action_recipe_candidates=(),
            decision=None,
        )


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

    def test_chrome_api_rejects_concurrent_chat_send_for_same_session(self):
        from autonomy.chrome_api import ChromeSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            bridge = ChromeSessionBridge(
                conversation_factory=BlockingConversation,
                store_factory=FakeStore,
            )
            started = bridge.handle({"type": "session.start", "workspace": str(workspace)})
            session_id = started["session_id"]
            conversation = bridge.sessions[session_id]
            results: list[dict[str, object]] = []

            worker = threading.Thread(
                target=lambda: results.append(
                    bridge.handle(
                        {
                            "type": "chat.send",
                            "session_id": session_id,
                            "text": "first",
                        }
                    )
                )
            )
            worker.start()
            self.assertTrue(conversation.entered.wait(timeout=1.0))

            busy = bridge.handle(
                {
                    "type": "chat.send",
                    "session_id": session_id,
                    "text": "second",
                }
            )
            conversation.release.set()
            worker.join(timeout=1.0)

        self.assertEqual(busy, {"ok": False, "error": "session is busy"})
        self.assertEqual(results[0]["type"], "chat.result")


class AutonomyNativeChromeApprovalTest(unittest.TestCase):
    def test_approval_broker_allows_matching_response(self):
        from autonomy.chrome_api import ChromeApprovalBroker

        events = []
        broker = ChromeApprovalBroker(events.append, timeout_seconds=1.0)
        result = {}

        thread = threading.Thread(
            target=lambda: result.update({"allowed": broker.prompt("Approve high-risk action?")})
        )
        thread.start()
        time.sleep(0.05)

        self.assertEqual(events[0]["type"], "approval.requested")
        response = broker.respond(events[0]["approval_id"], "allow")
        thread.join(timeout=1.0)

        self.assertTrue(response["ok"])
        self.assertTrue(result["allowed"])

    def test_approval_broker_denies_timeout_and_bad_decision(self):
        from autonomy.chrome_api import ChromeApprovalBroker

        events = []
        broker = ChromeApprovalBroker(events.append, timeout_seconds=0.01)

        self.assertFalse(broker.prompt("Approve medium-risk action?"))
        self.assertEqual(events[0]["type"], "approval.requested")

        denied = broker.respond("missing", "allow")
        self.assertFalse(denied["ok"])

        broker = ChromeApprovalBroker(events.append, timeout_seconds=1.0)
        result = {}
        thread = threading.Thread(target=lambda: result.update({"allowed": broker.prompt("Approve?")}))
        thread.start()
        time.sleep(0.05)
        broker.respond(events[-1]["approval_id"], "deny")
        thread.join(timeout=1.0)
        self.assertFalse(result["allowed"])

    def test_chrome_host_bridge_allows_real_approval_round_trip(self):
        from autonomy.chrome_api import ChromeSessionBridge
        from autonomy.chrome_host import run_chrome_host

        input_read_fd, input_write_fd = os.pipe()
        output_read_fd, output_write_fd = os.pipe()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            os.fdopen(input_read_fd, "rb", buffering=0) as host_input,
            os.fdopen(input_write_fd, "wb", buffering=0) as input_writer,
            os.fdopen(output_read_fd, "rb", buffering=0) as output_reader,
            os.fdopen(output_write_fd, "wb", buffering=0) as host_output,
        ):
            workspace = Path(tmpdir)
            bridge = ChromeSessionBridge(
                workspace=workspace,
                conversation_factory=ApprovalConversation,
                store_factory=FakeStore,
            )
            bridge.approval_broker.timeout_seconds = 5.0
            host_thread = threading.Thread(
                target=run_chrome_host,
                kwargs={
                    "input_stream": host_input,
                    "output_stream": host_output,
                    "api": bridge,
                },
            )

            with patch("autonomy.chrome_api.build_agent_loop") as build_agent_loop:
                build_agent_loop.return_value = type(
                    "AgentLoop",
                    (),
                    {"action_gateway": type("Gateway", (), {"approval": None})()},
                )()
                host_thread.start()
                input_writer.write(framed({"type": "session.start", "workspace": str(workspace)}))
                session_started = read_framed_message(output_reader)
                input_writer.write(
                    framed(
                        {
                            "type": "chat.send",
                            "session_id": session_started["session_id"],
                            "text": "Approve high-risk action?",
                        }
                    )
                )
                approval_requested = read_framed_message(output_reader)
                input_writer.write(
                    framed(
                        {
                            "type": "approval.respond",
                            "approval_id": approval_requested["approval_id"],
                            "decision": "allow",
                        }
                    )
                )
                approval_result = read_framed_message(output_reader)
                chat_result = read_framed_message(output_reader)
                input_writer.close()
                host_thread.join(timeout=1.0)

        self.assertFalse(host_thread.is_alive())
        self.assertEqual(session_started["type"], "session.started")
        self.assertEqual(approval_requested["type"], "approval.requested")
        self.assertEqual(approval_result["type"], "approval.result")
        self.assertEqual(chat_result["type"], "chat.result")
        self.assertEqual(chat_result["reason"], "allow")
        self.assertEqual(chat_result["reply"], "approved")

    def test_chrome_host_disconnect_denies_pending_approval_immediately(self):
        from autonomy.chrome_api import ChromeSessionBridge
        from autonomy.chrome_host import run_chrome_host

        input_read_fd, input_write_fd = os.pipe()
        output_read_fd, output_write_fd = os.pipe()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            os.fdopen(input_read_fd, "rb", buffering=0) as host_input,
            os.fdopen(input_write_fd, "wb", buffering=0) as input_writer,
            os.fdopen(output_read_fd, "rb", buffering=0) as output_reader,
            os.fdopen(output_write_fd, "wb", buffering=0) as host_output,
        ):
            workspace = Path(tmpdir)
            bridge = ChromeSessionBridge(
                workspace=workspace,
                conversation_factory=ApprovalConversation,
                store_factory=FakeStore,
            )
            bridge.approval_broker.timeout_seconds = 30.0
            host_thread = threading.Thread(
                target=run_chrome_host,
                kwargs={
                    "input_stream": host_input,
                    "output_stream": host_output,
                    "api": bridge,
                },
            )

            with patch("autonomy.chrome_api.build_agent_loop") as build_agent_loop:
                build_agent_loop.return_value = type(
                    "AgentLoop",
                    (),
                    {"action_gateway": type("Gateway", (), {"approval": None})()},
                )()
                host_thread.start()
                input_writer.write(framed({"type": "session.start", "workspace": str(workspace)}))
                session_started = read_framed_message(output_reader)
                input_writer.write(
                    framed(
                        {
                            "type": "chat.send",
                            "session_id": session_started["session_id"],
                            "text": "Approve disconnect path?",
                        }
                    )
                )
                approval_requested = read_framed_message(output_reader)
                start = time.monotonic()
                input_writer.close()
                chat_result = read_framed_message(output_reader)
                host_thread.join(timeout=1.0)

        self.assertFalse(host_thread.is_alive())
        self.assertEqual(approval_requested["type"], "approval.requested")
        self.assertEqual(chat_result["type"], "chat.result")
        self.assertEqual(chat_result["reason"], "deny")
        self.assertLess(time.monotonic() - start, 1.0)


class ChromeExtensionPackagingTest(unittest.TestCase):
    def test_pyproject_registers_dedicated_chrome_host_console_script(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('autonomy-chrome-host = "autonomy.chrome_host:main"', pyproject)

    def test_native_host_example_points_to_chrome_host_console_script(self):
        manifest = json.loads(
            Path("chrome-extension/native-host.example.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["path"], "/absolute/path/to/autonomy-chrome-host")
