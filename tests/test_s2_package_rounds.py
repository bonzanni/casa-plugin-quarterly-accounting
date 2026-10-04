# tests/test_s2_package_rounds.py
from tests._base import StoreCase
from tests.sim_job import JobDriver

A = "aaaaaaaa-1"


class PackageRounds(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self)

    def test_a_package_ask_runs_its_rounds_and_waits_buildable(self):
        import asks
        asks.request_package(self.conn, "2026-Q3")
        units = self.drv.run_job(A)
        self.assertEqual(units[-1]["unit"], "complete")
        r = self.conn.execute("SELECT state, token, lease_at FROM package_requests").fetchone()
        self.assertEqual(r["state"], "snapshot-done")
        self.assertIsNone(r["lease_at"])               # job_report may claim it at once

    def test_a_check_and_a_package_are_drained_by_one_job(self):
        import asks
        asks.request_work(self.conn, "check", "operator")
        asks.request_package(self.conn, "2026-Q3")
        self.drv.run_job(A)
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "done")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "snapshot-done")

    def test_the_package_sweep_is_quarter_scoped(self):
        import asks
        asks.request_package(self.conn, "2026-Q3")
        units = self.drv.run_job(A)
        self.assertTrue(all(u.get("quarter") == "2026-Q3" for u in units
                            if u["unit"] in ("sweep", "judge")))

    def test_a_resumed_package_snapshot_still_confirms_an_out_of_quarter_erasure(self):
        """Astra plan-r6 S1: a package round cut between its import and its erase-candidate
        observations resumes with the sweep, and the quarter's sweep still lists a row
        absent from the export, whatever its quarter — so the erasure is confirmed."""
        import asks
        self.drv = JobDriver(self, payments=0)
        self.drv.add_payments(["2026-08-05", "2026-10-01"])      # a Q3 and a Q4 payment
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)                                     # both become lineages
        self.assertEqual(self.conn.execute("SELECT count(*) FROM projections WHERE"
                                           " ended IS NULL").fetchone()[0], 2)
        self.drv.bankfeed.purge_before("2026-09-01")            # the Q3 row is erased
        asks.request_package(self.conn, "2026-Q4")
        self.drv.cut_after_import = True                        # the snapshot's turn ends
        units = self.drv.run_job(A)                             # re-claimed, to the end
        self.assertIn("sweep", [u["unit"] for u in units])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM projections WHERE"
                                           " ended='erased'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "snapshot-done")
