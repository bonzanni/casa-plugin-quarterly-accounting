"""S2 final review (fix wave): a handover whose judgment never covers it ends the job
through the no-progress guard (FW-I1; under the credit rule, test_s2_credits.NeverWhole),
a ledger restored between two bank reads of one pass stops it (FW-I2), and the guards
whose obvious mutant survived (FW-I3, minors)."""
import json

from tests._base import StoreCase
from tests.sim_job import JobDriver

A = "aaaaaaaa-1"


class HandoverVerdicts(StoreCase):
    """FW-I1 (b): the answer that finishes a judgment gives each handed-over document
    its verdict; (c) a handover names filed documents only."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=2)

    def test_a_handover_of_an_unfiled_document_is_refused(self):
        import asks, db
        d = self.doc()
        with self.assertRaises(db.Refusal) as cm:
            asks.request_work(self.conn, "handover", "operator", doc_ids=[d, d + 5000, d + 6000])
        text = str(cm.exception)
        self.assertIn(f"documents {d + 5000}, {d + 6000} are not", text)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0],
                         0)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])     # filed: taken

class RestoreMidPass(StoreCase):
    """FW-I2: against the real bank-feed (tests/bankfeed.py)."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = self._job_driver = JobDriver(self, payments=3)

    def test_a_restore_between_two_reads_of_one_pass_stops_it(self):
        """Simple loop Task 10: the run's pass read the bank (its probes), its turn died
        before the import, bank-feed was restored, and the next batch reads it again: the
        import refuses (the ledger is not the one the pass's gate was decided on), nothing
        is imported or remembered, and the pass stops."""
        import asks, db, job, version
        asks.request_work(self.conn, "check", "operator")
        self.drive(A, stop_before="snapshot")             # read once, not imported
        bf = self.drv.bf
        remembered = self.conn.execute("SELECT ledger_generation FROM binding").fetchone()[0]
        bf.call("tag_transaction", row_ids=[bf.rows()[0]["row_id"]], tags=["acct::open"],
                workflow=version.WORKFLOW, expected_generation=bf.generation())
        bf.call("restore_backup", backup_id=bf.registered()[version.WORKFLOW])
        t = self.drv.claim(A)                             # the next batch reads again
        u = job.next_unit(self.conn, t)
        self.assertEqual(u["unit"], "probes")
        self.drv.do(u, t)
        u = job.next_unit(self.conn, t)
        self.assertEqual(u["unit"], "snapshot")
        with self.assertRaises(db.Refusal) as cm:
            self.drv.do(u, t)
        self.assertIn("nothing was imported", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT ledger_generation FROM binding"
                                           ).fetchone()[0], remembered)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0)
        units = self.drv._loop(A)                         # the pass stops; its message, then
        self.assertEqual([x["unit"] for x in units], ["view", "complete"])
        self.assertIsNone(job.live_job_pass(self.conn))
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("reported", "stopped"))
        self.assertIn("Accounting check stopped:", units[-1]["text"])
