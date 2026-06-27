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
        self.assertIn('id="run-id"', html)
        self.assertIn('id="inspect-run"', html)
        self.assertIn('id="approval-modal"', html)
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

    def test_native_host_example_restricts_extension_origin(self):
        manifest = json.loads((EXTENSION / "native-host.example.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "com.autonomy.app")
        self.assertEqual(manifest["type"], "stdio")
        self.assertEqual(manifest["allowed_origins"], ["chrome-extension://EXTENSION_ID/"])
