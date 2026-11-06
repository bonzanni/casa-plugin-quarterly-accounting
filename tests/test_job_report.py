# tests/test_job_report.py
"""Results shown at least once — what is kept of job_report's tests (a run's result is its one message, test_loop_run;
the per-request result pages are deleted). deleted job_report, the orphan handoff, the standing retry and
the cancel records; the stalled-send recovery moved to the claim (test_starter_claim). The
result cases are ported onto the job's own `post` and `view` units; the package
recovery onto its `build` and `deliver` units."""
import json

from tests._base import StoreCase

A, B, C, D = "aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3", "dddddddd-4"


class Report(StoreCase):
    def test_the_answer_is_deliverable_text_by_text(self):
        # job_report's `texts` left with the tool (no server code
        # returns it); the per-page check is pinned on `receipt_pages`
        import tools, views
        out = {"receipt_pages": ["ok", "x" * (views.TELEGRAM_LIMIT + 1)]}
        with self.assertRaises(tools.Undeliverable):
            tools._deliverable("record_delivery", out)
        tools._deliverable("record_delivery", {"receipt_pages": ["ok", "ok"]})


class StoppedResult(StoreCase):
    def test_a_stop_without_a_reason_says_only_that(self):
        import db, job, loop
        t = job.claim(self.conn, A)
        with db.tx(self.conn):
            loop.end_pass(self.conn, t, "stopped", {})
        u = job.next_unit(self.conn, t)
        self.assertEqual((u["unit"], u["text"]), ("complete", "Accounting check stopped."))

    def test_a_cron_check_that_completed_shows_nothing(self):
        """A scheduled run with nothing to say posts nothing; its
        request is done and reported at the pass's end."""
        import db, job, loop
        t = job.claim(self.conn, A)                   # the implicit cron check, taken
        with db.tx(self.conn):
            loop.end_pass(self.conn, t, "complete")
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "complete")
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "reported")
