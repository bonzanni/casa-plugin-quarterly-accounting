# tests/test_s2_cursor.py
from tests._base import StoreCase
from tests.sim_job import JobDriver          # added in this task: drives units mechanically

A = "aaaaaaaa-1"


class CheckPass(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self)            # a real bank-feed (tests/bankfeed.py) + Gmail fake

    def test_a_check_runs_unit_by_unit_to_completion(self):
        import asks
        asks.request_work(self.conn, "check", "operator")
        units = self.drv.run_job(A)           # claim, then job_next until complete
        kinds = [u["unit"] for u in units]
        self.assertEqual(kinds[0], "probes")
        self.assertIn("snapshot", kinds)
        self.assertLess(kinds.index("snapshot"), kinds.index("gmail-probe"))
        self.assertIn("judge", kinds)
        self.assertEqual(kinds[-1], "complete")
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "complete"))

    def test_a_turn_cut_mid_item_rehands_that_item_first(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "item")                   # the first item is handed out
        cut = self.drv.last["item"]["pid"]
        t2 = job.claim(self.conn, A)                     # next batch, fresh conversation
        nxt = self.drv.next_until(t2, "item")
        self.assertEqual(nxt["item"]["pid"], cut)

    def test_a_refresh_split_across_claims_restarts_the_acquisition(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        u = self.drv.run_until(A, "snapshot")           # probes done under claim 1
        t2 = job.claim(self.conn, A)
        self.assertEqual(job.next_unit(self.conn, t2)["unit"], "probes")   # not snapshot

    def test_a_mid_pass_request_forces_a_refresh_before_the_next_decision(self):
        import asks, job
        asks.request_work(self.conn, "check", "cron")
        self.drv.run_until(A, "gmail-probe")
        asks.request_work(self.conn, "check", "operator")
        t = self.drv.token
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "probes")

    def test_a_gate_poisoned_after_the_import_stops_the_pass(self):
        import asks, db, passes
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "sweep")
        with db.tx(self.conn):
            passes.poison(self.conn, "the bank ledger changed during this pass")
        self.drv.run_job(A)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))

    def test_two_w_refreshes_import_twice(self):
        import asks, datetime as _dt, db, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "judge")
        n0 = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        for k in (1, 2):
            later = db._clock() + _dt.timedelta(seconds=job.W_S * 2 * k)
            with self.patch_clock(later):
                t = job.claim(self.conn, A)
                self.drv.next_until(t, "judge")         # a refresh, then judging again
        n = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        self.assertEqual(n - n0, 2)
        self.assertEqual(self.conn.execute("SELECT w_refreshes, w_pending FROM passes"
                                           " ORDER BY rowid DESC LIMIT 1").fetchone()[:], (2, 0))

    def test_missing_bank_tools_stop_the_pass(self):
        import asks
        self.drv.no_bank_tools()                  # the probes unit records bank_tools=false
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))

    def test_a_failed_sync_stops_the_pass_with_its_reason(self):
        import asks
        self.drv.fail_next_sync("bank unreachable")
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0)

    def test_eight_asks_that_each_stop_at_once_all_get_dispositions(self):
        import asks
        self.drv.bankfeed.restore_since_install()        # every pass stops at its probes
        for q in ("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4",
                  "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"):
            asks.request_package(self.conn, q, "telegram")
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                           " state='queued'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests WHERE"
                                           " state='queued'").fetchone()[0], 0)

    def test_the_report_carries_the_classification_queue(self):
        import asks, json
        self.drv = JobDriver(self, queue=(4, 1))
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)
        rep = json.loads(self.conn.execute("SELECT report_json FROM passes ORDER BY rowid"
                                           " DESC LIMIT 1").fetchone()[0])
        self.assertEqual(rep["awaiting_classification"], 5)

    def test_a_refused_completion_is_reissued_to_any_fresh_turn(self):
        """ha-casa-app#1180: emit_completion refused (unread inbound); a fresh batch's
        job_next answers `complete` again, and a topic turn's job_status says done."""
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        units = self.drv.run_job(A)                       # ends at `complete`
        self.assertEqual(units[-1]["unit"], "complete")
        t2 = job.claim(self.conn, A)                       # a fresh batch after the refusal
        self.assertEqual(job.next_unit(self.conn, t2)["unit"], "complete")
        self.assertEqual(job.status(self.conn, A), {"done": True,
                                                     "text": "Accounting work finished."})

    def test_job_status_never_claims_and_says_not_done_while_work_remains(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "sweep")
        top = self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
        self.assertFalse(job.status(self.conn, A)["done"])
        self.assertEqual(self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0], top)

    def test_a_stopped_gate_ends_the_pass_stopped(self):
        import asks
        self.drv.bankfeed.restore_since_install()        # the ledger was restored
        asks.request_work(self.conn, "check", "operator")
        units = self.drv.run_job(A)
        r = self.conn.execute("SELECT outcome FROM work_requests").fetchone()
        self.assertEqual(r["outcome"], "stopped")

    def test_the_job_that_started_a_pass_is_never_charged_an_adoption(self):
        """_begin_next seeds adopters_json with the starter (spec §6.3): its own later
        claims re-take the pass without spending the adoption budget."""
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "item")
        for _ in range(job.ADOPTIONS_MAX + 1):
            t = job.claim(self.conn, A)
        self.assertEqual(self.drv.next_until(t, "item")["unit"], "item")
        p = self.conn.execute("SELECT adoptions, adopters_json, ended_at FROM passes").fetchone()
        self.assertEqual((p["adoptions"], p["adopters_json"], p["ended_at"]),
                         (0, '["%s"]' % A, None))

    def test_a_quarter_page_in_a_check_pass_does_not_complete_its_sweep(self):
        """W counts from the sweep's completion (§5.2): a quarter-scoped listing with
        nothing due in that quarter completes a package pass's sweep, never a check's
        while other quarters' rows are still due."""
        import asks, sweep
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "sweep")                   # imported; Q3 rows due
        page = sweep.list_projections(self.conn, token=self.drv.token, quarter="2026-Q4")
        self.assertEqual(page["remaining_in_cycle"], 0)
        self.assertIsNone(self.conn.execute("SELECT swept_at FROM snapshots ORDER BY"
                                            " snapshot_id DESC LIMIT 1").fetchone()[0])

    def test_a_new_claim_never_resumes_the_previous_claims_acquisition(self):
        """§5.2 (Astra r4 S2): the acquisition a previous claim handed out and did not
        import is abandoned — the new claim's probes carry a new acq, and its import lands."""
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        old = self.drv.run_until(A, "snapshot")["acq"]
        t2 = job.claim(self.conn, A)
        u = job.next_unit(self.conn, t2)
        self.assertEqual(u["unit"], "probes")
        self.assertNotEqual(u["acq"], old)
        self.drv.do(u, t2)
        self.assertEqual(self.drv.next_until(t2, "snapshot")["acq"], u["acq"])

    def test_job_status_is_not_done_while_a_request_waits(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")      # queued, no pass yet
        self.assertEqual(job.status(self.conn, A), {"done": False, "text": None})
