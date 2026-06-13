import unittest
import json
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
    ToolsetConfiguration,
    build_local_tool_registry,
)
from autonomy.browser_tools import BrowserController, browser_tools_available, register_browser_tools
from autonomy.tools import ToolRegistry


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
            self.assertEqual(registry.spec("process.start").toolset, "terminal")
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
            [
                "filesystem.list",
                "filesystem.patch",
                "filesystem.read",
                "filesystem.search_files",
                "filesystem.write",
                "process.log",
                "process.poll",
                "process.start",
                "process.stop",
                "process.wait",
                "search.text",
                "shell.execute",
            ],
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
        self.assertIn("process.start", registry.names)

    def test_web_exposes_and_unavailable_browser_hides_tools(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.tools.browser_tools_available", return_value=(False, "missing browser")),
        ):
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("browser", "web")),
            )

        self.assertEqual(sorted(registry.names), ["web.extract", "web.fetch"])

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
        self.assertTrue(created.succeeded)
        self.assertTrue(created_payload["created"])
        self.assertFalse(overwritten_payload["created"])
        self.assertEqual(final_content, "second\n")
        self.assertIn("path escapes workspace", escape_reason)
        self.assertIn("binary-like file extension", binary_reason)

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
            final_content = path.read_text(encoding="utf-8")

        unique_payload = json.loads(unique.output)
        replace_all_payload = json.loads(replace_all.output)
        self.assertFalse(missing.succeeded)
        self.assertFalse(ambiguous.succeeded)
        self.assertIn("not unique", ambiguous.error)
        self.assertIn("-alpha", unique_payload["diff"])
        self.assertEqual(replace_all_payload["replacements"], 2)
        self.assertEqual(final_content, "ALPHA\nBETA\nBETA\n")

    def test_filesystem_search_files_content_and_filename_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "src").mkdir()
            (root / ".git").mkdir()
            (root / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
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
            files = registry.execute(
                Action(
                    "filesystem.search_files",
                    {"pattern": "*.md", "target": "files", "path": "."},
                    "find",
                    "verify",
                )
            )
            empty_reason = registry.rejection_reason(
                ActionIntent("filesystem.search_files", {"pattern": ""}, "bad search")
            )

        self.assertEqual(content.output, "src/app.py:1:needle = 1")
        self.assertEqual(files.output, "src/notes.md")
        self.assertIn("pattern must not be empty", empty_reason)

    def test_web_tools_fetch_and_extract_local_http(self):
        class Headers:
            def get_content_charset(self):
                return "utf-8"

            def get(self, name, default=""):
                return "text/html; charset=utf-8" if name == "content-type" else default

        class Response:
            status = 200
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback

            def read(self, size=-1):
                body = (
                    "<html><head><style>.x{}</style><script>ignored()</script></head>"
                    "<body><h1>Hello Web</h1><p>needle text</p></body></html>"
                ).encode("utf-8")
                return body if size < 0 else body[:size]

            def geturl(self):
                return "https://example.test/"

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("urllib.request.urlopen", return_value=Response()),
        ):
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("web",)),
            )
            fetched = registry.execute(
                Action(
                    "web.fetch",
                    {"url": "https://example.test/", "max_chars": 32},
                    "fetch",
                    "verify",
                )
            )
            extracted = registry.execute(
                Action(
                    "web.extract",
                    {"url": "https://example.test/"},
                    "extract",
                    "verify",
                )
            )

        fetch_payload = json.loads(fetched.output)
        extract_payload = json.loads(extracted.output)
        self.assertTrue(fetched.succeeded)
        self.assertEqual(fetch_payload["status"], 200)
        self.assertTrue(fetch_payload["truncated"])
        self.assertTrue(extracted.succeeded)
        self.assertIn("Hello Web", extract_payload["text"])
        self.assertIn("needle text", extract_payload["text"])
        self.assertNotIn("ignored", extract_payload["text"])

    def test_web_tools_reject_non_http_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = build_local_tool_registry(
                tmpdir,
                ToolsetConfiguration(enabled_toolsets=("web",)),
            )
            reason = registry.rejection_reason(
                ActionIntent("web.fetch", {"url": "file:///tmp/example"}, "fetch")
            )

        self.assertIn("url must use http or https", reason)

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
        self.assertFalse(full.tool_statuses()["browser.get_images"]["available"])
        self.assertFalse(full.tool_statuses()["browser.console"]["available"])
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

            def snapshot(self, extra=None):
                self.calls.append(("snapshot", extra))
                payload = {"url": "https://example.test", "title": "Title", "text": "Loaded"}
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

        controller = FakeController()
        registry = ToolRegistry()
        register_browser_tools(
            registry,
            controller,
            availability_check=lambda: (True, ""),
        )

        actions = [
            Action("browser.navigate", {"url": "https://example.test"}, "navigate", "verify"),
            Action("browser.snapshot", {}, "snapshot", "verify"),
            Action("browser.click", {"selector": "#submit"}, "click", "verify"),
            Action("browser.type", {"selector": "#q", "text": "hello"}, "type", "verify"),
            Action("browser.scroll", {"direction": "up"}, "scroll", "verify"),
            Action("browser.back", {}, "back", "verify"),
            Action("browser.press", {"key": "Enter"}, "press", "verify"),
            Action("browser.get_images", {}, "images", "verify"),
            Action("browser.console", {"clear": True}, "console", "verify"),
        ]

        observations = [registry.execute(action) for action in actions]

        self.assertTrue(all(observation.succeeded for observation in observations))
        self.assertEqual(registry.spec("browser.navigate").default_risk, RiskLevel.MEDIUM)
        self.assertEqual(registry.spec("browser.get_images").default_risk, RiskLevel.MEDIUM)
        self.assertEqual(registry.spec("browser.console").default_risk, RiskLevel.MEDIUM)
        self.assertIn(("click", "#submit", 30000), controller.calls)
        self.assertIn(("type", "#q", "hello", 30000), controller.calls)
        self.assertIn(("get_images",), controller.calls)
        self.assertIn(("console", True, None), controller.calls)

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
        console_payload = json.loads(console.output)
        self.assertEqual(image_payload["count"], 1)
        self.assertEqual(image_payload["images"][0]["alt"], "Sample image")
        self.assertEqual(console_payload["result"], {"ready": True, "count": 2})

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
