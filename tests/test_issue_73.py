"""0.11.11: issue #73 — a document re-dated after it was matched kept a match outside its
payment's period, so the payment it belongs to could not take it ("document is taken")."""
from tests._base import StoreCase, untag
import db
from tests.test_issue_67 import NOW


class Redated(StoreCase):
    """Six monthly EUR 57.84 charges, Apr–Sep, all missing after a full check (as #72's)."""

    def setUp(self):
        super().setUp()
        from tests.sim_job import JobDriver
        self.bind(watermark="2026-04-01")
        self.drv = JobDriver(self, payments=0)
        self.nos = [self.drv.pay_once("LINKEDIN", 5784, f"2026-{m:02d}-15")
                    for m in range(4, 10)]
        with self.patch_clock(NOW):
            self.drv.run_job("aaaaaaaa-1")

    def _april_invoice(self):
        import documents
        doc = self.drv.file_unread("5004871233", "LinkedIn", 5784, document_date="2026-04-15")
        self.drv.printed[doc]["document_date"] = "2026-04-15"
        documents.update_document_metadata(self.conn, doc, **self.drv.printed[doc])
        return doc
    def _pair(self, kind, pid, doc, day):
        import matches
        rev = self.conn.execute("SELECT revision FROM projections WHERE pid=?",
                                (pid,)).fetchone()[0]
        with db.tx(self.conn):
            matches.machine_in_tx(self.conn, kind, pid, doc, expected_revision=rev,
                                  document_date=day)

    def _holders(self, doc):
        import matches
        return [p for p, _how in matches.holders(self.conn, doc)]

    def test_a_reading_that_moves_the_date_out_releases_the_jobs_match(self):
        import documents
        doc = self._april_invoice()
        apr, sep = self.drv.pid_of(self.nos[0]), self.drv.pid_of(self.nos[-1])
        self.drv.claim("cccccccc-3")
        self._pair("pair", apr, doc, "2026-04-15")
        self.assertEqual(self._holders(doc), [apr])
        out = documents.update_document_metadata(self.conn, doc, document_date="2026-09-15")
        self.assertEqual(self._holders(doc), [])
        self.assertIn("released", out)
        self.assertIn("2026-09-15", out["released"])
        self.assertIn("2026-04-15", out["released"])
        st = self.conn.execute("SELECT status FROM projections WHERE pid=?", (apr,)).fetchone()[0]
        self.assertNotIn(st, ("matched", "proposed"))
        # the September payment can take it now
        self._pair("pair", sep, doc, "2026-09-15")
        self.assertEqual(self._holders(doc), [sep])

    def test_a_proposal_is_released_too_and_a_date_in_the_window_keeps_it(self):
        import documents
        doc = self._april_invoice()
        apr = self.drv.pid_of(self.nos[0])
        self.drv.claim("cccccccc-3")
        self._pair("propose", apr, doc, "2026-04-15")
        out = documents.update_document_metadata(self.conn, doc, document_date="2026-04-20")
        self.assertEqual(self._holders(doc), [apr])
        self.assertNotIn("released", out)
        out = documents.update_document_metadata(self.conn, doc, document_date="2026-09-15")
        self.assertEqual(self._holders(doc), [])
        self.assertIn("released", out)

    def test_the_operators_pairing_is_never_released(self):
        import documents, matches
        doc = self._april_invoice()
        apr = self.drv.pid_of(self.nos[0])
        with db.tx(self.conn):
            matches._operator_pair(self.conn, apr, doc, None)
            import lineage
            lineage.settle_doc_holders(self.conn, doc)
        out = documents.update_document_metadata(self.conn, doc, document_date="2026-09-15")
        self.assertEqual(self._holders(doc), [apr])
        self.assertNotIn("released", out)

    def test_the_next_check_matches_it_to_the_payment_it_belongs_to(self):
        import documents
        doc = self._april_invoice()
        apr, sep = self.drv.pid_of(self.nos[0]), self.drv.pid_of(self.nos[-1])
        self.drv.claim("cccccccc-3")
        self._pair("pair", apr, doc, "2026-04-15")
        documents.update_document_metadata(self.conn, doc, document_date="2026-09-15")
        self.drv.printed[doc]["document_date"] = "2026-09-15"
        import asks
        asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])
        with self.patch_clock(NOW):
            self.drv.run_job("dddddddd-4")
        self.assertEqual(self._holders(doc), [sep])
        self.assertIn("15 Sep", untag(self.drv.posted_end("dddddddd-4")["text"]))

    def test_a_reading_of_amount_and_date_together_is_judged_on_the_new_date(self):
        """r1 (Terra S2): a job proposal made on a wrong date (as before 0.11.8), corrected in
        ONE reading to its amount and a date that fits — kept, not released on the old date."""
        import documents, loop
        doc = self.drv.file_unread("5004871233", "LinkedIn", 5784, document_date="2026-04-15")
        documents.update_document_metadata(self.conn, doc, document_date="2026-04-15")
        sep = self.drv.pid_of(self.nos[-1])
        self.drv.claim("cccccccc-3")
        real = loop.in_window
        loop.in_window = lambda row, day: True              # as 0.11.7 allowed it
        try:
            self._pair("propose", sep, doc, None)
        finally:
            loop.in_window = real
        self.assertEqual(self._holders(doc), [sep])
        out = documents.update_document_metadata(self.conn, doc, amount_minor=5784,
                                                 currency="EUR", document_date="2026-09-15")
        self.assertEqual(self._holders(doc), [sep])
        self.assertNotIn("released", out)
