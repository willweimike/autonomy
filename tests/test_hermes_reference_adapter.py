import unittest
from pathlib import Path

from adapters.hermes import HermesReferenceAdapter


WORKSPACE = Path(__file__).resolve().parents[1]
HERMES_ROOT = WORKSPACE / "hermes-agent"



class HermesReferenceAdapterTest(unittest.TestCase):
    def test_hermes_reference_adapter_reads_reference_without_mutating(self):
        adapter = HermesReferenceAdapter(str(HERMES_ROOT))
        before = (HERMES_ROOT / "README.md").stat().st_mtime_ns

        self.assertTrue(adapter.is_available())
        readme = adapter.read_reference_file("README.md")
        skills = adapter.list_skill_names()

        after = (HERMES_ROOT / "README.md").stat().st_mtime_ns
        self.assertIn("Hermes", readme)
        self.assertTrue(skills)
        self.assertEqual(before, after)

    def test_hermes_reference_adapter_blocks_path_escape(self):
        adapter = HermesReferenceAdapter(str(HERMES_ROOT))

        with self.assertRaises(ValueError):
            adapter.read_reference_file("../pyproject.toml")
