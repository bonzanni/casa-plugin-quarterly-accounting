"""Interrupted sessions and a late invoice, each case reproduced through the real surface:
- long reads interrupted again and again still finish, and a payment handed out when the
  session lost its token (a context summary, a restart) comes again to the next claim;
- a later invoice of a checked quarter, filed after quarter-end, is listed in
  THAT quarter's package as unmatched (by its own date, not its filing quarter)."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported


class InterruptedSessions(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_long_reads_interrupted_every_80_calls_finish(self):
        """Twelve vendor invoices with 4,000-character refs (two per
        hand-out) and long PDF reads on every other one — each segment files a few before the
        session is interrupted: the run finishes."""
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
        drv.interrupt_at = 80
        drv.run_job("e4e4e4e4-a1")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 12)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_items WHERE"
                                           " state='queued'").fetchone()[0], 0)

    def test_a_payment_handed_before_an_interruption_comes_again(self):
        """The first payment is worked; the second is handed, then the session loses its
        token. The same job id claims again: the old token is refused, and the new claim's
        first unit is that second payment."""
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
        handed, old = u, drv.token
        tok = job.claim(self.conn, "e4e4e4e4-a2", started_by="Started by: operator")
        with self.assertRaisesRegex(db.Refusal, "no longer the current one"):
            job.next_unit(self.conn, old)
        u = job.next_unit(self.conn, tok)
        self.assertEqual((u["unit"], u["pid"]), ("payment", handed["pid"]))
        self.assertEqual(u["pass_token"], tok)


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
