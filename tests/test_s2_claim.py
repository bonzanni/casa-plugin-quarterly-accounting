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

    def test_a_claim_ends_a_live_delegation_pass_first(self):
        import job
        self.pass_("cron")                      # a delegation pass (old protocol)
        job.claim(self.conn, A)
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)

    def test_adoption_by_another_job_is_counted_once_per_job(self):
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        job.claim(self.conn, A)                 # same job, next batch: not an adoption
        job.claim(self.conn, B)                 # adoption 1
        job.claim(self.conn, B)                 # same adopter again: not counted
        row = self.conn.execute("SELECT adoptions, holder_job FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual((row["adoptions"], row["holder_job"]), (1, B))

    def test_a_return_of_an_earlier_holder_spends_the_budget(self):
        """Design r1 (Astra S2): the budget counts holder changes, not distinct adopters —
        A→B→A is two, and the third change ends the pass `stopped`."""
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        job.claim(self.conn, B)                 # A→B: 1
        job.claim(self.conn, A)                 # B→A: 2
        row = self.conn.execute("SELECT adoptions, holder_job, ended_at FROM passes WHERE"
                                " pass_id=?", (pid,)).fetchone()
        self.assertEqual(tuple(row), (2, A, None))
        job.claim(self.conn, B)                 # A→B again: the third, refused
        row = self.conn.execute("SELECT adoptions, holder_job, outcome FROM passes WHERE"
                                " pass_id=?", (pid,)).fetchone()
        self.assertEqual(tuple(row), (2, A, "stopped"))

    def test_third_adoption_stops_the_pass(self):
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        job.claim(self.conn, B)
        job.claim(self.conn, C)
        job.claim(self.conn, D)                 # would be the third adoption
        row = self.conn.execute("SELECT ended_at, outcome FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["outcome"], "stopped")
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)

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

    def test_the_stopping_claim_does_not_take_the_pass(self):
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        job.claim(self.conn, B)
        job.claim(self.conn, C)
        job.claim(self.conn, D)                 # the third adoption: the pass ends instead
        row = self.conn.execute("SELECT holder_job, adoptions FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual((row["holder_job"], row["adoptions"]), (C, 2))
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)
