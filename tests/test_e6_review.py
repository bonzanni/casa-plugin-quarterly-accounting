"""Diff round e6 (26b68ee..bf9a6ef, rev 18.4), Astra S2 reproduced: a vendor name longer
than a clip is handed EXACTLY by the payment unit, so the invoice filed under it stays the
payment's candidate (the FX one included)."""
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported


class LongVendorIsAnIdentity(StoreCase):
    def test_a_long_vendors_filed_fx_invoice_stays_a_candidate(self):
        import documents, lineage, loop
        self.bind()
        self.token = self.run_claim()
        name = "Long Vendor " + "N" * 226                       # 238 characters
        self.row(1, counterparty=name, booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        loop.build_work(self.conn, self.job_id)
        u = loop.payment_unit(self.conn, self.job_id)
        self.assertEqual(u["vendor"], name)                   # exact, never clipped
        path = self.publish("usd.pdf", b"%PDF-1.4 usd invoice")
        doc = documents.ingest_document(
            self.conn, source_path=path, kind="invoice", source="gmail",
            extraction_author="specialist", issuer="Long Vendor", amount_minor=11000,
            currency="USD", document_date="2026-09-01", document_number="USD-1",
            vendor=u["vendor"], token=self.token)["doc_id"]
        row = lineage.live_row(self.conn, lineage.projection(self.conn, pid))
        self.assertIn(doc, [c["doc_id"] for c in loop.candidates(self.conn, pid, row, name)])
