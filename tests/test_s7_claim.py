# tests/test_s7_claim.py
"""S7 §4.1, §10, §9: a cron launch with nothing queued checks; a run that did not answer
complete leaves its package asks to be closed at the next start, unless renewed; check
and handover asks carry over; no drain, no cancel record, no orphan, no job_report."""
from tests._base import StoreCase

A, B = "aaaaaaaa-1", "bbbbbbbb-2"


class Claim(StoreCase):
    def test_a_launch_with_nothing_queued_records_a_cron_check(self):
        import job
        job.claim(self.conn, A)
        r = self.conn.execute("SELECT kind, trigger, state FROM work_requests").fetchall()
        self.assertEqual([tuple(x) for x in r], [("check", "cron", "queued")])

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

    def test_a_failed_runs_package_ask_is_closed_at_the_next_start(self):
        import asks, job
        asks.request_package(self.conn, "2026-Q3")
        job.claim(self.conn, A)                       # run A dies without answering complete
        job.claim(self.conn, B)
        r = self.conn.execute("SELECT state, reason FROM package_requests").fetchone()
        self.assertEqual(r["state"], "stopped")
        self.assertEqual(r["reason"], job.LEFT_BEHIND)
        self.assertIn("package-stopped", [x[0] for x in self.conn.execute(
            "SELECT kind FROM alerts WHERE sent_at IS NULL")])

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
        job.claim(self.conn, A)
        job.claim(self.conn, B)
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "queued")

    def test_adopting_a_package_pass_closes_its_unrenewed_request_and_ends_the_pass(self):
        import asks, job
        asks.request_package(self.conn, "2026-Q3")
        tok = job.claim(self.conn, A)
        job.next_unit(self.conn, tok)                 # the package round's pass begins
        self.assertIsNotNone(job.live_job_pass(self.conn))
        job.claim(self.conn, B)                       # adopts: closes and ends it
        self.assertIsNone(job.live_job_pass(self.conn))
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "stopped")

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

    def test_an_ask_left_by_a_run_that_completed_on_its_budget_is_served(self):
        """§4.2: a run that answers complete with its pass budget spent leaves its queued
        asks for the next start — completed, so they are not closed (asked before its
        last claim, they would be, had it not answered complete)."""
        import asks, db, job, views
        asks.request_package(self.conn, "2026-Q3")
        tok = job.claim(self.conn, A)
        with db.tx(self.conn):          # the run has begun its MAX_PASSES_PER_JOB passes
            self.conn.execute("INSERT INTO runs(job_id, passes) VALUES (?, ?)",
                              (A, job.MAX_PASSES_PER_JOB))
        u = job.next_unit(self.conn, tok)               # §4.2: the asks-waiting line first
        self.assertEqual(self.render_text(u["render_ids"][0]), job.LEFT_WAITING)
        views.mark_rendering_delivered(self.conn, u["render_ids"][0])
        self.assertEqual(job.next_unit(self.conn, tok)["unit"], "complete")
        job.claim(self.conn, B)
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "queued")
