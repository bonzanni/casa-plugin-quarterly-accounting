# tests/test_s7_claim.py
"""S7 §4.1, §10, §9: a cron launch with nothing queued checks; check and handover asks
carry over; a stalled staged send is recovered by any claim; no drain, no cancel record,
no orphan, no job_report."""
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
        did = self.staged_package(lapsed=True)        # staged, lease older than LEASE_S
        job.claim(self.conn, A)
        d = self.conn.execute("SELECT status FROM deliveries WHERE delivery_id=?",
                              (did,)).fetchone()
        self.assertEqual(d[0], "uncertain")
