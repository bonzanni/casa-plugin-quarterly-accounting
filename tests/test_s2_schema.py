from tests._base import StoreCase


class Schema10(StoreCase):
    def cols(self, table):
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def test_version_and_new_tables(self):
        import db
        self.assertEqual(db.SCHEMA_VERSION, 11)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "11")
        self.assertTrue({"gen", "job_id", "at", "spent", "reported", "batch", "closed"}
                        <= self.cols("claims"))
        self.assertNotIn("measure_json", self.cols("claims"))     # INV-J8 is credits (§15)
        self.assertEqual({"pass_id", "key", "gen"}, self.cols("credits"))
        self.assertEqual({"job_id", "passes", "completed"}, self.cols("runs"))
        self.assertTrue({"request_id", "kind", "trigger", "doc_ids_json", "created_seq",
                         "state", "pass_id", "outcome", "render_ids_json", "verdicts_json"}
                        <= self.cols("work_requests"))

    def test_new_columns(self):
        self.assertTrue({"protocol", "holder_job", "orphaned_by", "adoptions", "adopters_json",
                         "acq",
                         "acq_gen", "read_seq", "w_refreshes", "judge_after", "judge_epoch",
                         "late_takes"}
                        <= self.cols("passes"))
        self.assertFalse({"judge_high", "judge_pages"} & self.cols("passes"))
        self.assertTrue({"protocol", "started_seq", "started_gen"} <= self.cols("pass_steps"))
        self.assertIn("w_pending", self.cols("passes"))
        self.assertTrue({"job_id", "read_seq", "acq", "export_ref", "swept_at"}
                        <= self.cols("snapshots"))
        self.assertTrue({"readback_owed", "note_issued_gen", "note_other_issued_gen",
                         "note_seen_gen"}
                        <= self.cols("projections"))
        self.assertIn("gen", self.cols("probes"))

    def test_migration_from_9_closes_a_live_delegation_pass(self):
        import db, passes, pathlib, sqlite3
        # a schema-9 store with a live delegation pass, built by the 0.8.0 DDL kept in
        # tests/schema_history.py (V9_DDL), then opened by this code
        path = pathlib.Path(self.tmp) / "v9.sqlite"
        from tests.schema_history import build_v9_store
        build_v9_store(path, live_pass=True)
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        m = conn.execute("SELECT live FROM pass_marker").fetchone()
        self.assertEqual(m["live"], 0)
        p = conn.execute("SELECT outcome FROM passes ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(p["outcome"], "interrupted")
        # the old token is refused everywhere
        with self.assertRaises(db.Refusal):
            with db.tx(conn):
                passes.check_token(conn, 1)

    def test_reset_store_wipes_credits_and_runs(self):
        import binding, db
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO credits(pass_id, key, gen) VALUES ('p', 'file:unit', 1)")
            self.conn.execute("INSERT INTO runs(job_id, passes) VALUES ('aaaaaaaa-1', 4)")
        binding.reset_store(self.conn)
        for t in ("credits", "runs", "claims"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0)
