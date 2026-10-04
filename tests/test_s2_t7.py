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
FINISHED = ("Accounting work finished.", "All accounting work done")
NO_TOOLS = ("Accounting check stopped: bank-feed's tools are not available to the finance "
            "specialist.")


class FailedSync(StoreCase):
    """F1: the pass imports the cached ledger, sweeps and judges; bank_through stays."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=2)

    def failed_sync_run(self):
        """A good sync (job A), then a failed one three days later (job B). Returns
        (bank_through after A, B's clock, B's units)."""
        import asks, db
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)                               # a good sync: checked through today
        first = self.conn.execute("SELECT bank_through FROM snapshots ORDER BY snapshot_id"
                                  " DESC LIMIT 1").fetchone()[0]
        self.assertIsNotNone(first)
        later = db._clock() + _dt.timedelta(days=3)
        with self.patch_clock(later):
            self.drv.add_payments(["2026-09-20"])        # rows bank-feed holds in its cache
            self.drv.fail_next_sync(DEAD_LINK)
            asks.request_work(self.conn, "check", "operator")
            units = self.drv.run_job(B)
        return first, later, units

    def test_a_failed_sync_imports_sweeps_and_judges_with_bank_through_unchanged(self):
        first, later, units = self.failed_sync_run()
        kinds = [u["unit"] for u in units]
        for k in ("probes", "snapshot", "sweep", "judge"):
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
        keys = [r[0] for r in self.conn.execute("SELECT key FROM credits WHERE pass_id=?",
                                                (p["pass_id"],))]
        self.assertTrue(any(k.startswith("sweep:") for k in keys), keys)
        self.assertTrue(any(k.startswith("judge:") for k in keys), keys)
        self.assertEqual((units[-1]["text"], units[-1]["progress"]["summary"]), FINISHED)

    def test_a_failed_sync_is_still_told_to_the_operator(self):
        import dates, views
        first, later, units = self.failed_sync_run()
        kinds = [u["unit"] for u in units]
        # the operator still sees it, posted by the job (S7 §5): the bank-connection
        # alert, and how far the bank was checked (the date before the failed sync)
        def text(rid):
            return self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                     (rid,)).fetchone()[0]
        posted = [text(r) for u in units if u["unit"] == "post" for r in u["render_ids"]]
        alert = [x for x in posted if x.startswith("The bank connection stopped")]
        self.assertEqual(len(alert), 1, posted)
        speak = " ".join(alert[0].split())                 # the rendering wraps its lines
        self.assertIn(f"The bank connection stopped ({views.field(DEAD_LINK)}) — new payments aren't "
                      "coming in.", speak)
        sheets = [text(u["render_id"]) for u in units if u["unit"] == "view"]
        self.assertEqual(len(sheets), 1)
        self.assertLess(kinds.index("post"), kinds.index("view"))   # the alert first
        texts = " ".join(" ".join(sheets).split())
        # the view's coverage line ("First review · bank checked through …" on a first
        # sheet, else "Bank checked through …"): the date before the failed sync
        said = texts.lower()
        self.assertIn(f"bank checked through {dates.short_day(first).lower()}", said)
        today = dates.short_day(later.strftime("%Y-%m-%d")).lower()
        self.assertNotIn(f"bank checked through {today}", said)
        for word in views.FORBIDDEN:
            self.assertNotIn(word, alert[0])

    def test_an_import_alone_earns_nothing(self):
        """The credit rule is unchanged: a failed-sync pass's import earns no credit of
        its own (only sweep, search, judge, filing and request credits exist)."""
        import asks
        self.drv.fail_next_sync(DEAD_LINK)
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "sweep")                     # imported, nothing swept yet
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM credits").fetchone()[0], 0)


class RunEnd(StoreCase):
    """F2: the completion's text and summary, and job_status's text, are ONE function
    (job.run_end) of the run's passes."""

    def setUp(self):
        super().setUp()
        self.bind()

    def take_the_cron_check(self, pass_id):
        """S7 §4.1: a job id's first claim with nothing queued records a cron check; the
        pass takes it, as the cursor would, so the pass's end settles it."""
        import asks, db
        with db.tx(self.conn):
            asks.take_queued(self.conn, pass_id)

    def deliver_posts(self, token):
        """S7 §5: the job posts what the run owes before it may end; each post's receipt
        arrives and is marked."""
        import db, job, views
        while True:
            with db.tx(self.conn):
                if job.done(self.conn, self._job_of(token), token):
                    return
            u = job.next_unit(self.conn, token)
            assert u["unit"] in ("post", "view"), u
            for rid in u.get("render_ids") or [u["render_id"]]:
                views.mark_rendering_delivered(self.conn, rid)

    def _job_of(self, token):
        return self.conn.execute("SELECT job_id FROM claims WHERE gen=?",
                                 (token,)).fetchone()[0]

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
        self.assertEqual((last["text"], last["progress"]["summary"]), FINISHED)
        self.assertEqual(job.status(self.conn, A), {"done": True, "text": FINISHED[0]})

    def test_another_runs_stop_is_not_this_runs(self):
        import asks, job
        drv = JobDriver(self)
        drv.no_bank_tools()
        asks.request_work(self.conn, "check", "operator")
        drv.run_job(A)                                    # A's pass stopped
        drv._no_tools = False
        asks.request_work(self.conn, "check", "operator")
        last = drv.run_job(B)[-1]                          # B's pass finished
        self.assertEqual((last["text"], last["progress"]["summary"]), FINISHED)
        self.assertEqual(job.status(self.conn, B)["text"], FINISHED[0])
        self.assertEqual(job.status(self.conn, A)["text"], NO_TOOLS)

    def test_one_stopped_pass_among_finished_ones_is_named(self):
        """A run of two passes: the first finished, the second stopped."""
        import asks, job
        drv = JobDriver(self)
        asks.request_work(self.conn, "check", "operator")
        drv.run_until(A, "complete")
        drv.no_bank_tools()
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)                        # the same run's next batch
        last = drv.next_until(t, "complete")
        self.assertEqual(job.run_passes(self.conn, A), 2)
        self.assertEqual((last["text"], last["progress"]["summary"]), (NO_TOOLS, NO_TOOLS))

    def test_an_interrupted_pass_is_named(self):
        import db, job, passes
        t = job.claim(self.conn, A)
        self.take_the_cron_check(self.start_job_pass(t))
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "interrupted", {})
        text = job.status(self.conn, A)["text"]
        self.assertEqual(text, "Accounting check interrupted before it finished.")
        self.assert_operator_text(text)
        u = job.next_unit(self.conn, job.claim(self.conn, A))
        self.assertEqual((u["unit"], u["text"], u["progress"]["summary"]),
                         ("complete", text, text))

    def test_an_interrupted_check_says_how_far_it_got(self):
        import job
        self.assertEqual(job._end_line("interrupted", {"checked": 3, "total": 5}),
                         "Accounting check interrupted: 3 of 5 new payments checked.")

    def test_a_pass_this_runs_claim_stopped_on_adoption_is_named(self):
        """The stopping claim never takes the pass (test_s2_claim), yet its run ended
        it stopped: the run says so."""
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        self.take_the_cron_check(pid)
        job.claim(self.conn, B)
        job.claim(self.conn, C)
        tD = job.claim(self.conn, D)                       # the third adoption: stopped
        row = self.conn.execute("SELECT holder_job, outcome, report_json FROM passes WHERE"
                                " pass_id=?", (pid,)).fetchone()
        self.assertEqual((row["holder_job"], row["outcome"]), (C, "stopped"))
        self.assertEqual(json.loads(row["report_json"])["stopped_by"], D)
        want = "Accounting check stopped: it kept stopping."
        self.assertFalse(job.status(self.conn, D)["done"])  # S7 §5: its stop line is owed
        self.deliver_posts(tD)
        self.assertEqual(job.status(self.conn, D)["text"], want)
        u = job.next_unit(self.conn, tD)
        self.assertEqual((u["unit"], u["text"]), ("complete", want))

    def test_a_long_reason_is_kept_to_one_topic_line(self):
        import job
        rep = {"stopped_reason": "x " * 400 + "\nsecond line"}
        line = job._end_line("stopped", rep)
        self.assertNotIn("\n", line)
        import db
        t = job.claim(self.conn, A)
        self.take_the_cron_check(self.start_job_pass(t))
        import passes
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "stopped", rep)
        self.deliver_posts(t)                             # S7 §5: the stop line first
        self.assert_operator_text(job.status(self.conn, A)["text"])
