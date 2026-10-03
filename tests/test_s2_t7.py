# tests/test_s2_t7.py
"""PLAY T7 (the live test of v0.9.0 on casa-test): a failed bank sync continues on the
cached ledger, as v0.8.0 did (F1)."""
import datetime as _dt

from tests._base import StoreCase
from tests.sim_job import JobDriver

A, B = "aaaaaaaa-1", "bbbbbbbb-2"
DEAD_LINK = "HTTP 404 not_found; cached data unchanged"      # casa-test's dead bank link
FINISHED = ("Accounting work finished.", "All accounting work done")


class FailedSync(StoreCase):
    """F1: the pass imports the cached ledger, sweeps and judges; bank_through stays."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=2)

    def test_a_failed_sync_imports_sweeps_and_judges_with_bank_through_unchanged(self):
        import asks, dates, db, job, views
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
        # the operator still sees it: the bank-connection alert, and how far the bank
        # was checked (the date before the failed sync)
        out = asks.job_report(self.conn, job_id=B, status="ok")
        speak = " ".join(out["speak"]["text"].split())     # the rendering wraps its lines
        self.assertIn(f"The bank connection stopped ({DEAD_LINK}) — new payments aren't "
                      "coming in.", speak)
        texts = " ".join(" ".join(x["text"] for x in out["texts"]).split())
        # the view's coverage line ("First review · bank checked through …" on a first
        # sheet, else "Bank checked through …"): the date before the failed sync
        said = texts.lower()
        self.assertIn(f"bank checked through {dates.short_day(first).lower()}", said)
        today = dates.short_day(later.strftime("%Y-%m-%d")).lower()
        self.assertNotIn(f"bank checked through {today}", said)
        for word in views.FORBIDDEN:
            self.assertNotIn(word, out["speak"]["text"])

    def test_an_import_alone_earns_nothing(self):
        """The credit rule is unchanged: a failed-sync pass's import earns no credit of
        its own (only sweep, search, judge, filing and request credits exist)."""
        import asks
        self.drv.fail_next_sync(DEAD_LINK)
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "sweep")                     # imported, nothing swept yet
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM credits").fetchone()[0], 0)
