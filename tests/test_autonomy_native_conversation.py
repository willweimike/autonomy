import tempfile
import unittest
from pathlib import Path

from autonomy import (
    ActionRecipe,
    AutonomyStore,
    ConversationDecision,
    ConversationLoop,
    ConversationMode,
    ModelConversationRouter,
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


class StaticRouter:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def route(self, conversation_context, user_input):
        self.calls.append(
            {
                "conversation_context": conversation_context,
                "user_input": user_input,
            }
        )
        return self.decision


class StaticResponder:
    def __init__(self, chat_reply="chat reply", task_reply="task reply"):
        self.chat_reply = chat_reply
        self.task_reply = task_reply
        self.chat_calls = []
        self.task_calls = []

    def respond_to_chat(self, conversation_context, user_input):
        self.chat_calls.append(
            {
                "conversation_context": conversation_context,
                "user_input": user_input,
            }
        )
        return self.chat_reply

    def summarize_task_result(self, conversation_context, user_input, result):
        self.task_calls.append(
            {
                "conversation_context": conversation_context,
                "user_input": user_input,
                "run_id": result.run_id,
            }
        )
        return self.task_reply


TASK_ROUTER = StaticRouter(
    ConversationDecision(
        mode=ConversationMode.TASK,
        task_goal="",
        reason="test task",
    )
)


class ConversationLoopTest(unittest.TestCase):
    def test_model_router_asks_model_even_for_greeting(self):
        class RouterModel:
            def __init__(self):
                self.calls = []

            def classify_conversation_turn(self, conversation_context, user_input):
                self.calls.append(
                    {
                        "conversation_context": conversation_context,
                        "user_input": user_input,
                    }
                )
                return ConversationDecision(
                    mode=ConversationMode.CHAT,
                    reason="model decision",
                )

        model = RouterModel()
        decision = ModelConversationRouter(model).route("", "hello")

        self.assertEqual(model.calls, [{"conversation_context": "", "user_input": "hello"}])
        self.assertEqual(decision.mode, ConversationMode.CHAT)
        self.assertEqual(decision.reason, "model decision")

    def test_model_chat_decision_creates_chat_turn_without_agent_loop_run(self):
        router = StaticRouter(
            ConversationDecision(
                mode=ConversationMode.CHAT,
                reason="model classified chat",
            )
        )
        responder = StaticResponder(chat_reply="你好，我可以陪你聊，也可以協助執行任務。")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            agent_loop = RecordingAgentLoop()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda workspace, db_path: agent_loop,
                router=router,
                responder=responder,
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("hello")
            conversation = store.inspect_conversation("session")

        self.assertEqual(agent_loop.calls, [])
        self.assertIsNone(response.run_result)
        self.assertEqual(response.decision.mode, ConversationMode.CHAT)
        self.assertEqual(response.decision.reason, "model classified chat")
        self.assertEqual(response.reply, "你好，我可以陪你聊，也可以協助執行任務。")
        self.assertEqual(responder.chat_calls[0]["user_input"], "hello")
        self.assertEqual(len(conversation["turns"]), 2)
        self.assertEqual(conversation["turns"][0]["run_id"], None)
        self.assertEqual(conversation["turns"][1]["run_id"], None)

    def test_first_input_creates_session_turns_and_linked_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            agent_loop = RecordingAgentLoop()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda workspace, db_path: agent_loop,
                router=TASK_ROUTER,
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
                router=TASK_ROUTER,
                responder=StaticResponder(),
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("inspect repository")

        self.assertEqual(
            [candidate["id"] for candidate in response.action_recipe_candidates],
            ["new-0", "new-1", "new-2"],
        )

    def test_router_task_goal_can_rewrite_user_input_before_agent_loop(self):
        router = StaticRouter(
            ConversationDecision(
                    mode=ConversationMode.TASK,
                    task_goal="inspect repository architecture",
                    reason="explicit project task",
                )
        )
        responder = StaticResponder(task_reply="我已完成專案架構分析。")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            agent_loop = RecordingAgentLoop()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                agent_loop_factory=lambda workspace, db_path: agent_loop,
                router=router,
                responder=responder,
                store=store,
                session_id="session",
            )

            response = loop.handle_user_input("分析目前專案架構")

        self.assertEqual(agent_loop.calls[0]["goal"], "inspect repository architecture")
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
                router=TASK_ROUTER,
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
                router=TASK_ROUTER,
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
