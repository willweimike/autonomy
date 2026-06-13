import tempfile
import unittest
from pathlib import Path

from autonomy import (
    AutonomyStore,
    ProcedureSkillDraft,
    ProcedureSkillError,
    ProcedureSkillLibrary,
)


def write_skill(
    root: Path,
    name: str,
    description: str,
    *,
    requires_tools=("filesystem.read",),
    platforms=("macos", "linux", "windows"),
    body="# Procedure\n\nFollow the procedure.",
):
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    tools = ", ".join(requires_tools)
    supported_platforms = ", ".join(platforms)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
version: 1.0.0
tags: [test]
platforms: [{supported_platforms}]
requires_tools: [{tools}]
---

{body}
""",
        encoding="utf-8",
    )


class AutonomyNativeProcedureSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name) / "workspace"
        self.global_skills = Path(self.tmpdir.name) / "global-skills"
        self.global_candidates = Path(self.tmpdir.name) / "global-skill-candidates"
        self.workspace.mkdir()
        self.store = AutonomyStore(Path(self.tmpdir.name) / "autonomy.db")
        self.library = ProcedureSkillLibrary(
            self.workspace,
            self.store,
            skills_dir=self.global_skills,
            candidates_dir=self.global_candidates,
        )
        self.tools = {"filesystem.read", "filesystem.list", "search.text", "shell.execute"}

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_global_skill_store_is_only_formal_source(self):
        write_skill(self.global_skills, "orientation", "global description")
        write_skill(self.workspace / "skills", "orientation", "workspace description")

        index = self.library.index(self.tools)

        self.assertEqual(len(index), 1)
        self.assertEqual(index[0].source, "global")
        self.assertEqual(index[0].description, "global description")

    def test_required_tools_platform_and_disabled_state_filter_index(self):
        write_skill(self.global_skills, "available", "available")
        write_skill(
            self.global_skills,
            "missing-tool",
            "missing",
            requires_tools=("browser.navigate",),
        )
        write_skill(
            self.global_skills,
            "wrong-platform",
            "wrong",
            platforms=("unsupported-platform",),
        )
        self.library.index(self.tools, include_disabled=True)
        self.library.disable("available", self.tools)

        self.assertEqual(self.library.index(self.tools), [])

    def test_load_selected_limits_to_three_and_records_usage(self):
        for name in ("one", "two", "three", "four"):
            write_skill(self.global_skills, name, name, body=f"# {name}\n\nsecret-{name}")

        loaded = self.library.load_selected(["one", "two", "three", "four"], self.tools)

        self.assertEqual([skill.summary.name for skill in loaded], ["one", "two", "three"])
        records = {
            record["name"]: record
            for record in self.store.list_procedure_skill_records()
        }
        self.assertEqual(records["one"]["load_count"], 1)
        self.assertEqual(records["four"]["load_count"], 0)

    def test_candidate_directory_is_global_not_scanned_and_approval_targets_global_store(self):
        candidate = self.library.write_candidate(
            ProcedureSkillDraft(
                name="candidate-procedure",
                description="candidate",
                body="# Candidate\n\nFollow steps.",
                requires_tools=("filesystem.read",),
            ),
            source_run_id="run-1",
        )

        self.assertEqual(self.library.index(self.tools), [])
        self.assertTrue(
            (self.global_candidates / candidate["candidate_id"] / "SKILL.md").is_file()
        )
        self.assertFalse((self.workspace / ".autonomy" / "skill-candidates").exists())
        self.assertEqual(candidate["source_workspace"], str(self.workspace.resolve()))
        self.assertEqual(candidate["source_run_id"], "run-1")
        approved = self.library.approve_candidate(candidate["candidate_id"])
        self.assertEqual(approved.summary.source, "global")
        self.assertTrue(
            (self.global_skills / "candidate-procedure" / "SKILL.md").is_file()
        )
        self.assertEqual(self.library.list_candidates(), [])
        with self.assertRaises(FileExistsError):
            self.library.approve_candidate(candidate["candidate_id"])

    def test_candidate_reject_marks_status_without_deleting_skill_md(self):
        candidate = self.library.write_candidate(
            ProcedureSkillDraft(
                name="rejected-procedure",
                description="candidate",
                body="# Candidate\n\nFollow steps.",
            )
        )

        rejected = self.library.reject_candidate(candidate["candidate_id"])

        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(
            (self.global_candidates / candidate["candidate_id"] / "SKILL.md").is_file()
        )
        self.assertEqual(self.library.list_candidates(), [])

    def test_install_bundled_web_browser_skills_and_filter_by_available_tools(self):
        installed = self.library.install_bundled(
            ["code-editing", "web-research", "browser-navigation", "process-management"]
        )

        self.assertEqual(
            [summary.name for summary in installed],
            ["code-editing", "web-research", "browser-navigation", "process-management"],
        )
        web_only = self.library.index({"web.fetch", "web.extract"})
        code_tools = {
            "filesystem.read",
            "filesystem.list",
            "filesystem.write",
            "filesystem.patch",
            "filesystem.search_files",
            "shell.execute",
        }
        browser_tools = {
            "browser.navigate",
            "browser.snapshot",
            "browser.click",
            "browser.type",
            "browser.scroll",
            "browser.back",
            "browser.press",
            "browser.get_images",
            "browser.console",
        }
        process_tools = {
            "shell.execute",
            "process.start",
            "process.poll",
            "process.log",
            "process.wait",
            "process.stop",
        }
        code_only = self.library.index(code_tools)
        browser_only = self.library.index(browser_tools)
        process_only = self.library.index(process_tools)

        self.assertEqual([summary.name for summary in code_only], ["code-editing"])
        self.assertEqual([summary.name for summary in web_only], ["web-research"])
        self.assertEqual([summary.name for summary in browser_only], ["browser-navigation"])
        self.assertEqual([summary.name for summary in process_only], ["process-management"])
        with self.assertRaises(FileExistsError):
            self.library.install_bundled(["web-research"])

    def test_invalid_skill_and_path_escape_are_rejected(self):
        invalid = self.global_skills / "invalid"
        invalid.mkdir(parents=True)
        (invalid / "SKILL.md").write_text("not frontmatter", encoding="utf-8")
        with self.assertRaises(ProcedureSkillError):
            self.library.index(self.tools)

        with self.assertRaises(ProcedureSkillError):
            self.library.approve_candidate("../escape")


if __name__ == "__main__":
    unittest.main()
