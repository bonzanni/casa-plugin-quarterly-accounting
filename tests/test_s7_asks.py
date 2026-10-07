# tests/test_s7_asks.py
"""S7 §4: every ask returns its request_id and kind; ask_state says whether the live run
will take it; desk filing."""
import re

from tests._base import StoreCase


class Asks(StoreCase):
    def test_an_ask_returns_its_id_and_kind(self):
        import asks
        w = asks.request_work(self.conn, "check", "operator")
        self.assertEqual((w["kind"], type(w["request_id"])), ("work", int))

    def test_ask_state_queued_without_a_live_run_says_busy_just_now(self):
        import asks, db, job
        r = asks.request_work(self.conn, "check", "operator")
        self.run_job_to_complete("aaaaaaaa-1")       # a run that answered complete
        r2 = asks.request_work(self.conn, "check", "operator")
        s = asks.ask_state(self.conn, "work", r2["request_id"])
        self.assertEqual((s["state"], s["live_run"]), ("queued", False))
        self.assertEqual(s["line"], asks.BUSY_NO_RESULT)

    def test_ask_state_with_a_live_run_says_the_asks_line(self):
        import asks, job
        r = asks.request_work(self.conn, "check", "operator")
        job.claim(self.conn, "aaaaaaaa-1")            # a live run, not complete
        r2 = asks.request_work(self.conn, "check", "operator")
        s = asks.ask_state(self.conn, "work", r2["request_id"])
        self.assertTrue(s["live_run"])
        self.assertEqual(s["line"], asks.LINES["check"])

    def test_job_status_is_done_once_the_run_completed(self):
        """T8-a (§10), simple loop Task 10: job_status says `done` once the run answered
        `complete` (runs.completed_at) — never for a live run; it stamps nothing itself."""
        import asks, job
        self.bind()
        job.claim(self.conn, "aaaaaaaa-1")
        self.assertFalse(job.status(self.conn, "aaaaaaaa-1")["done"])
        self.assertIsNone(self.conn.execute("SELECT completed_at FROM runs WHERE job_id=?",
                                            ("aaaaaaaa-1",)).fetchone()[0])
        self.run_job_to_complete("bbbbbbbb-2")
        out = job.status(self.conn, "bbbbbbbb-2")
        self.assertTrue(out["done"])
        # #47; 0.11.2: job.CARD_POSTED after a delivered end card
        self.assertRegex(out["text"], r"^Q\d \d{4} checked: |^Nothing to check |"
                                      + re.escape(job.CARD_POSTED))
        self.assertFalse(asks._live_run(self.conn))

    def test_ask_state_taken_done_and_refusals(self):
        """T8-a: a taken work ask says its line; a finished one is done; unknown kinds and
        ids are refused in words."""
        import asks, db
        r = asks.request_work(self.conn, "check", "operator")
        with db.tx(self.conn):
            self.conn.execute("UPDATE work_requests SET state='taken' WHERE request_id=?",
                              (r["request_id"],))
        s = asks.ask_state(self.conn, "work", r["request_id"])
        self.assertEqual((s["state"], s["line"]), ("taken", asks.LINES["check"]))
        with db.tx(self.conn):
            self.conn.execute("UPDATE work_requests SET state='done' WHERE request_id=?",
                              (r["request_id"],))
        s = asks.ask_state(self.conn, "work", r["request_id"])
        self.assertEqual((s["state"], s["line"]), ("done", asks.DONE_ALREADY))
        # simple loop §4: package asks are gone with their requests
        for kind, rid in (("email", r["request_id"]), ("package", r["request_id"]),
                          ("work", 9999)):
            with self.assertRaises(db.Refusal):
                asks.ask_state(self.conn, kind, rid)

    def test_desk_filing_needs_no_token_and_resident_is_refused(self):
        import documents, db
        path = self.publish("inv.pdf", b"%PDF-1.4 x", producer="casa")
        out = documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                        source="manual-telegram", extraction_author="desk")
        self.assertIn("doc_id", out)
        with self.assertRaises(db.Refusal):
            documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                      source="manual-telegram", extraction_author="resident")
