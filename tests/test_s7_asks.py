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

    def test_job_status_stamps_completed_at_only_when_done(self):
        """T8-a (§10): job_status stamps the run complete exactly when it may end — a run
        completed through a topic turn is not live, so its asks are closed at the next
        claim; a run that may not end stays live."""
        import asks, db, job
        self.bind()
        job.claim(self.conn, "aaaaaaaa-1")
        with db.tx(self.conn):
            self.conn.execute("INSERT OR IGNORE INTO runs(job_id, passes) VALUES (?, 0)",
                              ("aaaaaaaa-1",))
        asks.request_work(self.conn, "check", "operator")      # work left: not done
        self.assertFalse(job.status(self.conn, "aaaaaaaa-1")["done"])
        self.assertIsNone(self.conn.execute("SELECT completed_at FROM runs WHERE job_id=?",
                                            ("aaaaaaaa-1",)).fetchone()[0])
        self.run_job_to_complete("bbbbbbbb-2")
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET completed_at=NULL WHERE job_id=?",
                              ("bbbbbbbb-2",))
        out = job.status(self.conn, "bbbbbbbb-2")
        self.assertTrue(out["done"])
        stamp = self.conn.execute("SELECT completed_at FROM runs WHERE job_id=?",
                                  ("bbbbbbbb-2",)).fetchone()[0]
        self.assertIsNotNone(stamp)
        job.status(self.conn, "bbbbbbbb-2")                   # a second answer keeps it
        self.assertEqual(self.conn.execute("SELECT completed_at FROM runs WHERE job_id=?",
                                           ("bbbbbbbb-2",)).fetchone()[0], stamp)
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
        for kind, rid in (("email", r["request_id"]), ("work", 9999)):
            with self.assertRaises(db.Refusal):
                asks.ask_state(self.conn, kind, rid)

    def test_ask_state_for_a_package_ask(self):
        """T8-a: a package ask being checked is `taken` with its own line; delivered, done."""
        import asks, db
        p = asks.request_package(self.conn, "2026-Q3")
        with db.tx(self.conn):
            self.conn.execute("UPDATE package_requests SET state='snapshot' WHERE"
                              " request_id=?", (p["request_id"],))
        s = asks.ask_state(self.conn, "package", p["request_id"])
        self.assertEqual(s["state"], "taken")
        self.assertIn("for Q3 2026", s["line"])
        self.assertIn("the package follows", s["line"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE package_requests SET state='delivered' WHERE"
                              " request_id=?", (p["request_id"],))
        self.assertEqual(asks.ask_state(self.conn, "package", p["request_id"])["line"],
                         asks.DONE_ALREADY)

    def test_desk_filing_needs_no_token_and_resident_is_refused(self):
        import documents, db
        path = self.publish("inv.pdf", b"%PDF-1.4 x", producer="casa")
        out = documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                        source="manual-telegram", extraction_author="desk")
        self.assertIn("doc_id", out)
        with self.assertRaises(db.Refusal):
            documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                      source="manual-telegram", extraction_author="resident")
