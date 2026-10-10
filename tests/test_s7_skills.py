# tests/test_s7_skills.py
"""S7 §3: closes #41 — no skill or tool description assigns filing, asking or answering
to Ellen or a resident; the desk skill fits a fresh session; every flow of §4–§11 is in
it; the job skill names the simple loop's units."""
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
                  "record_delivery", "ask_state", "get_package", "propose_account",
                  "mark_rendering_delivered", "<silent/>", "forward it from Telegram",
                  "I couldn't start the check", 'show_view(view="open")',
                  "Where a quarter stands",            # 0.11.2: intents, not phrases
                  "#1305", "check_setup",
                  'quarter=<the quarter named, else the one talked about'):  # #116
            self.assertIn(s, DESK, s)
        for gone in ("job_report", "apply_reply", "build_review(", "send_media", "email it",
                     "request_package", "note_render_id"):  # removed-name: asserted absent
            self.assertNotIn(gone, DESK, gone)

    def test_the_desk_never_calls_a_buttons_tool(self):
        for t in ("verdict", "apply_reading", "cancel_reading", "bind_account"):
            self.assertIsNone(re.search(rf"`{t}\(", DESK), t)

    def test_the_job_skill_has_the_simple_loops_units_and_no_relay(self):
        for unit in ("probes", "snapshot", "filing", "payment", "mirror", "post", "view"):
            self.assertIn(f"### `{unit}`", JOB, unit)
        for s in ("post_results", "show_view", "decide(", "record_mirror"):
            self.assertIn(s, JOB, s)
        # simple loop §1/§2: the job never builds or sends a package; the S2 units are gone
        for s in ("### `build`", "### `deliver`", "### `item`", "### `judge`",
                  "### `gmail-probe`", "post_package", "build_quarterly_package",  # removed-name: asserted absent
                  "stage_for_delivery"):
            self.assertNotIn(s, JOB, s)
        self.assertNotIn("job_report", JOB)
        self.assertNotIn("asking for work and relaying it", JOB.lower())


class T16DelegatedStart(unittest.TestCase):
    """T16: a delegation asking finance to start the accounting check (naming
    quarterly-accounting:work) is the desk's check ask, never the job."""

    def test_the_job_skill_turns_a_turn_without_a_job_id_back_to_the_desk(self):
        body = JOB.split("---", 2)[2]
        head = " ".join(body[:600].split())
        self.assertIn("Only with a `Job id:` line in your brief.", head)
        self.assertIn("load skill quarterly-accounting", head)
        self.assertIn('`request_work(kind="check", trigger="operator")`, then `start_job`', head)

    def test_the_desk_asks_name_the_delegated_start(self):
        asks = DESK[DESK.index("## Asks"):DESK.index("## A file the operator sent")]
        flat = " ".join(asks.split())
        self.assertIn("a delegate relaying the operator's ask for that work (even naming "
                      "`quarterly-accounting:work`)", flat)                   # #103
        self.assertIn("never ask the delegate to", flat)

    def test_request_work_names_the_delegated_start(self):
        import qa_server, tools  # noqa: F401
        d = " ".join(qa_server.TOOLS["request_work"]["description"].split())
        self.assertIn("when the operator asks for the work, also relayed by a delegate (even "
                      "naming quarterly-accounting:work)", d)                # #103
        self.assertIn('A bare "check Q3" asks where the quarter stands', d)
        self.assertIn("kind=check, trigger=operator", d)
