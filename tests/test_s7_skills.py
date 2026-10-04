# tests/test_s7_skills.py
"""S7 §3: closes #41 — no skill or tool description assigns filing, asking or answering
to Ellen or a resident; the desk skill fits a fresh session; every flow of §4–§11 is in
it; the job skill names the four new units."""
import re
from tests._base import ROOT

import unittest

DESK = (ROOT / "skills/quarterly-accounting/SKILL.md").read_text()
JOB = (ROOT / "skills/quarterly-job/SKILL.md").read_text()


class Skills(unittest.TestCase):
    def test_no_skill_names_ellen_or_a_resident(self):
        for p in sorted((ROOT / "skills").glob("*/SKILL.md")):
            t = p.read_text().replace('extraction_author="resident"', "")
            self.assertNotIn("Ellen", t, p)
            self.assertNotIn("resident", t.lower(), p)

    def test_the_desk_skill_fits_a_fresh_session(self):
        self.assertLessEqual(len(DESK), 10_000)

    def test_the_desk_files_a_routed_file_itself(self):
        for s in ("list_inbound_files", "share_inbound_file", "ingest_document",
                  'extraction_author="desk"', 'source="manual-telegram"',
                  'request_work(kind="handover"', "start_job"):
            self.assertIn(s, DESK, s)

    def test_the_desk_flows(self):
        for s in ("show_view", "propose_reading", "post_results", "post_package",
                  "record_delivery", "ask_state", "request_package", "propose_account",
                  "mark_rendering_delivered", "<silent/>", "forward it from Telegram",
                  "I couldn't start the check"):
            self.assertIn(s, DESK, s)
        for gone in ("job_report", "apply_reply", "build_review(", "send_media", "email it"):
            self.assertNotIn(gone, DESK, gone)

    def test_the_desk_never_calls_a_buttons_tool(self):
        for t in ("verdict", "apply_reading", "cancel_reading", "bind_account"):
            self.assertIsNone(re.search(rf"`{t}\(", DESK), t)

    def test_the_job_skill_has_the_four_units_and_no_relay(self):
        for s in ("### `post`", "### `view`", "### `build`", "### `deliver`",
                  "post_results", "show_view", "post_package", "record_delivery",
                  "build_quarterly_package", "stage_for_delivery"):
            self.assertIn(s, JOB, s)
        self.assertNotIn("job_report", JOB)
        self.assertNotIn("asking for work and relaying it", JOB.lower())
