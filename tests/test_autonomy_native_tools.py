import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from autonomy import (
    Action,
    ActionIntent,
    ApprovalPolicy,
    RiskLevel,
    ToolsetConfiguration,
    build_local_tool_registry,
)
from autonomy.browser_tools import BrowserController, register_browser_tools
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
        self.assertIn("filesystem.read", registry.names)

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
        ]

        observations = [registry.execute(action) for action in actions]

        self.assertTrue(all(observation.succeeded for observation in observations))
        self.assertEqual(registry.spec("browser.navigate").default_risk, RiskLevel.MEDIUM)
        self.assertIn(("click", "#submit", 30000), controller.calls)
        self.assertIn(("type", "#q", "hello", 30000), controller.calls)

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


if __name__ == "__main__":
    unittest.main()
