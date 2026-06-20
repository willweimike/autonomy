import unittest
import json
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from autonomy import (
    Action,
    ActionIntent,
    ApprovalPolicy,
    RiskLevel,
    TOOLSET_CATALOG,
    ToolsetConfiguration,
    build_local_tool_registry,
    toolset_catalog_status,
)
from autonomy.tools import ToolRegistry
from autonomy.tools.toolsets.browser import (
    BrowserController,
    browser_tools_available,
    register_browser_tools,
)


class AutonomyNativeToolsTest(unittest.TestCase):
    def test_tool_modules_are_grouped_under_tools_package(self):
        import autonomy.tools as tools_package

        self.assertTrue(Path(tools_package.__file__).match("*/autonomy/tools/__init__.py"))
        self.assertFalse((Path(__file__).resolve().parents[1] / "autonomy" / "browser_tools.py").exists())
        self.assertFalse((Path(__file__).resolve().parents[1] / "autonomy" / "process_tools.py").exists())
        self.assertFalse((Path(__file__).resolve().parents[1] / "autonomy" / "web_tools.py").exists())

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
            paged_search = registry.execute(
                Action(
                    "search.text",
                    {"path": ".", "query": "needle", "limit": 1, "offset": 0},
                    "paged search",
                    "verify",
                )
            )
            invalid_search = registry.rejection_reason(
                ActionIntent(
                    "search.text",
                    {"path": ".", "query": "needle", "offset": -1},
                    "bad search",
                )
            )
            shell = registry.execute(Action("shell.execute", {"command": "pwd"}, "pwd", "verify"))

            self.assertTrue(read.succeeded)
            self.assertEqual(action.purpose, "read sample")
            self.assertEqual(registry.spec("filesystem.read").toolset, "file")
            self.assertEqual(registry.spec("filesystem.imports").toolset, "file")
            self.assertEqual(registry.spec("filesystem.diff").toolset, "file")
            self.assertEqual(registry.spec("filesystem.list").toolset, "file")
            self.assertEqual(registry.spec("filesystem.tree").toolset, "file")
            self.assertEqual(registry.spec("filesystem.stat_many").toolset, "file")
            self.assertEqual(registry.spec("filesystem.outline").toolset, "file")
            self.assertEqual(registry.spec("filesystem.symbol_search").toolset, "file")
            self.assertEqual(registry.spec("filesystem.syntax_check").toolset, "file")
            self.assertEqual(registry.spec("search.text").toolset, "search")
            self.assertEqual(registry.spec("shell.execute").toolset, "terminal")
            self.assertEqual(registry.spec("process.start").toolset, "terminal")
            self.assertEqual(
                registry.contracts["filesystem.read"],
                {
                    "path": "string",
                    "offset": "integer line number, 1-indexed (optional)",
                    "limit": "integer max lines, default 500, max 2000 (optional)",
                },
            )
            self.assertEqual(read.output, "needle\n")
            self.assertIn("sample.txt", listing.output)
            self.assertIn("filesystem.imports", registry.contracts)
            self.assertIn("filesystem.diff", registry.contracts)
            self.assertIn("filesystem.tree", registry.contracts)
            self.assertIn("filesystem.stat_many", registry.contracts)
            self.assertIn("filesystem.outline", registry.contracts)
            self.assertIn("filesystem.symbol_search", registry.contracts)
            self.assertIn("filesystem.syntax_check", registry.contracts)
            self.assertIn("sample.txt:1:needle", search.output)
            self.assertIn("sample.txt:1:needle", paged_search.output)
            self.assertIn("offset must be at least 0", invalid_search)
            self.assertEqual(shell.exit_code, 0)

    def test_shell_execute_bounds_large_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
            command = f"{sys.executable} -c \"print('A' * 300)\""

            observation = registry.execute(
                Action(
                    "shell.execute",
                    {"command": command, "max_chars": 80},
                    "large output",
                    "verify",
                )
            )
            invalid = registry.rejection_reason(
                ActionIntent(
                    "shell.execute",
                    {"command": "pwd", "max_chars": 0},
                    "bad max chars",
                )
            )

        self.assertTrue(observation.succeeded)
        self.assertLess(len(observation.output), 301)
        self.assertIn("Output truncated", observation.output)
        self.assertIn("stdout_truncated:true", observation.evidence)
        self.assertIn("max_chars must be at least 1", invalid)

    def test_shell_execute_redacts_secret_like_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
            secret = "sk-testsecretvalue1234567890"
            command = f"{sys.executable} -c \"print('OPENAI_API_KEY={secret}')\""

            observation = registry.execute(
                Action(
                    "shell.execute",
                    {"command": command},
                    "print secret",
                    "verify",
                )
            )

        self.assertTrue(observation.succeeded)
        self.assertNotIn(secret, observation.output)
        self.assertIn("OPENAI_API_KEY=***", observation.output)
        self.assertIn("stdout_redacted:true", observation.evidence)

    def test_shell_execute_supports_shell_operators(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)

            observation = registry.execute(
                Action(
                    "shell.execute",
                    {"command": "false || printf recovered"},
                    "recover with shell operator",
                    "verify",
                )
            )

        self.assertTrue(observation.succeeded)
        self.assertEqual(observation.output, "recovered")
        self.assertEqual(observation.exit_code, 0)

    def test_filesystem_read_supports_line_pagination_for_large_contexts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "large.txt").write_text(
                "\n".join(f"line-{index}" for index in range(1, 8)) + "\n",
                encoding="utf-8",
            )
            registry = build_local_tool_registry(root)

            window = registry.execute(
                Action(
                    "filesystem.read",
                    {"path": "large.txt", "offset": 3, "limit": 2},
                    "read window",
                    "verify",
                )
            )
            invalid = registry.rejection_reason(
                ActionIntent(
                    "filesystem.read",
                    {"path": "large.txt", "offset": 0},
                    "bad read",
                )
            )

        self.assertTrue(window.succeeded)
        self.assertIn("3|line-3", window.output)
        self.assertIn("4|line-4", window.output)
        self.assertNotIn("5|line-5", window.output)
        self.assertIn("Use offset=5", window.output)
        self.assertIn("offset must be at least 1", invalid)

    def test_assistant_respond_tool_returns_direct_reply(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
            observation = registry.execute(
                Action(
                    "assistant.respond",
                    {"response": "你好，我是 Autonomy。"},
                    "answer user",
                    "verify",
                )
            )

        self.assertTrue(observation.succeeded)
        self.assertEqual(observation.output, "你好，我是 Autonomy。")
        self.assertIn("assistant_response", observation.evidence)

    def test_filesystem_read_many_batches_bounded_text_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("# Demo\nfirst\nsecond\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            batch = registry.execute(
                Action(
                    "filesystem.read_many",
                    {
                        "paths": ["README.md", "pyproject.toml", "missing.txt"],
                        "limit": 2,
                        "max_chars": 200,
                    },
                    "read batch",
                    "verify",
                )
            )
            truncated = registry.execute(
                Action(
                    "filesystem.read_many",
                    {
                        "paths": ["README.md", "pyproject.toml"],
                        "limit": 20,
                        "max_chars": 20,
                    },
                    "read tiny batch",
                    "verify",
                )
            )
            env_reason = registry.rejection_reason(
                ActionIntent("filesystem.read_many", {"paths": [".env"]}, "read env")
            )
            escape_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.read_many",
                    {"paths": ["README.md", "../outside"]},
                    "escape",
                )
            )
            too_many_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.read_many",
                    {"paths": [f"file-{index}.txt" for index in range(13)]},
                    "too many",
                )
            )

        payload = json.loads(batch.output)
        truncated_payload = json.loads(truncated.output)
        self.assertTrue(batch.succeeded)
        self.assertEqual(payload["succeeded_count"], 2)
        self.assertEqual(payload["error_count"], 1)
        self.assertEqual(payload["files"][0]["path"], "README.md")
        self.assertIn("1|# Demo", payload["files"][0]["content"])
        self.assertIn("revision", payload["files"][0])
        self.assertNotIn("3|second", payload["files"][0]["content"])
        self.assertFalse(payload["files"][2]["succeeded"])
        self.assertIn("not a file", payload["files"][2]["error"])
        self.assertTrue(truncated.succeeded)
        self.assertTrue(truncated_payload["truncated"])
        self.assertIn("max_chars reached", truncated.output)
        self.assertIn("secret-bearing environment file", env_reason)
        self.assertIn("path escapes workspace", escape_reason)
        self.assertIn("at most 12", too_many_reason)

    def test_filesystem_list_supports_entry_pagination_for_large_contexts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for filename in ("alpha.txt", "beta.txt", "gamma.txt"):
                (root / filename).write_text(filename, encoding="utf-8")
            registry = build_local_tool_registry(root)

            first_page = registry.execute(
                Action(
                    "filesystem.list",
                    {"path": ".", "limit": 2},
                    "list first page",
                    "verify",
                )
            )
            second_page = registry.execute(
                Action(
                    "filesystem.list",
                    {"path": ".", "limit": 2, "offset": 2},
                    "list second page",
                    "verify",
                )
            )
            invalid = registry.rejection_reason(
                ActionIntent(
                    "filesystem.list",
                    {"path": ".", "offset": -1},
                    "bad list",
                )
            )

        self.assertTrue(first_page.succeeded)
        self.assertIn("alpha.txt", first_page.output)
        self.assertIn("beta.txt", first_page.output)
        self.assertNotIn("gamma.txt", first_page.output)
        self.assertIn("Use offset=2", first_page.output)
        self.assertIn("list_window:0:2:3", first_page.evidence)
        self.assertTrue(second_page.succeeded)
        self.assertIn("gamma.txt", second_page.output)
        self.assertIn("reached end of results", second_page.output)
        self.assertIn("offset must be at least 0", invalid)

    def test_filesystem_tree_returns_compact_bounded_workspace_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src" / "pkg").mkdir(parents=True)
            (root / "src" / "pkg" / "core.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "src" / "main.py").write_text("from pkg import core\n", encoding="utf-8")
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("hidden\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            tree = registry.execute(
                Action(
                    "filesystem.tree",
                    {"path": ".", "depth": 2, "max_entries": 10},
                    "tree",
                    "verify",
                )
            )
            dirs_only = registry.execute(
                Action(
                    "filesystem.tree",
                    {"path": ".", "depth": 3, "include_files": False},
                    "tree dirs",
                    "verify",
                )
            )
            truncated = registry.execute(
                Action(
                    "filesystem.tree",
                    {"path": ".", "depth": 3, "max_entries": 2},
                    "tree small",
                    "verify",
                )
            )
            invalid = registry.rejection_reason(
                ActionIntent("filesystem.tree", {"path": "../outside"}, "bad tree")
            )

        self.assertTrue(tree.succeeded)
        self.assertIn("./", tree.output)
        self.assertIn("|-- src/", tree.output)
        self.assertIn("README.md", tree.output)
        self.assertIn("pkg/", tree.output)
        self.assertNotIn(".env", tree.output)
        self.assertNotIn(".git", tree.output)
        self.assertTrue(any(item.startswith("tree_entries:") for item in tree.evidence))
        self.assertTrue(dirs_only.succeeded)
        self.assertIn("src/", dirs_only.output)
        self.assertNotIn("README.md", dirs_only.output)
        self.assertTrue(truncated.succeeded)
        self.assertIn("tree truncated", truncated.output)
        self.assertIn("truncated:true", truncated.evidence)
        self.assertIn("path escapes workspace", invalid)

    def test_filesystem_stat_returns_bounded_metadata_without_reading_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("SECRET_VALUE\n", encoding="utf-8")
            (root / "src" / "notes.txt").write_text("notes\n", encoding="utf-8")
            (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            file_stat = registry.execute(
                Action("filesystem.stat", {"path": "src/app.py"}, "stat file", "verify")
            )
            dir_stat = registry.execute(
                Action("filesystem.stat", {"path": "src"}, "stat dir", "verify")
            )
            missing = registry.execute(
                Action("filesystem.stat", {"path": "src/ap.py"}, "stat missing", "verify")
            )
            secret_reason = registry.rejection_reason(
                ActionIntent("filesystem.stat", {"path": ".env"}, "stat env")
            )
            escape_reason = registry.rejection_reason(
                ActionIntent("filesystem.stat", {"path": "../outside"}, "stat outside")
            )

        file_payload = json.loads(file_stat.output)
        dir_payload = json.loads(dir_stat.output)
        self.assertTrue(file_stat.succeeded)
        self.assertEqual(file_payload["path"], "src/app.py")
        self.assertEqual(file_payload["type"], "file")
        self.assertEqual(file_payload["suffix"], ".py")
        self.assertFalse(file_payload["binary_like"])
        self.assertNotIn("SECRET_VALUE", file_stat.output)
        self.assertIn("permissions_octal", file_payload)
        self.assertIn("modified_time", file_payload)
        self.assertIn("revision", file_payload)
        self.assertTrue(dir_stat.succeeded)
        self.assertEqual(dir_payload["type"], "directory")
        self.assertEqual(dir_payload["children_count"], 2)
        self.assertEqual(dir_payload["file_count"], 2)
        self.assertFalse(missing.succeeded)
        self.assertIn("Similar paths", missing.error)
        self.assertIn("secret-bearing environment file", secret_reason)
        self.assertIn("path escapes workspace", escape_reason)

    def test_filesystem_stat_many_batches_bounded_metadata_without_reading_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("SECRET_VALUE\n", encoding="utf-8")
            (root / "src" / "notes.txt").write_text("notes\n", encoding="utf-8")
            (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            stat_many = registry.execute(
                Action(
                    "filesystem.stat_many",
                    {"paths": ["src", "src/app.py", "src/missing.py"]},
                    "stat many",
                    "verify",
                )
            )
            secret_reason = registry.rejection_reason(
                ActionIntent("filesystem.stat_many", {"paths": [".env"]}, "stat env")
            )
            escape_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.stat_many",
                    {"paths": ["src", "../outside"]},
                    "stat outside",
                )
            )
            too_many_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.stat_many",
                    {"paths": [f"path-{index}" for index in range(51)]},
                    "too many stats",
                )
            )

        payload = json.loads(stat_many.output)
        self.assertTrue(stat_many.succeeded)
        self.assertEqual(payload["succeeded_count"], 2)
        self.assertEqual(payload["error_count"], 1)
        self.assertEqual(payload["paths"][0]["path"], "src")
        self.assertEqual(payload["paths"][0]["type"], "directory")
        self.assertIn("revision", payload["paths"][0])
        self.assertEqual(payload["paths"][0]["file_count"], 2)
        self.assertEqual(payload["paths"][1]["path"], "src/app.py")
        self.assertEqual(payload["paths"][1]["type"], "file")
        self.assertIn("revision", payload["paths"][1])
        self.assertEqual(payload["paths"][1]["suffix"], ".py")
        self.assertFalse(payload["paths"][2]["succeeded"])
        self.assertIn("path not found", payload["paths"][2]["error"])
        self.assertNotIn("SECRET_VALUE", stat_many.output)
        self.assertIn("secret-bearing environment file", secret_reason)
        self.assertIn("path escapes workspace", escape_reason)
        self.assertIn("at most 50", too_many_reason)

    def test_filesystem_diff_returns_bounded_git_diff_without_secret_env_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
            (root / ".env").write_text("SAFE=1\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "tracked.txt", ".env"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            (root / "tracked.txt").write_text("initial\nupdated\n", encoding="utf-8")
            (root / ".env").write_text("SECRET_VALUE=secret-value\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            diff = registry.execute(
                Action(
                    "filesystem.diff",
                    {"path": ".", "max_chars": 1000},
                    "inspect diff",
                    "verify",
                )
            )
            stat_only = registry.execute(
                Action(
                    "filesystem.diff",
                    {"path": ".", "stat_only": True},
                    "inspect diff stat",
                    "verify",
                )
            )
            secret_reason = registry.rejection_reason(
                ActionIntent("filesystem.diff", {"path": ".env"}, "diff env")
            )
            escape_reason = registry.rejection_reason(
                ActionIntent("filesystem.diff", {"path": "../outside"}, "diff outside")
            )

        payload = json.loads(diff.output)
        stat_payload = json.loads(stat_only.output)
        self.assertTrue(diff.succeeded)
        self.assertIn("tracked.txt", payload["status_short"][0])
        self.assertEqual(payload["changed_files"], ["tracked.txt"])
        self.assertIn("+updated", payload["diff"])
        self.assertNotIn("secret-value", diff.output)
        self.assertNotIn(".env", diff.output)
        self.assertGreaterEqual(payload["omitted_secret_paths"], 1)
        self.assertTrue(stat_only.succeeded)
        self.assertEqual(stat_payload["diff"], "")
        self.assertTrue(stat_payload["stat_only"])
        self.assertIn("tracked.txt", stat_payload["diff_stat"])
        self.assertIn("secret-bearing environment file", secret_reason)
        self.assertIn("path escapes workspace", escape_reason)

    def test_filesystem_outline_returns_compact_python_symbols(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text(
                """
def top_level(a, b=1):
    def inner():
        pass
    return a + b

async def fetch(value):
    return value

class Service:
    def run(self, item):
        return item

    async def _private(self):
        return None
""".lstrip(),
                encoding="utf-8",
            )
            (root / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
            (root / "notes.txt").write_text("def not_python(): pass\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            file_outline = registry.execute(
                Action(
                    "filesystem.outline",
                    {"path": "src/app.py"},
                    "outline file",
                    "verify",
                )
            )
            private_outline = registry.execute(
                Action(
                    "filesystem.outline",
                    {"path": "src/app.py", "include_private": True},
                    "outline private",
                    "verify",
                )
            )
            directory_outline = registry.execute(
                Action(
                    "filesystem.outline",
                    {"path": "src", "limit": 3},
                    "outline dir",
                    "verify",
                )
            )
            broken = registry.execute(
                Action(
                    "filesystem.outline",
                    {"path": "src/broken.py"},
                    "outline broken",
                    "verify",
                )
            )
            invalid = registry.rejection_reason(
                ActionIntent("filesystem.outline", {"path": "notes.txt"}, "outline text")
            )

        self.assertTrue(file_outline.succeeded)
        self.assertIn("src/app.py:1:function top_level(a, b)", file_outline.output)
        self.assertIn("src/app.py:6:async_function fetch(value)", file_outline.output)
        self.assertIn("src/app.py:9:class Service", file_outline.output)
        self.assertIn("src/app.py:10:method Service.run(self, item)", file_outline.output)
        self.assertNotIn("_private", file_outline.output)
        self.assertIn("async_method Service._private(self)", private_outline.output)
        self.assertTrue(directory_outline.succeeded)
        self.assertIn("Use offset=3", directory_outline.output)
        self.assertTrue(any(item.startswith("outline_symbols:") for item in directory_outline.evidence))
        self.assertFalse(broken.succeeded)
        self.assertIn("syntax_error", broken.error)
        self.assertIn("currently supports Python files", invalid)

    def test_filesystem_imports_returns_python_import_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text(
                """
import os
import pathlib as pl
from autonomy.tools import ToolRegistry
from .local import helper
""".lstrip(),
                encoding="utf-8",
            )
            (root / "src" / "tests.py").write_text(
                "from pytest import raises\nimport unittest\n",
                encoding="utf-8",
            )
            (root / "src" / "broken.py").write_text("import \n", encoding="utf-8")
            (root / "notes.txt").write_text("import nope\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            imports = registry.execute(
                Action(
                    "filesystem.imports",
                    {"path": "src", "limit": 10},
                    "imports",
                    "verify",
                )
            )
            filtered = registry.execute(
                Action(
                    "filesystem.imports",
                    {"path": "src", "module_filter": "pytest"},
                    "pytest imports",
                    "verify",
                )
            )
            broken = registry.execute(
                Action(
                    "filesystem.imports",
                    {"path": "src/broken.py"},
                    "broken imports",
                    "verify",
                )
            )
            invalid = registry.rejection_reason(
                ActionIntent("filesystem.imports", {"path": "notes.txt"}, "bad imports")
            )

        self.assertTrue(imports.succeeded)
        self.assertIn("src/app.py:1:import os", imports.output)
        self.assertIn("src/app.py:2:import pathlib", imports.output)
        self.assertIn("src/app.py:3:from autonomy.tools import ToolRegistry", imports.output)
        self.assertIn("src/app.py:4:from .local import helper", imports.output)
        self.assertIn("import_errors:1", imports.evidence)
        self.assertEqual(filtered.output, "src/tests.py:1:from pytest import raises")
        self.assertFalse(broken.succeeded)
        self.assertIn("syntax_error", broken.error)
        self.assertIn("currently supports Python files", invalid)

    def test_filesystem_symbol_search_finds_python_definitions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "service.py").write_text(
                """
class Service:
    def run(self):
        return True

    def _hidden(self):
        return False

def run_task():
    return Service().run()
""".lstrip(),
                encoding="utf-8",
            )
            (root / "src" / "other.py").write_text(
                "async def runner():\n    return None\n",
                encoding="utf-8",
            )
            (root / "notes.txt").write_text("class Nope: pass\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            contains = registry.execute(
                Action(
                    "filesystem.symbol_search",
                    {"path": "src", "query": "run"},
                    "find symbols",
                    "verify",
                )
            )
            exact_method = registry.execute(
                Action(
                    "filesystem.symbol_search",
                    {"path": "src", "query": "run", "match": "exact", "kind": "method"},
                    "find exact method",
                    "verify",
                )
            )
            regex_private = registry.execute(
                Action(
                    "filesystem.symbol_search",
                    {
                        "path": "src",
                        "query": "^Service\\._",
                        "match": "regex",
                        "include_private": True,
                    },
                    "find private",
                    "verify",
                )
            )
            invalid = registry.rejection_reason(
                ActionIntent("filesystem.symbol_search", {"path": "notes.txt", "query": "Nope"}, "bad")
            )
            invalid_match = registry.rejection_reason(
                ActionIntent(
                    "filesystem.symbol_search",
                    {"path": "src", "query": "run", "match": "glob"},
                    "bad match",
                )
            )

        self.assertTrue(contains.succeeded)
        self.assertIn("src/service.py:2:method Service.run(self)", contains.output)
        self.assertIn("src/service.py:8:function run_task()", contains.output)
        self.assertIn("src/other.py:1:async_function runner()", contains.output)
        self.assertTrue(any(item.startswith("symbol_matches:") for item in contains.evidence))
        self.assertEqual(exact_method.output, "src/service.py:2:method Service.run(self)")
        self.assertIn("src/service.py:5:method Service._hidden(self)", regex_private.output)
        self.assertIn("currently supports Python files", invalid)
        self.assertIn("match must be contains, exact, or regex", invalid_match)

    def test_filesystem_syntax_check_reports_python_diagnostics_without_executing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pkg").mkdir()
            (root / "pkg" / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
            (root / "pkg" / "bad.py").write_text("def bad(:\n", encoding="utf-8")
            (root / "notes.txt").write_text("def not_python(:\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            ok = registry.execute(
                Action(
                    "filesystem.syntax_check",
                    {"path": "pkg/ok.py"},
                    "syntax ok",
                    "verify",
                )
            )
            directory = registry.execute(
                Action(
                    "filesystem.syntax_check",
                    {"path": "pkg"},
                    "syntax dir",
                    "verify",
                )
            )
            invalid = registry.rejection_reason(
                ActionIntent("filesystem.syntax_check", {"path": "notes.txt"}, "syntax text")
            )
            offset_reason = registry.rejection_reason(
                ActionIntent("filesystem.syntax_check", {"path": "pkg", "offset": -1}, "bad offset")
            )

        self.assertTrue(ok.succeeded)
        self.assertIn("OK: checked 1 Python file", ok.output)
        self.assertIn("syntax_ok:true", ok.evidence)
        self.assertTrue(directory.succeeded)
        self.assertIn("pkg/bad.py:1", directory.output)
        self.assertIn("syntax_error", directory.output)
        self.assertIn("syntax_errors:1", directory.evidence)
        self.assertIn("syntax_ok:false", directory.evidence)
        self.assertIn("currently supports Python files", invalid)
        self.assertIn("offset must be at least 0", offset_reason)

    def test_file_tools_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
            intent = ActionIntent("filesystem.read", {"path": "../outside"}, "read outside")

            self.assertIn("path escapes workspace", registry.rejection_reason(intent))
            observation = registry.execute(registry.action_from_intent(intent))

            self.assertFalse(observation.succeeded)
            self.assertIn("path escapes workspace", observation.error)

    def test_file_tools_suggest_similar_workspace_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / ".git").mkdir()
            (root / ".git" / "main.py").write_text("hidden\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            missing_read = registry.execute(
                Action(
                    "filesystem.read",
                    {"path": "src/mian.py"},
                    "read typo",
                    "verify",
                )
            )
            missing_list = registry.execute(
                Action(
                    "filesystem.list",
                    {"path": "doc"},
                    "list typo",
                    "verify",
                )
            )
            missing_search = registry.execute(
                Action(
                    "search.text",
                    {"path": "soruce", "query": "ok"},
                    "search typo",
                    "verify",
                )
            )

        self.assertFalse(missing_read.succeeded)
        self.assertIn("Similar paths:", missing_read.error)
        self.assertIn("src/main.py", missing_read.error)
        self.assertNotIn(".git/main.py", missing_read.error)
        self.assertFalse(missing_list.succeeded)
        self.assertIn("docs", missing_list.error)
        self.assertFalse(missing_search.succeeded)
        self.assertIn("src", missing_search.error)

    def test_file_tools_block_secret_environment_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".env").write_text("OPENAI_API_KEY=secret-value\n", encoding="utf-8")
            (root / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
            (root / ".autonomy").mkdir()
            (root / ".autonomy" / ".env").write_text("OPENAI_API_KEY=workspace-secret\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            denied_read = registry.execute(
                Action("filesystem.read", {"path": ".env"}, "read secret", "verify")
            )
            denied_read_reason = registry.rejection_reason(
                ActionIntent("filesystem.read", {"path": ".env"}, "read secret")
            )
            example_read = registry.execute(
                Action("filesystem.read", {"path": ".env.example"}, "read example", "verify")
            )
            denied_write = registry.execute(
                Action(
                    "filesystem.write",
                    {"path": ".env", "content": "OPENAI_API_KEY=changed\n"},
                    "write secret",
                    "verify",
                )
            )
            denied_patch = registry.execute(
                Action(
                    "filesystem.patch",
                    {
                        "path": ".env",
                        "old_string": "secret-value",
                        "new_string": "changed",
                    },
                    "patch secret",
                    "verify",
                )
            )
            listing = registry.execute(
                Action("filesystem.list", {"path": ".", "recursive": True}, "list", "verify")
            )
            direct_search = registry.execute(
                Action(
                    "search.text",
                    {"path": ".env", "query": "secret-value"},
                    "search secret",
                    "verify",
                )
            )
            broad_search = registry.execute(
                Action(
                    "search.text",
                    {"path": ".", "query": "secret-value"},
                    "search all",
                    "verify",
                )
            )
            file_search = registry.execute(
                Action(
                    "filesystem.search_files",
                    {"path": ".", "pattern": ".env*", "target": "files"},
                    "find env files",
                    "verify",
                )
            )
            env_content_after_tools = (root / ".env").read_text(encoding="utf-8")

        self.assertFalse(denied_read.succeeded)
        self.assertIn("secret-bearing environment file", denied_read.error)
        self.assertIn("secret-bearing environment file", denied_read_reason)
        self.assertNotIn("secret-value", denied_read.error)
        self.assertTrue(example_read.succeeded)
        self.assertIn("OPENAI_API_KEY=", example_read.output)
        self.assertFalse(denied_write.succeeded)
        self.assertFalse(denied_patch.succeeded)
        self.assertEqual(env_content_after_tools, "OPENAI_API_KEY=secret-value\n")
        self.assertNotIn(".env\n", f"{listing.output}\n")
        self.assertNotIn(".autonomy/.env", listing.output)
        self.assertFalse(direct_search.succeeded)
        self.assertNotIn("secret-value", direct_search.error)
        self.assertNotIn("secret-value", broad_search.output)
        self.assertIn(".env.example", file_search.output)
        self.assertNotIn(".autonomy/.env", file_search.output)

    def test_default_toolsets_expose_mvp_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir, ToolsetConfiguration())

        self.assertEqual(
            sorted(registry.names),
            sorted(
                [
                    "assistant.respond",
                    "filesystem.diff",
                    "filesystem.imports",
                    "filesystem.list",
                    "filesystem.outline",
                    "filesystem.patch",
                    "filesystem.read",
                    "filesystem.read_many",
                    "filesystem.search_files",
                    "filesystem.stat",
                    "filesystem.stat_many",
                    "filesystem.symbol_search",
                    "filesystem.syntax_check",
                    "filesystem.mkdir",
                    "filesystem.move",
                    "filesystem.tree",
                    "filesystem.write",
                    "memory.forget",
                    "memory.list",
                    "memory.recall",
                    "memory.remember",
                    "process.log",
                    "process.poll",
                    "process.start",
                    "process.stop",
                    "process.wait",
                    "search.text",
                    "shell.execute",
                ]
                + (["filesystem.trash"] if "filesystem.trash" in registry.names else [])
            ),
        )

    def test_project_toolset_exposes_read_only_project_tools_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "pyproject.toml").write_text(
                "[project]\nname = \"demo\"\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n",
                encoding="utf-8",
            )
            (root / "package.json").write_text(
                json.dumps({"scripts": {"test": "node --test", "dev": "vite"}}),
                encoding="utf-8",
            )
            (root / "data.json").write_text("{\"ok\": true}\n", encoding="utf-8")
            (root / "config.yaml").write_text("ok: true\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-c", "user.email=a@example.test", "-c", "user.name=A", "commit", "-m", "init"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            (root / "data.json").write_text("{\"ok\": false}\n", encoding="utf-8")
            registry = build_local_tool_registry(
                root,
                ToolsetConfiguration(enabled_toolsets=("project",)),
            )

            self.assertEqual(
                sorted(registry.names),
                [
                    "git.diff",
                    "git.log",
                    "git.show",
                    "git.status",
                    "json.parse",
                    "project.detect",
                    "python.test_discover",
                    "yaml.parse",
                ],
            )
            self.assertEqual(registry.spec("git.status").toolset, "project")
            self.assertEqual(registry.spec("git.status").default_risk, RiskLevel.LOW)

            status = registry.execute(Action("git.status", {}, "status", "verify"))
            diff = registry.execute(Action("git.diff", {"max_chars": 80}, "diff", "verify"))
            log = registry.execute(Action("git.log", {"limit": 1}, "log", "verify"))
            show = registry.execute(Action("git.show", {"revision": "HEAD", "max_chars": 1000}, "show", "verify"))
            parsed_json = registry.execute(Action("json.parse", {"path": "data.json"}, "json", "verify"))
            parsed_yaml = registry.execute(Action("yaml.parse", {"path": "config.yaml"}, "yaml", "verify"))
            detected = registry.execute(Action("project.detect", {}, "detect", "verify"))
            tests = registry.execute(Action("python.test_discover", {}, "tests", "verify"))

        self.assertTrue(status.succeeded)
        self.assertIn("data.json", status.output)
        self.assertTrue(diff.succeeded)
        self.assertIn("truncated", diff.output)
        self.assertTrue(log.succeeded)
        self.assertIn("init", log.output)
        self.assertTrue(show.succeeded)
        self.assertIn("pyproject.toml", show.output)
        self.assertEqual(json.loads(parsed_json.output)["parsed"], {"ok": False})
        self.assertEqual(json.loads(parsed_yaml.output)["parsed"], {"ok": True})
        self.assertTrue(any("pytest" in command for command in json.loads(tests.output)["commands"]))
        detect_payload = json.loads(detected.output)
        self.assertIn("pyproject.toml", detect_payload["manifests"])
        self.assertIn("npm test", detect_payload["commands"]["test"])

    def test_project_toolset_is_not_enabled_by_default_and_rejects_escaping_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            default_registry = build_local_tool_registry(tmpdir, ToolsetConfiguration())
            project_registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("project",)),
            )

            reason = project_registry.rejection_reason(
                ActionIntent("json.parse", {"path": "../outside.json"}, "escape")
            )

        self.assertNotIn("git.status", default_registry.names)
        self.assertIn("path escapes workspace", reason)

    def test_project_toolset_catalog_lists_all_implemented_project_tools(self):
        project_toolset = next(definition for definition in TOOLSET_CATALOG if definition.name == "project")
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("project",)),
            )
        implemented_project_tools = {
            name
            for name in registry.names
            if registry.spec(name).toolset == "project"
        }

        self.assertEqual(set(project_toolset.tools), implemented_project_tools)

    def test_file_toolset_catalog_lists_all_implemented_file_tools(self):
        file_toolset = next(definition for definition in TOOLSET_CATALOG if definition.name == "file")
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
        implemented_file_tools = {
            name
            for name in registry.names
            if registry.spec(name).toolset == "file"
        }

        self.assertEqual(set(file_toolset.tools), implemented_file_tools)

    def test_memory_toolset_remembers_recalls_lists_and_forgets_workspace_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("memory",)),
            )

            remembered = registry.execute(
                Action(
                    "memory.remember",
                    {
                        "content": "User prefers Traditional Chinese for Autonomy architecture notes.",
                        "scope": "user",
                        "wing": "preference",
                        "room": "language",
                    },
                    "remember user preference",
                    "verify persisted memory",
                )
            )
            remembered_payload = json.loads(remembered.output)
            memory_id = remembered_payload["id"]
            recalled = registry.execute(
                Action(
                    "memory.recall",
                    {"query": "Traditional Chinese architecture notes", "scope": "user"},
                    "recall relevant preference",
                    "verify recalled memory",
                )
            )
            listed = registry.execute(
                Action(
                    "memory.list",
                    {"scope": "user"},
                    "list user memories",
                    "verify memory list",
                )
            )
            forgotten = registry.execute(
                Action(
                    "memory.forget",
                    {"id": memory_id},
                    "forget memory",
                    "verify deletion",
                )
            )
            recalled_after_forget = registry.execute(
                Action(
                    "memory.recall",
                    {"query": "Traditional Chinese architecture notes", "scope": "user"},
                    "recall after deletion",
                    "verify deleted memory is absent",
                )
            )

        self.assertTrue(remembered.succeeded, remembered.error)
        self.assertEqual(remembered_payload["scope"], "user")
        self.assertEqual(remembered_payload["wing"], "preference")
        self.assertEqual(remembered_payload["room"], "language")
        self.assertTrue(recalled.succeeded, recalled.error)
        self.assertEqual(json.loads(recalled.output)["memories"][0]["id"], memory_id)
        self.assertEqual(json.loads(listed.output)["memories"][0]["id"], memory_id)
        self.assertTrue(forgotten.succeeded, forgotten.error)
        self.assertEqual(json.loads(forgotten.output), {"forgotten": True, "id": memory_id})
        self.assertEqual(json.loads(recalled_after_forget.output)["memories"], [])

    def test_memory_toolset_catalog_lists_all_implemented_memory_tools(self):
        memory_toolset = next(
            definition for definition in TOOLSET_CATALOG if definition.name == "memory"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("memory",)),
            )
        implemented_memory_tools = {
            name
            for name in registry.names
            if registry.spec(name).toolset == "memory"
        }

        self.assertEqual(set(memory_toolset.tools), implemented_memory_tools)

    def test_database_retrieve_reads_sqlite_schema_and_rejects_mutations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "sample.db"
            import sqlite3

            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL)")
                conn.execute("CREATE TABLE secrets (value TEXT)")
                conn.execute("INSERT INTO orders (customer, total) VALUES ('Ada', 12.5)")
                conn.execute("INSERT INTO orders (customer, total) VALUES ('Linus', 20.0)")
                conn.execute("INSERT INTO secrets (value) VALUES ('hidden')")
            (root / ".autonomy").mkdir()
            (root / ".autonomy" / "database_connections.yaml").write_text(
                "connections:\n"
                "  sample:\n"
                "    dialect: sqlite\n"
                "    path: sample.db\n"
                "    allowed_tables: [orders]\n",
                encoding="utf-8",
            )
            registry = build_local_tool_registry(
                root,
                ToolsetConfiguration(enabled_toolsets=("database",)),
            )

            schema = registry.execute(
                Action(
                    "database.retrieve",
                    {"action": "schema", "database_id": "sample"},
                    "inspect schema",
                    "verify",
                )
            )
            query = registry.execute(
                Action(
                    "database.retrieve",
                    {
                        "action": "query",
                        "database_id": "sample",
                        "sql": "SELECT customer, total FROM orders ORDER BY total DESC",
                        "max_rows": 1,
                    },
                    "read rows",
                    "verify",
                )
            )
            mutation = registry.execute(
                Action(
                    "database.retrieve",
                    {"action": "query", "database_id": "sample", "sql": "DROP TABLE orders"},
                    "drop table",
                    "verify",
                )
            )
            blocked_table = registry.execute(
                Action(
                    "database.retrieve",
                    {"action": "query", "database_id": "sample", "sql": "SELECT value FROM secrets"},
                    "read blocked table",
                    "verify",
                )
            )

        self.assertIn("database.retrieve", registry.names)
        self.assertEqual(registry.spec("database.retrieve").toolset, "database")
        self.assertTrue(schema.succeeded, schema.error)
        self.assertIn('"orders"', schema.output)
        self.assertTrue(query.succeeded, query.error)
        self.assertEqual(json.loads(query.output)["rows"], [{"customer": "Linus", "total": 20.0}])
        self.assertFalse(mutation.succeeded)
        self.assertIn("Only read-only SELECT queries are allowed", mutation.error)
        self.assertFalse(blocked_table.succeeded)
        self.assertIn("query references tables outside allowed_tables: secrets", blocked_table.error)

    def test_database_toolset_catalog_lists_implemented_database_tool(self):
        database_toolset = next(
            definition for definition in TOOLSET_CATALOG if definition.name == "database"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("database",)),
            )
        implemented_database_tools = {
            name
            for name in registry.names
            if registry.spec(name).toolset == "database"
        }

        self.assertEqual(set(database_toolset.tools), implemented_database_tools)

    def test_database_retrieve_uses_sqlglot_for_dialect_validation_and_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "sample.db"
            import sqlite3

            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL)")
                conn.execute("INSERT INTO orders (customer, total) VALUES ('Ada', 12.5)")
            (root / ".autonomy").mkdir()
            (root / ".autonomy" / "database_connections.yaml").write_text(
                "connections:\n"
                "  sample:\n"
                "    dialect: sqlite\n"
                "    path: sample.db\n"
                "    allowed_tables: [orders]\n",
                encoding="utf-8",
            )
            registry = build_local_tool_registry(
                root,
                ToolsetConfiguration(enabled_toolsets=("database",)),
            )

            validate = registry.execute(
                Action(
                    "database.retrieve",
                    {
                        "action": "validate",
                        "database_id": "sample",
                        "source_dialect": "mysql",
                        "sql": "SELECT `customer`, total FROM orders",
                        "max_rows": 5,
                    },
                    "validate mysql sql",
                    "verify",
                )
            )
            with patch(
                "autonomy.tools.toolsets.database._call_sql_generation_llm",
                return_value="SELECT customer, total FROM orders ORDER BY total DESC",
            ) as generator:
                generated = registry.execute(
                    Action(
                        "database.retrieve",
                        {
                            "action": "generate",
                            "database_id": "sample",
                            "request": "largest orders",
                            "source_dialect": "postgres",
                            "max_rows": 5,
                        },
                        "generate sql",
                        "verify",
                    )
                )

        self.assertTrue(validate.succeeded, validate.error)
        validate_payload = json.loads(validate.output)
        self.assertEqual(validate_payload["source_dialect"], "mysql")
        self.assertEqual(validate_payload["target_dialect"], "sqlite")
        self.assertEqual(validate_payload["referenced_tables"], ["orders"])
        self.assertIn("LIMIT 5", validate_payload["sql"])
        self.assertTrue(generated.succeeded, generated.error)
        generated_payload = json.loads(generated.output)
        self.assertEqual(generated_payload["action"], "generate")
        self.assertFalse(generated_payload["executed"])
        self.assertIn("ORDER BY total DESC", generated_payload["sql"])
        self.assertEqual(generator.call_count, 1)

    def test_toolset_status_compacts_long_unavailable_reasons(self):
        long_reason = (
            "BrowserType.launch: Target page, context or browser has been closed\n"
            "Browser logs:\n"
            + ("x" * 4000)
        )

        status = toolset_catalog_status(
            ToolsetConfiguration(enabled_toolsets=("browser",)),
            {
                "browser.navigate": {
                    "available": False,
                    "unavailable_reason": long_reason,
                }
            },
        )
        browser_row = next(row for row in status if row["name"] == "browser")
        reason = browser_row["unavailable_tools"][0]["reason"]

        self.assertLessEqual(len(reason), 500)
        self.assertIn("BrowserType.launch", reason)
        self.assertNotIn("Browser logs", reason)
        self.assertNotIn("xxxx", reason)

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
        self.assertIn("process.start", registry.names)

    def test_removed_web_toolset_is_rejected_and_unavailable_browser_hides_tools(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.tools.browser_tools_available", return_value=(False, "missing browser")),
        ):
            with self.assertRaisesRegex(ValueError, "unknown toolsets: web"):
                ToolsetConfiguration(enabled_toolsets=("browser", "web")).validate()
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("browser",)),
            )

        self.assertEqual(sorted(registry.names), [])

    def test_disabled_individual_tool_is_not_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(disabled_tools=("shell.execute",)),
            )

        self.assertNotIn("shell.execute", registry.names)
        self.assertIn("process.start", registry.names)
        self.assertIn("filesystem.read", registry.names)

    def test_process_tools_start_wait_log_poll_and_validate_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "subdir").mkdir()
            registry = build_local_tool_registry(root)

            start = registry.execute(
                Action(
                    "process.start",
                    {
                        "command": f"{sys.executable} -c \"print('ready', flush=True)\"",
                        "workdir": "subdir",
                    },
                    "start process",
                    "verify",
                )
            )
            process_id = json.loads(start.output)["process_id"]
            waited = registry.execute(
                Action(
                    "process.wait",
                    {"process_id": process_id, "timeout": 5},
                    "wait process",
                    "verify",
                )
            )
            polled = registry.execute(
                Action("process.poll", {"process_id": process_id}, "poll", "verify")
            )
            logged = registry.execute(
                Action(
                    "process.log",
                    {"process_id": process_id, "max_chars": 20},
                    "log",
                    "verify",
                )
            )
            escape_reason = registry.rejection_reason(
                ActionIntent(
                    "process.start",
                    {"command": "pwd", "workdir": "../outside"},
                    "escape",
                )
            )
            timeout_reason = registry.rejection_reason(
                ActionIntent(
                    "process.wait",
                    {"process_id": process_id, "timeout": 0},
                    "bad timeout",
                )
            )
            registry.close()

        start_payload = json.loads(start.output)
        waited_payload = json.loads(waited.output)
        polled_payload = json.loads(polled.output)
        logged_payload = json.loads(logged.output)
        self.assertTrue(start.succeeded)
        self.assertEqual(start_payload["status"], "running")
        self.assertEqual(start_payload["cwd"], "subdir")
        self.assertTrue(waited.succeeded)
        self.assertFalse(waited_payload["timed_out"])
        self.assertEqual(waited_payload["status"], "exited")
        self.assertEqual(waited_payload["exit_code"], 0)
        self.assertEqual(polled_payload["status"], "exited")
        self.assertIn("ready", logged_payload["output"])
        self.assertIn("workdir escapes workspace", escape_reason)
        self.assertIn("timeout must be at least 1", timeout_reason)

    def test_process_tools_redact_secret_like_output_and_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
            secret = "sk-processsecretvalue1234567890"
            start = registry.execute(
                Action(
                    "process.start",
                    {
                        "command": (
                            f"{sys.executable} -c "
                            f"\"print('OPENAI_API_KEY={secret}', flush=True)\""
                        )
                    },
                    "start secret process",
                    "verify",
                )
            )
            process_id = json.loads(start.output)["process_id"]
            waited = registry.execute(
                Action(
                    "process.wait",
                    {"process_id": process_id, "timeout": 5},
                    "wait process",
                    "verify",
                )
            )
            logged = registry.execute(
                Action(
                    "process.log",
                    {"process_id": process_id},
                    "log",
                    "verify",
                )
            )
            registry.close()

        start_payload = json.loads(start.output)
        waited_payload = json.loads(waited.output)
        logged_payload = json.loads(logged.output)
        self.assertNotIn(secret, start.output)
        self.assertNotIn(secret, waited.output)
        self.assertNotIn(secret, logged.output)
        self.assertIn("***", start_payload["command"])
        self.assertTrue(waited_payload["output_redacted"])
        self.assertIn("OPENAI_API_KEY=***", waited_payload["output_preview"])
        self.assertIn("OPENAI_API_KEY=***", logged_payload["output"])

    def test_process_wait_timeout_and_stop_terminate_running_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
            start = registry.execute(
                Action(
                    "process.start",
                    {
                        "command": (
                            f"{sys.executable} -c "
                            "\"import time; print('started', flush=True); time.sleep(30)\""
                        )
                    },
                    "start long process",
                    "verify",
                )
            )
            process_id = json.loads(start.output)["process_id"]
            waited = registry.execute(
                Action(
                    "process.wait",
                    {"process_id": process_id, "timeout": 1},
                    "wait timeout",
                    "verify",
                )
            )
            stopped = registry.execute(
                Action("process.stop", {"process_id": process_id}, "stop", "verify")
            )
            polled = registry.execute(
                Action("process.poll", {"process_id": process_id}, "poll", "verify")
            )
            registry.close()

        waited_payload = json.loads(waited.output)
        stopped_payload = json.loads(stopped.output)
        polled_payload = json.loads(polled.output)
        self.assertTrue(waited.succeeded)
        self.assertTrue(waited_payload["timed_out"])
        self.assertEqual(waited_payload["status"], "running")
        self.assertTrue(stopped.succeeded)
        self.assertTrue(stopped_payload["was_running"])
        self.assertEqual(polled_payload["status"], "exited")

    def test_process_cleanup_stops_background_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(tmpdir)
            start = registry.execute(
                Action(
                    "process.start",
                    {
                        "command": (
                            f"{sys.executable} -c "
                            "\"import time; print('cleanup', flush=True); time.sleep(30)\""
                        )
                    },
                    "start long process",
                    "verify",
                )
            )
            process_id = json.loads(start.output)["process_id"]

            registry.close()
            polled = registry.execute(
                Action("process.poll", {"process_id": process_id}, "poll", "verify")
            )

        self.assertEqual(json.loads(polled.output)["status"], "exited")

    def test_file_write_creates_overwrites_and_requires_workspace_text_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = build_local_tool_registry(root)

            created = registry.execute(
                Action(
                    "filesystem.write",
                    {"path": "nested/sample.txt", "content": "first\n"},
                    "write",
                    "verify",
                )
            )
            overwritten = registry.execute(
                Action(
                    "filesystem.write",
                    {"path": "nested/sample.txt", "content": "second\n"},
                    "write",
                    "verify",
                )
            )
            valid_python = registry.execute(
                Action(
                    "filesystem.write",
                    {"path": "nested/module.py", "content": "def ok():\n    return 1\n"},
                    "write python",
                    "verify",
                )
            )
            invalid_python = registry.execute(
                Action(
                    "filesystem.write",
                    {"path": "nested/broken.py", "content": "def broken(:\n"},
                    "write broken python",
                    "verify",
                )
            )
            escape_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.write",
                    {"path": "../outside.txt", "content": "no"},
                    "escape",
                )
            )
            binary_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.write",
                    {"path": "image.png", "content": "no"},
                    "binary",
                )
            )
            final_content = (root / "nested" / "sample.txt").read_text(encoding="utf-8")

        created_payload = json.loads(created.output)
        overwritten_payload = json.loads(overwritten.output)
        valid_python_payload = json.loads(valid_python.output)
        invalid_python_payload = json.loads(invalid_python.output)
        self.assertTrue(created.succeeded)
        self.assertTrue(created_payload["created"])
        self.assertFalse(overwritten_payload["created"])
        self.assertNotIn("syntax_ok", created_payload)
        self.assertTrue(valid_python.succeeded)
        self.assertTrue(valid_python_payload["syntax_ok"])
        self.assertEqual(valid_python_payload["syntax_diagnostic"], "")
        self.assertIn("syntax_checked:true", valid_python.evidence)
        self.assertIn("syntax_ok:true", valid_python.evidence)
        self.assertTrue(invalid_python.succeeded)
        self.assertFalse(invalid_python_payload["syntax_ok"])
        self.assertIn("syntax_error", invalid_python_payload["syntax_diagnostic"])
        self.assertIn("syntax_ok:false", invalid_python.evidence)
        self.assertEqual(final_content, "second\n")
        self.assertIn("path escapes workspace", escape_reason)
        self.assertIn("binary-like file extension", binary_reason)

    def test_file_trash_uses_trash_cli_and_validates_workspace_boundaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trash_script = root / "fake-trash"
            trash_script.write_text(
                """#!/usr/bin/env python3
import pathlib
import shutil
import sys

path = pathlib.Path(sys.argv[1])
destination = path.parent / ".fake-trash"
destination.mkdir(exist_ok=True)
shutil.move(str(path), str(destination / path.name))
""",
                encoding="utf-8",
            )
            trash_script.chmod(0o755)
            (root / "obsolete.txt").write_text("old\n", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("config\n", encoding="utf-8")
            (root / ".autonomy").mkdir()
            (root / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
            with patch("autonomy.tools.local.shutil.which", return_value=str(trash_script)):
                registry = build_local_tool_registry(root, ToolsetConfiguration())
                trashed = registry.execute(
                    Action(
                        "filesystem.trash",
                        {"path": "obsolete.txt"},
                        "remove obsolete file",
                        "verify",
                    )
                )
                root_reason = registry.rejection_reason(
                    ActionIntent("filesystem.trash", {"path": "."}, "trash root")
                )
                escape_reason = registry.rejection_reason(
                    ActionIntent("filesystem.trash", {"path": "../outside.txt"}, "escape")
                )
                git_reason = registry.rejection_reason(
                    ActionIntent("filesystem.trash", {"path": ".git/config"}, "trash git")
                )
                autonomy_reason = registry.rejection_reason(
                    ActionIntent("filesystem.trash", {"path": ".autonomy"}, "trash autonomy")
                )
                env_reason = registry.rejection_reason(
                    ActionIntent("filesystem.trash", {"path": ".env"}, "trash env")
                )

            payload = json.loads(trashed.output)
            self.assertTrue(trashed.succeeded)
            self.assertEqual(payload["path"], "obsolete.txt")
            self.assertEqual(payload["kind"], "file")
            self.assertTrue(payload["trashed"])
            self.assertFalse((root / "obsolete.txt").exists())
            self.assertTrue((root / ".fake-trash" / "obsolete.txt").is_file())
            self.assertIn("file-delete", trashed.side_effects)
            self.assertEqual(registry.spec("filesystem.trash").default_risk, RiskLevel.MEDIUM)
            self.assertIn("cannot trash the workspace root", root_reason)
            self.assertIn("path escapes workspace", escape_reason)
            self.assertIn("protected workspace metadata", git_reason)
            self.assertIn("protected workspace metadata", autonomy_reason)
            self.assertIn("secret-bearing environment file", env_reason)

    def test_file_trash_is_unavailable_without_trash_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("autonomy.tools.local.shutil.which", return_value=None):
                registry = build_local_tool_registry(tmpdir, ToolsetConfiguration())
                full = build_local_tool_registry(tmpdir)
                full_statuses = full.tool_statuses()

        self.assertNotIn("filesystem.trash", registry.names)
        self.assertFalse(full_statuses["filesystem.trash"]["available"])
        self.assertIn("trash CLI", full_statuses["filesystem.trash"]["unavailable_reason"])

    def test_file_mkdir_creates_workspace_directories_without_shell(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            registry = build_local_tool_registry(root)

            created = registry.execute(
                Action(
                    "filesystem.mkdir",
                    {"path": "src/pkg"},
                    "create package dir",
                    "verify",
                )
            )
            existing = registry.execute(
                Action(
                    "filesystem.mkdir",
                    {"path": "src/pkg"},
                    "create existing dir",
                    "verify",
                )
            )
            exist_ok = registry.execute(
                Action(
                    "filesystem.mkdir",
                    {"path": "src/pkg", "exist_ok": True},
                    "create existing dir ok",
                    "verify",
                )
            )
            escape_reason = registry.rejection_reason(
                ActionIntent("filesystem.mkdir", {"path": "../outside"}, "escape")
            )
            git_reason = registry.rejection_reason(
                ActionIntent("filesystem.mkdir", {"path": ".git/hooks"}, "git")
            )

            payload = json.loads(created.output)
            self.assertTrue(created.succeeded)
            self.assertEqual(payload["path"], "src/pkg")
            self.assertTrue((root / "src" / "pkg").is_dir())
            self.assertFalse(existing.succeeded)
            self.assertIn("already exists", existing.error)
            self.assertTrue(exist_ok.succeeded)
            self.assertIn("file-write", created.side_effects)
            self.assertEqual(registry.spec("filesystem.mkdir").default_risk, RiskLevel.MEDIUM)
            self.assertIn("path escapes workspace", escape_reason)
            self.assertIn("protected workspace metadata", git_reason)

    def test_file_move_renames_without_overwriting_or_escaping_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "source.txt").write_text("value\n", encoding="utf-8")
            (root / "existing.txt").write_text("existing\n", encoding="utf-8")
            (root / "dir").mkdir()
            (root / "dir" / "child.txt").write_text("child\n", encoding="utf-8")
            (root / ".autonomy").mkdir()
            registry = build_local_tool_registry(root)

            moved = registry.execute(
                Action(
                    "filesystem.move",
                    {"source": "source.txt", "destination": "nested/renamed.txt"},
                    "rename file",
                    "verify",
                )
            )
            missing = registry.execute(
                Action(
                    "filesystem.move",
                    {"source": "missing.txt", "destination": "target.txt"},
                    "missing move",
                    "verify",
                )
            )
            overwrite_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.move",
                    {"source": "nested/renamed.txt", "destination": "existing.txt"},
                    "overwrite",
                )
            )
            escape_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.move",
                    {"source": "nested/renamed.txt", "destination": "../outside.txt"},
                    "escape",
                )
            )
            protected_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.move",
                    {"source": "nested/renamed.txt", "destination": ".autonomy/file"},
                    "protected",
                )
            )
            self_move_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.move",
                    {"source": "dir", "destination": "dir/child"},
                    "self move",
                )
            )

            payload = json.loads(moved.output)
            self.assertTrue(moved.succeeded)
            self.assertEqual(payload["source"], "source.txt")
            self.assertEqual(payload["destination"], "nested/renamed.txt")
            self.assertFalse((root / "source.txt").exists())
            self.assertEqual(
                (root / "nested" / "renamed.txt").read_text(encoding="utf-8"),
                "value\n",
            )
            self.assertFalse(missing.succeeded)
            self.assertIn("source does not exist", missing.error)
            self.assertIn("destination already exists", overwrite_reason)
            self.assertIn("path escapes workspace", escape_reason)
            self.assertIn("protected workspace metadata", protected_reason)
            self.assertIn("cannot move a directory into itself", self_move_reason)
            self.assertIn("file-write", moved.side_effects)
            self.assertIn("file-delete", moved.side_effects)
            self.assertEqual(registry.spec("filesystem.move").default_risk, RiskLevel.MEDIUM)

    def test_file_patch_replaces_unique_or_all_matches_and_preserves_on_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "sample.txt"
            path.write_text("alpha\nbeta\nbeta\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            missing = registry.execute(
                Action(
                    "filesystem.patch",
                    {"path": "sample.txt", "old_string": "gamma", "new_string": "delta"},
                    "patch",
                    "verify",
                )
            )
            ambiguous = registry.execute(
                Action(
                    "filesystem.patch",
                    {"path": "sample.txt", "old_string": "beta", "new_string": "BETA"},
                    "patch",
                    "verify",
                )
            )
            unique = registry.execute(
                Action(
                    "filesystem.patch",
                    {"path": "sample.txt", "old_string": "alpha", "new_string": "ALPHA"},
                    "patch",
                    "verify",
                )
            )
            replace_all = registry.execute(
                Action(
                    "filesystem.patch",
                    {
                        "path": "sample.txt",
                        "old_string": "beta",
                        "new_string": "BETA",
                        "replace_all": True,
                    },
                    "patch",
                    "verify",
                )
            )
            invalid_mode_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.patch",
                    {
                        "path": "sample.txt",
                        "old_string": "alpha",
                        "new_string": "ALPHA",
                        "match_mode": "loose",
                    },
                    "bad mode",
                )
            )
            final_content = path.read_text(encoding="utf-8")

        unique_payload = json.loads(unique.output)
        replace_all_payload = json.loads(replace_all.output)
        self.assertFalse(missing.succeeded)
        self.assertFalse(ambiguous.succeeded)
        self.assertIn("match_mode=strip_lines", missing.error)
        self.assertIn("not unique", ambiguous.error)
        self.assertIn("match_mode must be exact or strip_lines", invalid_mode_reason)
        self.assertIn("-alpha", unique_payload["diff"])
        self.assertEqual(replace_all_payload["replacements"], 2)
        self.assertEqual(final_content, "ALPHA\nBETA\nBETA\n")

    def test_file_revision_precondition_guards_write_and_patch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "sample.txt"
            path.write_text("alpha\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            stat = registry.execute(
                Action("filesystem.stat", {"path": "sample.txt"}, "stat", "verify")
            )
            revision = json.loads(stat.output)["revision"]
            stale_write = registry.execute(
                Action(
                    "filesystem.write",
                    {
                        "path": "sample.txt",
                        "content": "stale\n",
                        "expected_revision": "stale-revision",
                    },
                    "stale write",
                    "verify",
                )
            )
            guarded_patch = registry.execute(
                Action(
                    "filesystem.patch",
                    {
                        "path": "sample.txt",
                        "old_string": "alpha",
                        "new_string": "beta",
                        "expected_revision": revision,
                    },
                    "guarded patch",
                    "verify",
                )
            )
            new_revision = json.loads(guarded_patch.output)["revision"]
            stale_patch = registry.execute(
                Action(
                    "filesystem.patch",
                    {
                        "path": "sample.txt",
                        "old_string": "beta",
                        "new_string": "gamma",
                        "expected_revision": revision,
                    },
                    "stale patch",
                    "verify",
                )
            )
            guarded_write = registry.execute(
                Action(
                    "filesystem.write",
                    {
                        "path": "sample.txt",
                        "content": "gamma\n",
                        "expected_revision": new_revision,
                    },
                    "guarded write",
                    "verify",
                )
            )
            final_content = path.read_text(encoding="utf-8")

        guarded_write_payload = json.loads(guarded_write.output)
        self.assertFalse(stale_write.succeeded)
        self.assertIn("expected_revision mismatch", stale_write.error)
        self.assertIn("revision_mismatch:true", stale_write.evidence)
        self.assertTrue(guarded_patch.succeeded)
        self.assertEqual(json.loads(guarded_patch.output)["previous_revision"], revision)
        self.assertFalse(stale_patch.succeeded)
        self.assertIn("expected_revision mismatch", stale_patch.error)
        self.assertTrue(guarded_write.succeeded)
        self.assertEqual(guarded_write_payload["previous_revision"], new_revision)
        self.assertEqual(final_content, "gamma\n")

    def test_file_patch_reports_python_syntax_diagnostics_after_successful_edit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "sample.py"
            path.write_text("def ok():\n    return 1\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            invalid_patch = registry.execute(
                Action(
                    "filesystem.patch",
                    {
                        "path": "sample.py",
                        "old_string": "def ok():",
                        "new_string": "def ok(:",
                    },
                    "patch invalid python",
                    "verify",
                )
            )

        payload = json.loads(invalid_patch.output)
        self.assertTrue(invalid_patch.succeeded)
        self.assertFalse(payload["syntax_ok"])
        self.assertIn("syntax_error", payload["syntax_diagnostic"])
        self.assertIn("syntax_checked:true", invalid_patch.evidence)
        self.assertIn("syntax_ok:false", invalid_patch.evidence)

    def test_file_patch_strip_lines_match_mode_handles_indentation_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "sample.py"
            path.write_text(
                "def main():\n"
                "    value = 1\n"
                "    return value\n",
                encoding="utf-8",
            )
            registry = build_local_tool_registry(root)

            exact = registry.execute(
                Action(
                    "filesystem.patch",
                    {
                        "path": "sample.py",
                        "old_string": "value = 1\nreturn value",
                        "new_string": "value = 2\nreturn value",
                    },
                    "exact patch",
                    "verify",
                )
            )
            fuzzy = registry.execute(
                Action(
                    "filesystem.patch",
                    {
                        "path": "sample.py",
                        "old_string": "value = 1\nreturn value",
                        "new_string": "    value = 2\n    return value",
                        "match_mode": "strip_lines",
                    },
                    "strip-lines patch",
                    "verify",
                )
            )
            final_content = path.read_text(encoding="utf-8")

        fuzzy_payload = json.loads(fuzzy.output)
        self.assertFalse(exact.succeeded)
        self.assertTrue(fuzzy.succeeded)
        self.assertEqual(fuzzy_payload["match_mode"], "strip_lines")
        self.assertEqual(fuzzy_payload["replacements"], 1)
        self.assertIn("value = 2", final_content)
        self.assertNotIn("value = 1", final_content)

    def test_filesystem_search_files_content_and_filename_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / ".git").mkdir()
            (root / "src" / "app.py").write_text(
                "before\nneedle = 1\nafter\n",
                encoding="utf-8",
            )
            (root / "src" / "other.py").write_text("needle = 2\n", encoding="utf-8")
            (root / "src" / "notes.md").write_text("needle docs\n", encoding="utf-8")
            (root / ".git" / "ignored.py").write_text("needle hidden\n", encoding="utf-8")
            registry = build_local_tool_registry(root)

            content = registry.execute(
                Action(
                    "filesystem.search_files",
                    {
                        "pattern": "needle",
                        "path": ".",
                        "file_glob": "*.py",
                        "limit": 10,
                    },
                    "search",
                    "verify",
                )
            )
            with_context = registry.execute(
                Action(
                    "filesystem.search_files",
                    {
                        "pattern": "needle",
                        "path": "src/app.py",
                        "context": 1,
                        "limit": 10,
                    },
                    "search context",
                    "verify",
                )
            )
            files_only = registry.execute(
                Action(
                    "filesystem.search_files",
                    {
                        "pattern": "needle",
                        "path": ".",
                        "output_mode": "files_only",
                        "limit": 10,
                    },
                    "search files only",
                    "verify",
                )
            )
            counts = registry.execute(
                Action(
                    "filesystem.search_files",
                    {
                        "pattern": "needle",
                        "path": ".",
                        "output_mode": "count",
                        "limit": 10,
                    },
                    "search counts",
                    "verify",
                )
            )
            files = registry.execute(
                Action(
                    "filesystem.search_files",
                    {"pattern": "*.py", "target": "files", "path": ".", "limit": 1},
                    "find",
                    "verify",
                )
            )
            second_file = registry.execute(
                Action(
                    "filesystem.search_files",
                    {"pattern": "*.py", "target": "files", "path": ".", "limit": 1, "offset": 1},
                    "find next",
                    "verify",
                )
            )
            empty_reason = registry.rejection_reason(
                ActionIntent("filesystem.search_files", {"pattern": ""}, "bad search")
            )
            offset_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.search_files",
                    {"pattern": "needle", "offset": -1},
                    "bad offset",
                )
            )
            output_mode_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.search_files",
                    {"pattern": "needle", "output_mode": "verbose"},
                    "bad output mode",
                )
            )
            context_reason = registry.rejection_reason(
                ActionIntent(
                    "filesystem.search_files",
                    {"pattern": "needle", "context": -1},
                    "bad context",
                )
            )

        self.assertIn("src/app.py:2:needle = 1", content.output)
        self.assertIn("src/app.py-1-before", with_context.output)
        self.assertIn("src/app.py:2:needle = 1", with_context.output)
        self.assertIn("src/app.py-3-after", with_context.output)
        self.assertIn("search_context:1", with_context.evidence)
        self.assertIn("src/app.py", files_only.output)
        self.assertIn("src/other.py", files_only.output)
        self.assertNotIn("needle = 1", files_only.output)
        self.assertIn("src/app.py:1", counts.output)
        self.assertIn("src/other.py:1", counts.output)
        self.assertIn("src/app.py", files.output)
        self.assertNotIn("src/other.py", files.output)
        self.assertIn("Use offset=1", files.output)
        self.assertIn("src/other.py", second_file.output)
        self.assertIn("pattern must not be empty", empty_reason)
        self.assertIn("offset must be at least 0", offset_reason)
        self.assertIn("output_mode must be content, files_only, or count", output_mode_reason)
        self.assertIn("context must be at least 0", context_reason)

    def test_browser_tools_are_hidden_when_unavailable(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.tools.browser_tools_available", return_value=(False, "missing browser")),
        ):
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("browser",)),
            )
            full = build_local_tool_registry(tmpdir)

        self.assertEqual(registry.names, set())
        self.assertFalse(full.tool_statuses()["browser.navigate"]["available"])
        self.assertFalse(full.tool_statuses()["browser.screenshot"]["available"])
        self.assertFalse(full.tool_statuses()["browser.get_images"]["available"])
        self.assertFalse(full.tool_statuses()["browser.console"]["available"])
        self.assertFalse(full.tool_statuses()["browser.dialog"]["available"])
        self.assertIn("missing browser", full.tool_statuses()["browser.navigate"]["unavailable_reason"])

    def test_browser_tools_execute_through_controller(self):
        class FakeController:
            def __init__(self):
                self.calls = []

            def close(self):
                self.calls.append(("close",))

            def navigate(self, url, timeout_ms):
                self.calls.append(("navigate", url, timeout_ms))
                return {"url": url, "title": "Title", "text": "Loaded"}

            def snapshot(self, extra=None, *, full=False, max_chars=12000):
                self.calls.append(("snapshot", extra, full, max_chars))
                payload = {
                    "url": "https://example.test",
                    "title": "Title",
                    "text": "Loaded",
                    "full": full,
                    "max_chars": max_chars,
                    "text_truncated": False,
                }
                if extra:
                    payload.update(extra)
                return payload

            def click(self, selector, timeout_ms):
                self.calls.append(("click", selector, timeout_ms))
                return {"url": "https://example.test", "title": "Title", "text": "Clicked"}

            def type_text(self, selector, text, timeout_ms):
                self.calls.append(("type", selector, text, timeout_ms))
                return {"url": "https://example.test", "title": "Title", "text": text}

            def scroll(self, direction):
                self.calls.append(("scroll", direction))
                return {"url": "https://example.test", "title": "Title", "text": direction}

            def back(self):
                self.calls.append(("back",))
                return {"url": "https://example.test", "title": "Title", "text": "Back"}

            def press(self, key):
                self.calls.append(("press", key))
                return {"url": "https://example.test", "title": "Title", "text": key}

            def screenshot(self, *, full_page=True):
                self.calls.append(("screenshot", full_page))
                return {
                    "success": True,
                    "url": "https://example.test",
                    "title": "Title",
                    "path": "/tmp/browser_screenshot.png",
                    "bytes": 12,
                    "full_page": full_page,
                    "action": "screenshot",
                }

            def get_images(self):
                self.calls.append(("get_images",))
                return {
                    "url": "https://example.test",
                    "title": "Title",
                    "images": [{"src": "https://example.test/a.png", "alt": "A"}],
                    "count": 1,
                }

            def console(self, *, clear=False, expression=None):
                self.calls.append(("console", clear, expression))
                return {
                    "success": True,
                    "url": "https://example.test",
                    "console_messages": [{"type": "log", "text": "ready"}],
                    "page_errors": [],
                    "total_messages": 1,
                    "total_errors": 0,
                }

            def dialog(self, *, action, prompt_text="", dialog_id=""):
                self.calls.append(("dialog", action, prompt_text, dialog_id))
                return {
                    "success": True,
                    "action": "dialog",
                    "dialog_action": action,
                    "dialog": {
                        "id": dialog_id or "dialog_1",
                        "type": "confirm",
                        "message": "Continue?",
                        "default_value": "",
                    },
                    "pending_dialogs": [],
                }

        controller = FakeController()
        registry = ToolRegistry()
        register_browser_tools(
            registry,
            controller,
            availability_check=lambda: (True, ""),
        )

        actions = [
            Action("browser.navigate", {"url": "https://example.test"}, "navigate", "verify"),
            Action("browser.snapshot", {"full": True, "max_chars": 250}, "snapshot", "verify"),
            Action("browser.click", {"selector": "#submit"}, "click", "verify"),
            Action("browser.type", {"selector": "#q", "text": "hello"}, "type", "verify"),
            Action("browser.scroll", {"direction": "up"}, "scroll", "verify"),
            Action("browser.back", {}, "back", "verify"),
            Action("browser.press", {"key": "Enter"}, "press", "verify"),
            Action("browser.screenshot", {"full_page": False}, "screenshot", "verify"),
            Action("browser.get_images", {}, "images", "verify"),
            Action("browser.console", {"clear": True}, "console", "verify"),
            Action("browser.dialog", {"action": "accept", "dialog_id": "dialog_1"}, "dialog", "verify"),
        ]

        observations = [registry.execute(action) for action in actions]

        self.assertTrue(all(observation.succeeded for observation in observations))
        self.assertEqual(registry.spec("browser.navigate").default_risk, RiskLevel.MEDIUM)
        self.assertIn("max_chars", registry.spec("browser.snapshot").argument_contract)
        self.assertEqual(registry.spec("browser.screenshot").default_risk, RiskLevel.MEDIUM)
        self.assertIn("file-write", registry.spec("browser.screenshot").side_effects)
        self.assertEqual(registry.spec("browser.get_images").default_risk, RiskLevel.MEDIUM)
        self.assertEqual(registry.spec("browser.console").default_risk, RiskLevel.MEDIUM)
        self.assertEqual(registry.spec("browser.dialog").default_risk, RiskLevel.MEDIUM)
        self.assertIn(("click", "#submit", 30000), controller.calls)
        self.assertIn(("type", "#q", "hello", 30000), controller.calls)
        self.assertIn(("snapshot", {"action": "snapshot"}, True, 250), controller.calls)
        self.assertIn(("screenshot", False), controller.calls)
        self.assertIn(("get_images",), controller.calls)
        self.assertIn(("console", True, None), controller.calls)
        self.assertIn(("dialog", "accept", "", "dialog_1"), controller.calls)

    def test_browser_screenshot_writes_workspace_artifact(self):
        class FakePage:
            url = "https://example.test/ui"

            def title(self):
                return "UI"

            def screenshot(self, *, path, full_page):
                self.screenshot_path = path
                self.full_page = full_page
                Path(path).write_bytes(b"png")

        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_dir = Path(tmpdir) / ".autonomy" / "browser-screenshots"
            controller = BrowserController(screenshot_dir)
            controller._page = FakePage()

            payload = controller.screenshot(full_page=False)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["bytes"], 3)
        self.assertFalse(payload["full_page"])
        self.assertIn(".autonomy/browser-screenshots/browser_screenshot_", payload["path"])

    def test_browser_snapshot_includes_actionable_elements(self):
        class FakeLocator:
            def count(self):
                return 1

            def inner_text(self, timeout=0):
                del timeout
                return "Search page"

        class FakePage:
            url = "https://example.test/search"

            def locator(self, selector):
                self.selector = selector
                return FakeLocator()

            def title(self):
                return "Search"

            def evaluate(self, script):
                self.script = script
                return [
                    {
                        "selector": "input[name=\"q\"]",
                        "tag": "input",
                        "text": "",
                        "role": "",
                        "aria_label": "Search",
                        "name": "q",
                        "placeholder": "Search terms",
                        "type": "text",
                        "href": "",
                        "disabled": False,
                    },
                    {
                        "selector": "button[type=\"submit\"]",
                        "tag": "button",
                        "text": "Search",
                        "role": "button",
                        "aria_label": "",
                        "name": "",
                        "placeholder": "",
                        "type": "submit",
                        "href": "",
                        "disabled": False,
                    },
                ]

        controller = BrowserController()
        controller._page = FakePage()

        snapshot = controller.snapshot(extra={"action": "snapshot"})

        self.assertEqual(snapshot["title"], "Search")
        self.assertEqual(
            snapshot["elements"],
            [
                {
                    "selector": "input[name=\"q\"]",
                    "tag": "input",
                    "text": "",
                    "role": "",
                    "aria_label": "Search",
                    "name": "q",
                    "placeholder": "Search terms",
                    "type": "text",
                    "href": "",
                    "disabled": False,
                },
                {
                    "selector": "button[type=\"submit\"]",
                    "tag": "button",
                    "text": "Search",
                    "role": "button",
                    "aria_label": "",
                    "name": "",
                    "placeholder": "",
                    "type": "submit",
                    "href": "",
                    "disabled": False,
                },
            ],
        )

    def test_browser_snapshot_bounds_visible_text(self):
        class FakeLocator:
            def count(self):
                return 1

            def inner_text(self, timeout=0):
                del timeout
                return "0123456789" * 20

        class FakePage:
            url = "https://example.test/long"

            def locator(self, selector):
                del selector
                return FakeLocator()

            def title(self):
                return "Long"

            def evaluate(self, script):
                del script
                return []

        controller = BrowserController()
        controller._page = FakePage()

        snapshot = controller.snapshot(extra={"action": "snapshot"}, full=True, max_chars=25)

        self.assertEqual(snapshot["text"], "0123456789012345678901234")
        self.assertTrue(snapshot["full"])
        self.assertEqual(snapshot["max_chars"], 25)
        self.assertEqual(snapshot["text_chars"], 200)
        self.assertTrue(snapshot["text_truncated"])

    def test_browser_get_images_normalizes_page_inventory(self):
        class FakePage:
            url = "https://example.test/gallery"

            def title(self):
                return "Gallery"

            def evaluate(self, script):
                self.script = script
                return [
                    {
                        "src": "https://example.test/a.png",
                        "alt": "Alpha",
                        "width": 640,
                        "height": 480,
                        "selector": "img[alt=\"Alpha\"]",
                    },
                    {
                        "src": "data:image/png;base64,ignored",
                        "alt": "Ignored",
                        "width": 1,
                        "height": 1,
                        "selector": "img",
                    },
                    {"src": "", "alt": "Empty"},
                ]

        controller = BrowserController()
        controller._page = FakePage()

        payload = controller.get_images()

        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["images"],
            [
                {
                    "src": "https://example.test/a.png",
                    "alt": "Alpha",
                    "width": 640,
                    "height": 480,
                    "selector": "img[alt=\"Alpha\"]",
                }
            ],
        )

    def test_browser_console_collects_clears_and_evaluates(self):
        class FakeConsoleMessage:
            type = "warning"
            text = "be careful"
            location = {"url": "https://example.test/app.js", "lineNumber": 10}

        class FakePage:
            url = "https://example.test/app"

            def evaluate(self, expression):
                self.expression = expression
                return {"title": "App"}

        controller = BrowserController()
        controller._page = FakePage()
        controller._record_console_message(FakeConsoleMessage())
        controller._record_page_error(ValueError("boom"))

        first = controller.console()
        evaluated = controller.console(expression="document.title", clear=True)
        cleared = controller.console()

        self.assertEqual(first["total_messages"], 1)
        self.assertEqual(first["total_errors"], 1)
        self.assertEqual(evaluated["result"], {"title": "App"})
        self.assertEqual(cleared["total_messages"], 0)
        self.assertEqual(cleared["total_errors"], 0)

    def test_browser_observations_redact_secret_like_payloads(self):
        secret = "sk-browsersecretvalue1234567890"

        class FakePage:
            url = f"https://example.test/app?token={secret}"

            def title(self):
                return "Secret App"

            def evaluate(self, script):
                if script == "window.secretState":
                    return {"access_token": secret, "message": f"OPENAI_API_KEY={secret}"}
                return [
                    {
                        "src": f"https://cdn.example.test/image.png?api_key={secret}",
                        "alt": f"token {secret}",
                        "width": 640,
                        "height": 480,
                        "selector": "img",
                    }
                ]

        controller = BrowserController()
        controller._page = FakePage()
        registry = ToolRegistry()
        register_browser_tools(
            registry,
            controller,
            availability_check=lambda: (True, ""),
        )

        images = registry.execute(Action("browser.get_images", {}, "images", "verify"))
        console = registry.execute(
            Action("browser.console", {"expression": "window.secretState"}, "console", "verify")
        )

        combined = "\n".join(
            [
                images.output,
                console.output,
                *images.evidence,
                *console.evidence,
                images.error,
                console.error,
            ]
        )
        self.assertTrue(images.succeeded)
        self.assertTrue(console.succeeded)
        self.assertNotIn(secret, combined)
        self.assertIn("api_key=***", images.output)
        self.assertIn("token=***", images.output)
        self.assertIn('"access_token": "***"', console.output)
        self.assertIn("OPENAI_API_KEY=***", console.output)
        self.assertIn("browser_redacted:true", images.evidence)
        self.assertIn("browser_redacted:true", console.evidence)

    def test_browser_dialog_records_and_responds_to_pending_dialog(self):
        class FakeDialog:
            type = "prompt"
            message = "Name?"
            default_value = "Autonomy"

            def __init__(self):
                self.accepted = None
                self.dismissed = False

            def accept(self, prompt_text=""):
                self.accepted = prompt_text

            def dismiss(self):
                self.dismissed = True

        class FakePage:
            url = "https://example.test/dialog"

            def title(self):
                return "Dialog"

        controller = BrowserController()
        controller._page = FakePage()
        dialog = FakeDialog()

        controller._record_dialog(dialog)
        snapshot = controller.snapshot(extra={"action": "snapshot"})
        dialog_id = snapshot["pending_dialogs"][0]["id"]
        response = controller.dialog(
            action="accept",
            prompt_text="Ada",
            dialog_id=dialog_id,
        )

        self.assertEqual(snapshot["pending_dialogs"][0]["type"], "prompt")
        self.assertEqual(snapshot["elements"], [])
        self.assertTrue(response["success"])
        self.assertEqual(response["pending_dialogs"], [])
        self.assertEqual(dialog.accepted, "Ada")

    def test_browser_console_eval_failure_returns_failed_observation(self):
        class FakePage:
            url = "https://example.test/app"

            def evaluate(self, expression):
                del expression
                raise ValueError("bad expression")

        controller = BrowserController()
        controller._page = FakePage()
        registry = ToolRegistry()
        register_browser_tools(
            registry,
            controller,
            availability_check=lambda: (True, ""),
        )

        observation = registry.execute(
            Action(
                "browser.console",
                {"expression": "throw new Error('bad')"},
                "eval",
                "verify",
            )
        )

        self.assertFalse(observation.succeeded)
        self.assertIn("ValueError: bad expression", observation.error)

    def test_browser_images_and_console_smoke_on_controlled_page(self):
        available, reason = browser_tools_available()
        if not available:
            self.skipTest(reason)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "index.html").write_text(
                """<!doctype html>
<html>
  <head>
    <title>Browser Smoke</title>
    <script>
      console.log("smoke-ready");
      window.smokeState = {ready: true, count: 2};
    </script>
  </head>
  <body>
    <h1>Browser Smoke</h1>
    <img src="/asset.png" alt="Sample image" width="10" height="20">
  </body>
</html>
""",
                encoding="utf-8",
            )
            (root / "asset.png").write_bytes(b"not-a-real-png")
            handler = partial(SimpleHTTPRequestHandler, directory=str(root))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            registry = None
            try:
                registry = build_local_tool_registry(
                    root,
                    ToolsetConfiguration(enabled_toolsets=("browser",)),
                )
                url = f"http://127.0.0.1:{server.server_port}/index.html"

                navigate = registry.execute(
                    Action("browser.navigate", {"url": url}, "navigate", "verify")
                )
                images = registry.execute(
                    Action("browser.get_images", {}, "images", "verify")
                )
                screenshot = registry.execute(
                    Action("browser.screenshot", {"full_page": False}, "screenshot", "verify")
                )
                console = registry.execute(
                    Action("browser.console", {"expression": "window.smokeState"}, "console", "verify")
                )
            finally:
                if registry is not None:
                    registry.close()
                server.shutdown()
                server.server_close()

        self.assertTrue(navigate.succeeded)
        image_payload = json.loads(images.output)
        screenshot_payload = json.loads(screenshot.output)
        console_payload = json.loads(console.output)
        self.assertEqual(image_payload["count"], 1)
        self.assertEqual(image_payload["images"][0]["alt"], "Sample image")
        self.assertTrue(screenshot_payload["path"].endswith(".png"))
        self.assertGreater(screenshot_payload["bytes"], 0)
        self.assertEqual(console_payload["result"], {"ready": True, "count": 2})

    def test_browser_dialog_smoke_on_controlled_page(self):
        available, reason = browser_tools_available()
        if not available:
            self.skipTest(reason)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "index.html").write_text(
                """<!doctype html>
<html>
  <head><title>Dialog Smoke</title></head>
  <body>
    <button id="alert-button" onclick="alert('Hello dialog')">Open dialog</button>
  </body>
</html>
""",
                encoding="utf-8",
            )
            handler = partial(SimpleHTTPRequestHandler, directory=str(root))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            registry = None
            try:
                registry = build_local_tool_registry(
                    root,
                    ToolsetConfiguration(enabled_toolsets=("browser",)),
                )
                url = f"http://127.0.0.1:{server.server_port}/index.html"

                navigate = registry.execute(
                    Action("browser.navigate", {"url": url}, "navigate", "verify")
                )
                clicked = registry.execute(
                    Action("browser.click", {"selector": "#alert-button"}, "click", "verify")
                )
                click_payload = json.loads(clicked.output)
                dialog_id = click_payload["pending_dialogs"][0]["id"]
                accepted = registry.execute(
                    Action(
                        "browser.dialog",
                        {"action": "accept", "dialog_id": dialog_id},
                        "accept dialog",
                        "verify",
                    )
                )
            finally:
                if registry is not None:
                    registry.close()
                server.shutdown()
                server.server_close()

        accepted_payload = json.loads(accepted.output)
        self.assertTrue(navigate.succeeded)
        self.assertTrue(clicked.succeeded)
        self.assertEqual(click_payload["pending_dialogs"][0]["message"], "Hello dialog")
        self.assertTrue(accepted.succeeded)
        self.assertEqual(accepted_payload["pending_dialogs"], [])

    def test_shell_risk_is_reassessed_by_policy(self):
        policy = ApprovalPolicy(prompt=lambda message: False)
        safe = Action("shell.execute", {"command": "git status"}, "status", "verify")
        unknown = Action("shell.execute", {"command": "touch file"}, "touch", "verify")

        self.assertEqual(policy.authorize(safe, interactive=False), (True, "low-risk action"))
        allowed, reason = policy.authorize(unknown, interactive=False)
        self.assertFalse(allowed)
        self.assertIn("approval required", reason)

    def test_browser_medium_risk_requires_approval_in_non_interactive_mode(self):
        policy = ApprovalPolicy(prompt=lambda message: True)
        action = Action(
            "browser.navigate",
            {"url": "https://example.test"},
            "navigate",
            "verify",
            risk_level=RiskLevel.MEDIUM,
        )

        allowed, reason = policy.authorize(action, interactive=False)

        self.assertFalse(allowed)
        self.assertIn("approval required", reason)

    def test_process_start_requires_approval_in_non_interactive_mode(self):
        policy = ApprovalPolicy(prompt=lambda message: True)
        action = Action(
            "process.start",
            {"command": "python3.13 -m http.server 8000"},
            "start server",
            "verify",
            risk_level=RiskLevel.MEDIUM,
        )

        allowed, reason = policy.authorize(action, interactive=False)

        self.assertFalse(allowed)
        self.assertIn("approval required", reason)


if __name__ == "__main__":
    unittest.main()
