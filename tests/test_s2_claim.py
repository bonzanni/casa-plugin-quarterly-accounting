"""S2 Task 4 (spec §4, §6.3): a job turn's claim rotates the token, only the newest
claim's token may act, and a live job pass is adopted at most ADOPTIONS_MAX times."""

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

    def test_a_claim_records_its_batch(self):
        """claims.batch (design r6, round 5): a job id's first claim starts a batch named
        by its own gen; a re-claim inherits it until the batch is answered end-batch."""
        import db, job
        t1 = job.claim(self.conn, A)
        t2 = job.claim(self.conn, A)
        batch = lambda t: self.conn.execute("SELECT batch FROM claims WHERE gen=?",
                                            (t,)).fetchone()[0]
        self.assertEqual((batch(t1), batch(t2)), (t1, t1))
        with db.tx(self.conn):
            self.conn.execute("UPDATE claims SET closed=1 WHERE gen=?", (t2,))
        t3 = job.claim(self.conn, A)
        self.assertEqual(batch(t3), t3)
        self.assertEqual(batch(job.claim(self.conn, B)), t3 + 1)    # another job id's first

    def test_an_adoption_moves_the_fence_to_the_adopter(self):
        import db, job, passes
        tA = job.claim(self.conn, A)
        self.start_job_pass(tA)
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
        t = job.claim(self.conn, A)
        self.start_job_pass(t)
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
