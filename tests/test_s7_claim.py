# tests/test_s7_claim.py
"""S7 §4.1, §10, §9: a cron launch with nothing queued checks; a run that did not answer
complete leaves its package asks to be closed at the next start, unless renewed; check
and handover asks carry over; no drain, no cancel record, no orphan, no job_report."""
from tests._base import StoreCase

A, B = "aaaaaaaa-1", "bbbbbbbb-2"


class Claim(StoreCase):
    def test_a_launch_with_nothing_queued_records_a_cron_check(self):
        import job
        job.claim(self.conn, A)          # Task 10: the run's pass takes it at the claim
        r = self.conn.execute("SELECT kind, trigger, state FROM work_requests").fetchall()
        self.assertEqual([tuple(x) for x in r], [("check", "cron", "taken")])

    def test_a_launch_with_an_ask_queued_records_nothing_more(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        job.claim(self.conn, A)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0], 1)

    def test_a_second_claim_of_the_same_job_never_adds_a_check(self):
        import job
        job.claim(self.conn, A)
        self.run_job_to_complete(A)
        job.claim(self.conn, A)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0], 1)

    def test_a_package_ask_renewed_after_the_failure_is_served(self):
        """Review Focus 5."""
        import asks, job
        asks.request_package(self.conn, "2026-Q3")
        job.claim(self.conn, A)                       # A dies
        asks.request_package(self.conn, "2026-Q3")   # the operator asks again: renewed
        job.claim(self.conn, B)
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "queued")

    def test_a_completed_runs_queued_asks_are_served_not_closed(self):
        import asks, job
        self.run_job_to_complete(A)
        asks.request_package(self.conn, "2026-Q3")
        job.claim(self.conn, B)
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "queued")

    def test_check_and_handover_asks_carry_over(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        asks.request_work(self.conn, "handover", "operator", [self.doc()])
        job.claim(self.conn, A)                       # A's pass takes both, then A dies
        job.claim(self.conn, B)                       # B interrupts it: both carry over
        b_pass = self.conn.execute("SELECT pass_id FROM runs WHERE job_id=?", (B,)).fetchone()[0]
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT state, pass_id FROM work_requests ORDER BY request_id")],
            [("taken", b_pass), ("taken", b_pass)])

    def test_no_drain_no_cancel_records_no_job_report(self):
        import job, qa_server, tools  # noqa: F401
        job.claim(self.conn, A)
        keys = {r[0] for r in self.conn.execute("SELECT key FROM meta")}
        self.assertNotIn("drain", keys)
        self.assertNotIn("job_report", qa_server.TOOLS)
        import passes
        for gone in ("is_cancelled", "check_revoked", "cancelled_key"):
            self.assertFalse(hasattr(passes, gone), gone)

    def test_completion_stamps_the_run(self):
        import job
        self.run_job_to_complete(A)
        self.assertIsNotNone(self.conn.execute(
            "SELECT completed_at FROM runs WHERE job_id=?", (A,)).fetchone()[0])

    def test_a_stalled_staged_send_is_recovered_by_any_claim(self):
        import job
        did = self.stage_stalled_package()            # staged, lease older than LEASE_S
        job.claim(self.conn, A)
        d = self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                              (did,)).fetchone()
        self.assertEqual(d[0], "uncertain")

    def test_a_superseded_claims_token_is_refused_by_the_package_fence(self):
        import asks, db, job, passes
        rid = asks.request_package(self.conn, "2026-Q3")["request_id"]
        old = job.claim(self.conn, A)
        with db.tx(self.conn):          # a request handed out under claim A's gen (Task 11)
            self.conn.execute("UPDATE package_requests SET state='snapshot-done', token=?"
                              " WHERE request_id=?", (old, rid))
        with db.tx(self.conn):
            self.assertEqual(passes.check_package_token(self.conn, rid, old)["request_id"],
                             rid)
        job.claim(self.conn, A)         # a newer turn of the same job claims
        with db.tx(self.conn):
            with self.assertRaises(db.Refusal) as cm:
                passes.check_package_token(self.conn, rid, old)
        self.assertIn("no longer the current one", str(cm.exception))
