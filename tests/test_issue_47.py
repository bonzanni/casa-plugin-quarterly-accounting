"""Issue #47: an empty check told neither the assistant nor the operator why nothing was
checked. Live (2026-10-07): bound with the books starting 1 Oct 2026, the operator asked the
assistant for a Q2 check; the run (started by the assistant: `agent`, scheduled per #45)
posted nothing and completed with "Accounting work finished."; started by the operator it
said "Q4 checked · 0 payments · all accounted for.". Now an empty check says what happened
and how to change it — in its completion text whoever started it, and in the operator's end
message — and a check that did work completes with a short factual line."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
from tests.test_quarter_e2e import pin_clock


class EmptyCheck(StoreCase):
    def setUp(self):
        super().setUp()
        pin_clock(self)                                   # 6 Oct 2026: in Q4 2026

    def run_it(self, started_by, job_id, payments=2, quarter=None):
        import asks
        self.bind(watermark="2026-10-01")
        drv = JobDriver(self, payments=payments)          # payments dated in Q3 2026
        if quarter:
            asks.request_work(self.conn, "check", "operator", quarter=quarter)
        units = drv.run_job(job_id, started_by=started_by)
        end = self.conn.execute("SELECT end_render_id FROM runs WHERE job_id=?",
                                (job_id,)).fetchone()[0]
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (end,)).fetchone() if end else None
        return units, (text[0] if text else None)

    def test_an_assistant_started_empty_check_completes_saying_why(self):
        units, end = self.run_it("agent", "47474747-01")
        self.assertIsNone(end)                            # #45: no message of its own
        self.assertEqual(units[-1]["text"],
                         "Nothing to check yet: the books start 1 Oct 2026 and the bank has "
                         "no payment since. Say 'start from Q3 2026' to include Q3 2026.")

    def test_a_named_quarter_before_the_books_says_how_to_include_it(self):
        units, end = self.run_it("agent", "47474747-02", quarter="2026-Q2")
        self.assertEqual(units[-1]["text"],
                         "Nothing to check for Q2 2026: the books start 1 Oct 2026. Say "
                         "'start from Q2 2026' to include it.")

    def test_the_operators_empty_check_posts_the_same_plain_sentence(self):
        units, end = self.run_it("operator", "47474747-03", quarter="2026-Q2")
        self.assertIn("view", [u["unit"] for u in units])
        self.assertIn("Nothing to check for Q2 2026: the books start 1 Oct 2026. Say "
                      "'start from Q2 2026' to include it.", end)
        self.assertNotIn("all accounted for", end)
        self.assertNotIn("checked ·", end)

    def test_a_check_that_worked_completes_with_its_counts(self):
        import asks
        self.bind(watermark="2026-07-01")
        drv = JobDriver(self, payments=3)
        for i in range(2):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i], f"ZAP-{i + 1}")
        asks.request_work(self.conn, "check", "operator", quarter="2026-Q3")
        units = drv.run_job("47474747-04", started_by="agent")
        self.assertEqual(units[-1]["text"], "Q3 2026 checked: 2 matched, 0 to confirm, "
                                            "1 missing")
