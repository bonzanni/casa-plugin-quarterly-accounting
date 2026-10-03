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

    def test_a_failed_sync_does_not_stop_the_pass(self):
        """PLAY T7 F1 (as v0.8.0): a failed sync imports bank-feed's cached ledger and the
        pass goes on (tests/test_s2_t7.py has the whole case)."""
        import asks
        self.drv.fail_next_sync("bank unreachable")
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "complete"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)

    def test_eight_asks_that_each_stop_at_once_all_get_dispositions(self):
        import asks, job
        self.drv.bankfeed.restore_since_install()        # every pass stops at its probes
        for q in ("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4",
                  "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"):
            asks.request_package(self.conn, q, "telegram")
        asks.request_work(self.conn, "check", "operator")
        # one Casa job run begins at most MAX_PASSES_PER_JOB passes (spec §15); the
        # standing retry starts the next job, which takes the rest
        for k, job_id in enumerate((A, "bbbbbbbb-2", "cccccccc-3")):
            units = self.drv.run_job(job_id)
            self.assertEqual(units[-1]["unit"], "complete")
            self.assertEqual(job.run_passes(self.conn, job_id), (4, 4, 1)[k])
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


class HandoverMidJudgment(StoreCase):
    """Fix round 1 (ruling on the Task 7 review): spec §5.2 — taking a handover restarts
    the pass's judge step at once; a `judged` answers only the judge unit it echoes
    (`judgment`, `after`), so a repeated or older answer is refused (INV-J4, INV-J12)."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=10)        # two triage pages of 8

    def judge_row(self):
        return self.conn.execute("SELECT started_seq, finished_at FROM pass_steps WHERE"
                                 " step='judge'").fetchone()

    def judge_after(self):
        return self.conn.execute("SELECT judge_after FROM passes ORDER BY rowid DESC"
                                 " LIMIT 1").fetchone()[0]

    def test_the_reviewers_sequence_completes_only_on_a_judgment_after_the_handover(self):
        import asks, db, job
        asks.request_work(self.conn, "check", "cron")
        u1 = self.drv.run_until(A, "judge")             # judgment S handed out, then cut
        doc = self.doc()
        rid = asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])["request_id"]
        created = self.conn.execute("SELECT created_seq FROM work_requests WHERE"
                                    " request_id=?", (rid,)).fetchone()[0]
        t2 = job.claim(self.conn, A)
        u2 = self.drv.next_until(t2, "judge")
        t2 = self.drv.token                             # the batch's budget may end it first
        self.assertGreater(u2["judgment"], created)
        self.assertNotEqual(u2["judgment"], u1["judgment"])
        self.assertEqual(u2["documents_first"], [doc])
        stale = self.drv.do(u1, t2)                     # the old unit's answer, replayed
        with self.assertRaises(db.Refusal) as cm:
            job.next_unit(self.conn, t2, judged=stale)
        self.assertIn("another judge step", str(cm.exception))
        judged = self.drv.do(u2, t2)
        job.next_unit(self.conn, t2, judged=judged)
        with self.assertRaises(db.Refusal):             # the same answer twice
            job.next_unit(self.conn, t2, judged=judged)
        self.drv.next_until(t2, "complete")
        r = self.conn.execute("SELECT state, outcome, verdicts_json FROM work_requests WHERE"
                              " request_id=?", (rid,)).fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "complete"))
        import json
        v = json.loads(r["verdicts_json"])[str(doc)]
        self.assertGreater(v["judge"], created)

    def test_taking_a_handover_mid_judgment_restarts_it_at_once(self):
        import asks, job
        asks.request_work(self.conn, "check", "cron")
        u1 = self.drv.run_until(A, "judge")
        u = job.next_unit(self.conn, self.drv.token, judged=self.drv.do(u1, self.drv.token))
        self.assertEqual(u["unit"], "judge")
        self.assertIsNotNone(self.judge_after())         # page 2 is next
        doc = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])
        job.next_unit(self.conn, self.drv.token)        # takes it: a refresh is handed out
        row = self.judge_row()
        self.assertNotEqual(row["started_seq"], u1["judgment"])
        self.assertIsNone(row["finished_at"])
        self.assertIsNone(self.judge_after())
        nxt = self.drv.next_until(self.drv.token, "judge")
        self.assertEqual((nxt["judgment"], nxt["after"], nxt["documents_first"]),
                         (row["started_seq"], None, [doc]))

    def test_a_judged_with_a_wrong_or_missing_echo_is_refused(self):
        import asks, db, job
        asks.request_work(self.conn, "check", "cron")
        u = self.drv.run_until(A, "judge")
        good = self.drv.do(u, self.drv.token)
        for bad in ({k: v for k, v in good.items() if k != "judgment"},
                    {k: v for k, v in good.items() if k != "after"},
                    {**good, "judgment": u["judgment"] + 1},
                    {**good, "after": [1]}):
            with self.assertRaises(db.Refusal):
                job.next_unit(self.conn, self.drv.token, judged=bad)
        self.assertIsNone(self.judge_row()["finished_at"])
        self.assertIsNone(self.judge_after())
        job.next_unit(self.conn, self.drv.token, judged=good)     # the right echo is taken
        self.assertIsNotNone(self.judge_after())
