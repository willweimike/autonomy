import unittest
from pathlib import Path
import tempfile

from autonomy import (
    Action,
    ActionIntent,
    ApprovalPolicy,
    RiskLevel,
    ToolsetConfiguration,
    build_local_tool_registry,
)


class AutonomyNativeToolsTest(unittest.TestCase):
    def test_local_read_list_search_and_safe_shell_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "sample.txt").write_text("needle\n", encoding="utf-8")
            registry = build_local_tool_registry(root)
            action = registry.action_from_intent(
                ActionIntent("filesystem.read", {"path": "sample.txt"}, "read sample")
            )

            read = registry.execute(action)
            listing = registry.execute(Action("filesystem.list", {"path": "."}, "list", "verify"))
            search = registry.execute(
                Action("search.text", {"path": ".", "query": "needle"}, "search", "verify")
            )
            shell = registry.execute(Action("shell.execute", {"command": "pwd"}, "pwd", "verify"))

            self.assertTrue(read.succeeded)
            self.assertEqual(action.purpose, "read sample")
            self.assertEqual(registry.spec("filesystem.read").toolset, "file")
            self.assertEqual(registry.spec("filesystem.list").toolset, "file")
            self.assertEqual(registry.spec("search.text").toolset, "search")
            self.assertEqual(registry.spec("shell.execute").toolset, "terminal")
            self.assertEqual(registry.contracts["filesystem.read"], {"path": "string"})
            self.assertIn("sample.txt", listing.output)
            self.assertIn("sample.txt:1:needle", search.output)
            self.assertEqual(shell.exit_code, 0)

    def test_file_tools_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
            intent = ActionIntent("filesystem.read", {"path": "../outside"}, "read outside")

            self.assertIn("path escapes workspace", registry.rejection_reason(intent))
            observation = registry.execute(registry.action_from_intent(intent))

            self.assertFalse(observation.succeeded)
            self.assertIn("path escapes workspace", observation.error)

    def test_default_toolsets_expose_mvp_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir, ToolsetConfiguration())

        self.assertEqual(
            sorted(registry.names),
            ["filesystem.list", "filesystem.read", "search.text", "shell.execute"],
        )

    def test_disabled_toolset_is_not_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("search", "terminal", "skills")),
            )

        self.assertNotIn("filesystem.read", registry.names)
        self.assertNotIn("filesystem.list", registry.names)
        self.assertIn("search.text", registry.names)
        self.assertIn("shell.execute", registry.names)

    def test_planned_toolsets_do_not_expose_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("browser", "web")),
            )

        self.assertEqual(registry.names, set())

    def test_disabled_individual_tool_is_not_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(disabled_tools=("shell.execute",)),
            )

        self.assertNotIn("shell.execute", registry.names)
        self.assertIn("filesystem.read", registry.names)

    def test_shell_risk_is_reassessed_by_policy(self):
        policy = ApprovalPolicy(prompt=lambda message: False)
        safe = Action("shell.execute", {"command": "git status"}, "status", "verify")
        unknown = Action("shell.execute", {"command": "touch file"}, "touch", "verify")

        self.assertEqual(policy.authorize(safe, interactive=False), (True, "low-risk action"))
        allowed, reason = policy.authorize(unknown, interactive=False)
        self.assertFalse(allowed)
        self.assertIn("approval required", reason)


if __name__ == "__main__":
    unittest.main()
