# tests/test_s2_t7.py
"""PLAY T7 (the live test of v0.9.0 on casa-test): a failed bank sync continues on the
cached ledger, as v0.8.0 did (F1); a run whose pass did not finish never says "finished"
(F2)."""
import datetime as _dt
import json
import unittest

from tests._base import StoreCase
from tests.sim_job import JobDriver
import views  # noqa: E402

A, B, C, D = "aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3", "dddddddd-4"
DEAD_LINK = "HTTP 404 not_found; cached data unchanged"      # casa-test's dead bank link
# issue #47: a finished run completes with what it checked (cards.checked_line); 0.11.2:
# after a delivered end card, job.CARD_POSTED
FINISHED_RE = (r"^(Q\d \d{4} checked: \d+ matched, \d+ to confirm, \d+ missing(, \d+ pending)?"
               r"|Nothing to check .*"
               r"|The result card is posted in the chat; there is nothing to add\.)$")
NO_TOOLS = ("Accounting check stopped: bank-feed's tools are not available to the finance "
            "specialist.")


class FailedSync(StoreCase):
    """F1: the pass imports the cached ledger, sweeps and judges; bank_through stays."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=2)

    def failed_sync_run(self, days=3):
        """A good sync (job A), then a failed one `days` later (job B). Returns
        (bank_through after A, B's clock, B's units)."""
        import asks, db
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)                               # a good sync: checked through today
        first = self.conn.execute("SELECT bank_through FROM snapshots ORDER BY snapshot_id"
                                  " DESC LIMIT 1").fetchone()[0]
        self.assertIsNotNone(first)
        later = db._clock() + _dt.timedelta(days=days)
        with self.patch_clock(later):
            self.drv.add_payments(["2026-09-20"])        # rows bank-feed holds in its cache
            self.drv.fail_next_sync(DEAD_LINK)
            asks.request_work(self.conn, "check", "operator")
            units = self.drv.run_job(B)
        return first, later, units

    def test_a_failed_sync_imports_and_works_with_bank_through_unchanged(self):
        """Simple loop Task 10: the failed-sync run still imports bank-feed's cached ledger
        and works the payments (the new units), its pass complete, bank_through kept."""
        first, later, units = self.failed_sync_run()
        kinds = [u["unit"] for u in units]
        for k in ("probes", "snapshot", "filing", "payment"):
            self.assertIn(k, kinds)
        self.assertEqual(kinds[-1], "complete")
        sync = self.conn.execute("SELECT ok, detail FROM probes WHERE kind='bank_sync'"
                                 ).fetchone()
        self.assertEqual(tuple(sync), (0, DEAD_LINK))      # the sync really failed
        p = self.conn.execute("SELECT pass_id, outcome FROM passes ORDER BY generation DESC"
                              " LIMIT 1").fetchone()
        self.assertEqual(p["outcome"], "complete")         # never `stopped`
        snaps = self.conn.execute("SELECT pass_id, bank_through FROM snapshots ORDER BY"
                                  " snapshot_id").fetchall()
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps[-1]["pass_id"], p["pass_id"])
        self.assertEqual(snaps[-1]["bank_through"], first)  # not advanced
        self.assertNotEqual(first, later.strftime("%Y-%m-%d"))
        decided = self.conn.execute("SELECT count(*) FROM run_work WHERE job_id=? AND"
                                    " outcome='missing'", (B,)).fetchone()[0]
        self.assertEqual(decided, 3)                        # the cached rows were worked
        self.assertRegex(units[-1]["text"], FINISHED_RE)
        self.assertEqual(units[-1]["progress"]["summary"], "All accounting work done")
        # D10: one failed sync three days after a good one is not yet a failure line
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)

    def test_a_failed_sync_is_told_once_the_last_good_sync_is_over_a_week_old(self):
        """D10 (simple loop §1): one failed sync is not yet a failure line; the store's last
        successful sync more than 7 days old is, in the run's one message."""
        import dates
        first, later, units = self.failed_sync_run(days=8)
        (msg,) = [u for u in units if u["unit"] in ("view", "post")]
        text = " ".join(self.render_text(msg.get("render_id") or msg["render_ids"][0]).split())
        self.assertIn(f"Bank not synced since {dates.short_day(first)} · bank-feed needs"
                      " attention", text)
        for word in views.FORBIDDEN:
            self.assertNotIn(word, text)


class RunEnd(StoreCase):
    """F2: the completion's text and summary, and job_status's text, are ONE function
    (job.run_end) of the run's passes."""

    def setUp(self):
        super().setUp()
        self.bind()

    def assert_operator_text(self, text):
        import job, views
        self.assertNotIn("\n", text)
        self.assertLessEqual(views.utf16_len(text), job.TOPIC_MAX)
        for word in views.FORBIDDEN:
            self.assertNotIn(word, text)

    def test_a_stopped_run_names_the_reason_everywhere(self):
        import asks, job
        drv = JobDriver(self)
        drv.no_bank_tools()
        asks.request_work(self.conn, "check", "operator")
        units = drv.run_job(A)
        last = units[-1]
        self.assertEqual(last["unit"], "complete")
        self.assertEqual(last["text"], NO_TOOLS)
        self.assertEqual(last["progress"]["summary"], NO_TOOLS)
        self.assertEqual(job.status(self.conn, A), {"done": True, "text": NO_TOOLS})
        self.assert_operator_text(NO_TOOLS)

    def test_an_all_finished_run_keeps_the_old_texts(self):
        import asks, job
        drv = JobDriver(self)
        asks.request_work(self.conn, "check", "operator")
        last = drv.run_job(A)[-1]
        self.assertRegex(last["text"], FINISHED_RE)
        self.assertEqual(last["progress"]["summary"], "All accounting work done")
        self.assertEqual(job.status(self.conn, A), {"done": True, "text": last["text"]})

    def test_another_runs_stop_is_not_this_runs(self):
        import asks, job
        drv = JobDriver(self)
        drv.no_bank_tools()
        asks.request_work(self.conn, "check", "operator")
        drv.run_job(A)                                    # A's pass stopped
        drv._no_tools = False
        asks.request_work(self.conn, "check", "operator")
        last = drv.run_job(B)[-1]                          # B's pass finished
        self.assertRegex(last["text"], FINISHED_RE)
        self.assertEqual(last["progress"]["summary"], "All accounting work done")
        self.assertEqual(job.status(self.conn, B)["text"], last["text"])
        self.assertEqual(job.status(self.conn, A)["text"], NO_TOOLS)

    def test_an_interrupted_pass_is_named(self):
        import db, job, loop
        t = job.claim(self.conn, A)                        # the run's one pass
        with db.tx(self.conn):
            loop.end_pass(self.conn, t, "interrupted", {})
        text = job.run_end(self.conn, A)[0]
        self.assertEqual(text, "Accounting check interrupted before it finished.")
        self.assert_operator_text(text)
        u = job.next_unit(self.conn, job.claim(self.conn, A))
        self.assertEqual((u["unit"], u["text"], u["progress"]["summary"]),
                         ("complete", text, text))
        self.assertEqual(job.status(self.conn, A), {"done": True, "text": text})

    def test_an_interrupted_check_says_how_far_it_got(self):
        import job
        self.assertEqual(job._end_line("interrupted", {"checked": 3, "total": 5}),
                         "Accounting check interrupted: 3 of 5 new payments checked.")

    def test_a_long_reason_is_kept_to_one_topic_line(self):
        import db, job, loop
        rep = {"stopped_reason": "x " * 400 + "\nsecond line"}
        self.assertNotIn("\n", job._end_line("stopped", rep))
        t = job.claim(self.conn, A)
        with db.tx(self.conn):
            loop.end_pass(self.conn, t, "stopped", rep)
        self.assert_operator_text(job.run_end(self.conn, A)[0])
