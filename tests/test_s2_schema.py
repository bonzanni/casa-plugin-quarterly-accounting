from tests._base import StoreCase


class Schema10(StoreCase):
    def cols(self, table):
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def test_version_and_new_tables(self):
        import db
        self.assertGreaterEqual(db.SCHEMA_VERSION, 10)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
        self.assertTrue({"gen", "job_id", "at", "batch", "closed"} <= self.cols("claims"))
        # simple loop §4 (schema 12): the batch budget and INV-J8's credits are gone
        self.assertFalse({"spent", "reported"} & self.cols("claims"))  # removed-name: asserted absent
        self.assertEqual(self.cols("credits"), set())                # removed-name: asserted absent
        self.assertTrue({"job_id", "completed_at"} <= self.cols("runs"))
        self.assertNotIn("passes", self.cols("runs"))
        self.assertTrue({"request_id", "kind", "trigger", "doc_ids_json", "created_seq",
                         "state", "pass_id", "outcome", "render_ids_json"}
                        <= self.cols("work_requests"))
        self.assertNotIn("verdicts_json", self.cols("work_requests"))  # removed-name: asserted absent

    def test_new_columns(self):
        self.assertTrue({"protocol", "holder_job", "acq", "acq_gen"} <= self.cols("passes"))
        # simple loop §4 (schema 12): adoption, W, the judge and late takes are gone
        self.assertFalse({"orphaned_by", "adoptions", "adopters_json", "read_seq",  # removed-name: asserted absent
                          "w_refreshes", "judge_after", "judge_epoch", "late_takes",  # removed-name: asserted absent
                          "w_pending"} & self.cols("passes"))  # removed-name: asserted absent
        self.assertEqual(self.cols("pass_steps"), set())             # removed-name: asserted absent
        self.assertTrue({"job_id", "acq", "export_ref"} <= self.cols("snapshots"))
        self.assertFalse({"read_seq", "swept_at"} & self.cols("snapshots"))  # removed-name: asserted absent
        self.assertFalse({"readback_owed", "note_issued_gen", "note_other_issued_gen",  # removed-name: asserted absent
                          "note_seen_gen"} & self.cols("projections"))  # removed-name: asserted absent
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

    def test_reset_store_wipes_claims_and_runs(self):
        import binding, db
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO runs(job_id) VALUES ('aaaaaaaa-1')")
            self.conn.execute("INSERT INTO claims(gen, job_id, at, batch) VALUES"
                              " (1, 'aaaaaaaa-1', 'x', 1)")
        binding.reset_store(self.conn)
        for t in ("runs", "claims"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0)
