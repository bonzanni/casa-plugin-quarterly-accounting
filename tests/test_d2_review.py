"""Diff round d2 (Astra, 26b68ee..3d9f262), the three accepted findings, each reproduced
through the real surface (qa_server.TOOLS, a real bank-feed):
- Astra S1a: no batch Casa cuts at 80 calls is left without a progress report (since the
  no-budget ruling of 2026-10-07: the next claim reports it), and filing ends only once its queue
  (queues: the own-mail search and every attachment it found) is empty;
- Astra S1b: a handover that joins an operator check keeps the check's full summary and
  Review order, the handover's receipt line added; the handover-only rendering is for a
  standalone continuation;
- Astra S2: own-mail filing passes the reading (amount, currency, date, issuer, number),
  so the document is a candidate and is matched in the same run."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported

CASA_CALLS = 80               # Casa ends a batch at this many tool calls


class OwnMailFilingIsSliced(StoreCase):
    """Astra d2 S1a: 83 own-mail attachments, each downloaded, read and filed."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=2)          # Zapier EUR 10.00 and 20.00

    def test_83_attachments_file_across_batches_each_reporting_progress(self):
        import loop
        g = self.drv.gmail
        g.own(1000, day="2026-07-05", number="ZAP-1")      # the two that fit
        g.own(2000, day="2026-08-05", number="ZAP-2")
        for i in range(81):
            g.own(70000 + i, day="2026-07-20")
        self.drv.casa_cut = CASA_CALLS
        units = self.drv.run_job("d2d2d2d2-a1")
        # no call budget: Casa's cut ends every batch but the last, each at most 80 calls
        self.assertEqual(self.drv.cuts, len(self.drv.batch_calls) - 1)
        self.assertLessEqual(max(self.drv.batch_calls), CASA_CALLS, self.drv.batch_calls)
        self.assertGreaterEqual(len(self.drv.batch_calls), 4)    # 83 files: several batches
        self.assertTrue(all(self.drv.batch_reported), self.drv.batch_reported)
        filing = [u for u in units if u["unit"] == "filing"]
        self.assertGreater(len(filing), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 83)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM operator_refs WHERE source='manual-email'").fetchone()[0], 83)
        # the quarter completes: both payments matched to their own-mail invoices
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 2})
        run = self.conn.execute("SELECT * FROM runs WHERE job_id='d2d2d2d2-a1'").fetchone()
        self.assertIsNotNone(run["completed_at"])
        self.assertFalse(run["partial"])
        self.assertTrue(loop.complete(self.conn, "2026-Q3"))

    def test_filing_is_handed_until_its_queue_is_empty(self):
        """Queues: the server re-hands `filing` while its own-mail search or an attachment
        it found is still queued; the list is built only after."""
        import job

        def queued():
            return self.conn.execute("SELECT count(*) FROM run_items WHERE job_id="
                                     "'d2d2d2d2-a2' AND unit='filing' AND state='queued'"
                                     ).fetchone()[0]
        g = self.drv.gmail
        for i in range(30):
            g.own(70000 + i)
        self.drv.claim("d2d2d2d2-a2")
        seen = []
        for _ in range(20):
            u = self.drv.next()
            self.drv.calls += 1
            if u["unit"] == "filing":
                seen.append(self.conn.execute("SELECT count(*) FROM operator_refs"
                                              ).fetchone()[0])
                self.assertGreater(queued(), 0)             # not drained yet
                self.assertIsNone(self.conn.execute(
                    "SELECT listed_at FROM runs WHERE job_id='d2d2d2d2-a2'").fetchone()[0])
            if u["unit"] == "payment":
                break
            self.drv.do(u, self.drv.token)
        self.assertGreaterEqual(len(seen), 1)          # handed until its queue is empty
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(queued(), 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM operator_refs").fetchone()[0],
                         30)

    def test_every_filed_ref_is_held_exactly(self):
        """More files than d2's old 60-ref cap: every ref is filed once and held whole
        (d4: membership is the server's, on the exact refs)."""
        g = self.drv.gmail
        for i in range(83):
            g.own(70000 + i)
        self.drv.run_job("d2d2d2d2-a3")
        refs = [r[0] for r in self.conn.execute("SELECT ref FROM operator_refs")]
        self.assertEqual(sorted(refs), sorted(m["ref"] for m in g.own_mail))

    def test_a_new_ref_for_bytes_already_held_is_progress(self):
        """A slice whose attachments are all copies of held files still persisted work —
        their refs — so its batch reports progress; a ref filed again does not."""
        import job
        self.drv.claim("d2d2d2d2-a4")
        for _ in range(10):
            u = self.drv.next()
            if u["unit"] == "filing":
                break
            self.drv.do(u, self.drv.token)
        self.assertEqual(u["unit"], "filing")
        for ref, expect in (("m1:a1", 1), ("m2:a1", 1), ("m2:a1", 0)):
            with db.tx(self.conn):
                self.conn.execute("UPDATE claims SET progressed=0 WHERE gen=?",
                                  (self.drv.token,))
            path = self.publish("same.pdf", b"%PDF-1.4 the same bytes")
            self.drv._tool("ingest_document", dict(
                source_path=path, kind="invoice", source="manual-email",
                extraction_author="specialist", source_ref=ref, pass_token=self.drv.token))
            self.assertEqual(self.conn.execute("SELECT progressed FROM claims WHERE gen=?",
                                               (self.drv.token,)).fetchone()[0], expect, ref)


class HandoverJoiningACheck(StoreCase):
    """Astra d2 S1b: an operator check with two booked payments; a EUR 10 invoice handed
    over before the end message is composed."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=2)          # Zapier EUR 10.00 and 20.00

    def hand_over_during(self, jid, at="payment", started="operator"):
        import job
        self.drv.claim(jid, started)
        for _ in range(40):
            u = self.drv.next()
            self.drv.calls += 1
            if u["unit"] == at:
                break
            self.drv.do(u, self.drv.token)
        else:
            self.fail(f"{at} was never handed out")
        path = self.publish("handover.pdf", b"%PDF-1.4 invoice Zapier EUR 10.00",
                            producer="telegram")
        doc = self.drv._tool("ingest_document", dict(
            source_path=path, kind="invoice", source="manual-telegram",
            extraction_author="desk", counterparty="Zapier", issuer="Zapier",
            amount_minor=1000, currency="EUR", document_date="2026-07-05",
            document_number="ZAP-HAND"))
        self.drv._tool("request_work", dict(kind="handover", trigger="operator",
                                            doc_ids=[doc["doc_id"]]))
        self.drv.do(u, self.drv.token)
        self.drv._loop(jid)
        return self.drv.posted_end(jid)

    def test_astras_sequence_keeps_the_counts_review_and_receipt(self):
        end = self.hand_over_during("d2d2d2d2-b1")
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 1, "open": 1})
        lines = end["text"].split("\n")
        self.assertTrue(lines[0].startswith("Q3 checked · 2 payments"), lines)
        self.assertIn("1 matched · 1 missing", lines)
        (receipt,) = [ln for ln in lines if ln.startswith("Filed. ")]
        self.assertTrue(receipt.startswith("Filed. Matched to Zapier · 5 Jul · EUR 10.00"))
        self.assertEqual([b["label"] for b in end["buttons"]],
                         ["Review", "Invoice links", "Get package"])        # #57
        # the Review order is the check's: the missing payment's vendor
        rid = self.conn.execute("SELECT end_render_id FROM runs WHERE job_id='d2d2d2d2-b1'"
                                ).fetchone()[0]
        import json
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (rid,)).fetchone()[0])
        missing = self.conn.execute("SELECT pid FROM projections WHERE status='open'"
                                    ).fetchone()[0]
        self.assertEqual(scope["order"], [{"v": "Zapier", "pids": [missing]}])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE"
                                           " delivered_at IS NOT NULL").fetchone()[0], 1)

    def test_a_check_whose_handover_answers_everything_says_so_with_the_receipt(self):
        """The quarter completes in the check that took the handover: the ready notice,
        with the receipt line."""
        self.drv.gmail.invoice("Zapier", 2000, "EUR", "2026-08-05", "ZAP-2")
        end = self.hand_over_during("d2d2d2d2-b2")
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 2})
        self.assertIn("Q3 complete · 2 of 2 accounted for · package ready", end["text"])
        self.assertIn("Filed. Matched to Zapier · 5 Jul · EUR 10.00", end["text"])

    def test_a_standalone_continuation_keeps_the_handover_only_message(self):
        """A run whose only request is the handover shows only what it changed."""
        self.drv.run_job("d2d2d2d2-b3")                        # both missing, shown
        path = self.publish("handover.pdf", b"%PDF-1.4 invoice Zapier EUR 10.00",
                            producer="telegram")
        doc = self.drv._tool("ingest_document", dict(
            source_path=path, kind="invoice", source="manual-telegram",
            extraction_author="desk", issuer="Zapier", amount_minor=1000, currency="EUR",
            document_date="2026-07-05", document_number="ZAP-HAND"))
        self.drv._tool("request_work", dict(kind="handover", trigger="operator",
                                            doc_ids=[doc["doc_id"]]))
        self.drv.run_job("d2d2d2d2-b4")
        end = self.drv.posted_end("d2d2d2d2-b4")
        self.assertTrue(end["text"].split("\n")[0].startswith(
            "Filed. Matched to Zapier · 5 Jul · EUR 10.00"))
        self.assertNotIn("checked", end["text"])
        self.assertEqual([b["label"] for b in end["buttons"]], ["Get package"])


class OwnMailInvoiceIsACandidate(StoreCase):
    """Astra d2 S2: an own-mail invoice filed with its reading is matched in the run."""

    def test_an_own_mail_invoice_is_matched_in_the_same_run(self):
        self.bind()
        drv = JobDriver(self, payments=1)                    # Zapier EUR 10.00, 5 Jul
        drv.gmail.own(1000, day="2026-07-05", number="ZAP-OWN", issuer="Zapier")
        units = drv.run_job("d2d2d2d2-c1")
        doc = self.conn.execute("SELECT * FROM documents").fetchone()
        self.assertEqual((doc["amount_minor"], doc["currency"], doc["document_date"],
                          doc["issuer"], doc["document_number"], doc["vendor"]),
                         (1000, "EUR", "2026-07-05", "Zapier", "ZAP-OWN", None))
        (v,) = [u for u in units if u["unit"] == "payment"]
        pay = v
        self.assertEqual([c["doc_id"] for c in pay["candidates"]], [doc["doc_id"]])
        self.assertIsNone(pay["exact_fit"])                  # vendorless: never exact_fit
        self.assertEqual(self.conn.execute("SELECT status FROM projections").fetchone()[0],
                         "matched")
        self.assertEqual(drv.gmail.searches[1:], [])         # no vendor search was needed
