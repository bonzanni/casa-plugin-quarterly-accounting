import multiprocessing
import sqlite3
import unittest

from tests._base import TempEnv
from tests import _procs
import db  # noqa: E402


class TestSchema(TempEnv):
    def test_open_creates_every_table_once_and_is_idempotent(self):
        c1 = db.open_store()
        self.addCleanup(c1.close)
        c2 = db.open_store()
        self.addCleanup(c2.close)
        names = {r[0] for r in c2.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for t in ("meta", "counters", "binding", "pass_marker", "passes", "probes",
                  "documents", "document_status", "counterparties", "chain_overrides",
                  "snapshots", "bank_rows", "projections", "aliases", "cursor", "matches",
                  "log", "match_state", "residue", "renders", "render_items", "shown",
                  "packages", "deliveries", "delivered_rows", "alerts"):
            self.assertIn(t, names, t)
        self.assertEqual(c1.execute("SELECT value FROM meta WHERE key='schema_version'")
                         .fetchone()[0], str(db.SCHEMA_VERSION))
        self.assertEqual(c1.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def _released_store(self, ddl: str, version: int):
        """A store exactly as a released plugin of schema `version` left it, holding
        a delivered and an undelivered rendering, one import, one classified
        lineage, and a package with an unsent (staged) first send."""
        path = db.data_dir() / db.DB_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(str(path), isolation_level=None)
        for stmt in db._statements(ddl):
            old.execute(stmt)
        old.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(version),))
        old.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, delivered_at,"
                    " text, membership_json) VALUES ('r-old','status','{}','2030-01-01T00:00:00Z',"
                    "'2030-01-01T00:00:00Z','old words','[]')")
        old.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                    " text, membership_json) VALUES ('r-new','status','{}','2026-01-01T00:00:00Z',"
                    "'','[]')")
        old.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                    " VALUES ('p', '2026-09-01T00:00:00Z', 1, 1)")
        old.execute("INSERT INTO projections(dest_row_id, admitted_at, class_tags_json,"
                    " class_observed_at) VALUES (1, '2026-09-01T00:00:00Z', '[\"software\"]',"
                    " '2026-09-02T00:00:00Z')")
        old.execute("INSERT INTO packages(quarter, filename, path, built_at, partial, digest,"
                    " size, caption, manifest_json) VALUES ('2026-Q3', 'q3.zip', '/x/q3.zip',"
                    " '2026-09-03T00:00:00Z', 1, 'dg', 10, 'cap', '[]')")
        old.execute("INSERT INTO deliveries(package_id, channel, staged_path, status, created_at)"
                    " VALUES (1, 'telegram', '/x/out/q3.zip', 'staged', '2026-09-03T00:00:00Z')")
        return old

    def _assert_current_behaviour(self, c, old_seq: int, deliveries=1, first_sent=False):
        self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema_version'")
                         .fetchone()[0], str(db.SCHEMA_VERSION))
        self.assertEqual(db.SCHEMA_VERSION, 6)
        # the migrated store has every column and index a fresh store has
        fresh = sqlite3.connect(":memory:")
        self.addCleanup(fresh.close)
        for stmt in db._statements(db.DDL):
            fresh.execute(stmt)

        def columns(conn):
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                                 " AND name NOT LIKE 'sqlite_%'")]
            return {t: sorted(r[1] for r in conn.execute(f"PRAGMA table_info({t})"))
                    for t in tables}
        self.assertEqual(columns(c), columns(fresh))

        def indexes(conn):
            return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE"
                                                     " type='index' AND sql IS NOT NULL"))
        self.assertEqual(indexes(c), indexes(fresh))
        # the data survived
        self.assertEqual(c.execute("SELECT count(*) FROM renders").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT text FROM renders WHERE render_id='r-old'")
                         .fetchone()[0], "old words")
        self.assertEqual(c.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT count(*) FROM packages").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT count(*) FROM deliveries").fetchone()[0], deliveries)
        # fix wave D: the delivery sequence — the old delivery orders below any later one
        self.assertEqual(c.execute("SELECT delivered_seq FROM renders WHERE render_id='r-old'")
                         .fetchone()[0], old_seq)
        self.assertEqual(db.last_delivered(c)["render_id"], "r-old")
        import views
        views.mark_rendering_delivered(c, "r-new")
        self.assertEqual(db.last_delivered(c)["render_id"], "r-new")
        # fix E2: every migrated lineage is non-fresh until the sweep re-reads it
        import lineage
        p = lineage.projection(c, 1)
        self.assertEqual(p["class_tags_json"], '["software"]')
        self.assertEqual((p["class_observed_snapshot"], p["observed_revision"]), (None, None))
        # issue #1: a migrated lineage has no note confirmation, so it is read once
        self.assertEqual(tuple(p[k] for k in ("export_tag_revision", "note_seen_seq",
                                              "note_seen_rev", "note_seen_at",
                                              "note_issued_at", "read_snapshot")),
                         (None,) * 6)
        # issue #14: the store's epoch is the upgrade; no issue of an unknown revision
        self.assertEqual((p["note_issued_seq"], p["note_other_issued_at"]), (None, None))
        self.assertIsNotNone(db.epoch(c))
        self.assertFalse(lineage.is_fresh(c, p))
        # fix E4/E5: a package built before the migration names no import, so its
        # first send is refused (build it again); the unsent send is not revoked yet
        pk = c.execute("SELECT snapshot_id, digest FROM packages WHERE package_id=1").fetchone()
        self.assertEqual((pk[0], pk[1]), (None, "dg"))
        self.assertIsNone(c.execute("SELECT revoked_at FROM deliveries").fetchone()[0])
        import delivery
        if not first_sent:
            with self.assertRaises(db.Refusal):
                delivery._require_current_snapshot(c, 1)

    def test_a_schema_1_store_migrates_to_current_keeping_its_data(self):
        # schema 1 as released (6509806): no delivered_seq, no freshness, no package snapshot
        from tests.schema_history import DDL_V1
        self.assertNotIn("delivered_seq", DDL_V1)
        self.assertNotIn("class_observed_snapshot", DDL_V1)
        self._released_store(DDL_V1, 1).close()
        c = db.open_store()
        self.addCleanup(c.close)
        self._assert_current_behaviour(c, old_seq=0)
        c.close()
        c2 = db.open_store()                               # idempotent: a second open migrates nothing
        self.addCleanup(c2.close)
        self.assertEqual(c2.execute("SELECT value FROM meta WHERE key='schema_version'")
                         .fetchone()[0], "6")

    def test_a_fix_d_schema_2_store_migrates_to_current_keeping_its_sequence(self):
        # schema 2 as fix wave D shipped it (b055022): delivered_seq, no freshness
        from tests.schema_history import DDL_V2
        self.assertIn("delivered_seq", DDL_V2)
        self.assertNotIn("class_observed_snapshot", DDL_V2)
        old = self._released_store(DDL_V2, 2)
        old.execute("UPDATE renders SET delivered_seq=7 WHERE render_id='r-old'")
        old.execute("INSERT INTO counters(name, value) VALUES ('seq', 7)"
                    " ON CONFLICT(name) DO UPDATE SET value=7")
        old.close()
        c = db.open_store()
        self.addCleanup(c.close)
        self._assert_current_behaviour(c, old_seq=7)
        self.assertGreater(c.execute("SELECT delivered_seq FROM renders WHERE render_id='r-new'")
                           .fetchone()[0], 7)

    def test_a_v0_1_0_schema_3_store_migrates_to_current_keeping_its_data(self):
        # schema 3 as v0.1.0 shipped it (e9b4eff): no pass steps, no package requests,
        # and a resend could reuse the outbox name of an earlier send of the same bytes
        from tests.schema_history import DDL_V3
        self.assertIn("class_observed_snapshot", DDL_V3)
        self.assertNotIn("pass_steps", DDL_V3)
        old = self._released_store(DDL_V3, 3)
        old.execute("UPDATE renders SET delivered_seq=0 WHERE render_id='r-old'")
        old.execute("UPDATE deliveries SET status='uncertain'")
        old.execute("INSERT INTO deliveries(package_id, channel, staged_path, status, created_at)"
                    " VALUES (1, 'telegram', '/x/out/q3.zip', 'staged', '2026-09-04T00:00:00Z')")
        old.execute("INSERT INTO counters(name, value) VALUES ('pass_generation', 5)"
                    " ON CONFLICT(name) DO UPDATE SET value=5")
        old.execute("INSERT INTO pass_marker(id, generation, live, pass_id, trigger, started_at)"
                    " VALUES (1, 5, 1, 'p5', 'cron', '2026-09-05T00:00:00Z')")
        old.execute("INSERT INTO passes(pass_id, generation, trigger, started_at)"
                    " VALUES ('p5', 5, 'cron', '2026-09-05T00:00:00Z')")
        old.close()
        c = db.open_store()
        self.addCleanup(c.close)
        self._assert_current_behaviour(c, old_seq=0, deliveries=2, first_sent=True)
        # the live pass keeps its token; nothing is claimed; its reply defaults
        m = c.execute("SELECT * FROM pass_marker").fetchone()
        self.assertEqual((m["generation"], m["live"], m["claimed_step"], m["lease_at"]),
                         (5, 1, None, None))
        self.assertEqual(c.execute("SELECT reply FROM passes").fetchone()[0], "telegram")
        self.assertEqual(c.execute("SELECT count(*) FROM pass_steps").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT count(*) FROM package_requests").fetchone()[0], 0)
        # two sends that shared one outbox name: the newest keeps it, the older is
        # renamed out of the way, and from now on no two deliveries share a path
        paths = [r[0] for r in c.execute("SELECT staged_path FROM deliveries"
                                         " ORDER BY delivery_id")]
        self.assertEqual(paths, ["/x/out/q3.zip#1", "/x/out/q3.zip"])
        self.assertIsNone(c.execute("SELECT withdrawn_at FROM deliveries WHERE delivery_id=2")
                          .fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO deliveries(package_id, channel, staged_path, status,"
                      " created_at) VALUES (1, 'telegram', '/x/out/q3.zip', 'staged', 'x')")

    def test_a_v0_2_0_schema_4_store_migrates_to_current_keeping_its_data(self):
        # schema 4 as v0.2.0 shipped it (e79f77d): the import did not observe tags
        from tests.schema_history import DDL_V4
        self.assertIn("pass_steps", DDL_V4)
        self.assertNotIn("note_seen_seq", DDL_V4)
        old = self._released_store(DDL_V4, 4)
        old.execute("UPDATE renders SET delivered_seq=0 WHERE render_id='r-old'")
        old.close()
        c = db.open_store()
        self.addCleanup(c.close)
        self._assert_current_behaviour(c, old_seq=0)

    def test_a_v0_3_5_schema_5_store_migrates_to_current_keeping_its_data(self):
        # schema 5 as v0.3.5 shipped it (7cd8eda). Issue #14: an issue of unknown revision
        # is an "other" one, and the upgrade is the store's epoch. Issue #15: a request an
        # older version left buildable without its check goes back to `queued`, and a
        # package remembers the request it was built for
        from tests.schema_history import DDL_V5
        self.assertIn("note_seen_seq", DDL_V5)
        self.assertNotIn("note_issued_seq", DDL_V5)
        old = self._released_store(DDL_V5, 5)
        old.execute("UPDATE renders SET delivered_seq=0 WHERE render_id='r-old'")
        for rid, state, pkg, tok in ((1, "built", 1, 7), (2, "snapshot-done", None, 8),
                                     (3, "staged", None, 9), (4, "delivered", None, None),
                                     (5, "snapshot", None, None)):
            old.execute("INSERT INTO package_requests(request_id, quarter, channel, pass_id,"
                        " package_id, token, lease_at, state, created_at, updated_at) VALUES"
                        " (?, '2026-Q3', 'telegram', 'p', ?, ?, ?, ?, 'x', 'x')",
                        (rid, pkg, tok, "2026-09-03T00:00:00Z" if tok else None, state))
        old.execute("INSERT INTO projections(dest_row_id, admitted_at, note_issued_at) VALUES"
                    " (2, '2026-09-01T00:00:00Z', '2026-09-02T10:00:00Z')")
        old.close()
        c = db.open_store()
        self.addCleanup(c.close)
        self._assert_current_behaviour(c, old_seq=0)
        reqs = {r["request_id"]: dict(r) for r in c.execute("SELECT * FROM package_requests")}
        self.assertEqual({k: (v["state"], v["package_id"], v["token"], v["lease_at"])
                          for k, v in reqs.items()},
                         {1: ("queued", None, None, None), 2: ("queued", None, None, None),
                          3: ("staged", None, 9, "2026-09-03T00:00:00Z"),
                          4: ("delivered", None, None, None), 5: ("snapshot", None, None, None)})
        self.assertEqual(c.execute("SELECT request_id FROM packages WHERE package_id=1")
                         .fetchone()[0], 1)
        # the old build is never sent as a first send: its request went back to its check
        import delivery
        with self.assertRaises(db.Refusal) as cm:
            delivery.stage_for_delivery(c, channel="telegram", package_id=1, package_token=7)
        self.assertIn("no longer the one its request will send", str(cm.exception))
        import lineage
        p2 = lineage.projection(c, 2)
        self.assertEqual((p2["note_issued_seq"], p2["note_other_issued_at"]),
                         (None, "2026-09-02T10:00:00Z"))
        self.assertEqual(c.execute("SELECT as_built FROM deliveries").fetchone()[0], 0)

    def test_a_staged_path_is_unique_in_a_fresh_store(self):
        c = db.open_store()
        self.addCleanup(c.close)
        c.execute("INSERT INTO deliveries(channel, staged_path, status, created_at)"
                  " VALUES ('telegram', '/o/qa-1.zip', 'staged', 'x')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO deliveries(channel, staged_path, status, created_at)"
                      " VALUES ('email', '/o/qa-1.zip', 'delivered', 'x')")

    def test_a_newer_schema_is_refused(self):
        c = db.open_store()
        self.addCleanup(c.close)
        c.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        with self.assertRaises(RuntimeError):
            db.open_store()

    def test_active_document_is_unique_across_lineages(self):
        c = db.open_store()
        self.addCleanup(c.close)
        with db.tx(c):
            c.execute("INSERT INTO match_state(match_id,pid,doc_id,state,author,activation)"
                      " VALUES (1,1,7,'matched','auto',1)")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.tx(c):
                c.execute("INSERT INTO match_state(match_id,pid,doc_id,state,author,activation)"
                          " VALUES (2,2,7,'proposed','auto',2)")
        with db.tx(c):   # a conflicted record holds no slot
            c.execute("INSERT INTO match_state(match_id,pid,doc_id,state,author,activation)"
                      " VALUES (3,2,7,'conflicted','auto',3)")

    def test_next_seq_requires_a_transaction(self):
        c = db.open_store()
        self.addCleanup(c.close)
        with self.assertRaises(AssertionError):
            db.next_seq(c)


class TestConcurrency(TempEnv):
    def test_concurrent_first_open_of_a_fresh_store_all_succeed(self):
        # A fresh path's very first WAL-mode conversion writes the file header,
        # so it can contend exactly like BEGIN IMMEDIATE does. Every sibling is
        # released by the barrier at once, on a path that does not exist yet
        # (never opened by this process, or any other, before this call).
        path = str(self.data / db.DB_NAME)
        ctx = multiprocessing.get_context("spawn")
        n = 8
        barrier = ctx.Barrier(n)
        procs = [ctx.Process(target=_procs.open_fresh, args=(path, barrier)) for _ in range(n)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        for p in procs:
            self.assertEqual(p.exitcode, 0, f"pid {p.pid} exited {p.exitcode}")

    def test_sequence_is_unique_across_processes(self):
        path = str(self.data / db.DB_NAME)
        seed = db.open_store(path)
        seed.close()
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_procs.allocate, args=(path, 40, q)) for _ in range(3)]
        for p in procs:
            p.start()
        seqs = [s for _ in procs for s in q.get(timeout=60)]
        for p in procs:
            p.join(60)
        self.assertEqual(len(seqs), 120)
        self.assertEqual(len(set(seqs)), 120)

    def test_contention_within_the_bound_waits_and_applies(self):
        path = str(self.data / db.DB_NAME)
        c = db.open_store(path)
        self.addCleanup(c.close)
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        p = ctx.Process(target=_procs.hold_lock, args=(path, 1.0, ready))
        p.start()
        ready.wait(30)
        with db.tx(c, bound_s=10):
            s = db.next_seq(c)
        p.join(30)
        self.assertGreater(s, 0)

    def test_contention_past_the_bound_is_an_error_and_applies_nothing(self):
        path = str(self.data / db.DB_NAME)
        c = db.open_store(path)
        self.addCleanup(c.close)
        before = c.execute("SELECT value FROM counters WHERE name='seq'").fetchone()[0]
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        p = ctx.Process(target=_procs.hold_lock, args=(path, 3.0, ready))
        p.start()
        ready.wait(30)
        with self.assertRaises(db.Busy):
            with db.tx(c, bound_s=0.3):
                db.next_seq(c)
        p.join(30)
        after = c.execute("SELECT value FROM counters WHERE name='seq'").fetchone()[0]
        self.assertEqual(before, after)


class _CommitFails:
    """A store connection whose COMMIT fails once (a full disk, an I/O error),
    as the persistent tools._CONN would meet it."""
    def __init__(self, conn, exc):
        self._conn, self._exc = conn, exc

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        if sql == "COMMIT" and self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc
        return self._conn.execute(sql, *args)


class TestTransactionFailure(TempEnv):
    """Fix wave F (6): a failed COMMIT must not leave the transaction open (every
    later call on the persistent connection answered "tx() does not nest" until a
    restart), and a ROLLBACK after SQLite already rolled back must not mask the
    original error."""
    def setUp(self):
        super().setUp()
        self.conn = db.open_store()
        self.addCleanup(self.conn.close)

    def value(self):
        return self.conn.execute("SELECT value FROM counters WHERE name='seq'").fetchone()[0]

    def test_a_failed_commit_rolls_back_reraises_and_the_next_call_works(self):
        before = self.value()
        wrapped = _CommitFails(self.conn, sqlite3.OperationalError("disk I/O error"))
        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
            with db.tx(wrapped):
                db.next_seq(self.conn)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.value(), before)               # nothing of it landed
        with db.tx(self.conn):                               # not wedged: no "does not nest"
            db.next_seq(self.conn)
        self.assertEqual(self.value(), before + 1)

    def test_an_error_after_sqlite_already_rolled_back_is_the_one_raised(self):
        with self.assertRaisesRegex(ValueError, "the original"):
            with db.tx(self.conn):
                self.conn.execute("ROLLBACK")                # SQLite gave the transaction up
                raise ValueError("the original")
        self.assertFalse(self.conn.in_transaction)
        with db.tx(self.conn):
            db.next_seq(self.conn)


if __name__ == "__main__":
    unittest.main()
