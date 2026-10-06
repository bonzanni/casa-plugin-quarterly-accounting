# tests/test_s2_progress.py
from tests._base import StoreCase
from tests.sim_job import JobDriver

A = "aaaaaaaa-1"


class Progress(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=40)       # enough work for several batches

    def test_a_batch_ends_inside_its_turn_budget(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)
        spent = 0
        while True:
            u = job.next_unit(self.conn, t)
            if u["unit"] in ("end-batch", "complete"):
                break
            spent += job.UNIT_COST[u["unit"]]
            self.drv.do(u, t)
        self.assertLessEqual(spent, job.TURNS_PER_BATCH - job.BATCH_RESERVE)
        self.assertTrue(u["report"])

    def test_a_refresh_only_batch_is_not_progress(self):
        """An import alone earns nothing (spec §15): a fresh batch that only reads the bank
        again reports no progress after its import."""
        import asks, job
        asks.request_work(self.conn, "check", "cron")
        self.drv.run_until(A, "gmail-probe")
        self.drv.next_until(self.drv.token, "end-batch")         # the batch is answered
        asks.request_work(self.conn, "check", "operator")      # forces a refresh
        t = job.claim(self.conn, A)                               # a new batch
        u = job.next_unit(self.conn, t)
        self.assertEqual(u["unit"], "probes")
        self.drv.do(u, t)
        u = job.next_unit(self.conn, t)                           # snapshot
        self.drv.do(u, t)
        self.assertFalse(job.next_unit(self.conn, t)["progress"]["progressed"])

    def test_the_summary_carries_no_machinery_words(self):
        import job
        import views
        for unit in job.WORDS:
            text = job._summary(unit, 3)
            for word in views.FORBIDDEN:
                self.assertNotIn(word, text)
