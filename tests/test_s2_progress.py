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
        import asks, job
        asks.request_work(self.conn, "check", "cron")
        self.drv.run_until(A, "gmail-probe")
        asks.request_work(self.conn, "check", "operator")      # forces a refresh
        t = job.claim(self.conn, A)
        u = job.next_unit(self.conn, t)                           # probes
        self.drv.do(u, t)
        u = job.next_unit(self.conn, t)                           # snapshot
        self.drv.do(u, t)
        self.assertFalse(job.next_unit(self.conn, t)["progress"]["progressed"])

    def test_a_recorded_search_is_progress_and_reports_once_early(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "item")
        t = self.drv.token
        self.drv.do(self.drv.last, t)                             # record_search
        u = job.next_unit(self.conn, t)
        self.assertTrue(u["progress"]["progressed"])
        self.assertTrue(u["report"])
        self.assertFalse(job.next_unit(self.conn, t)["report"])   # once, until end-batch

    def test_the_first_chunk_is_not_progress_but_its_first_search_is(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        u = self.drv.run_until(A, "item")                 # the first item of the pass
        self.assertFalse(u["progress"]["progressed"])
        t = self.drv.token
        self.drv.do(u, t)                                 # record_search (40 -> 39 searchable)
        self.assertTrue(job.next_unit(self.conn, t)["progress"]["progressed"])

    def test_the_summary_carries_no_machinery_words(self):
        import job
        import views
        for unit in job.WORDS:
            text = job._summary(unit, [0, 3, 0, 0])
            for word in views.FORBIDDEN:
                self.assertNotIn(word, text)
