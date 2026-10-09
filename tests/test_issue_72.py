"""0.11.8: issue #72 — the casa-test live check of 0.11.7 (PLAY session 65): a 15 Sep invoice
for a monthly EUR 57.84 charge was proposed for the 15 Apr payment (oldest first) and then
blocked the 15 Sep one; a pending payment of the invoice's amount was called "no payment";
a card with nothing to tap was refused by Casa; the Keep/Use new card showed two identical
lines; a proposal line named no date."""
import json

from tests._base import StoreCase, LoopCase, untag
import db                     # server/ is on sys.path once tests._base is imported
from tests.test_issue_67 import NOW, kinds


class Monthly(StoreCase):
    """Six monthly EUR 57.84 charges, Apr–Sep, all missing after a full check."""

    def setUp(self):
        super().setUp()
        from tests.sim_job import JobDriver
        self.bind(watermark="2026-04-01")
        self.drv = JobDriver(self, payments=0)
        self.nos = [self.drv.pay_once("LINKEDIN", 5784, f"2026-{m:02d}-15")
                    for m in range(4, 10)]
        with self.patch_clock(NOW):
            self.drv.run_job("aaaaaaaa-1")

    def give(self, *docs):
        import asks
        return asks.request_work(self.conn, "handover", "operator", doc_ids=list(docs))

    def test_a_september_invoice_is_never_offered_to_an_april_payment(self):
        doc = self.drv.file_unread("5004871233", "LinkedIn", 5784, document_date="2026-09-15")
        self.give(doc)
        with self.patch_clock(NOW):
            units = self.drv.run_job("bbbbbbbb-2")
        offered = sorted(self.drv.pid_of(n) for n in self.nos[3:])     # Jul, Aug, Sep only
        got = sorted(u["pid"] for u in units if u["unit"] == "payment")
        self.assertEqual(got, offered)
        # the model is shown every payment the invoice could fit, with its date
        u = [u for u in units if u["unit"] == "payment"][0]
        self.assertEqual(sorted(f["pid"] for f in u["handed_fits"][str(doc)]), offered)
        sep = self.drv.pid_of(self.nos[-1])
        # operator ruling 2026-10-09: the model is sure — matched, and the card says so
        self.assertEqual(self.conn.execute("SELECT pid FROM match_state WHERE doc_id=? AND"
                                           " state='matched'", (doc,)).fetchall()[0][0], sep)
        text = untag(self.drv.posted_end("bbbbbbbb-2")["text"])
        self.assertIn(": matched automatically to the 15 Sep EUR 57.84 payment (LINKEDIN).", text)
        # nearest the invoice's own date first (cheap steering)
        u = [u for u in units if u["unit"] == "payment"][0]
        self.assertEqual(u["handed_fits"][str(doc)][0]["pid"], sep)
        self.assertIn("Get package: the Q3 zip", text)

    def test_decide_refuses_a_dated_document_outside_the_payments_window(self):
        import decide
        doc = self.drv.file_unread("5004871233", "LinkedIn", 5784, document_date="2026-09-15")
        import documents
        documents.update_document_metadata(self.conn, doc, **self.drv.printed[doc])
        apr = self.drv.pid_of(self.nos[0])
        token = self.drv.claim("cccccccc-3")
        rev = self.conn.execute("SELECT revision FROM projections WHERE pid=?", (apr,)).fetchone()[0]
        with db.tx(self.conn):
            import matches
            with self.assertRaises(db.Refusal) as cm:
                matches.machine_in_tx(self.conn, "propose", apr, doc, expected_revision=rev,
                                      document_date="2026-09-15")
        self.assertIn("window", str(cm.exception))
        self.assertIsNotNone(token)
        self.assertIsNotNone(decide)


class CorrectedDate(StoreCase):
    def test_a_date_read_at_the_decision_is_the_one_judged(self):
        """0118-r1 (Terra S1): filed as 15 Apr, read as 15 Sep at the decision — refused for
        the April payment."""
        from tests.sim_job import JobDriver
        import documents, matches
        self.bind(watermark="2026-04-01")
        drv = JobDriver(self, payments=0)
        no = drv.pay_once("LINKEDIN", 5784, "2026-04-15")
        with self.patch_clock(NOW):
            drv.run_job("aaaaaaaa-1")
        doc = drv.file_unread("5004871233", "LinkedIn", 5784, document_date="2026-04-15")
        documents.update_document_metadata(self.conn, doc, **drv.printed[doc])
        apr = drv.pid_of(no)
        drv.claim("cccccccc-3")
        rev = self.conn.execute("SELECT revision FROM projections WHERE pid=?", (apr,)).fetchone()[0]
        with db.tx(self.conn):
            with self.assertRaises(db.Refusal) as cm:
                matches.machine_in_tx(self.conn, "propose", apr, doc, expected_revision=rev,
                                      document_date="2026-09-15")
        self.assertIn("window", str(cm.exception))


class Alternatives(Monthly):
    def test_an_out_of_window_alternative_is_refused_too(self):
        """0118-r2 (Astra S1): the April invoice with the September one as its alternative."""
        import documents, matches
        apr_doc = self.drv.file_unread("APR-1", "LinkedIn", 5784, document_date="2026-04-15")
        sep_doc = self.drv.file_unread("SEP-1", "LinkedIn", 5784, document_date="2026-09-15")
        for d in (apr_doc, sep_doc):
            documents.update_document_metadata(self.conn, d, **self.drv.printed[d])
        apr = self.drv.pid_of(self.nos[0])
        self.drv.claim("cccccccc-3")
        rev = self.conn.execute("SELECT revision FROM projections WHERE pid=?", (apr,)).fetchone()[0]
        with db.tx(self.conn):
            with self.assertRaises(db.Refusal) as cm:
                matches.machine_in_tx(self.conn, "propose", apr, apr_doc, expected_revision=rev,
                                      alternatives=[sep_doc], document_date="2026-04-15")
        self.assertIn(f"document #{sep_doc} is dated 2026-09-15", str(cm.exception))


class PreUpgradeProposal(Monthly):
    def test_a_cross_period_proposal_made_before_is_released_not_kept(self):
        """0118-r3 (Terra S1): 0.11.7 proposed the September invoice for April; the correct
        April proposal releases it instead of keeping it as an alternative."""
        import documents, loop, matches
        apr_doc = self.drv.file_unread("APR-1", "LinkedIn", 5784, document_date="2026-04-15")
        sep_doc = self.drv.file_unread("SEP-1", "LinkedIn", 5784, document_date="2026-09-15")
        for d in (apr_doc, sep_doc):
            documents.update_document_metadata(self.conn, d, **self.drv.printed[d])
        apr = self.drv.pid_of(self.nos[0])
        self.drv.claim("cccccccc-3")

        def rev():
            return self.conn.execute("SELECT revision FROM projections WHERE pid=?",
                                     (apr,)).fetchone()[0]
        real = loop.in_window
        loop.in_window = lambda row, day: True              # as 0.11.7 allowed it
        try:
            with db.tx(self.conn):
                matches.machine_in_tx(self.conn, "propose", apr, sep_doc,
                                      expected_revision=rev(), document_date="2026-09-15")
        finally:
            loop.in_window = real
        with db.tx(self.conn):
            matches.machine_in_tx(self.conn, "propose", apr, apr_doc, expected_revision=rev(),
                                  document_date="2026-04-15")
        held = [h for h, _how, _d in matches.purchase_holders(self.conn, sep_doc)]
        self.assertNotIn(apr, held)
        alts = self.conn.execute("SELECT m.alternatives_json FROM matches m JOIN match_state s"
                                 " USING(match_id) WHERE s.pid=? AND s.state='proposed'",
                                 (apr,)).fetchone()[0]
        self.assertNotIn(sep_doc, json.loads(alts))


class ReplaceOfAnotherPeriod(StoreCase):
    def test_a_replace_with_an_out_of_window_document_is_refused(self):
        """0118-r3 (Astra S1): April holds its invoice; the job answers replace with the
        September invoice — refused, no question asked."""
        import asks
        from tests.sim_job import JobDriver
        self.bind(watermark="2026-04-01")
        drv = JobDriver(self, payments=0)
        apr_no = drv.pay_once("LINKEDIN", 5784, "2026-04-15")
        drv.pay_once("LINKEDIN", 5784, "2026-09-15")
        drv.gmail.invoice("LINKEDIN", 5784, "EUR", "2026-04-15", "APR-ORIGINAL")
        with self.patch_clock(NOW):
            drv.run_job("aaaaaaaa-1")
        apr = drv.pid_of(apr_no)
        a = drv.file_unread("APR-REISSUE", "LINKEDIN", 5784, document_date="2026-04-15")
        sep = drv.file_unread("SEP-INVOICE", "LINKEDIN", 5784, document_date="2026-09-15")
        asks.request_work(self.conn, "handover", "operator", doc_ids=[a, sep])
        seen = []
        original = drv._payment

        def payment(u, token):
            if u["pid"] == apr and not seen:
                out = drv._tool("decide", {"pass_token": token, "entries": [{
                    "pid": apr, "expected_revision": u["revision"], "outcome": "replace",
                    "doc_id": sep}]})
                seen.append(out)
                return None
            return original(u, token)
        drv._payment = payment
        with self.patch_clock(NOW):
            drv.run_job("bbbbbbbb-2")
        self.assertEqual((seen[0]["applied"], seen[0]["refused"]), (0, 1))
        self.assertIn("window", seen[0]["results"][0]["refused"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM replace_questions WHERE"
                                           " new_doc_id=?", (sep,)).fetchone()[0], 0)


class EdgeOfQuarter(StoreCase):
    def test_an_invoice_dated_just_before_the_quarter_still_fits(self):
        from tests.sim_job import JobDriver
        self.bind(watermark="2026-06-01")
        drv = JobDriver(self, payments=0)
        no = drv.pay_once("Acme", 10000, "2026-07-02")
        with self.patch_clock(NOW):
            drv.run_job("aaaaaaaa-1")
        import asks
        doc = drv.file_unread("A-630", "Acme", 10000, document_date="2026-06-30")
        asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])
        with self.patch_clock(NOW):
            units = drv.run_job("bbbbbbbb-2")
        self.assertEqual([u["pid"] for u in units if u["unit"] == "payment"], [drv.pid_of(no)])


class Cards(LoopCase):
    def c(self, fn, *a, **k):
        with db.tx(self.conn):
            return fn(self.conn, *a, **k)

    def test_a_card_with_nothing_to_tap_carries_casas_close_button(self):
        import cards
        lone = self.doc(amount_minor=1)
        rid = self.c(cards.compose_end, self.job_id, scheduled=False, handover_docs=[lone])
        dep = self.c(cards.deposit_of, rid)
        self.assertEqual(dep["buttons"], [{"label": "Close", "close": True}])
        self.assertNotIn("Close:", dep["text"])

    def test_a_pending_payment_of_that_amount_is_named_not_denied(self):
        import cards
        self.row(1, counterparty="modal.com", amount_minor=46, booking_date="2026-09-26",
                 value_date="2026-09-26", status="PDNG")
        p = self.lineage_for(1)
        self.settle(p)
        doc = self.doc(amount_minor=46, document_date="2026-09-26", issuer="Modal Labs")
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO run_docs(job_id, doc_id, fitted_seq, fits) VALUES"
                              " (?,?,1,0)", (self.job_id, doc))
        rid = self.c(cards.compose_end, self.job_id, scheduled=False, handover_docs=[doc])
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?", (rid,)).fetchone()[0]
        self.assertIn("not matched yet — a payment of EUR 0.46 to modal.com on 26 Sep is still "
                      "pending at the bank.", untag(text))
        self.assertNotIn("no payment of", text)

    def test_the_replace_card_says_what_differs(self):
        import cards
        out = self.c(cards._differs, self.doc(recipient="Nicola Bonzanni", document_number="X1"),
                     self.doc(recipient="Lesina BV", document_number="X1"))
        self.assertEqual(out, "Differs: recipient Nicola Bonzanni → Lesina BV.")
