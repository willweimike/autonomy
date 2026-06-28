import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "chrome-extension"


class ChromeExtensionStaticTest(unittest.TestCase):
    def test_manifest_declares_mv3_side_panel_and_native_messaging(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["name"], "Autonomy")
        self.assertIn("nativeMessaging", manifest["permissions"])
        self.assertIn("sidePanel", manifest["permissions"])
        self.assertEqual(manifest["side_panel"]["default_path"], "sidepanel.html")
        self.assertEqual(manifest["background"]["service_worker"], "service_worker.js")

    def test_extension_files_reference_required_message_types(self):
        service_worker = (EXTENSION / "service_worker.js").read_text(encoding="utf-8")
        sidepanel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
        html = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")

        for message_type in (
            "status",
            "session.start",
            "chat.send",
            "run.inspect",
            "approval.respond",
            "approval.requested",
        ):
            self.assertIn(message_type, service_worker + sidepanel)
        self.assertIn('id="workspace"', html)
        self.assertIn('id="max-steps"', html)
        self.assertIn('id="start-session"', html)
        self.assertIn('id="status"', html)
        self.assertIn('id="prompt"', html)
        self.assertIn('id="send"', html)
        self.assertIn('id="run-id"', html)
        self.assertIn('id="inspect-run"', html)
        self.assertIn('id="approval-modal"', html)
        self.assertIn('id="session-status"', html)
        self.assertIn('id="empty-state"', html)
        self.assertIn('id="busy-indicator"', html)
        self.assertIn('id="run-metadata"', html)
        self.assertRegex(sidepanel, r"lastRunId\s*=\s*message\.run_id")
        match = re.search(
            r'document\.getElementById\("inspect-run"\)\.addEventListener\("click",\s*\(\)\s*=>\s*\{(?P<body>.*?)\n\}\);',
            sidepanel,
            re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("send({", body)
        self.assertIn('type: "run.inspect"', body)
        self.assertIn("run_id", body)
        self.assertIn("active sessions", sidepanel)
        self.assertIn("not model/tool status", sidepanel)
        self.assertIn("nativeConnected", sidepanel)
        self.assertIn("busy", sidepanel)
        self.assertIn("chrome.storage.local", sidepanel)
        self.assertIn("updateControls", sidepanel)
        self.assertIn("keydown", sidepanel)
        self.assertIn("Shift", sidepanel)
        self.assertIn("steps_executed", sidepanel)

    def test_sidepanel_css_declares_chat_console_layout(self):
        css = (EXTENSION / "sidepanel.css").read_text(encoding="utf-8")

        for selector in (
            ".app-shell",
            ".topbar",
            ".setup-bar",
            ".transcript",
            ".composer",
            ".message",
            ".metadata-chip",
            ".status-pill",
            ".empty-state",
            ".busy-indicator",
        ):
            self.assertIn(selector, css)
        self.assertIn("height: 100vh", css)
        self.assertIn("overflow-y: auto", css)
        self.assertIn(":focus-visible", css)
        self.assertIn("@media", css)

    def test_native_host_example_restricts_extension_origin(self):
        manifest = json.loads((EXTENSION / "native-host.example.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "com.autonomy.app")
        self.assertEqual(manifest["type"], "stdio")
        self.assertEqual(manifest["allowed_origins"], ["chrome-extension://EXTENSION_ID/"])
        self.assertEqual(manifest["path"], "/absolute/path/to/autonomy-chrome-host")

    def test_service_worker_rejects_second_panel_connection(self):
        service_worker = (EXTENSION / "service_worker.js").read_text(encoding="utf-8")

        self.assertIn("let panelPort = null", service_worker)
        self.assertIn("Another Autonomy panel is already connected", service_worker)
        self.assertIn("port.disconnect()", service_worker)
