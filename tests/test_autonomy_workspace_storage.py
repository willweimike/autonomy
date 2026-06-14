import json
import tempfile
import unittest
from pathlib import Path

from autonomy.storage import workspace_autonomy_home, workspace_db_path


class AutonomyWorkspaceStorageTest(unittest.TestCase):
    def test_workspace_paths_are_project_local(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"

            self.assertEqual(workspace_autonomy_home(workspace), workspace.resolve() / ".autonomy")
            self.assertEqual(workspace_db_path(workspace), workspace.resolve() / ".autonomy" / "autonomy.db")

    


if __name__ == "__main__":
    unittest.main()
