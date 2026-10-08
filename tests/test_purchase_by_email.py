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

    def test_the_purchase_is_closed_over_both_links(self):
        """r1 (Terra S1): the receipt (email with the original invoice) and a refiled invoice
        (the original's issuer and number, another email) are one purchase: the original
        bridges them. The job's floor sees the whole purchase."""
        import documents, matches
        common = dict(issuer="Eleven Labs Inc.", document_date="2026-07-01", amount_minor=2200,
                      currency="USD")
        orig = self.doc(document_number="CUWVSRB8-0004", source_ref="m1:1", **common)
        rec = self.doc(kind="receipt", document_number="2062-6406-1116", source_ref="m1:2",
                       **common)
        refiled = self.doc(document_number="CUWVSRB8-0004", source_ref="m2:1", **common)
        for d in (orig, rec, refiled):
            self.assertEqual(sorted(documents.purchase(self.conn, d)),
                             sorted([orig, rec, refiled]), d)
        p1 = self.pay("Elevenlabs.io", 1958, "2026-07-01")
        p2 = self.pay("Elevenlabs.io", 1958, "2026-07-02")
        self.machine_entry(p1, rec)
        with self.assertRaises(db.Refusal):
            matches.propose_match(self.conn, pid=p2, doc_id=refiled,
                                  expected_revision=self.rev(p2), token=self.token,
                                  document_date="2026-07-01")


class R1Bridges(LoopCase):
    """r1 (Astra S1 ×3): every way a purchase grows is seen by the job's floor."""
    C = dict(issuer="Eleven Labs Inc.", document_date="2026-07-01", amount_minor=2200,
             currency="USD")

    def propose(self, pid, doc_id):
        import matches
        return matches.propose_match(self.conn, pid=pid, doc_id=doc_id,
                                     expected_revision=self.rev(pid), token=self.token,
                                     document_date="2026-07-01")

    def two(self):
        return (self.pay("Elevenlabs.io", 1958, "2026-07-01"),
                self.pay("Elevenlabs.io", 1958, "2026-07-02"))

    def test_a_chat_invoice_bridges_through_an_emailed_copy_to_its_receipt(self):
        p1, p2 = self.two()
        a = self.doc(document_number="CUWVSRB8-0004", source="manual-telegram",
                     source_ref=None, **self.C)
        self.doc(document_number="cuwvsrb8-0004 ", source_ref="m2:1", **self.C)
        c = self.doc(kind="receipt", document_number="2062-6406-1116", source_ref="m2:2",
                     **self.C)
        self.machine_entry(p1, a)
        with self.assertRaises(db.Refusal):
            self.propose(p2, c)

    def test_the_same_bytes_filed_again_from_an_email_join_its_purchase(self):
        import documents
        p1, p2 = self.two()
        a = self.doc(document_number="CUWVSRB8-0004", source="manual-telegram",
                     source_ref=None, **self.C)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO operator_refs(ref, source, doc_id, filed_at)"
                              " VALUES ('m3:1', 'gmail', ?, 'x')", (a,))
        r = self.doc(kind="receipt", document_number="2062-6406-1116", source_ref="m3:2",
                     **self.C)
        self.assertEqual(sorted(documents.purchase(self.conn, a)), sorted([a, r]))
        self.machine_entry(p1, a)
        with self.assertRaises(db.Refusal):
            self.propose(p2, r)

    def test_an_amount_read_later_cannot_join_a_purchase_another_payment_backs(self):
        import documents
        p1, p2 = self.two()
        inv = self.doc(document_number="CUWVSRB8-0004", source_ref="m4:1", **self.C)
        rec = self.doc(kind="receipt", document_number="2062-6406-1116", source_ref="m4:2",
                       **{**self.C, "amount_minor": None, "currency": None})
        self.machine_entry(p1, inv)
        self.propose(p2, rec)                       # not linked yet: its amount is unread
        with self.assertRaises(db.Refusal):
            documents.update_document_metadata(self.conn, rec, token=self.token,
                                               amount_minor=2200, currency="USD")
        self.assertIsNone(self.conn.execute("SELECT amount_minor FROM documents WHERE"
                                            " doc_id=?", (rec,)).fetchone()[0])
