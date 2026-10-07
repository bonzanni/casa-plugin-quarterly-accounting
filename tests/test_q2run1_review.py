"""Live Q2 run 1 on ba9e512 (PLAY, 2026-10-07), each failure reproduced:
- the model miscounted its calls for the batch budget, and skipped a `report: true`
  attached to a work unit — no batch ever reported, Casa's 3-batch guard ended a productive
  run. Now a claim that persisted work gets the `report` unit alone (and since the
  no-budget ruling, tests/test_no_budget.py, the server counts no calls at all);
- a reading that copies the payment: the same bytes filed again with a different amount
  make the amount unknown — such a document is proposed, never matched."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported


class ProgressReachesCasa(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_a_model_that_skips_attached_reports_still_reports_every_batch(self):
        drv = JobDriver(self, payments=60)
        for i in range(60):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
        drv.skip_attached_reports = True          # run 1's model
        drv.casa_cut = 80                         # Casa's guard, last-report semantics
        units = drv.run_job("a2a2a2a2-01")
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertGreater(sum(u["unit"] == "report" for u in units), 1)
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 60})


class ConflictingReadings(StoreCase):
    def test_a_refiled_document_whose_reading_disagrees_is_only_proposed(self):
        import decide, documents, loop
        self.bind()
        self.token = self.run_claim()
        self.row(1, counterparty="Openrouter", amount_minor=4592, booking_date="2026-08-02",
                 value_date="2026-08-02")
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        path = self.publish("or.pdf", b"%PDF-1.4 openrouter invoice 0005")
        first = documents.ingest_document(            # the copied reading: the payment's
            self.conn, source_path=path, kind="invoice", source="gmail",
            extraction_author="specialist", issuer="OpenRouter", amount_minor=4592,
            currency="EUR", document_date="2026-08-05", document_number="EITIZBQ1-0005",
            vendor="Openrouter", token=self.token)["doc_id"]
        path = self.publish("or2.pdf", b"%PDF-1.4 openrouter invoice 0005")
        again = documents.ingest_document(            # read for real: USD 105.93
            self.conn, source_path=path, kind="invoice", source="gmail",
            extraction_author="specialist", issuer="OpenRouter", amount_minor=10593,
            currency="USD", document_date="2026-08-05", document_number="EITIZBQ1-0005",
            vendor="Openrouter", token=self.token)
        self.assertEqual((again["doc_id"], again["created"]), (first, False))
        self.assertEqual(tuple(self.conn.execute(
            "SELECT amount_minor, currency FROM documents WHERE doc_id=?",
            (first,)).fetchone()), (None, None))       # the readings disagree: unknown
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        loop.build_work(self.conn, self.job_id)
        u = loop.payment_unit(self.conn, self.job_id)
        self.assertIn(first, [c["doc_id"] for c in u["candidates"]])
        out = decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "match", "doc_id": first, "document_date": "2026-04-05",
            "expected_revision": u["revision"]}])
        self.assertEqual(out["applied"], 0)
        self.assertIn("unknown", out["results"][0]["refused"])
        out = decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "propose", "doc_id": first, "document_date": "2026-04-05",
            "expected_revision": u["revision"]}])
        self.assertEqual(out["applied"], 1)


class ConflictIsSticky(StoreCase):
    """Round f1 (Terra S1): a conflict between readings is a fact about the document — a
    third reading never restores an amount, and a machine match already holding the
    document becomes a proposal (an operator's pairing stays the operator's)."""
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.row(1, counterparty="Zapier", amount_minor=1000, booking_date="2026-08-02",
                 value_date="2026-08-02")
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)

    def file(self, name, amount):
        import documents
        return documents.ingest_document(
            self.conn, source_path=self.publish(name, b"%PDF-1.4 zapier invoice 7"),
            kind="invoice", source="gmail", extraction_author="specialist", issuer="Zapier",
            amount_minor=amount, currency="EUR", document_date="2026-08-01",
            document_number="ZAP-7", vendor="Zapier", token=self.token)["doc_id"]

    def match(self, doc):
        import decide
        rev = self.rev(self.pid)
        return decide.decide(self.conn, self.token, [{
            "pid": self.pid, "outcome": "match", "doc_id": doc,
            "document_date": "2026-08-01", "expected_revision": rev}])

    def status(self):
        return self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                 (self.pid,)).fetchone()[0]

    def walk_payment(self):
        import loop
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        loop.build_work(self.conn, self.job_id)
        loop.payment_unit(self.conn, self.job_id)

    def test_a_third_reading_never_restores_the_amount(self):
        doc = self.file("a.pdf", 1000)
        self.file("b.pdf", 2000)
        self.file("c.pdf", 1000)
        self.walk_payment()
        out = self.match(doc)
        self.assertEqual(out["applied"], 0, out)
        self.assertIn("unknown", out["results"][0]["refused"])
        self.assertNotEqual(self.status(), "matched")

    def test_a_conflict_after_a_machine_match_makes_it_a_proposal(self):
        doc = self.file("a.pdf", 1000)
        self.walk_payment()
        self.assertEqual(self.match(doc)["applied"], 1)
        self.assertEqual(self.status(), "matched")
        self.file("b.pdf", 2000)
        self.assertEqual(self.status(), "proposed")

    def test_a_handover_receipt_shows_the_demoted_match_to_confirm(self):
        """Round f2 (Astra S2): the handed document's readings disagree with its match — the
        receipt must not say "Paired"; the payment is a proposal for the review."""
        import cards
        doc = self.file("a.pdf", 1000)
        self.walk_payment()
        self.assertEqual(self.match(doc)["applied"], 1)
        self.file("b.pdf", 2000)
        head, props = cards._receipts(self.conn, [doc])
        self.assertEqual((head, [p["pid"] for p in props]), ([], [self.pid]))


class SchemaTwelveStoresUpgrade(StoreCase):
    def test_a_ba9e512_store_gains_the_columns_and_the_job_runs(self):
        """Round f1 (Astra S1): run 1 left schema-12 stores live; 12 → 13 adds the claim
        and document columns, and the job's job_next runs on the upgraded store."""
        conn = self.conn
        for table, col in (("claims", "said_seq"), ("claims", "report_seq"),
                           ("documents", "amount_conflict"), ("documents", "read_at"),
                           ("runs", "end_text")):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")      # ba9e512's shape
        conn.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
        conn.commit()
        db.migrate(conn)
        cols = {(t, r[1]) for t in ("claims", "documents")
                for r in conn.execute(f"PRAGMA table_info({t})")}
        self.assertTrue({("claims", "said_seq"), ("claims", "report_seq"),
                         ("documents", "amount_conflict")} <= cols)
        self.bind()
        drv = JobDriver(self, payments=3)
        for i in range(3):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i], f"ZAP-{i + 1}")
        drv.run_job("a2a2a2a2-12")
        self.assertEqual(dict(conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 3})
