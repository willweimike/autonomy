import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autonomy.cli import build_parser
from autonomy.models import ConversationResponse, RunResult, TerminationReason


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
        del responder, store
        self.workspace = workspace
        self.db_path = db_path
        self.max_steps = max_steps
        self.agent_loop_factory = agent_loop_factory
        self.session_id = session_id
        self.interface = interface
        self.inputs = []

    def handle_user_input(self, text):
        self.inputs.append(text)
        run_result = RunResult(
            run_id=f"run-{text}",
            goal=text,
            termination=TerminationReason.ACHIEVED,
            reason="done",
            steps_executed=1,
        )
        return ConversationResponse(
            session_id="session-discord",
            user_turn_id="user-discord",
            assistant_turn_id="assistant-discord",
            run_result=run_result,
            reply="reply text",
            conversation_context="",
            candidate_skills=(),
            action_recipe_candidates=(),
            decision=None,
        )


class LongReplyConversation(FakeConversation):
    def handle_user_input(self, text):
        response = super().handle_user_input(text)
        return ConversationResponse(
            session_id=response.session_id,
            user_turn_id=response.user_turn_id,
            assistant_turn_id=response.assistant_turn_id,
            run_result=response.run_result,
            reply="x" * 5_000,
            conversation_context=response.conversation_context,
            candidate_skills=response.candidate_skills,
            action_recipe_candidates=response.action_recipe_candidates,
            decision=response.decision,
        )


class BlockingConversation(FakeConversation):
    entered = threading.Event()
    release = threading.Event()

    def handle_user_input(self, text):
        self.entered.set()
        self.release.wait(timeout=1.0)
        return super().handle_user_input(text)


class ApprovalConversation(FakeConversation):
    def handle_user_input(self, text):
        agent_loop = self.agent_loop_factory(self.workspace, self.db_path)
        allowed = agent_loop.action_gateway.approval.prompt(text)
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


class FakeStore:
    def __init__(self, db_path):
        self.db_path = db_path

    def inspect_run(self, run_id):
        return {"run_id": run_id, "events": [{"event_type": "run_started"}]}


class DiscordBotConfigTest(unittest.TestCase):
    def test_loads_discord_config_from_workspace_env(self):
        from autonomy.discord_bot import load_discord_config

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            autonomy_home = workspace / ".autonomy"
            autonomy_home.mkdir()
            env = autonomy_home / ".env"
            env.write_text('DISCORD_BOT_TOKEN="token-value"\nDISCORD_OWNER_ID="12345"\n', encoding="utf-8")
            env.chmod(0o600)

            config = load_discord_config(workspace)

        self.assertEqual(config.token, "token-value")
        self.assertEqual(config.owner_id, 12345)

    def test_missing_discord_config_returns_clear_error(self):
        from autonomy.discord_bot import DiscordConfigurationError, load_discord_config

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(DiscordConfigurationError, "DISCORD_BOT_TOKEN"):
                load_discord_config(Path(tmpdir))


class DiscordBotCliTest(unittest.TestCase):
    def test_cli_parser_accepts_discord_bot_command(self):
        args = build_parser().parse_args(["discord-bot", "--workspace", ".", "--max-steps", "3"])

        self.assertEqual(args.command, "discord-bot")
        self.assertEqual(args.workspace, Path("."))
        self.assertEqual(args.max_steps, 3)

    def test_missing_discord_dependency_returns_clear_error(self):
        from autonomy.discord_bot import run_discord_bot

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            autonomy_home = workspace / ".autonomy"
            autonomy_home.mkdir()
            env = autonomy_home / ".env"
            env.write_text('DISCORD_BOT_TOKEN="token-value"\nDISCORD_OWNER_ID="12345"\n', encoding="utf-8")
            env.chmod(0o600)

            result = run_discord_bot(
                workspace=workspace,
                db_path=workspace / ".autonomy" / "autonomy.db",
                max_steps=3,
                import_discord=lambda: (_ for _ in ()).throw(ImportError("missing")),
            )

        self.assertEqual(result, 2)


class DiscordSessionBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_owner_without_creating_session(self):
        from autonomy.discord_bot import DiscordSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                conversation_factory=FakeConversation,
                store_factory=FakeStore,
            )
            replies = await bridge.handle_message(author_id=999, content="hello")

        self.assertEqual(replies, ["This Autonomy Discord bot is owner-only."])
        self.assertEqual(bridge.session_count, 0)

    async def test_owner_message_starts_session_and_returns_run_metadata(self):
        from autonomy.discord_bot import DiscordSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                conversation_factory=FakeConversation,
                store_factory=FakeStore,
            )
            replies = await bridge.handle_message(author_id=12345, content="hello")

        self.assertEqual(bridge.session_count, 1)
        self.assertIn("reply text", replies[0])
        self.assertIn("run_id=run-hello", replies[0])
        self.assertIn("termination=achieved", replies[0])

    async def test_status_inspect_and_reset_commands(self):
        from autonomy.discord_bot import DiscordSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                conversation_factory=FakeConversation,
                store_factory=FakeStore,
            )
            await bridge.handle_message(author_id=12345, content="hello")
            status = await bridge.handle_message(author_id=12345, content="!status")
            inspected = await bridge.handle_message(author_id=12345, content="!inspect run-hello")
            reset = await bridge.handle_message(author_id=12345, content="!reset")

        self.assertIn("sessions=1", status[0])
        self.assertIn('"run_id": "run-hello"', inspected[0])
        self.assertIn("session reset", reset[0])

    async def test_rejects_concurrent_owner_messages(self):
        from autonomy.discord_bot import DiscordSessionBridge

        BlockingConversation.entered.clear()
        BlockingConversation.release.clear()
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                conversation_factory=BlockingConversation,
                store_factory=FakeStore,
            )
            first = asyncio.create_task(bridge.handle_message(author_id=12345, content="first"))
            await asyncio.to_thread(BlockingConversation.entered.wait, 1.0)
            busy = await bridge.handle_message(author_id=12345, content="second")
            BlockingConversation.release.set()
            first_reply = await first

        self.assertEqual(busy, ["session is busy"])
        self.assertIn("run_id=run-first", first_reply[0])

    async def test_rejects_reset_while_session_is_busy(self):
        from autonomy.discord_bot import DiscordSessionBridge

        BlockingConversation.entered.clear()
        BlockingConversation.release.clear()
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                conversation_factory=BlockingConversation,
                store_factory=FakeStore,
            )
            first = asyncio.create_task(bridge.handle_message(author_id=12345, content="first"))
            await asyncio.to_thread(BlockingConversation.entered.wait, 1.0)
            session_id = bridge.session_id
            reset = await bridge.handle_message(author_id=12345, content="!reset")
            BlockingConversation.release.set()
            await first

        self.assertEqual(reset, ["session is busy"])
        self.assertEqual(bridge.session_id, session_id)

    async def test_approval_prompt_allows_owner_button_response(self):
        from autonomy.discord_bot import DiscordSessionBridge

        approvals = []

        async def send_approval(approval_id, message):
            approvals.append((approval_id, message))

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                send_approval=send_approval,
                approval_timeout_seconds=1.0,
                conversation_factory=ApprovalConversation,
                store_factory=FakeStore,
            )
            with patch("autonomy.discord_bot.build_agent_loop") as build_agent_loop:
                build_agent_loop.return_value = type(
                    "AgentLoop",
                    (),
                    {"action_gateway": type("Gateway", (), {"approval": None})()},
                )()
                task = asyncio.create_task(
                    bridge.handle_message(author_id=12345, content="Approve high-risk action?")
                )
                while not approvals:
                    await asyncio.sleep(0.01)
                response = bridge.handle_approval(
                    author_id=12345,
                    approval_id=approvals[0][0],
                    decision="allow",
                )
                reply = await task

        self.assertEqual(response["decision"], "allow")
        self.assertIn("approved", reply[0])
        self.assertIn("run_id=run-approval", reply[0])

    async def test_approval_prompt_denies_owner_button_response(self):
        from autonomy.discord_bot import DiscordSessionBridge

        approvals = []

        async def send_approval(approval_id, message):
            approvals.append((approval_id, message))

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                send_approval=send_approval,
                approval_timeout_seconds=1.0,
                conversation_factory=ApprovalConversation,
                store_factory=FakeStore,
            )
            with patch("autonomy.discord_bot.build_agent_loop") as build_agent_loop:
                build_agent_loop.return_value = type(
                    "AgentLoop",
                    (),
                    {"action_gateway": type("Gateway", (), {"approval": None})()},
                )()
                task = asyncio.create_task(
                    bridge.handle_message(author_id=12345, content="Approve high-risk action?")
                )
                while not approvals:
                    await asyncio.sleep(0.01)
                response = bridge.handle_approval(
                    author_id=12345,
                    approval_id=approvals[0][0],
                    decision="deny",
                )
                reply = await task

        self.assertEqual(response["decision"], "deny")
        self.assertIn("denied", reply[0])

    async def test_approval_prompt_denies_on_timeout(self):
        from autonomy.discord_bot import DiscordSessionBridge

        approvals = []

        async def send_approval(approval_id, message):
            approvals.append((approval_id, message))

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                send_approval=send_approval,
                approval_timeout_seconds=0.01,
                conversation_factory=ApprovalConversation,
                store_factory=FakeStore,
            )
            with patch("autonomy.discord_bot.build_agent_loop") as build_agent_loop:
                build_agent_loop.return_value = type(
                    "AgentLoop",
                    (),
                    {"action_gateway": type("Gateway", (), {"approval": None})()},
                )()
                reply = await bridge.handle_message(
                    author_id=12345,
                    content="Approve high-risk action?",
                )

        self.assertEqual(len(approvals), 1)
        self.assertIn("denied", reply[0])

    async def test_approval_prompt_denies_and_clears_pending_when_send_fails(self):
        from autonomy.discord_bot import DiscordSessionBridge

        async def send_approval(approval_id, message):
            del approval_id, message
            raise RuntimeError("discord send failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                send_approval=send_approval,
                approval_timeout_seconds=1.0,
                conversation_factory=ApprovalConversation,
                store_factory=FakeStore,
            )
            with patch("autonomy.discord_bot.build_agent_loop") as build_agent_loop:
                build_agent_loop.return_value = type(
                    "AgentLoop",
                    (),
                    {"action_gateway": type("Gateway", (), {"approval": None})()},
                )()
                reply = await bridge.handle_message(
                    author_id=12345,
                    content="Approve high-risk action?",
                )

        self.assertIn("denied", reply[0])
        self.assertEqual(bridge.approval_broker.deny_all(), 0)

    async def test_approval_prompt_denies_and_clears_pending_when_send_hangs(self):
        from autonomy.discord_bot import DiscordSessionBridge

        async def send_approval(approval_id, message):
            del approval_id, message
            await asyncio.sleep(10)

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                send_approval=send_approval,
                approval_timeout_seconds=0.01,
                conversation_factory=ApprovalConversation,
                store_factory=FakeStore,
            )
            with patch("autonomy.discord_bot.build_agent_loop") as build_agent_loop:
                build_agent_loop.return_value = type(
                    "AgentLoop",
                    (),
                    {"action_gateway": type("Gateway", (), {"approval": None})()},
                )()
                reply = await bridge.handle_message(
                    author_id=12345,
                    content="Approve high-risk action?",
                )

        self.assertIn("denied", reply[0])
        self.assertEqual(bridge.approval_broker.deny_all(), 0)

    async def test_long_replies_are_truncated_for_discord(self):
        from autonomy.discord_bot import DISCORD_MESSAGE_LIMIT, DiscordSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = DiscordSessionBridge(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                owner_id=12345,
                max_steps=3,
                conversation_factory=LongReplyConversation,
                store_factory=FakeStore,
            )
            replies = await bridge.handle_message(author_id=12345, content="hello")

        self.assertEqual(len(replies), 1)
        self.assertLessEqual(len(replies[0]), DISCORD_MESSAGE_LIMIT)
        self.assertIn("[truncated]", replies[0])
