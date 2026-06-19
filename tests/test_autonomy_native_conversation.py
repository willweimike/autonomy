import tempfile
import unittest
from pathlib import Path

from autonomy import (
    ActionRecipe,
    AutonomyStore,
    ConversationLoop,
    ConversationMode,
    RunResult,
    TerminationReason,
)
from autonomy.models import jsonable


class RecordingAgentLoop:
    def __init__(self):
        self.calls = []

    def run(
        self,
        goal,
        max_steps=12,
        interactive=True,
        interface="run",
        conversation_context="",
        journal_metadata=None,
    ):
        self.calls.append(
            {
                "goal": goal,
                "max_steps": max_steps,
                "interactive": interactive,
                "interface": interface,
                "conversation_context": conversation_context,
                "journal_metadata": journal_metadata or {},
            }
        )
        return RunResult(
            run_id=f"run-{len(self.calls)}",
            goal=goal,
            termination=TerminationReason.ACHIEVED,
            steps_executed=1,
            reason=f"handled {goal}",
        )


class StaticResponder:
    def __init__(self, task_reply="task reply"):
        self.task_reply = task_reply
        self.task_calls = []

    def summarize_task_result(self, conversation_context, user_input, result):
        self.task_calls.append(
            {
                "conversation_context": conversation_context,
                "user_input": user_input,
                "run_id": result.run_id,
            }
        )
        return self.task_reply


class ConversationLoopTest(unittest.TestCase):
    def test_chat_like_input_still_runs_agent_loop(self):
        responder = StaticResponder(task_reply="你好，我可以陪你聊，也可以協助執行任務。")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            agent_loop = RecordingAgentLoop()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda workspace, db_path: agent_loop,
                responder=responder,
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("hello")
            conversation = store.inspect_conversation("session")

        self.assertEqual(agent_loop.calls[0]["goal"], "hello")
        self.assertIsNotNone(response.run_result)
        self.assertEqual(response.decision.mode, ConversationMode.TASK)
        self.assertEqual(response.decision.reason, "agent turn")
        self.assertIn("你好，我可以陪你聊，也可以協助執行任務。", response.reply)
        self.assertIn("run_id: run-1", response.reply)
        self.assertEqual(responder.task_calls[0]["user_input"], "hello")
        self.assertEqual(len(conversation["turns"]), 2)
        self.assertEqual(conversation["turns"][0]["run_id"], "run-1")
        self.assertEqual(conversation["turns"][1]["run_id"], "run-1")

    def test_first_input_creates_session_turns_and_linked_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            agent_loop = RecordingAgentLoop()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda workspace, db_path: agent_loop,
                responder=StaticResponder(),
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("inspect repository")
            conversation = store.inspect_conversation("session")

        self.assertEqual(response.run_result.run_id, "run-1")
        self.assertEqual(len(conversation["turns"]), 2)
        self.assertEqual(conversation["turns"][0]["role"], "user")
        self.assertEqual(conversation["turns"][0]["run_id"], "run-1")
        self.assertEqual(conversation["turns"][1]["role"], "assistant")
        self.assertEqual(conversation["turns"][1]["run_id"], "run-1")
        self.assertEqual(agent_loop.calls[0]["conversation_context"], "")
        self.assertEqual(agent_loop.calls[0]["journal_metadata"]["conversation_session_id"], "session")
        self.assertEqual(
            agent_loop.calls[0]["journal_metadata"]["conversation_turn_id"],
            conversation["turns"][0]["id"],
        )

    def test_first_input_passes_startup_memory_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            store.create_memory(
                scope="user",
                wing="preference",
                room="language",
                content="Use Traditional Chinese for Autonomy architecture notes.",
            )
            agent_loop = RecordingAgentLoop()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda workspace, db_path: agent_loop,
                responder=StaticResponder(),
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("inspect architecture")

        self.assertIn("Persistent memory loaded at session start:", response.conversation_context)
        self.assertIn("Use Traditional Chinese", agent_loop.calls[0]["conversation_context"])

    def test_assistant_respond_observation_is_used_as_reply(self):
        class AssistantRespondAgentLoop:
            def __init__(self, store):
                self.store = store

            def run(self, goal, max_steps=12, interactive=True, interface="run", conversation_context="", journal_metadata=None):
                del goal, max_steps, interactive, interface, conversation_context, journal_metadata
                run_id = "run-1"
                self.store.create_run(run_id, "hello")
                self.store.record_event(
                    run_id,
                    1,
                    "action_selected",
                    {"tool": "assistant.respond", "purpose": "answer directly"},
                )
                self.store.record_event(
                    run_id,
                    1,
                    "observation",
                    {"succeeded": True, "output": "你好，我是 Autonomy。"},
                )
                result = RunResult(
                    run_id=run_id,
                    goal="hello",
                    termination=TerminationReason.ACHIEVED,
                    steps_executed=1,
                    reason="assistant responded",
                )
                self.store.complete_run(result)
                return result

        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda _workspace, _db_path: AssistantRespondAgentLoop(store),
                responder=StaticResponder(task_reply="summary should not be used"),
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("hello")

        self.assertEqual(response.reply, "你好，我是 Autonomy。")

    def test_blocked_run_uses_deterministic_failure_summary(self):
        class BlockedAgentLoop:
            def __init__(self, store):
                self.store = store

            def run(self, goal, max_steps=12, interactive=True, interface="run", conversation_context="", journal_metadata=None):
                del goal, max_steps, interactive, interface, conversation_context, journal_metadata
                run_id = "run-1"
                self.store.create_run(run_id, "send email")
                self.store.record_event(
                    run_id,
                    1,
                    "action_selected",
                    {
                        "tool": "shell.execute",
                        "purpose": "Check if a command-line mail sending tool is available.",
                    },
                )
                self.store.record_event(
                    run_id,
                    1,
                    "observation",
                    {
                        "succeeded": False,
                        "output": "/usr/sbin/sendmail\n/usr/bin/mail\n",
                        "error": "",
                        "exit_code": 1,
                    },
                )
                result = RunResult(
                    run_id=run_id,
                    goal="send email",
                    termination=TerminationReason.BLOCKED,
                    steps_executed=1,
                    reason="shell.execute failed with exit_code 1",
                )
                self.store.complete_run(result)
                return result

        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            responder = StaticResponder(task_reply="The email has been sent successfully.")
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda _workspace, _db_path: BlockedAgentLoop(store),
                responder=responder,
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("send email")

        self.assertEqual(responder.task_calls, [])
        self.assertIn("Task did not complete.", response.reply)
        self.assertIn("termination: blocked", response.reply)
        self.assertIn("reason: shell.execute failed with exit_code 1", response.reply)
        self.assertIn("selected action: shell.execute", response.reply)
        self.assertIn("observation: failed", response.reply)
        self.assertIn("exit_code: 1", response.reply)
        self.assertIn("/usr/sbin/sendmail", response.reply)
        self.assertNotIn("sent successfully", response.reply)

    def test_task_response_includes_new_action_recipe_candidates_only(self):
        class RecipeCandidateAgentLoop:
            def __init__(self, store):
                self.store = store

            def run(
                self,
                goal,
                max_steps=12,
                interactive=True,
                interface="run",
                conversation_context="",
                journal_metadata=None,
            ):
                del max_steps, interactive, interface, conversation_context, journal_metadata
                self.store.create_run("run-1", goal)
                for index in range(4):
                    recipe = ActionRecipe(
                        f"new-{index}",
                        "intent",
                        "condition",
                        {
                            "tool": "filesystem.read",
                            "arguments": {"path": f"file-{index}.txt"},
                            "purpose": "read file",
                        },
                        "effect",
                        "verify",
                        evidence_count=2,
                    )
                    self.store.record_event(
                        "run-1",
                        index,
                        "candidate_recipe_learned",
                        {"created": True, "recipe": jsonable(recipe)},
                    )
                existing = ActionRecipe(
                    "existing",
                    "intent",
                    "condition",
                    {"tool": "filesystem.read", "arguments": {"path": "old.txt"}},
                    "effect",
                    "verify",
                    evidence_count=3,
                )
                self.store.record_event(
                    "run-1",
                    5,
                    "candidate_recipe_learned",
                    {"created": False, "recipe": jsonable(existing)},
                )
                return RunResult(
                    "run-1",
                    goal,
                    TerminationReason.ACHIEVED,
                    1,
                    "done",
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda workspace, db_path: RecipeCandidateAgentLoop(store),
                responder=StaticResponder(),
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("inspect repository")

        self.assertEqual(
            [candidate["id"] for candidate in response.action_recipe_candidates],
            ["new-0", "new-1", "new-2"],
        )

    def test_user_input_goes_to_agent_loop_unchanged(self):
        responder = StaticResponder(task_reply="我已完成專案架構分析。")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            agent_loop = RecordingAgentLoop()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda workspace, db_path: agent_loop,
                responder=responder,
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("分析目前專案架構")

        self.assertEqual(agent_loop.calls[0]["goal"], "分析目前專案架構")
        self.assertIn("我已完成專案架構分析。", response.reply)
        self.assertIn("run_id: run-1", response.reply)
        self.assertEqual(responder.task_calls[0]["user_input"], "分析目前專案架構")

    def test_second_input_passes_recent_turns_as_conversation_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            agent_loop = RecordingAgentLoop()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=3,
                agent_loop_factory=lambda workspace, db_path: agent_loop,
                responder=StaticResponder(task_reply="handled"),
                store=store,
                session_id="session",
            )

            loop.handle_user_input("inspect repository")
            second = loop.handle_user_input("continue from that")

        self.assertIn("inspect repository", agent_loop.calls[1]["conversation_context"])
        self.assertIn("assistant run_id=run-1: handled", agent_loop.calls[1]["conversation_context"])
        self.assertEqual(second.conversation_context, agent_loop.calls[1]["conversation_context"])
        self.assertEqual(agent_loop.calls[1]["max_steps"], 3)

    def test_workspace_and_max_steps_updates_affect_later_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            next_workspace = root / "next"
            next_workspace.mkdir()
            store = AutonomyStore(root / "autonomy.db")
            calls = []
            agent_loops = []

            def factory(workspace, db_path):
                calls.append({"workspace": workspace, "db_path": db_path})
                agent_loop = RecordingAgentLoop()
                agent_loops.append(agent_loop)
                return agent_loop

            loop = ConversationLoop(
                workspace=root,
                db_path=root / "autonomy.db",
                max_steps=2,
                agent_loop_factory=factory,
                responder=StaticResponder(),
                store=store,
                session_id="session",
            )
            loop.set_workspace(next_workspace)
            loop.set_max_steps(5)

            response = loop.handle_user_input("inspect next workspace")
            conversation = store.inspect_conversation("session")

        self.assertEqual(calls[0]["workspace"], next_workspace.resolve())
        self.assertEqual(agent_loops[0].calls[0]["max_steps"], 5)
        self.assertEqual(response.run_result.steps_executed, 1)
        self.assertEqual(conversation["session"]["workspace"], str(next_workspace.resolve()))
