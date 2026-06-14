import json
import tempfile
import unittest
from pathlib import Path

from autonomy.storage import migrate_legacy_storage, workspace_autonomy_home, workspace_db_path


class AutonomyWorkspaceStorageTest(unittest.TestCase):
    def test_workspace_paths_are_project_local(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"

            self.assertEqual(workspace_autonomy_home(workspace), workspace.resolve() / ".autonomy")
            self.assertEqual(workspace_db_path(workspace), workspace.resolve() / ".autonomy" / "autonomy.db")

    def test_migrates_legacy_storage_to_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            legacy = root / "home" / ".autonomy"
            legacy.mkdir(parents=True)
            for file_name in ("config.yaml", ".env", "tools.yaml", "autonomy.db"):
                (legacy / file_name).write_text(file_name, encoding="utf-8")
            (legacy / "skills" / "one").mkdir(parents=True)
            (legacy / "skills" / "one" / "SKILL.md").write_text("skill", encoding="utf-8")
            (legacy / "skill-candidates" / "candidate").mkdir(parents=True)
            (legacy / "skill-candidates" / "candidate" / "SKILL.md").write_text(
                "candidate",
                encoding="utf-8",
            )

            result = migrate_legacy_storage(
                workspace,
                legacy_home=legacy,
                trash_binary="definitely-not-installed-trash",
            )

            target = workspace / ".autonomy"
            self.assertTrue((target / "config.yaml").is_file())
            self.assertTrue((target / ".env").is_file())
            self.assertTrue((target / "tools.yaml").is_file())
            self.assertTrue((target / "autonomy.db").is_file())
            self.assertTrue((target / "skills" / "one" / "SKILL.md").is_file())
            self.assertTrue((target / "skill-candidates" / "candidate" / "SKILL.md").is_file())
            self.assertTrue((target / "storage-migration.json").is_file())
            self.assertEqual(len(result["migrated"]), 6)

    def test_migration_conflicts_keep_workspace_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            target = workspace / ".autonomy"
            legacy = root / "home" / ".autonomy"
            (target / "skills" / "same").mkdir(parents=True)
            (target / "skills" / "same" / "SKILL.md").write_text("workspace", encoding="utf-8")
            (legacy / "skills" / "same").mkdir(parents=True)
            (legacy / "skills" / "same" / "SKILL.md").write_text("legacy", encoding="utf-8")

            result = migrate_legacy_storage(
                workspace,
                legacy_home=legacy,
                trash_binary="definitely-not-installed-trash",
            )

            self.assertEqual((target / "skills" / "same" / "SKILL.md").read_text(), "workspace")
            conflict = target / "migration-conflicts" / "skills" / "same" / "SKILL.md"
            self.assertEqual(conflict.read_text(), "legacy")
            self.assertEqual(len(result["conflicts"]), 1)

    def test_migration_marker_prevents_second_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            target = workspace / ".autonomy"
            legacy = root / "home" / ".autonomy"
            target.mkdir(parents=True)
            marker = target / "storage-migration.json"
            marker.write_text(json.dumps({"version": 1, "migrated": ["already"]}), encoding="utf-8")
            legacy.mkdir(parents=True)
            (legacy / "config.yaml").write_text("legacy", encoding="utf-8")

            result = migrate_legacy_storage(workspace, legacy_home=legacy)

            self.assertEqual(result["migrated"], ["already"])
            self.assertFalse((target / "config.yaml").exists())


if __name__ == "__main__":
    unittest.main()
