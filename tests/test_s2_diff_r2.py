"""S2 diff review round 2, R7 (INV-J8): work added by a bounded event — an accepted
import, a pass begun, a search chunk, a judge restart for a bounded cause — moves the
batch's baseline by exactly what it changed (job.work_added): never credit, never a
loss. Astra r2 S2: a W refresh's import made a productive batch the third no-progress
one in a row."""
import datetime

from tests._base import StoreCase
from tests.sim_job import JobDriver
from tests.test_s2_diff_r1 import A, Tools


class WRefresh(Tools):
    def test_a_w_refresh_batch_that_sweeps_is_progress(self):
        """Astra's reproduction: 80 classified payments; W expires before batch 3, whose
        refreshed acquisition and five sweep pages read 50 rows. Not three no-progress
        batches; and the whole check never has three in a row."""
        import db, job
        u = self.start(80)
        flags, units, start = [], [], db._clock()
        for _ in range(4000):
            if u["unit"] == "complete":
                break
            if u["unit"] == "end-batch":
                flags.append(u["progress"]["progressed"])
                self.assertNotEqual(flags[-3:], [False] * 3, (flags, units))
                if len(flags) == 2:
                    later = start + datetime.timedelta(seconds=job.W_S + 2)
                    db._clock = lambda: later
                    self.addCleanup(setattr, db, "_clock",
                                    lambda: datetime.datetime.now(datetime.timezone.utc))
                if len(flags) == 3:
                    self.assertEqual(units[:2], ["probes", "snapshot"])     # the refresh
                    self.assertTrue(flags[2], units)
                units = []
                u = self.call("job_next", job_id=A)
                continue
            units.append(u["unit"])
            u = self.do(u)
        else:
            self.fail("the check never completed")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 2)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "complete"))


class FirstBatch(StoreCase):
    def test_a_first_batch_that_sweeps_is_progress(self):
        """The first batch reads the bank, imports and sweeps: the import's rows due are
        no credit, but every sweep page below the post-import count is."""
        import asks, db, job
        self.bind()
        drv = JobDriver(self, payments=80)
        asks.request_work(self.conn, "check", "cron")
        t = job.claim(self.conn, A)
        u = job.next_unit(self.conn, t)
        after_import = None
        while u["unit"] != "end-batch":
            drv.do(u, t)
            if u["unit"] == "snapshot":
                with db.tx(self.conn):
                    after_import = job.measure(self.conn)
                nxt = job.next_unit(self.conn, t)
                self.assertFalse(nxt["progress"]["progressed"])  # the import alone: none
                u = nxt
                continue
            u = job.next_unit(self.conn, t)
        with db.tx(self.conn):
            now = job.measure(self.conn)
        self.assertLess(now[3], after_import[3])                 # its sweeps lowered due
        self.assertTrue(u["progress"]["progressed"])


class Hook(StoreCase):
    def test_an_unbounded_cause_never_moves_the_baseline(self):
        import job
        for cause in job.UNBOUNDED:
            self.assertFalse(job.bounded(cause))
        for cause in job.BOUNDED:
            self.assertTrue(job.bounded(cause))
        with self.assertRaises(AssertionError):
            job.bounded("a cause nobody named")
