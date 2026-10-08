"""Issue #47: an empty check told neither the assistant nor the operator why nothing was
checked. Live (2026-10-07): bound with the books starting 1 Oct 2026, the operator asked the
assistant for a Q2 check; the run (started by the assistant: `agent`, scheduled per #45)
posted nothing and completed with "Accounting work finished."; started by the operator it
said "Q4 checked · 0 payments · all accounted for.". Now an empty check says what happened
and how to change it — in its completion text whoever started it, and in the operator's end
message — and a check that did work completes with a short factual line."""
from tests._base import StoreCase, untag
from tests.sim_job import JobDriver
from tests.test_quarter_e2e import pin_clock
import views


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

    def end_text(self, job_id):
        end = self.conn.execute("SELECT end_render_id FROM runs WHERE job_id=?",
                                (job_id,)).fetchone()[0]
        return self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (end,)).fetchone()[0]

    def test_an_assistant_started_empty_check_completes_saying_why(self):
        units, end = self.run_it("agent", "47474747-01")
        self.assertIsNone(end)                            # #45: no message of its own
        self.assertEqual(units[-1]["text"],
                         "Nothing to check for Q4 2026 yet: the bank has no payment in it "
                         "(the books start 1 Oct 2026). Ask me to do Q3 to include it.")

    def test_the_books_quarter_empty_with_a_later_payment_claims_nothing_beyond_it(self):
        """h1 (Astra S2): bound from the quarter's start, a payment only in a LATER quarter,
        the books' quarter checked: it has no payment, the bank does — never "since"."""
        import asks
        self.bind(watermark="2026-07-01")
        drv = JobDriver(self, payments=0)
        drv.pay_once("Zapier", 1000, "2026-10-02")
        asks.request_work(self.conn, "check", "operator", quarter="2026-Q3")
        units = drv.run_job("47474747-05", started_by="agent")
        # 0.11.2: the operator's ask posts its end card; the completion says only that
        import job
        self.assertEqual(units[-1]["text"], job.CARD_POSTED)
        # r1 (Astra S2): pinned on the card the operator got — the quarter's plain sentence,
        # the later quarter's payment summarised, no package for an empty quarter
        card = drv.posted_end("47474747-05")
        text = views.unesc(card["text"])
        self.assertTrue(text.startswith("Nothing to check for Q3 2026 yet: the bank has no "
                                        "payment in it (the books start 1 Jul 2026). Ask me to "
                                        "do Q2 to include it."), text)
        self.assertIn("Q4 so far: 1 missing", text)
        self.assertNotIn("checked ·", text)
        self.assertNotIn("Get package", [b["label"] for b in card["buttons"]])

    def test_a_completed_runs_line_never_changes_with_a_later_run(self):
        """h3 (Astra S2): job_status of a completed run re-derived its line from the store
        as it is now, so a later run's quarter replaced it. The line is kept at completion."""
        import job
        self.bind(watermark="2026-04-01")
        drv = JobDriver(self, payments=0)
        drv.pay_once("Zapier", 1000, "2026-05-05")
        drv.gmail.invoice("Zapier", 1000, "EUR", "2026-05-05", "ZAP-Q2")
        # r2 (Astra S2): a run whose end card was NOT delivered completes with its checked
        # line — the line h3 freezes (a delivered card's CARD_POSTED is constant)
        drv.deliver = False
        a = drv.run_job("47474747-06", started_by="agent")[-1]["text"]
        drv.deliver = True
        self.assertEqual(a, "Q2 2026 checked: 1 matched, 0 to confirm, 0 missing")
        self.assertTrue(untag(self.end_text("47474747-06")).startswith("Q2"),
                        self.end_text("47474747-06"))
        drv.pay_once("Zapier", 2000, "2026-07-05")
        drv.gmail.invoice("Zapier", 2000, "EUR", "2026-07-05", "ZAP-Q3")
        drv.run_job("47474747-07", started_by="agent")
        self.assertEqual(job.run_end(self.conn, "47474747-06")[0], a)

    def test_a_named_quarter_before_the_books_says_how_to_include_it(self):
        # 0.11.2 (#55): the ask naming a quarter before the books moves the start to its
        # first day; the (empty) quarter's end card says so plainly
        import job
        units, end = self.run_it("agent", "47474747-02", quarter="2026-Q2")
        self.assertEqual(units[-1]["text"], job.CARD_POSTED)
        # r1 (Astra S2): pinned on the card the operator got
        self.assertTrue(views.unesc(end).startswith(
            "Nothing to check for Q2 2026 yet: the bank has no payment in it (the books start "
            "1 Apr 2026). Ask me to do Q1 to include it."), end)
        self.assertNotIn("checked ·", end)
        self.assertNotIn("Q2 zip", end)

    def test_the_operators_empty_check_posts_the_same_plain_sentence(self):
        units, end = self.run_it("operator", "47474747-03", quarter="2026-Q2")
        self.assertIn("view", [u["unit"] for u in units])
        self.assertIn("Nothing to check for Q2 2026 yet: the bank has no payment in it (the "
                      "books start 1 Apr 2026). Ask me to do Q1 to include it.", views.unesc(end))
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
        # 0.11.2: a delivered end card makes the completion CARD_POSTED; the counts line
        # (the completion without a delivered card) still counts the quarter
        import cards, job
        self.assertEqual(units[-1]["text"], job.CARD_POSTED)
        # r1 (Astra S2): the counts are pinned on the card the operator got, not recomputed
        lines = drv.posted_end("47474747-04")["text"].split("\n")
        self.assertTrue(lines[0].startswith("Q3 checked · 3 payments · "), lines)
        self.assertEqual(lines[1], "2 matched · 1 missing")
