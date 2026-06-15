import tempfile
import unittest
from pathlib import Path

from autonomy.project_context import load_project_context


class AutonomyProjectContextTest(unittest.TestCase):
    def test_loads_first_workspace_project_context_by_priority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "AGENTS.md").write_text("agents guidance\n", encoding="utf-8")
            (root / ".cursorrules").write_text("cursor guidance\n", encoding="utf-8")

            context = load_project_context(root)

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.source, "AGENTS.md")
        self.assertIn("## AGENTS.md", context.content)
        self.assertIn("agents guidance", context.content)
        self.assertNotIn("cursor guidance", context.content)
        self.assertFalse(context.truncated)

    def test_workspace_autonomy_context_overrides_generic_agents_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "AUTONOMY.md").write_text("autonomy guidance\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("agents guidance\n", encoding="utf-8")

            context = load_project_context(root)

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.source, "AUTONOMY.md")
        self.assertIn("autonomy guidance", context.content)
        self.assertNotIn("agents guidance", context.content)

    def test_truncates_large_project_context_with_head_and_tail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "AGENTS.md").write_text("A" * 120 + "TAIL", encoding="utf-8")

            context = load_project_context(root, max_chars=60)

        self.assertIsNotNone(context)
        assert context is not None
        self.assertTrue(context.truncated)
        self.assertIn("truncated AGENTS.md", context.content)
        self.assertIn("TAIL", context.content)
        self.assertGreater(context.original_chars, len(context.content))

    def test_returns_none_when_no_context_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(load_project_context(tmpdir))


if __name__ == "__main__":
    unittest.main()
