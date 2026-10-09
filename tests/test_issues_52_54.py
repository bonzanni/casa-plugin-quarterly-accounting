"""Issue #52: a proposal line counts purchases (#48's unit: issuer + number), so an invoice
and its own receipt read as one document. Issue #54: no fixed-width hard break inside a
line; one logical item per line, and the client wraps."""
import json
from tests._base import LoopCase, StoreCase
import db                     # server/ is on sys.path once tests._base is imported


class ProposalLineCountsPurchases(LoopCase):
    def end_text(self):
        import cards
        with db.tx(self.conn):
            rid = cards.compose_end(self.conn, self.job_id, scheduled=False)
        return self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (rid,)).fetchone()[0]

    def proposal_line(self):
        return next(ln for ln in self.end_text().split("\n") if ln.startswith("1. "))

    def test_an_invoice_and_its_own_receipt_read_as_one_document(self):
        """Prod 2026-10-08: "2 invoices fit; chose CUWVSRB8-0007" for one purchase."""
        p = self.pay("Elevenlabs.io", 1958, "2026-09-01")
        receipt = self.doc(kind="receipt", issuer="Adobe", document_number="CUWVSRB8-0007",
                           document_date="2026-09-01", amount_minor=2200, currency="USD")
        self.propose(p, alternatives=[receipt], document_number="CUWVSRB8-0007",
                     document_date="2026-09-01", amount_minor=2200, currency="USD")
        line = self.proposal_line()
        self.assertNotIn("fit", line)
        self.assertIn("↔ invoice · USD 22.00", line)

    def test_the_twin_compares_by_the_purchase_normalisation(self):
        p = self.pay("Adobe", 10000, "2026-09-01")
        twin = self.doc(kind="receipt", issuer=" adobe ", document_number="inv-7 ")
        self.propose(p, alternatives=[twin], document_number="INV-7")
        self.assertIn("↔ invoice · EUR 100.00", self.proposal_line())

    def test_two_purchases_still_say_how_many_fit(self):
        p = self.pay("AWS", 4120, "2026-08-02")
        alt = self.doc(issuer="AWS", document_number="INV-91", document_date="2026-08-04",
                       amount_minor=4120)
        alt_receipt = self.doc(kind="receipt", issuer="AWS", document_number="INV-91",
                               document_date="2026-08-04", amount_minor=4120)
        self.propose(p, alternatives=[alt, alt_receipt], issuer="AWS",
                     document_number="INV-88", document_date="2026-08-02", amount_minor=4120)
        self.assertIn("2 documents fit; chose INV\\-88 (2 Aug)", self.proposal_line())

    def test_documents_with_no_number_stay_purchases_of_their_own(self):
        p = self.pay("Bakker", 4550, "2026-09-01")
        alt = self.doc(kind="receipt", issuer="Bakker", document_number=None,
                       amount_minor=4550)
        self.propose(p, alternatives=[alt], issuer="Bakker", document_number=None,
                     amount_minor=4550)
        self.assertIn("2 documents fit; chose invoice", self.proposal_line())

    def test_a_joint_set_of_one_purchase_names_it(self):
        """A legacy joint set (no chosen one, D3) holding one purchase's two documents."""
        p = self.pay("Adobe", 10000, "2026-09-01")
        self.machine_entry(p, self.doc(document_number="N-1"))
        self.machine_entry(p, self.doc(kind="receipt", document_number="N-1"))
        line = self.proposal_line()
        self.assertNotIn("fit", line)
        self.assertIn("↔ invoice · EUR 100.00", line)


class NoHardWrap(StoreCase):
    """#54: views and alerts never insert their own line breaks inside a line."""

    def test_a_view_keeps_every_line_whole(self):
        import views
        self.seed_payments([{"counterparty": "A" * 50, "amount_minor": 1000 + i}
                            for i in range(3)])
        r = views.build_review(self.conn, "status", quarter="2026-Q3")
        lines = r["text"].split("\n")
        self.assertIn('Download the PDFs and email them to yourself, then say "check '
                      'emailed invoices" to file them now.', lines)
        heads = [ln for ln in lines if ln.startswith("A" * 50)]
        self.assertEqual(len(heads), 3, lines)
        self.assertTrue(all("EUR" in ln and "14 Sep" in ln for ln in heads), heads)

    def test_wrap_is_gone(self):
        import views
        self.assertFalse(hasattr(views, "_wrap"))
        self.assertFalse(hasattr(views, "WIDTH"))
