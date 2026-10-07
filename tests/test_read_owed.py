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

    def test_withdrawing_a_held_currency_or_amount_makes_the_amount_unknown(self):
        """h3 (Astra S1): currency="" (through the tool) or amount_minor=None withdraws a
        held value — a reading that disagrees, so the amount is unknown for good."""
        import documents, qa_server, tools  # noqa: F401
        for how in ("tool-currency", "tool-amount", "tool-null-currency", "direct-amount"):
            with self.subTest(how=how):
                self.setUp()
                doc = self._filed_unread(amount=1000)
                if how.startswith("tool-"):        # h4 (Terra S1): a null amount too
                    qa_server.TOOLS["update_document_metadata"]["fn"](
                        {"doc_id": doc, **({"currency": ""} if how == "tool-currency"
                                           else {"currency": None}
                                           if how == "tool-null-currency"
                                           else {"amount_minor": None})})
                else:
                    documents.update_document_metadata(self.conn, doc, amount_minor=None)
                d = self.conn.execute("SELECT amount_minor, currency, amount_conflict FROM"
                                      " documents WHERE doc_id=?", (doc,)).fetchone()
                self.assertEqual(d["amount_conflict"], 1)
                self.assertTrue(documents.amount_unknown(d))

    def test_refiling_with_a_blank_or_null_amount_says_nothing(self):
        """The ingest contract (BRAIN, h6): ingest FILES the document — a blank or null
        amount or currency there is "not read here", never a withdrawal (that would make
        every refile of a read invoice propose-only); only update_document_metadata
        withdraws a held reading. A concrete disagreeing value still conflicts."""
        import qa_server, tools  # noqa: F401
        for said in ({"currency": ""}, {"currency": None}, {"amount_minor": None},
                     {"amount_minor": None, "currency": None}):
            with self.subTest(said=said):
                self.setUp()
                doc = self._filed_unread(amount=1000)
                path = self.publish("x.pdf", b"%PDF-1.4 same bytes\n")
                out = qa_server.TOOLS["ingest_document"]["fn"](
                    {"source_path": path, "kind": "invoice", "source": "gmail",
                     "extraction_author": "specialist", "source_ref": "m-1:att-2",
                     "pass_token": self.claim_token(), **said})
                self.assertEqual(out["doc_id"], doc)
                self.assertEqual(tuple(self.conn.execute(
                    "SELECT amount_minor, currency, amount_conflict FROM documents WHERE"
                    " doc_id=?", (doc,)).fetchone()), (1000, "EUR", 0))

    def test_refiling_with_a_disagreeing_amount_conflicts(self):
        import qa_server, tools  # noqa: F401
        doc = self._filed_unread(amount=1000)
        path = self.publish("x.pdf", b"%PDF-1.4 same bytes\n")
        qa_server.TOOLS["ingest_document"]["fn"](
            {"source_path": path, "kind": "invoice", "source": "gmail",
             "extraction_author": "specialist", "source_ref": "m-1:att-2",
             "amount_minor": 1100, "currency": "EUR", "pass_token": self.claim_token()})
        self.assertEqual(self.conn.execute("SELECT amount_conflict FROM documents WHERE"
                                           " doc_id=?", (doc,)).fetchone()[0], 1)

    def test_refiling_with_nothing_said_about_the_amount_changes_nothing(self):
        import qa_server, tools  # noqa: F401
        doc = self._filed_unread(amount=1000)
        path = self.publish("x.pdf", b"%PDF-1.4 same bytes\n")
        qa_server.TOOLS["ingest_document"]["fn"](
            {"source_path": path, "kind": "invoice", "source": "gmail",
             "extraction_author": "specialist", "source_ref": "m-1:att-2",
             "pass_token": self.claim_token()})
        self.assertEqual(tuple(self.conn.execute("SELECT amount_minor, amount_conflict FROM"
                                                 " documents WHERE doc_id=?", (doc,)
                                                 ).fetchone()), (1000, 0))

    def test_withdrawing_nothing_held_changes_nothing(self):
        import documents
        doc = self._filed_unread()
        documents.update_document_metadata(self.conn, doc, token=self.claim_token(),
                                           currency="")
        self.assertEqual(self.conn.execute("SELECT amount_conflict FROM documents WHERE"
                                           " doc_id=?", (doc,)).fetchone()[0], 0)

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
            self.conn.execute("ALTER TABLE runs DROP COLUMN end_text")
            self.conn.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
        db.migrate(self.conn)
        got = {r[0]: r[1] is not None for r in self.conn.execute(
            "SELECT doc_id, read_at FROM documents")}
        self.assertEqual(got, {read: True, unread: False, clash: True})

    def test_13_to_14_keeps_a_completed_runs_words_as_said(self):
        """h4 (Astra S2): a run completed before the upgrade said "Accounting work
        finished." — kept, never re-derived from a later store."""
        import db, job
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO runs(job_id, started_by, completed_at) VALUES"
                              " ('old-run', 'scheduled', '2026-10-01T00:00:00Z')")
            self.conn.execute("INSERT INTO runs(job_id, started_by) VALUES ('live-run',"
                              " 'scheduled')")
            self.conn.execute("ALTER TABLE documents DROP COLUMN read_at")
            self.conn.execute("ALTER TABLE runs DROP COLUMN end_text")
            self.conn.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
        db.migrate(self.conn)
        self.bind()
        self.assertEqual(job.run_end(self.conn, "old-run")[0], "Accounting work finished.")
        self.assertIsNone(self.conn.execute("SELECT end_text FROM runs WHERE"
                                            " job_id='live-run'").fetchone()[0])
