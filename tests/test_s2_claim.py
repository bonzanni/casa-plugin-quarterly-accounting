"""S2 Task 4 (spec §4, §6.3): a job turn's claim rotates the token, only the newest
claim's token may act, and a live job pass is adopted at most ADOPTIONS_MAX times."""
import json

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

    def test_a_job_that_held_the_pass_before_is_not_charged_again(self):
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        for j in (B, A, B, A, B):                # A→B→A→B…: one adoption, by B
            job.claim(self.conn, j)
        row = self.conn.execute("SELECT adoptions, ended_at FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual((row["adoptions"], row["ended_at"]), (1, None))

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

    def test_a_claim_stamps_the_measure(self):
        import job
        t = job.claim(self.conn, A)
        stamped = self.conn.execute("SELECT measure_json FROM claims WHERE gen=?",
                                    (t,)).fetchone()[0]
        self.assertEqual(json.loads(stamped), job.measure(self.conn))
