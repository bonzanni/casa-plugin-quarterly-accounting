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

    def test_a_newer_schema_is_refused(self):
        c = db.open_store()
        self.addCleanup(c.close)
        c.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        with self.assertRaises(RuntimeError):
            db.open_store()

    def test_a_schema_1_store_migrates_to_freshness_with_every_lineage_non_fresh(self):
        # the schema 1 store as released: the DDL without fix E2's two columns
        v1 = db.DDL
        for line in ("  class_observed_snapshot INTEGER,", "  observed_revision INTEGER,"):
            v1 = "\n".join(x for x in v1.splitlines() if not x.startswith(line))
        self.assertNotIn("observed_revision", v1)
        path = db.data_dir() / db.DB_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(str(path), isolation_level=None)
        for stmt in db._statements(v1):
            old.execute(stmt)
        old.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
        old.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                    " VALUES ('p', '2026-09-01T00:00:00Z', 1, 1)")
        old.execute("INSERT INTO projections(dest_row_id, admitted_at, class_tags_json,"
                    " class_observed_at) VALUES (1, '2026-09-01T00:00:00Z', '[\"software\"]',"
                    " '2026-09-02T00:00:00Z')")
        old.close()
        c = db.open_store()
        self.addCleanup(c.close)
        self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema_version'")
                         .fetchone()[0], "2")
        import lineage
        p = lineage.projection(c, 1)
        self.assertEqual((p["class_observed_snapshot"], p["observed_revision"]), (None, None))
        self.assertFalse(lineage.is_fresh(c, p))           # re-read before anything is decided
        c.close()
        c2 = db.open_store()                               # idempotent: a second open migrates nothing
        self.addCleanup(c2.close)
        self.assertEqual(c2.execute("SELECT value FROM meta WHERE key='schema_version'")
                         .fetchone()[0], "2")

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


if __name__ == "__main__":
    unittest.main()
