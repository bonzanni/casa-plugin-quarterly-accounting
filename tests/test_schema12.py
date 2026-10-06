"""Simple loop §3: schema 12 — the run's work list and mirror calls, the quarter's
ready notice, mirror_note, the filing vendor, alternatives, keyed documents, item states,
the Gmail streak, batch progress, the learned hint."""
import sqlite3
from tests._base import StoreCase


class Schema12(StoreCase):
    def cols(self, table, conn=None):
        return {r[1] for r in (conn or self.conn).execute(f"PRAGMA table_info({table})")}

    def test_version_tables_and_columns(self):
        import db
        self.assertEqual(db.SCHEMA_VERSION, 12)
        self.assertTrue({"job_id", "pid", "vendor", "why", "outcome", "reason", "handed", "hinted",
                         "plain"}
                        <= self.cols("run_work"))
        self.assertTrue({"job_id", "n", "tool", "args_json", "pids_json", "state", "error"}
                        <= self.cols("run_mirror"))
        self.assertTrue({"quarter", "sig", "times", "render_id"}
                        <= self.cols("quarter_notices"))
        self.assertTrue({"render_id", "pid", "item_state"} <= self.cols("render_states"))
        for table, col in (("projections", "mirror_note"), ("projections", "considered_seq"),
                           ("documents", "vendor"),
                           ("documents", "filed_seq"), ("matches", "alternatives_json"),
                           ("render_keys", "doc_id"), ("render_states", "item_state"),
                           ("probes", "fail_runs"), ("claims", "progressed"),
                           ("counterparties", "hint_sender"),
                           ("counterparties", "hint_subject"), ("runs", "started_by"),
                           ("runs", "end_render_id"), ("runs", "partial"),
                           ("runs", "mirror_at"), ("runs", "filed_at"),
                           ("runs", "listed_at")):
            self.assertIn(col, self.cols(table), f"{table}.{col}")

    def test_sqlite_can_drop_columns(self):
        self.assertGreaterEqual(sqlite3.sqlite_version_info, (3, 35, 0))   # Task 11's drops

    def test_run_work_outcomes_are_closed(self):
        import db
        with self.assertRaises(sqlite3.IntegrityError):
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO run_work(job_id, pid, vendor, why, outcome)"
                                  " VALUES ('j', 1, 'Adobe', 'open', 'not-needed')")

    def test_migration_from_11_keeps_data_and_fresh_equals_migrated(self):
        import db
        from tests.schema_history import build_v11_store
        path = self.tmp / "v11" / "accounting.sqlite"
        path.parent.mkdir()
        build_v11_store(path, with_rows=True)
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                         .fetchone()[0], "12")
        self.assertEqual(conn.execute("SELECT alternatives_json FROM matches").fetchone()[0],
                         "[]")
        self.assertIsNone(conn.execute("SELECT mirror_note FROM projections").fetchone()[0])

        def shape(c):
            return sorted((r["name"], tuple(sorted((x[1], x[2], x[3], x[5])
                          for x in c.execute(f"PRAGMA table_info({r['name']})"))))
                          for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                             " AND name<>'sqlite_sequence'"))
        self.assertEqual(shape(conn), shape(self.conn))

    def test_reset_store_wipes_the_run_tables(self):
        import binding, db
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO run_work(job_id, pid, vendor, why) VALUES"
                              " ('j', 1, 'Adobe', 'open')")
            self.conn.execute("INSERT INTO quarter_notices(quarter, sig) VALUES"
                              " ('2026-Q3', 'x')")
            self.conn.execute("INSERT INTO render_states(render_id, pid, item_state) VALUES"
                              " ('r1', 1, 'missing')")
        binding.reset_store(self.conn)
        for t in ("run_work", "run_mirror", "render_states", "quarter_notices"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0, t)
        with db.tx(self.conn):          # the sequence restarted: the same render id again
            self.conn.execute("INSERT INTO render_states(render_id, pid, item_state) VALUES"
                              " ('r1', 1, 'missing')")

    def test_run_claim_makes_a_real_run(self):
        """The one fixture later tasks start from: a real claim, pass, run and probes."""
        import job
        token = self.run_claim(started_by="cron")
        self.assertEqual(token, self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0])
        self.assertRegex(self.job_id, job.JOB_ID_RE)
        run = self.conn.execute("SELECT started_by, pass_id FROM runs WHERE job_id=?",
                                (self.job_id,)).fetchone()
        self.assertEqual((run[0], run[1]), ("scheduled", self.pass_id))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM probes").fetchone()[0], 4)
        first = self.job_id
        self.run_claim()
        self.assertNotEqual(first, self.job_id)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
        self.work_rows([1, 2])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_work WHERE job_id=?",
                                           (self.job_id,)).fetchone()[0], 2)
