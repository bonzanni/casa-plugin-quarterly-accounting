"""S2 Task 6 (spec §6.1–6.2, INV-J9, §6.3): work requests are recorded before start_job,
taken only by the live job pass, and given a disposition in the transaction that decides it
(simple loop Task 10: the run's one pass takes every request at its claim, and its end
settles them done and reported — the run's one message is their result)."""
from tests._base import StoreCase

A = "aaaaaaaa-1"


class Requests(StoreCase):
    def test_request_records_then_offers_start_job(self):
        import asks
        out = asks.request_work(self.conn, "check", "operator")
        self.assertEqual(out["start_job"]["job"], "quarterly-accounting:work")
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "queued")

    def test_a_package_ask_opens_a_queued_request_without_a_pass(self):
        import asks
        out = asks.request_package(self.conn, "2026-Q3")
        self.assertEqual(out["status"], "asked")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "queued")
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone(), None)
        self.assertEqual(asks.request_package(self.conn, "2026-Q3")["status"],
                         "already")

    def test_taken_only_by_the_live_pass_and_settled_at_its_end(self):
        import asks, db, job, loop
        asks.request_work(self.conn, "check", "cron")
        t = job.claim(self.conn, A)
        pid = self.conn.execute("SELECT pass_id FROM runs WHERE job_id=?", (A,)).fetchone()[0]
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT state, pass_id FROM work_requests")], [("taken", pid)])
        with db.tx(self.conn):
            loop.end_pass(self.conn, t, "stopped", {})
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("reported", "stopped"))

    def test_every_check_is_reported_at_its_pass_end(self):
        """FW-I3 (M7), simple loop Task 10: every request the pass took — cron or operator,
        whatever the outcome — is reported in the transaction that ends the pass."""
        import asks, db, job, loop
        for n, outcome in enumerate(("complete", "interrupted", "stopped")):
            with self.subTest(outcome=outcome):
                asks.request_work(self.conn, "check", "cron")
                asks.request_work(self.conn, "check", "operator")
                t = job.claim(self.conn, f"aaaaaaa{n}-1")
                pid = self.conn.execute("SELECT pass_id FROM pass_marker").fetchone()[0]
                with db.tx(self.conn):
                    loop.end_pass(self.conn, t, outcome)
                got = dict(self.conn.execute("SELECT trigger, state FROM work_requests WHERE"
                                             " pass_id=?", (pid,)).fetchall())
                self.assertEqual(got, {"cron": "reported", "operator": "reported"})

    def test_reset_fences_every_claim_and_drops_requests(self):
        import asks, binding, db, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)                      # the run's pass takes it
        binding.reset_store(self.conn)
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                job.check_claim(self.conn, t)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT value FROM meta WHERE key='drain'").fetchone())
