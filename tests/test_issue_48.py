# tests/test_issue_48.py
"""Issue #48: one purchase (the same issuer and the same document number; a document with no
number is a purchase of its own) backs at most one payment, through the job's writes. Q2
re-run #3: May's invoice + receipt twin were filed as two documents; July's payment was then
proposed with May's receipt. The operator's taps are not limited by it (design §#48)."""
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported


class _Twins(StoreCase):
    """May and July (Adobe, EUR 100.00 each); one purchase filed twice: invoice + receipt."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.pids = []
        for n, day in ((1, "2026-09-01"), (2, "2026-09-02")):
            self.row(n, counterparty="Adobe", amount_minor=10000, booking_date=day,
                     value_date=day)
            pid = self.lineage_for(n)
            self.classify(pid, {"software"})
            self.settle(pid)
            self.pids.append(pid)
        self.may, self.july = self.pids
        # one purchase, filed twice: its invoice and its receipt
        self.invoice = self.doc(document_number="CUWVSRB8-0002", document_date="2026-05-01")
        self.receipt = self.doc(kind="receipt", document_number="CUWVSRB8-0002",
                                document_date="2026-05-01")

    def entry(self, pid, outcome, doc_id, **kw):
        return {"pid": pid, "outcome": outcome, "expected_revision": self.rev(pid),
                "doc_id": doc_id, "document_date": "2026-05-01", **kw}

    def decide(self, *entries):
        import decide
        return decide.decide(self.conn, self.token, list(entries))["results"]


class OnePurchase(_Twins):
    def test_the_twin_of_a_purchase_another_payment_holds_is_refused(self):
        """The re-run #3 shape: May proposed with the invoice, July with the receipt twin."""
        self.assertTrue(self.decide(self.entry(self.may, "propose", self.invoice))[0]["applied"])
        out = self.decide(self.entry(self.july, "propose", self.receipt))[0]
        self.assertFalse(out["applied"])
        self.assertIn(f"document #{self.receipt} is the same purchase as document "
                      f"#{self.invoice} (Adobe CUWVSRB8-0002)", out["refused"])
        # the refusal names the payment it already backs, so the model searches again
        self.assertIn("the payment of 2026-09-01 (EUR 100.00)", out["refused"])
        self.assertIn("search for this payment's own document", out["refused"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM match_state WHERE doc_id=?",
                                           (self.receipt,)).fetchone()[0], 0)

    def test_a_match_is_refused_too(self):
        self.decide(self.entry(self.may, "match", self.invoice))
        self.assertFalse(self.decide(self.entry(self.july, "match", self.receipt))[0]["applied"])

    def test_issuer_and_number_compare_as_collisions_do(self):
        """lower/trim of the number and of coalesce(issuer, counterparty)."""
        twin = self.doc(issuer=None, counterparty="  adobe ", document_number=" cuwvsrb8-0002 ")
        self.decide(self.entry(self.may, "propose", self.invoice))
        self.assertFalse(self.decide(self.entry(self.july, "propose", twin))[0]["applied"])

    def test_another_number_or_another_issuer_is_another_purchase(self):
        self.decide(self.entry(self.may, "propose", self.invoice))
        other_no = self.doc(document_number="CUWVSRB8-0003")
        other_issuer = self.doc(issuer="Eleven Labs", counterparty="Eleven Labs",
                                document_number="CUWVSRB8-0002")
        self.assertTrue(self.decide(self.entry(self.july, "propose", other_no))[0]["applied"])
        self.decide(self.entry(self.july, "propose", other_no))
        out = self.decide(self.entry(self.july, "propose", other_issuer))[0]
        self.assertTrue(out["applied"], out)

    def test_a_document_with_no_number_is_a_purchase_of_its_own(self):
        a = self.doc(document_number=None)
        b = self.doc(document_number=None)
        self.decide(self.entry(self.may, "propose", a))
        self.assertTrue(self.decide(self.entry(self.july, "propose", b))[0]["applied"])

    def test_a_payment_may_take_its_own_purchases_twin(self):
        """§2.2 reopening: what the payment itself holds is never taken against it."""
        self.decide(self.entry(self.may, "propose", self.invoice))
        out = self.decide(self.entry(self.may, "match", self.receipt))[0]
        self.assertTrue(out["applied"], out)

    def test_an_alternative_whose_purchase_another_payment_holds_is_refused(self):
        other = self.doc(document_number="X-1")
        self.decide(self.entry(self.may, "propose", self.invoice))
        out = self.decide(self.entry(self.july, "propose", other,
                                     alternatives=[self.receipt]))[0]
        self.assertFalse(out["applied"])
        self.assertIn("same purchase", out["refused"])

    def test_the_twin_is_free_again_once_its_holder_lets_the_purchase_go(self):
        """Held is read live: May re-decided onto another document frees the purchase."""
        import matches
        self.decide(self.entry(self.may, "match", self.invoice))
        self.assertTrue(matches.taken_elsewhere(self.conn, self.receipt, self.july))
        self.decide(self.entry(self.may, "match", self.doc(document_number="MAY-OWN")))
        self.assertFalse(matches.taken_elsewhere(self.conn, self.receipt, self.july))
        self.assertTrue(self.decide(self.entry(self.july, "propose", self.receipt))[0]["applied"])

    def test_the_candidates_show_the_twin_held_by_another_payment(self):
        import lineage, loop
        self.decide(self.entry(self.may, "match", self.invoice))
        p = lineage.projection(self.conn, self.july)
        row = lineage.live_row(self.conn, p)
        got = {c["doc_id"] for c in loop.candidates(self.conn, self.july, row, "Adobe")}
        # matched elsewhere through its twin: not a candidate at all, as a matched document
        self.assertNotIn(self.receipt, got)
        self.assertNotIn(self.invoice, got)

    def test_the_candidates_mark_a_proposed_twin_held_other(self):
        import lineage, loop
        self.decide(self.entry(self.may, "propose", self.invoice))
        p = lineage.projection(self.conn, self.july)
        row = lineage.live_row(self.conn, p)
        held = {c["doc_id"]: c["held"]
                for c in loop.candidates(self.conn, self.july, row, "Adobe")}
        self.assertEqual(held.get(self.receipt), "other")

    def test_the_exact_fit_skips_the_twin(self):
        import lineage, loop, matches
        self.decide(self.entry(self.may, "propose", self.invoice))
        p = lineage.projection(self.conn, self.july)
        row = lineage.live_row(self.conn, p)
        doc = self.conn.execute("SELECT * FROM documents WHERE doc_id=?",
                                (self.receipt,)).fetchone()
        self.assertFalse(loop._fits_exactly(self.conn, self.july, row, doc, p["exp_kind"]))
        self.assertTrue(matches.taken_elsewhere(self.conn, self.receipt, self.july))


class OnePurchaseMetadata(_Twins):
    """d1 Terra S1: the job's metadata edit can make two held documents one purchase."""

    def test_the_jobs_edit_that_joins_two_held_purchases_is_refused(self):
        import documents
        other = self.doc(document_number="OTHER-9")
        self.decide(self.entry(self.may, "propose", self.invoice))
        self.decide(self.entry(self.july, "propose", other))
        with self.assertRaises(db.Refusal) as cm:
            documents.update_document_metadata(self.conn, other, token=self.token,
                                               document_number="CUWVSRB8-0002")
        self.assertIn("the payment of 2026-09-01", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT document_number FROM documents WHERE"
                                           " doc_id=?", (other,)).fetchone()[0], "OTHER-9")

    def test_the_jobs_edit_of_an_issuer_is_checked_too(self):
        import documents
        other = self.doc(issuer="Adobe Inc", counterparty="Adobe Inc",
                         document_number="CUWVSRB8-0002")
        self.decide(self.entry(self.may, "propose", self.invoice))
        self.decide(self.entry(self.july, "propose", other))
        with self.assertRaises(db.Refusal):
            documents.update_document_metadata(self.conn, other, token=self.token,
                                               issuer="Adobe")

    def test_the_jobs_edit_of_an_unheld_document_is_applied(self):
        import documents
        self.decide(self.entry(self.may, "propose", self.invoice))
        free = self.doc(document_number="OTHER-9")
        out = documents.update_document_metadata(self.conn, free, token=self.token,
                                                 document_number="CUWVSRB8-0002")
        self.assertEqual(out["document_number"], "CUWVSRB8-0002")

    def test_the_jobs_edit_within_one_payments_own_purchase_is_applied(self):
        import documents
        self.decide(self.entry(self.may, "propose", self.invoice, alternatives=[]))
        other = self.doc(document_number="OTHER-9")
        self.decide(self.entry(self.may, "propose", other))      # replaces its own pairing
        out = documents.update_document_metadata(self.conn, other, token=self.token,
                                                 document_number="CUWVSRB8-0002")
        self.assertEqual(out["document_number"], "CUWVSRB8-0002")

    def _double_backed(self):
        """A 0.11.0 store: May holds the invoice, July the receipt twin (re-run #3's rows)."""
        self.decide(self.entry(self.may, "propose", self.invoice))
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET document_number='TMP' WHERE doc_id=?",
                              (self.receipt,))
        self.decide(self.entry(self.july, "propose", self.receipt))
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET document_number='CUWVSRB8-0002' WHERE"
                              " doc_id=?", (self.receipt,))

    def test_a_reading_that_restates_a_held_documents_number_is_applied(self):
        """A purchase that already backed two payments before never refuses a reading that
        joins nothing (the store carries 0.11.0's double backings)."""
        import documents
        self._double_backed()
        out = documents.update_document_metadata(self.conn, self.receipt, token=self.token,
                                                 document_number="CUWVSRB8-0002",
                                                 document_date="2026-05-02")
        self.assertEqual(out["document_date"], "2026-05-02")

    def test_a_reading_of_an_unheld_third_twin_is_applied(self):
        import documents
        self._double_backed()
        third = self.doc(document_number="UNREAD")
        out = documents.update_document_metadata(self.conn, third, token=self.token,
                                                 document_number="CUWVSRB8-0002")
        self.assertEqual(out["document_number"], "CUWVSRB8-0002")

    def test_the_operators_edit_is_never_limited(self):
        import documents
        other = self.doc(document_number="OTHER-9")
        self.decide(self.entry(self.may, "propose", self.invoice))
        self.decide(self.entry(self.july, "propose", other))
        out = documents.update_document_metadata(self.conn, other,
                                                 document_number="CUWVSRB8-0002")
        self.assertEqual(out["document_number"], "CUWVSRB8-0002")


class OnePurchaseReplace(StoreCase):
    """d1 Astra S1 (the job's replace answer) and d2 Astra S1 (an open replace question)."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.pids = []
        for n, day in ((1, "2026-09-02"), (2, "2026-09-03")):
            self.row(n, counterparty="Adobe", booking_date=day, value_date=day)
            pid = self.lineage_for(n)
            self.classify(pid, {"software"})
            self.settle(pid)
            self.pids.append(pid)
        self.p1, self.p2 = self.pids
        self.a = self.doc(vendor="Adobe", document_date="2026-09-01", document_number="A1")
        self.machine_match(self.p1, self.a, self.token)
        self.b = self.doc(vendor=None, document_date="2026-09-02", document_number="B2")
        self.b_twin = self.doc(kind="receipt", vendor=None, document_date="2026-09-02",
                               document_number="B2")
        import asks
        asks.request_work(self.conn, "handover", "operator", [self.b])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)

    def test_the_jobs_replace_of_a_purchase_another_payment_holds_is_refused(self):
        import replace
        self.machine_match(self.p2, self.b_twin, self.token)
        with db.tx(self.conn), self.assertRaises(db.Refusal) as cm:
            replace.ask_in_tx(self.conn, self.job_id, self.p1, self.b)
        self.assertIn("same purchase", str(cm.exception))

    def test_an_open_question_whose_purchase_another_payment_took_is_retired(self):
        import replace
        with db.tx(self.conn):
            qid = replace.ask_in_tx(self.conn, self.job_id, self.p1, self.b)
        self.machine_match(self.p2, self.b_twin, self.token)
        with db.tx(self.conn):
            self.assertEqual(replace.open_ones(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT state FROM replace_questions WHERE"
                                           " question_id=?", (qid,)).fetchone()[0],
                         "superseded")

    def test_an_open_question_with_a_free_purchase_stays(self):
        import replace
        with db.tx(self.conn):
            qid = replace.ask_in_tx(self.conn, self.job_id, self.p1, self.b)
            self.assertEqual([q["question_id"] for q in replace.open_ones(self.conn)], [qid])
