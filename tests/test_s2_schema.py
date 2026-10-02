from tests._base import StoreCase


class Schema10(StoreCase):
    def cols(self, table):
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def test_version_and_new_tables(self):
        import db
        self.assertEqual(db.SCHEMA_VERSION, 10)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "10")
        self.assertTrue({"gen", "job_id", "at", "spent", "measure_json", "reported"}
                        <= self.cols("claims"))
        self.assertTrue({"request_id", "kind", "trigger", "doc_ids_json", "created_seq",
                         "state", "pass_id", "outcome", "render_ids_json", "verdicts_json"}
                        <= self.cols("work_requests"))

    def test_new_columns(self):
        self.assertTrue({"protocol", "holder_job", "orphaned_by", "adoptions", "adopters_json",
                         "acq",
                         "acq_gen", "read_seq", "w_refreshes", "judge_after", "judge_pages"}
                        <= self.cols("passes"))
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
