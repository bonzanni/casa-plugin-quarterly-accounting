"""Round h1 (Astra S1 ×2, design round d1: Astra + Terra): the branch split filing from
reading (finance's path_scope denies `Read` on a download). A found attachment's ref is done
only once its document is filed AND its reading recorded (documents.read_at): a cut between
filing and reading hands the ref again — own mail too, whose vendorless unread document is no
payment's candidate. A job reading goes through the one sticky conflict rule (_reread), a
partial reading included; the operator's correction outside a pass is written as before."""
from tests._base import StoreCase
from tests.sim_job import CasaCut, JobDriver


def _status(conn):
    return dict(conn.execute("SELECT status, count(*) FROM projections GROUP BY status"))


class ReadOwed(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = None

    def claim_token(self):
        if self.token is None:
            self.run_claim()
        return self.token

    def cut_first(self, drv, name):
        real, cut = getattr(drv, name), []

        def once(*a, **k):
            if not cut:
                cut.append(a)
                raise CasaCut()
            return real(*a, **k)
        setattr(drv, name, once)
        return cut

    def test_own_mail_cut_between_filing_and_reading_is_read_on_the_redo(self):
        drv = JobDriver(self, payments=1)                       # Zapier EUR 10.00
        drv.gmail.own(1000, day="2026-07-05", number="ZAP-1")
        real_tool, cut = drv._tool, []

        def tool(name, args):
            if name == "read_document" and not cut:
                cut.append(args)
                raise CasaCut()                                 # filed; never read
            return real_tool(name, args)
        drv._tool = tool
        drv.casa_cut = 80
        drv.run_job("eeee0001-01")
        self.assertEqual(len(cut), 1)
        self.assertEqual(_status(self.conn), {"matched": 1})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents WHERE read_at IS"
                                           " NULL").fetchone()[0], 0)

    def _filed_unread(self, amount=None):
        import documents
        path = self.publish("x.pdf", b"%PDF-1.4 same bytes\n")
        out = documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                        source="gmail", extraction_author="specialist",
                                        source_ref="m-1:att-1", amount_minor=amount,
                                        currency="EUR" if amount else None)
        return out["doc_id"]

    def test_a_job_reading_that_disagrees_makes_the_amount_unknown(self):
        import documents
        doc = self._filed_unread(amount=1200)
        tok = self.claim_token()
        documents.update_document_metadata(self.conn, doc, token=tok, amount_minor=1000,
                                           currency="EUR")
        d = self.conn.execute("SELECT amount_minor, amount_conflict FROM documents WHERE"
                              " doc_id=?", (doc,)).fetchone()
        self.assertEqual(tuple(d), (None, 1))

    def test_a_partial_job_reading_that_disagrees_conflicts_too(self):
        import documents
        for fields in ({"amount_minor": 1100}, {"currency": "USD"}):
            with self.subTest(fields=fields):
                self.setUp()
                doc = self._filed_unread(amount=1000)
                documents.update_document_metadata(self.conn, doc, token=self.claim_token(),
                                                   **fields)
                self.assertEqual(self.conn.execute(
                    "SELECT amount_conflict FROM documents WHERE doc_id=?", (doc,)
                ).fetchone()[0], 1)

    def test_the_same_job_reading_twice_is_no_conflict(self):
        import documents
        doc = self._filed_unread()
        for _ in range(2):
            documents.update_document_metadata(self.conn, doc, token=self.claim_token(),
                                               amount_minor=1000, currency="EUR")
        d = self.conn.execute("SELECT amount_minor, amount_conflict, read_at FROM documents"
                              " WHERE doc_id=?", (doc,)).fetchone()
        self.assertEqual((d[0], d[1]), (1000, 0))
        self.assertIsNotNone(d[2])

    def test_an_unreadable_amount_still_records_the_reading(self):
        import documents, work
        doc = self._filed_unread()
        self.assertFalse(work.filed(self.conn, "m-1:att-1"))        # owed: filed, unread
        documents.update_document_metadata(self.conn, doc, token=self.claim_token())
        self.assertTrue(work.filed(self.conn, "m-1:att-1"))

    def test_a_reading_without_the_token_goes_through_the_same_rule(self):
        """h2 (Astra S1): a job reading sent without its pass_token wrote the amount past
        the conflict rule, and a €12 document matched a €10 payment. Every write of a
        document's amount goes through _reread, token or none (no other caller writes one)."""
        import documents
        doc = self._filed_unread(amount=1200)
        documents.update_document_metadata(self.conn, doc, amount_minor=1000, currency="EUR")
        self.assertEqual(self.conn.execute("SELECT amount_minor, amount_conflict FROM"
                                           " documents WHERE doc_id=?", (doc,)).fetchone()[:],
                         (None, 1))

    def test_a_tokenless_reading_closes_the_refs_too(self):
        import documents, work
        doc = self._filed_unread()
        documents.update_document_metadata(self.conn, doc, amount_minor=1000, currency="EUR")
        self.assertTrue(work.filed(self.conn, "m-1:att-1"))

    def test_a_document_filed_under_a_bare_message_id_is_owed_until_read(self):
        """h2 (Astra S1): the legacy bare-id fallback counted an unread document filed."""
        import documents, work
        path = self.publish("y.pdf", b"%PDF-1.4 bare\n")
        doc = documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                        source="gmail", extraction_author="specialist",
                                        source_ref="msg-0001")["doc_id"]
        self.assertFalse(work.filed(self.conn, "msg-0001:att-1"))
        documents.update_document_metadata(self.conn, doc, token=self.claim_token())
        self.assertTrue(work.filed(self.conn, "msg-0001:att-1"))

    def test_a_reading_given_at_filing_is_read(self):
        import work
        self._filed_unread(amount=1000)
        self.assertTrue(work.filed(self.conn, "m-1:att-1"))


class Migration(StoreCase):
    def test_13_to_14_backfills_only_rows_that_carry_a_reading(self):
        import db
        read = self.doc()                                          # amount 100.00 EUR
        unread = self.doc(amount_minor=None, currency=None)        # a cut before its reading
        clash = self.doc(amount_minor=None, currency=None, amount_conflict=1)
        with db.tx(self.conn):                                     # the schema-13 store
            self.conn.execute("ALTER TABLE documents DROP COLUMN read_at")
            self.conn.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
        db.migrate(self.conn)
        got = {r[0]: r[1] is not None for r in self.conn.execute(
            "SELECT doc_id, read_at FROM documents")}
        self.assertEqual(got, {read: True, unread: False, clash: True})
