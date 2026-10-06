"""S7 §14: schema 11 — readings, tap keys, account choices, post offers; claims.seq,
package_requests.asked_seq, runs.completed_at; the drain and cancel records gone; open
email asks re-pointed to telegram; a staged email send settled uncertain."""
import json, sqlite3
from tests._base import StoreCase


class Schema11(StoreCase):
    def cols(self, table):
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def test_version_tables_and_columns(self):
        import db
        self.assertEqual(db.SCHEMA_VERSION, 11)
        self.assertTrue({"reading_id", "key", "text", "quoted", "render_id", "plan_json",
                         "created_seq", "created_at", "state", "settled_at"}
                        <= self.cols("readings"))
        self.assertTrue({"key", "render_id", "action", "pid", "created_at", "spent_at"}
                        <= self.cols("render_keys"))
        self.assertTrue({"key", "n", "account_id", "label", "created_at", "spent_at"}
                        <= self.cols("account_choices"))
        self.assertTrue({"render_id", "job_id", "n"} <= self.cols("post_offers"))
        self.assertIn("seq", self.cols("claims"))
        self.assertIn("asked_seq", self.cols("package_requests"))
        self.assertIn("completed_at", self.cols("runs"))

    def test_reading_states_are_closed(self):
        import db
        with self.assertRaises(sqlite3.IntegrityError):
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO readings(key, text, plan_json, created_seq,"
                                  " created_at, state) VALUES ('k','t','[]',1,'x','pending')")

    def test_savepoint_rolls_back_only_its_own_writes(self):
        import db
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO meta(key, value) VALUES ('a','1')")
            with self.assertRaises(db.Refusal):
                with db.savepoint(self.conn, "clause"):
                    self.conn.execute("INSERT INTO meta(key, value) VALUES ('b','1')")
                    raise db.Refusal("no")
        keys = {r[0] for r in self.conn.execute("SELECT key FROM meta")}
        self.assertIn("a", keys)
        self.assertNotIn("b", keys)

    def test_migration_from_10(self):
        """A v0.9.0 store: a drain, a cancel record, an open email package ask, a staged
        email send. After 10 -> 11: no drain, no cancel record; the open ask is telegram
        and keeps its created_seq as asked_seq; the staged email send is uncertain with
        its notice raised; claims.seq is NULL (read as 0)."""
        import db
        from tests.schema_history import build_v10_store
        # a path of its own: StoreCase.setUp already opened a schema-11 store at the
        # default path (plan round 2, Astra S2)
        path = self.tmp / "v10" / "accounting.sqlite"
        path.parent.mkdir()
        build_v10_store(path, drain="aaaaaaaa-1", cancelled="bbbbbbbb-2",
                        email_request=True, staged_email=True)
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        keys = {r[0] for r in conn.execute("SELECT key FROM meta")}
        self.assertNotIn("drain", keys)
        self.assertFalse(any(k.startswith("cancelled:") for k in keys))
        r = conn.execute("SELECT channel, asked_seq, created_seq FROM package_requests"
                         " WHERE state='queued'").fetchone()
        self.assertEqual(r["channel"], "telegram")
        self.assertEqual(r["asked_seq"], r["created_seq"])
        d = conn.execute("SELECT status, withdrawn_at FROM deliveries WHERE channel='email'"
                         ).fetchone()
        self.assertEqual(d["status"], "uncertain")
        self.assertIsNotNone(d["withdrawn_at"])
        a = conn.execute("SELECT kind FROM alerts WHERE sent_at IS NULL").fetchall()
        self.assertIn("package-uncertain", [x[0] for x in a])
        self.assertIsNone(conn.execute("SELECT seq FROM claims").fetchone()["seq"])

    def test_fresh_ddl_equals_migrated(self):
        import db
        from tests.schema_history import build_v10_store
        path = self.tmp / "old" / "accounting.sqlite"
        path.parent.mkdir()
        build_v10_store(path)
        old = sqlite3.connect(path)
        old.row_factory = sqlite3.Row
        db.migrate(old)
        def shape(c):
            return sorted((r["name"], tuple(sorted((x[1], x[2], x[3], x[5])
                          for x in c.execute(f"PRAGMA table_info({r['name']})"))))
                          for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        self.assertEqual(shape(old), shape(self.conn))

    def test_reset_store_wipes_the_new_tables(self):
        import binding, db
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO render_keys(key, render_id, action, created_at)"
                              " VALUES ('k','r1','all-good','x')")
            self.conn.execute("INSERT INTO post_offers(render_id, job_id, n) VALUES ('r1','j',1)")
        binding.reset_store(self.conn)
        for t in ("readings", "render_keys", "account_choices", "post_offers"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 0, t)
