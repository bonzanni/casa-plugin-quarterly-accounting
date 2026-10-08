"""S7 §15: the tool surface, the version, the README's floor."""
import json
import subprocess
import sys
import unittest

from tests._base import StoreCase, ROOT

REMOVED = ("job_report", "apply_reply", "confirm_match", "reject_match", "set_exemption",
           "stop_chasing", "set_watermark", "set_package_name")
ADDED = ("show_view", "post_results", "post_package", "propose_reading", "apply_reading",
         "cancel_reading", "verdict", "propose_account", "ask_state")


class Surface(StoreCase):
    def test_tool_lists_agree(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/check_tool_agreement.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_removed_and_added(self):
        import qa_server, tools  # noqa: F401
        for t in REMOVED:
            self.assertNotIn(t, qa_server.TOOLS)
        for t in ADDED:
            self.assertIn(t, qa_server.TOOLS)

    def test_version_and_floor(self):
        m = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(m["version"], "0.11.3")
        import version
        self.assertEqual(version.PLUGIN_VERSION, "0.11.3")
        readme = (ROOT / "README.md").read_text()
        self.assertIn("Casa v0.344.39 or newer", readme)
        self.assertIn("specialist:finance", readme)
        self.assertIn('job: "quarterly-accounting:work"', readme)

    def test_no_tool_description_names_ellen_or_resident(self):
        import qa_server, tools  # noqa: F401
        for name, t in qa_server.TOOLS.items():
            d = t["description"]
            self.assertNotIn("Ellen", d, name)
            self.assertNotIn("resident", d.replace('extraction_author="resident"', ""), name)
