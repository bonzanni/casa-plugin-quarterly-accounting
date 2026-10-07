"""0.11.2 (BRAIN ruling, prod 2026-10-08): a purchase is the same issuer and number, OR the
same email (message id) with the same read amount and currency. Prod read each receipt with
its OWN number (July: invoice CUWVSRB8-0004 + receipt 2062-6406-1116, both from one email),
so issuer + number alone left the receipts loose: August's proposal (prod pid 28) offered
July's and October's receipts as alternatives. documents.purchase() is the one definition."""
import json
from tests._base import LoopCase
import db

MONTHS = {"07": ("CUWVSRB8-0004", "2062-6406-1116"), "08": ("CUWVSRB8-0005", "2885-1930-5094"),
          "10": ("CUWVSRB8-0007", "2635-8754-8667")}


class PurchaseByEmail(LoopCase):
    def pair(self, mm):
        """One email: the month's invoice and its receipt, read with its own number."""
        inv_no, rec_no = MONTHS[mm]
        day = f"2026-{mm}-01"
        common = dict(issuer="Eleven Labs Inc.", document_date=day, amount_minor=2200,
                      currency="USD")
        inv = self.doc(document_number=inv_no, source_ref=f"msg{mm}:1", **common)
        rec = self.doc(kind="receipt", document_number=rec_no, source_ref=f"msg{mm}:2", **common)
        return inv, rec

    def test_an_invoice_and_its_own_receipt_from_one_email_are_one_purchase(self):
        import documents
        inv, rec = self.pair("07")
        self.assertEqual(sorted(documents.purchase(self.conn, inv)), sorted([inv, rec]))
        self.assertEqual(sorted(documents.purchase(self.conn, rec)), sorted([rec, inv]))

    def test_the_same_email_with_another_amount_or_an_unread_one_is_not(self):
        import documents
        a = self.doc(document_number="A-1", source_ref="digest:1", amount_minor=2200,
                     currency="USD")
        b = self.doc(document_number="B-1", source_ref="digest:2", amount_minor=900,
                     currency="USD")
        c = self.doc(document_number="C-1", source_ref="digest:3", amount_minor=None,
                     currency=None)
        self.assertEqual(documents.purchase(self.conn, a), [a])
        self.assertEqual(documents.purchase(self.conn, c), [c])
        self.assertEqual(documents.purchase(self.conn, b), [b])

    def test_augusts_card_offers_neither_julys_nor_octobers_receipt(self):
        """Prod pid 28: August's alternatives listed July's and October's receipts."""
        import cards
        july = self.pay("Elevenlabs.io", 1958, "2026-07-01")
        aug = self.pay("Elevenlabs.io", 1958, "2026-08-01")
        october = self.pay("Elevenlabs.io", 1958, "2026-10-01")
        j_inv, j_rec = self.pair("07")
        a_inv, a_rec = self.pair("08")
        o_inv, o_rec = self.pair("10")
        import matches
        # the prod state: August proposed with those alternatives before the email link
        # existed (today the job's proposal with them is refused, the next test's floor)
        matches.propose_match(self.conn, pid=aug, doc_id=a_inv, expected_revision=self.rev(aug),
                              token=self.token, document_date="2026-08-01",
                              alternatives=[a_rec, j_rec, o_rec])
        self.machine_entry(july, j_inv)
        self.machine_entry(october, o_inv)
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET quarter='2026-Q3' WHERE job_id=?",
                              (self.job_id,))
            end = cards.compose_end(self.conn, self.job_id, scheduled=False)
            rid = cards.card(self.conn, end, 0)
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (rid,)).fetchone()[0]
        for other in ("2062\\-6406\\-1116", "2635\\-8754\\-8667"):
            self.assertNotIn(other, text)
        self.assertNotIn("2885\\-1930\\-5094", text)       # its own receipt: the same line
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (rid,)).fetchone()[0])
        self.assertEqual(scope["alternatives"], [])
        end_text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                     (end,)).fetchone()[0]
        self.assertIn("↔ invoice CUWVSRB8\\-0005 · USD 22.00", end_text)
        self.assertNotIn("fit", end_text.split("To confirm:")[1].split("\n")[1])

    def test_the_job_cannot_pair_julys_receipt_with_august(self):
        """#48's floor through the email link: July's receipt is July's purchase."""
        import matches
        july = self.pay("Elevenlabs.io", 1958, "2026-07-01")
        aug = self.pay("Elevenlabs.io", 1958, "2026-08-01")
        j_inv, j_rec = self.pair("07")
        self.machine_entry(july, j_inv)
        with self.assertRaises(db.Refusal):
            matches.propose_match(self.conn, pid=aug, doc_id=j_rec,
                                  expected_revision=self.rev(aug), token=self.token,
                                  document_date="2026-07-01")
