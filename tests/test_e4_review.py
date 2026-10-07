"""Diff round e4 (26b68ee..49b65ef, rev 18.4), each accepted finding reproduced through the
real surface:
- Astra S1 (progress/budget #3 under rev 18 — generalized by SIMPLIFYING, BRAIN's
  pre-agreement): a claim that handed out work or persisted anything reports progressed,
  and its end-of-batch report never overwrites that with false — Casa reads the LAST one;
- Astra S2: a later invoice of a checked quarter, filed after quarter-end, is listed in
  THAT quarter's package as unmatched (by its own date, not its filing quarter)."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported


class LastReportHolds(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_astras_long_reads_keep_every_batch_progressed_and_finish(self):
        """Astra's e4 sequence: twelve vendor invoices with 4,000-character refs (two per
        hand-out) and long PDF reads on every other one — each batch files a few, then its
        end-batch report must stay true (Casa keeps the LAST): the run finishes."""
        drv = JobDriver(self, payments=1)
        for i in range(12):
            drv.gmail.invoice("Zapier", 1000, "EUR", drv.DATES[0], f"I{i}")
        for i, m in enumerate(drv.gmail.messages):
            m["ref"] = f"{i:04d}:" + "x" * 3995
        slow = {m["ref"] for m in drv.gmail.messages[::2]}
        real = drv._file_vendor

        def long_reads(token, vendor, refs):
            out = []
            for ref in refs:
                if ref in slow:
                    for _ in range(44):
                        drv._spend()            # a long PDF read, page range by range
                out += real(token, vendor, [ref])
            return out
        drv._file_vendor = long_reads
        drv.casa_cut = 80                       # Casa's idle guard, last-report semantics
        drv.run_job("e4e4e4e4-a1")
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 12)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_items WHERE"
                                           " state='queued'").fetchone()[0], 0)

    def test_a_batch_that_worked_ends_with_a_true_report(self):
        """Astra's order: the first payment is worked; the next answer reports progress
        (true); the second payment is handed but nothing more is done before the batch's
        end — its end-batch report, Casa's LAST, is still true."""
        import job
        drv = JobDriver(self, payments=2)
        drv.claim("e4e4e4e4-a2")
        seen = 0
        for _ in range(20):
            u = drv.next()
            drv.calls += 1
            if u["unit"] == "payment":
                seen += 1
                if seen == 2:
                    break
            drv.do(u, drv.token)
        u = job.next_unit(self.conn, drv.token, 79)          # the batch's end, nothing more
        self.assertEqual(u["unit"], "end-batch")
        self.assertTrue(u["report"])
        self.assertTrue(u["progress"]["progressed"])          # the last report stays true


class UnmatchedByDocumentDate(StoreCase):
    def test_a_q3_invoice_filed_in_october_is_listed_in_q3(self):
        import package
        self.bind()
        self.token = self.run_claim()
        self.row(1, counterparty="Adobe", booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        a = self.doc(vendor="Adobe", document_date="2026-09-01", document_number="OLDA")
        self.machine_match(pid, a, self.token)
        b = self.doc(vendor="Adobe", document_date="2026-09-02", document_number="LATERB")
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET ingest_quarter='2026-Q4' WHERE doc_id=?",
                              (b,))
        q3 = [d["doc_id"] for d in package._freeze(self.conn, "2026-Q3")["unmatched"]]
        q4 = [d["doc_id"] for d in package._freeze(self.conn, "2026-Q4")["unmatched"]]
        self.assertEqual((q3, q4), ([b], []))
