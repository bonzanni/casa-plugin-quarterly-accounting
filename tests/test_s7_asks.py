# tests/test_s7_asks.py
"""S7 §4: every ask returns its request_id and kind; a repeated package ask renews
asked_seq; ask_state says whether the live run will take it; no email; desk filing."""
from tests._base import StoreCase


class Asks(StoreCase):
    def test_both_asks_return_their_id_and_kind(self):
        import asks
        w = asks.request_work(self.conn, "check", "operator")
        self.assertEqual((w["kind"], type(w["request_id"])), ("work", int))
        p = asks.request_package(self.conn, "2026-Q3")
        again = asks.request_package(self.conn, "2026-Q3")
        self.assertEqual(again["status"], "already")
        self.assertEqual(again["request_id"], p["request_id"])
        self.assertEqual(again["kind"], "package")

    def test_a_repeated_package_ask_renews_asked_seq(self):
        import asks
        p = asks.request_package(self.conn, "2026-Q3")
        first = self.conn.execute("SELECT asked_seq FROM package_requests").fetchone()[0]
        asks.request_package(self.conn, "2026-Q3")
        second = self.conn.execute("SELECT asked_seq FROM package_requests").fetchone()[0]
        self.assertGreater(second, first)

    def test_no_channel_and_never_email(self):
        import asks
        asks.request_package(self.conn, "2026-Q3")
        self.assertEqual(self.conn.execute("SELECT channel FROM package_requests").fetchone()[0],
                         "telegram")
        import tools, qa_server  # noqa: F401
        self.assertNotIn("channel", qa_server.TOOLS["request_package"]["schema"]["properties"])

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

    def test_desk_filing_needs_no_token_and_resident_is_refused(self):
        import documents, db
        path = self.publish("inv.pdf", b"%PDF-1.4 x", producer="casa")
        out = documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                        source="manual-telegram", extraction_author="desk")
        self.assertIn("doc_id", out)
        with self.assertRaises(db.Refusal):
            documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                      source="manual-telegram", extraction_author="resident")
