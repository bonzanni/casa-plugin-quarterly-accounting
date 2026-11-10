"""A job's claim rotates the token, and only the newest claim's token may act (another
job's claim ends a live pass; none is adopted). The same job id claims again after a context
summary or a restart, as often as it needs."""

from tests._base import StoreCase

A, B, C, D = "aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3", "dddddddd-4"


class Claim(StoreCase):
    def test_each_claim_rotates_and_kills_the_previous_token(self):
        import db, job
        t1 = job.claim(self.conn, A)
        t2 = job.claim(self.conn, A)
        self.assertGreater(t2, t1)
        with db.tx(self.conn):
            job.check_claim(self.conn, t2)
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                job.check_claim(self.conn, t1)

    def test_job_id_is_validated(self):
        import db, job
        with self.assertRaises(db.Refusal):
            job.claim(self.conn, "x")

    def test_a_claim_writes_its_own_gen_into_the_kept_batch_column(self):
        """claims.batch is NOT NULL in every existing store and no longer read: each claim
        writes its own gen there."""
        import job
        t1 = job.claim(self.conn, A)
        t2 = job.claim(self.conn, A)
        batch = lambda t: self.conn.execute("SELECT batch FROM claims WHERE gen=?",
                                            (t,)).fetchone()[0]
        self.assertEqual((batch(t1), batch(t2)), (t1, t2))

    def test_the_same_job_claims_again_and_again(self):
        """A job whose session is summarised or restarted several times: every claim of
        the same job id keeps the one run, its one pass and its one implicit check."""
        import job
        t = job.claim(self.conn, A, started_by="Started by: operator")
        pass_id = self.conn.execute("SELECT pass_id FROM runs WHERE job_id=?",
                                    (A,)).fetchone()[0]
        for _ in range(5):
            t = job.claim(self.conn, A, started_by="Started by: operator")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT pass_id FROM runs WHERE job_id=?",
                                           (A,)).fetchone()[0], pass_id)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0],
                         1)                     # the implicit check, recorded once

    def test_a_claim_after_the_run_completed_answers_complete_again(self):
        """A session summarised or restarted after `complete`, before emit_completion:
        its new claim of the same job id is answered `complete` with the same text."""
        import job
        from tests.sim_job import JobDriver
        self.bind()
        drv = JobDriver(self, payments=1)
        drv.gmail.invoice("Zapier", 1000, "EUR", drv.DATES[0], "ZAP-1")
        done = drv.run_job(A)[-1]
        self.assertEqual(done["unit"], "complete")
        again = job.next_unit(self.conn, job.claim(self.conn, A,
                                                   started_by="Started by: operator"))
        self.assertEqual((again["unit"], again["text"]), ("complete", done["text"]))

    def test_an_adoption_moves_the_fence_to_the_adopter(self):
        import db, job, passes
        tA = job.claim(self.conn, A)                 # A's run and its pass
        tB = job.claim(self.conn, B)
        with db.tx(self.conn):
            job.check_claim(self.conn, tB)
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                passes.check_token(self.conn, tA)
        with self.assertRaises(db.Refusal):
            passes.record_probe(self.conn, tA, "bank_tools", True)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM probes WHERE kind='bank_tools'")
                         .fetchone()[0], 0)

    def test_check_claim_runs_the_pass_fence_while_a_job_pass_is_live(self):
        import db, job
        job.claim(self.conn, A)                      # the run's pass starts with its claim
        t = job.claim(self.conn, A)
        self.assertIsNone(self.conn.execute("SELECT lease_at FROM pass_marker").fetchone()[0])
        with db.tx(self.conn):
            job.check_claim(self.conn, t)       # the pass fence renews the holder's lease
        self.assertIsNotNone(self.conn.execute("SELECT lease_at FROM pass_marker").fetchone()[0])
        with db.tx(self.conn):                  # the marker no longer under the newest claim
            self.conn.execute("UPDATE pass_marker SET generation=generation+1 WHERE id=1")
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                job.check_claim(self.conn, t)
