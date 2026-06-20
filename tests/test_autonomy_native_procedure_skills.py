import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autonomy.bundled_procedure_skills import (
    BUNDLED_PROCEDURE_SKILLS,
    BUNDLED_SKILLS_DIR,
    _load_bundled_procedure_skills,
    bundled_skill_names,
)
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
        self.workspace_skills = Path(self.tmpdir.name) / "workspace-skills"
        self.workspace_candidates = Path(self.tmpdir.name) / "workspace-skill-candidates"
        self.workspace.mkdir()
        self.store = AutonomyStore(Path(self.tmpdir.name) / "autonomy.db")
        self.library = ProcedureSkillLibrary(
            self.workspace,
            self.store,
            skills_dir=self.workspace_skills,
            candidates_dir=self.workspace_candidates,
        )
        self.tools = {"filesystem.read", "filesystem.list", "search.text", "shell.execute"}

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_configured_skill_store_is_only_formal_source(self):
        write_skill(self.workspace_skills, "orientation", "configured description")
        write_skill(self.workspace / "skills", "orientation", "workspace description")

        index = self.library.index(self.tools)

        self.assertEqual(len(index), 1)
        self.assertEqual(index[0].source, "workspace")
        self.assertEqual(index[0].description, "configured description")

    def test_required_tools_platform_and_disabled_state_filter_index(self):
        write_skill(self.workspace_skills, "available", "available")
        write_skill(
            self.workspace_skills,
            "missing-tool",
            "missing",
            requires_tools=("browser.navigate",),
        )
        write_skill(
            self.workspace_skills,
            "wrong-platform",
            "wrong",
            platforms=("unsupported-platform",),
        )
        self.library.index(self.tools, include_disabled=True)
        self.library.disable("available", self.tools)

        self.assertEqual(self.library.index(self.tools), [])

    def test_load_selected_limits_to_three_and_records_usage(self):
        for name in ("one", "two", "three", "four"):
            write_skill(self.workspace_skills, name, name, body=f"# {name}\n\nsecret-{name}")

        loaded = self.library.load_selected(["one", "two", "three", "four"], self.tools)

        self.assertEqual([skill.summary.name for skill in loaded], ["one", "two", "three"])
        records = {
            record["name"]: record
            for record in self.store.list_procedure_skill_records()
        }
        self.assertEqual(records["one"]["load_count"], 1)
        self.assertEqual(records["four"]["load_count"], 0)

    def test_formal_skill_index_is_cached_until_files_change(self):
        write_skill(self.workspace_skills, "one", "one")
        write_skill(self.workspace_skills, "two", "two")

        with patch.object(
            self.library,
            "_read_skill",
            wraps=self.library._read_skill,
        ) as read_skill:
            self.library.index(self.tools)
            self.library.load_selected(["one"], self.tools)

            self.assertEqual(read_skill.call_count, 2)

            write_skill(self.workspace_skills, "three", "three")
            names = [summary.name for summary in self.library.index(self.tools)]

            self.assertEqual(names, ["one", "three", "two"])
            self.assertEqual(read_skill.call_count, 5)

    def test_candidate_directory_is_not_scanned_and_approval_targets_formal_store(self):
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
            (self.workspace_candidates / candidate["candidate_id"] / "SKILL.md").is_file()
        )
        self.assertFalse((self.workspace / ".autonomy" / "skill-candidates").exists())
        self.assertEqual(candidate["source_workspace"], str(self.workspace.resolve()))
        self.assertEqual(candidate["source_run_id"], "run-1")
        approved = self.library.approve_candidate(candidate["candidate_id"])
        self.assertEqual(approved.summary.source, "workspace")
        self.assertTrue(
            (self.workspace_skills / "candidate-procedure" / "SKILL.md").is_file()
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
            (self.workspace_candidates / candidate["candidate_id"] / "SKILL.md").is_file()
        )
        self.assertEqual(self.library.list_candidates(), [])

    def test_install_bundled_browser_skills_and_filter_by_available_tools(self):
        installed = self.library.install_bundled(
            [
                "code-editing",
                "browser-navigation",
                "process-management",
                "systematic-debugging",
                "test-driven-development",
                "technical-spike",
                "api-debugging",
                "codebase-documentation",
                "requesting-code-review",
                "plan",
                "writing-plans",
                "procedure-skill-authoring",
            ]
        )

        self.assertEqual(
            [summary.name for summary in installed],
            [
                "code-editing",
                "browser-navigation",
                "process-management",
                "systematic-debugging",
                "test-driven-development",
                "technical-spike",
                "api-debugging",
                "codebase-documentation",
                "requesting-code-review",
                "plan",
                "writing-plans",
                "procedure-skill-authoring",
            ],
        )
        code_tools = {
            "filesystem.read",
            "filesystem.list",
            "filesystem.tree",
            "filesystem.write",
            "filesystem.patch",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
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
            "browser.screenshot",
            "browser.get_images",
            "browser.console",
            "browser.dialog",
        }
        process_tools = {
            "shell.execute",
            "process.start",
            "process.poll",
            "process.log",
            "process.wait",
            "process.stop",
        }
        debugging_tools = {
            "filesystem.read",
            "filesystem.tree",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
            "shell.execute",
        }
        editing_tools = {
            "filesystem.read",
            "filesystem.tree",
            "filesystem.write",
            "filesystem.patch",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
            "shell.execute",
        }
        api_tools = {
            "shell.execute",
        }
        documentation_tools = {
            "filesystem.read",
            "filesystem.tree",
            "filesystem.write",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
            "shell.execute",
        }
        review_tools = {
            "filesystem.read",
            "filesystem.tree",
            "filesystem.diff",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
            "shell.execute",
        }
        planning_tools = {
            "filesystem.read",
            "filesystem.read_many",
            "filesystem.tree",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
            "filesystem.stat_many",
        }
        authoring_tools = {
            "filesystem.read",
            "filesystem.read_many",
            "filesystem.tree",
            "filesystem.write",
            "filesystem.patch",
            "filesystem.search_files",
            "filesystem.stat_many",
            "filesystem.syntax_check",
        }
        code_only = self.library.index(code_tools)
        browser_only = self.library.index(browser_tools)
        process_only = self.library.index(process_tools)
        debugging_only = self.library.index(debugging_tools)
        editing_only = self.library.index(editing_tools)
        api_only = self.library.index(api_tools)
        documentation_only = self.library.index(documentation_tools)
        review_only = self.library.index(review_tools)
        planning_only = self.library.index(planning_tools)
        authoring_only = self.library.index(authoring_tools)

        self.assertEqual(
            [summary.name for summary in code_only],
            [
                "api-debugging",
                "code-editing",
                "codebase-documentation",
                "systematic-debugging",
                "test-driven-development",
            ],
        )
        self.assertEqual([summary.name for summary in browser_only], ["browser-navigation"])
        self.assertEqual([summary.name for summary in process_only], ["api-debugging", "process-management"])
        self.assertEqual([summary.name for summary in debugging_only], ["api-debugging", "systematic-debugging"])
        self.assertIn("test-driven-development", [summary.name for summary in editing_only])
        self.assertEqual([summary.name for summary in api_only], ["api-debugging"])
        self.assertIn(
            "codebase-documentation",
            [summary.name for summary in documentation_only],
        )
        self.assertIn(
            "requesting-code-review",
            [summary.name for summary in review_only],
        )
        self.assertEqual([summary.name for summary in planning_only], ["plan", "writing-plans"])
        self.assertEqual(
            [summary.name for summary in authoring_only],
            ["procedure-skill-authoring"],
        )
        with self.assertRaises(FileExistsError):
            self.library.install_bundled(["code-editing"])

    def test_install_bundled_all_includes_software_engineering_skill_pack(self):
        installed = self.library.install_bundled()
        installed_names = {summary.name for summary in installed}

        self.assertTrue(
            {
                "systematic-debugging",
                "test-driven-development",
                "technical-spike",
                "api-debugging",
                "codebase-documentation",
                "requesting-code-review",
                "plan",
                "writing-plans",
                "procedure-skill-authoring",
            }.issubset(installed_names)
        )
        self.assertIn("email-himalaya", installed_names)

    def test_email_himalaya_bundled_skill_uses_governed_shell_only(self):
        installed = self.library.install_bundled(["email-himalaya"])
        available_tools = {"shell.execute"}

        self.assertEqual([summary.name for summary in installed], ["email-himalaya"])
        self.assertEqual(installed[0].requires_tools, ("shell.execute",))
        self.assertEqual(
            [summary.name for summary in self.library.index(available_tools)],
            ["email-himalaya"],
        )
        self.assertEqual(self.library.index(set()), [])

        skill = self.library.view("email-himalaya", available_tools)
        self.assertIn("himalaya --version", skill.body)
        self.assertIn("Do not read secret-bearing config values", skill.body)
        self.assertIn("--output json", skill.body)
        self.assertIn("Do not retry a failed send automatically", skill.body)
        self.assertIn("template send", skill.body)
        self.assertIn("guidance only", skill.body)

    def test_database_retrieval_bundled_skill_requires_database_tool(self):
        installed = self.library.install_bundled(["database-retrieval"])
        available_tools = {"database.retrieve"}

        self.assertEqual([summary.name for summary in installed], ["database-retrieval"])
        self.assertEqual(installed[0].requires_tools, ("database.retrieve",))
        self.assertEqual(
            [summary.name for summary in self.library.index(available_tools)],
            ["database-retrieval"],
        )
        self.assertEqual(self.library.index(set()), [])

        skill = self.library.view("database-retrieval", available_tools)
        self.assertIn("database.retrieve", skill.body)
        self.assertIn("read-only SELECT", skill.body)
        self.assertIn("SQLGlot", skill.body)
        self.assertIn("action: schema", skill.body)
        self.assertIn("action: generate", skill.body)
        self.assertIn("action: query", skill.body)
        self.assertIn("guidance only", skill.body)

    def test_bundled_skill_names_are_loaded_from_skill_files(self):
        skill_files = sorted(BUNDLED_SKILLS_DIR.glob("*/SKILL.md"))
        file_names = tuple(sorted(path.parent.name for path in skill_files))

        self.assertEqual(bundled_skill_names(), file_names)

    def test_bundled_skill_files_parse_with_existing_parser(self):
        for name, content in BUNDLED_PROCEDURE_SKILLS.items():
            with self.subTest(skill=name):
                parsed = self.library._parse_content(
                    content,
                    source="workspace",
                    path=BUNDLED_SKILLS_DIR / name / "SKILL.md",
                )

                self.assertEqual(parsed.summary.name, name)

    def test_bundled_skill_directory_must_match_frontmatter_name(self):
        bundled_root = Path(self.tmpdir.name) / "bundled"
        write_skill(bundled_root, "actual-directory", "wrong name")
        skill_file = bundled_root / "actual-directory" / "SKILL.md"
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8").replace(
                "name: actual-directory",
                "name: frontmatter-name",
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "directory name must match"):
            _load_bundled_procedure_skills(bundled_root)

    def test_software_engineering_skill_pack_content_invariants(self):
        installed = self.library.install_bundled(
            [
                "systematic-debugging",
                "test-driven-development",
                "technical-spike",
                "api-debugging",
                "codebase-documentation",
            ]
        )
        available_tools = {
            "filesystem.read",
            "filesystem.tree",
            "filesystem.write",
            "filesystem.patch",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
            "shell.execute",
            "process.start",
            "process.poll",
            "process.log",
            "process.wait",
            "process.stop",
        }

        for summary in installed:
            with self.subTest(skill=summary.name):
                skill = self.library.view(summary.name, available_tools)
                self.assertIn("Workflow:", skill.body)
                self.assertIn("Tool use rules:", skill.body)
                self.assertIn("Pitfalls:", skill.body)
                self.assertIn("Outcome checks:", skill.body)
                self.assertIn("guidance only", skill.body)
                if summary.name != "api-debugging":
                    self.assertIn("filesystem.outline", skill.body)
                    self.assertIn("filesystem.symbol_search", skill.body)
                    self.assertIn("filesystem.syntax_check", skill.body)

        self.assertIn(
            "root-cause hypothesis",
            self.library.view("systematic-debugging", available_tools).body,
        )
        self.assertIn(
            "RED -> GREEN -> REFACTOR",
            self.library.view("test-driven-development", available_tools).body,
        )
        self.assertIn(
            "production implementation",
            self.library.view("technical-spike", available_tools).body,
        )

    def test_planning_and_skill_authoring_bundled_skills_are_autonomy_native(self):
        installed = self.library.install_bundled(
            ["plan", "writing-plans", "procedure-skill-authoring"]
        )
        available_tools = {
            "filesystem.read",
            "filesystem.read_many",
            "filesystem.tree",
            "filesystem.write",
            "filesystem.patch",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
            "filesystem.stat_many",
        }

        self.assertEqual(
            [summary.name for summary in installed],
            ["plan", "writing-plans", "procedure-skill-authoring"],
        )
        for summary in installed:
            with self.subTest(skill=summary.name):
                skill = self.library.view(summary.name, available_tools)
                self.assertIn("Workflow:", skill.body)
                self.assertIn("Tool use rules:", skill.body)
                self.assertIn("Pitfalls:", skill.body)
                self.assertIn("Outcome checks:", skill.body)
                self.assertIn("guidance only", skill.body)
                self.assertNotIn("~/.hermes", skill.raw_content)
                self.assertNotIn(".hermes/plans", skill.raw_content)

        self.assertIn(
            "decision-complete",
            self.library.view("writing-plans", available_tools).body,
        )
        self.assertIn(
            "autonomy/bundled_skills/<name>/SKILL.md",
            self.library.view("procedure-skill-authoring", available_tools).body,
        )
        self.assertIn(
            "planning-only",
            self.library.view("plan", available_tools).body,
        )

    def test_requesting_code_review_bundled_skill_is_autonomy_native(self):
        installed = self.library.install_bundled(["requesting-code-review"])
        available_tools = {
            "filesystem.read",
            "filesystem.tree",
            "filesystem.diff",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
            "shell.execute",
        }

        self.assertEqual([summary.name for summary in installed], ["requesting-code-review"])
        skill = self.library.view("requesting-code-review", available_tools)

        self.assertIn("Workflow:", skill.body)
        self.assertIn("Tool use rules:", skill.body)
        self.assertIn("Pitfalls:", skill.body)
        self.assertIn("Outcome checks:", skill.body)
        self.assertIn("guidance only", skill.body)
        self.assertIn("filesystem.diff", skill.body)
        self.assertIn("security-sensitive changes", skill.body)
        self.assertNotIn("delegate_task", skill.raw_content)
        self.assertNotIn("~/.hermes", skill.raw_content)

    def test_invalid_skill_and_path_escape_are_rejected(self):
        invalid = self.workspace_skills / "invalid"
        invalid.mkdir(parents=True)
        (invalid / "SKILL.md").write_text("not frontmatter", encoding="utf-8")
        with self.assertRaises(ProcedureSkillError):
            self.library.index(self.tools)

        with self.assertRaises(ProcedureSkillError):
            self.library.approve_candidate("../escape")


if __name__ == "__main__":
    unittest.main()
