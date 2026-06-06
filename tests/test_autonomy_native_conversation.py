import tempfile
import unittest
from pathlib import Path

from autonomy import AutonomyStore, ConversationLoop, RunResult, TerminationReason


class RecordingRuntime:
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


class ConversationLoopTest(unittest.TestCase):
    def test_first_input_creates_session_turns_and_linked_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            runtime = RecordingRuntime()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=4,
                runtime_factory=lambda workspace, db_path: runtime,
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
        self.assertEqual(runtime.calls[0]["conversation_context"], "")
        self.assertEqual(runtime.calls[0]["journal_metadata"]["conversation_session_id"], "session")
        self.assertEqual(
            runtime.calls[0]["journal_metadata"]["conversation_turn_id"],
            conversation["turns"][0]["id"],
        )

    def test_second_input_passes_recent_turns_as_conversation_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AutonomyStore(Path(tmpdir) / "autonomy.db")
            runtime = RecordingRuntime()
            loop = ConversationLoop(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "autonomy.db",
                max_steps=3,
                runtime_factory=lambda workspace, db_path: runtime,
                store=store,
                session_id="session",
            )

            loop.handle_user_input("inspect repository")
            second = loop.handle_user_input("continue from that")

        self.assertIn("inspect repository", runtime.calls[1]["conversation_context"])
        self.assertIn("handled inspect repository", runtime.calls[1]["conversation_context"])
        self.assertEqual(second.conversation_context, runtime.calls[1]["conversation_context"])
        self.assertEqual(runtime.calls[1]["max_steps"], 3)

    def test_workspace_and_max_steps_updates_affect_later_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            next_workspace = root / "next"
            next_workspace.mkdir()
            store = AutonomyStore(root / "autonomy.db")
            calls = []
            runtimes = []

            def factory(workspace, db_path):
                calls.append({"workspace": workspace, "db_path": db_path})
                runtime = RecordingRuntime()
                runtimes.append(runtime)
                return runtime

            loop = ConversationLoop(
                workspace=root,
                db_path=root / "autonomy.db",
                max_steps=2,
                runtime_factory=factory,
                store=store,
                session_id="session",
            )
            loop.set_workspace(next_workspace)
            loop.set_max_steps(5)

            response = loop.handle_user_input("inspect next workspace")
            conversation = store.inspect_conversation("session")

        self.assertEqual(calls[0]["workspace"], next_workspace.resolve())
        self.assertEqual(runtimes[0].calls[0]["max_steps"], 5)
        self.assertEqual(response.run_result.steps_executed, 1)
        self.assertEqual(conversation["session"]["workspace"], str(next_workspace.resolve()))
