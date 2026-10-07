"""Live Q2 run 1 on ba9e512 (PLAY, 2026-10-07), each failure reproduced:
- the model sent `calls_made` as the calls since its previous job_next, and skipped a
  `report: true` attached to a work unit — no batch ever reported, Casa's 3-batch guard
  ended a productive run. Now `calls_made` is that delta (the server sums it per claim),
  and a claim that persisted work gets the `report` unit alone;
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

    def test_calls_made_is_summed_per_claim(self):
        import job, loop
        drv = JobDriver(self, payments=1)
        tok = drv.claim("a2a2a2a2-02")
        for _ in range(4):
            u = job.next_unit(self.conn, tok, 0)
            if u["unit"] == "payment":
                break
            drv.do(u, tok)
        units = [job.next_unit(self.conn, tok, 20)["unit"] for _ in range(4)]   # 4 × 20
        self.assertIn("end-batch", units)
        self.assertGreaterEqual(self.conn.execute("SELECT calls FROM claims WHERE gen=?",
                                                  (tok,)).fetchone()[0], loop.CALLS_SOFT)


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
