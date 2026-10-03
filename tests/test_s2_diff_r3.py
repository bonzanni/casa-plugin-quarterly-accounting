"""S2 diff review round 3, R8 (INV-J8): Astra r3 S2 — a package ask enqueued during each
of three batches made [F, F, F] while rows settled 0 → 50 → 120 → 190. Under the credit
rule (spec §15) an ask is no work of the job's and moves nothing: the batches' sweeps
earn. A write outside the job earns nothing (test_s2_credits.Credits)."""
from tests._base import StoreCase
from tests.test_s2_diff_r1 import A, Tools


class MidBatchAsks(Tools):
    def run_with_asks(self, ask):
        """Astra's reproduction: a check of 400 payments; an ask recorded at the start
        of each of three batches. Returns the end-batch flags and the settled counts."""
        u = self.start(400)
        flags, settled = [], []
        for batch in range(3):
            ask(batch)
            while u["unit"] != "end-batch":
                u = self.do(u)
            flags.append(u["progress"]["progressed"])
            settled.append(self.conn.execute("SELECT count(*) FROM projections WHERE"
                                             " observed_revision IS NOT NULL").fetchone()[0])
            if batch < 2:
                u = self.call("job_next", job_id=A)
        return flags, settled

    def test_package_asks_mid_batch_keep_progress(self):
        flags, settled = self.run_with_asks(
            lambda b: self.call("request_package", quarter=f"2026-Q{b + 1}",
                                channel="telegram"))
        self.assertEqual(settled, sorted(set(settled)))       # rows settled every batch
        self.assertGreater(settled[0], 0)
        self.assertEqual(flags, [True, True, True], settled)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                           " state='queued'").fetchone()[0], 3)

    def test_check_asks_mid_batch_keep_progress(self):
        """A check ask taken by the live pass forces a refresh each time (F condition 2),
        so its rows are read again: still never three batches without progress."""
        flags, settled = self.run_with_asks(
            lambda b: self.call("request_work", kind="check", trigger="operator"))
        self.assertNotEqual(flags, [False, False, False], settled)
        self.assertTrue(flags[0], (flags, settled))
