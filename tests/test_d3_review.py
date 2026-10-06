"""Diff round d3 (Astra, 26b68ee..a1a074d), the two accepted findings, reproduced through
the real surface (qa_server.TOOLS, a real bank-feed):
- Astra S1 (ruled: generalize — the 2nd instance after d2's own-mail filing): every unit
  carries a call budget (`max_calls`); at it the model checkpoints with job_next and the
  unit comes again as a continuation, not counted against queues.ATTEMPTS_MAX while it
  progressed (queues: an item closed or queued, a search kind first recorded);
  progress is reported on the first job_next after the batch persisted anything;
- Astra S2: the 11 -> 12 migration backfills render_states from delivered legacy
  renderings whose items are provably unchanged, so the first scheduled run after the
  upgrade does not announce them again."""
import json
import sqlite3

from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported

CASA_CALLS = 80


class EveryUnitHasABudget(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_astras_60_invoice_vendor_completes_reporting_every_batch(self):
        """60 Zapier payments, 60 invoices in the vendor's mail, Casa's 80-call cuts."""
        drv = JobDriver(self, payments=60)
        for i in range(60):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"INV-{i + 1}")
        drv.casa_cut = CASA_CALLS
        units = drv.run_job("d3d3d3d3-a1")
        self.assertEqual(drv.cuts, 0)
        self.assertLessEqual(max(drv.batch_calls), CASA_CALLS, drv.batch_calls)
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 60})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 60)
        run = self.conn.execute("SELECT * FROM runs WHERE job_id='d3d3d3d3-a1'").fetchone()
        self.assertIsNotNone(run["completed_at"])
        self.assertFalse(run["partial"])
        self.assertTrue(any(u["unit"] == "vendor" and u["continued"] for u in units))
        # a progress report mid-batch, not only at its end (the first answer after work)
        self.assertTrue(any(u["report"] and u["unit"] not in ("end-batch", "complete")
                            for u in units))

    def test_progress_is_reported_on_the_first_answer_after_work_once_per_batch(self):
        import job
        drv = JobDriver(self, payments=1)
        drv.claim("d3d3d3d3-a2")
        got = []
        for _ in range(4):
            u = job.next_unit(self.conn, drv.token, drv.calls)
            drv.calls += 1
            got.append((u["unit"], u["report"]))
            drv.do(u, drv.token)
        # the import persisted work: the next answer (filing) reports; filing persisted too,
        # but the batch already reported, so the vendor unit does not
        self.assertEqual(got, [("probes", False), ("snapshot", False), ("filing", True),
                               ("vendor", False)])
        self.assertTrue(all(u["max_calls"] > 0 for u in drv.units[-4:]))

    def test_a_continuation_that_progressed_is_not_counted(self):
        """Queues: a vendor hand-out that queues one more found attachment (a per-payment
        search recorded with its refs) and stops at its budget is handed again, more often
        than ATTEMPTS_MAX, until the group is decided; each attachment set aside closes."""
        import queues
        drv = JobDriver(self, payments=1)
        drv.gmail.invoice("Zapier", 1000, "EUR", drv.DATES[0], "INV-1")
        real, n = drv._vendor, [0]

        def partial(u, token):
            if u["files"]:                     # the attachment it queued: no invoice
                drv._tool("set_aside", {"pass_token": token, "reason": "no invoice",
                                        "items": [{"ref": r} for r in u["files"]]})
                return None
            n[0] += 1
            if n[0] <= 3:                      # one search with one find, then stops
                drv._tool("record_search", {"pass_token": token, "search": "payment",
                                            "pids": [p["pid"] for p in u["payments"]],
                                            "queries": [f"q{n[0]}"], "refs": [f"x-{n[0]}"]})
                return None
            return real(u, token)
        drv._vendor = partial
        units = drv.run_job("d3d3d3d3-a3")
        payments = [u for u in units if u["unit"] == "vendor" and u["payments"]]
        self.assertEqual(len(payments), 4)
        self.assertGreater(len(payments), queues.ATTEMPTS_MAX)
        self.assertEqual([u["continued"] for u in payments], [False, True, True, True])
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT key FROM run_items WHERE kind='ref' AND state='done' AND key LIKE 'x-%'")),
            ["x-1", "x-2", "x-3"])
        self.assertEqual(self.conn.execute("SELECT status FROM projections").fetchone()[0],
                         "matched")

    def test_a_vendor_unit_that_persists_nothing_still_ends_search_incomplete(self):
        drv = JobDriver(self, payments=1)
        drv._vendor = lambda u, token: None            # never persists anything
        units = drv.run_job("d3d3d3d3-a4")
        self.assertEqual(sum(u["unit"] == "vendor" for u in units), 2)
        run = self.conn.execute("SELECT * FROM runs WHERE job_id='d3d3d3d3-a4'").fetchone()
        self.assertIsNotNone(run["completed_at"])
        self.assertTrue(run["partial"])
        self.assertIn("missing · search incomplete", drv.posted_end("d3d3d3d3-a4")["text"])

    def test_a_filing_that_persists_nothing_ends_after_attempts_max(self):
        import queues
        drv = JobDriver(self, payments=1)
        drv._filing = lambda u, token: None            # never records the own-mail search
        units = drv.run_job("d3d3d3d3-a5")
        self.assertEqual(sum(u["unit"] == "filing" for u in units), queues.ATTEMPTS_MAX)
        self.assertEqual(units[-1]["unit"], "complete")
        self.assertIn("Your own mail was not read", drv.posted_end("d3d3d3d3-a5")["text"])


class MigratedDeliveredStates(StoreCase):
    """Astra d3 S2: a schema-11 store whose delivered missing view listed two payments."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=2)
        self.drv.run_job("d3d3d3d3-b0")                 # two missing payments, imported

    def v11(self, change=None):
        """This store's data as a schema-11 store (tests/schema_history.DDL_V11) holding a
        delivered legacy missing view of both payments at their current revisions — and no
        render_states, which schema 11 did not have. `change`: a pid whose revision moved
        after the delivery. The upgraded store replaces self.conn (the tools' too)."""
        import tools
        from tests.schema_history import build_v11_store
        pids = [r[0] for r in self.conn.execute("SELECT pid FROM projections ORDER BY pid")]
        path = self.tmp / "v11-upgrade" / "accounting.sqlite"
        path.parent.mkdir()
        build_v11_store(path)
        old = sqlite3.connect(path)
        old.execute("ATTACH DATABASE ? AS cur", (str(db.data_dir() / db.DB_NAME),))
        skip = {"meta", "render_states", "sqlite_sequence", "renders", "render_items"}
        for (t,) in old.execute("SELECT name FROM main.sqlite_master WHERE type='table'"
                                ).fetchall():
            if t in skip:
                continue
            have = {r[1] for r in old.execute(f"PRAGMA cur.table_info({t})")}
            if not have:
                continue
            cols = [r[1] for r in old.execute(f"PRAGMA main.table_info({t})") if r[1] in have]
            old.execute(f"DELETE FROM main.{t}")
            old.execute(f"INSERT INTO main.{t}({','.join(cols)}) SELECT {','.join(cols)}"
                        f" FROM cur.{t}")
        old.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, delivered_at,"
                    " text, membership_json, delivered_seq) VALUES ('legacy-1', 'view',"
                    " '{}', 'x', 'x', 'Q3 · 2 missing', ?, 1)", (json.dumps(pids),))
        for pid in pids:
            rev = old.execute("SELECT revision FROM projections WHERE pid=?",
                              (pid,)).fetchone()[0]
            old.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                        " match_revisions_json) VALUES ('legacy-1', ?, ?, '{}')", (pid, rev))
        if change is not None:
            old.execute("UPDATE projections SET revision=revision+1 WHERE pid=?", (change,))
        old.commit()
        old.execute("DETACH DATABASE cur")
        old.close()
        self.conn = db.open_store(path)
        self.addCleanup(self.conn.close)
        tools._CONN = self.conn
        self.drv.conn = self.conn
        return pids

    def scheduled(self, jid):
        before = self.conn.execute("SELECT count(*) FROM renders WHERE delivered_at IS NOT"
                                   " NULL").fetchone()[0]
        self.drv.run_job(jid, started_by="scheduled")
        rows = self.conn.execute("SELECT * FROM renders WHERE delivered_at IS NOT NULL"
                                 ).fetchall()
        return rows[before:]

    def test_the_upgrade_backfills_and_an_unchanged_scheduled_run_posts_nothing(self):
        pids = self.v11()
        self.assertEqual(self.scheduled("d3d3d3d3-b1"), [])     # nothing announced again
        self.assertEqual(sorted(tuple(r) for r in self.conn.execute(
            "SELECT pid, item_state FROM render_states WHERE render_id='legacy-1'")),
            [(p, "missing") for p in pids])

    def test_a_payment_changed_since_delivery_is_announced_alone(self):
        """Only a provably unchanged item (its revision still the delivered one) is
        backfilled: the payment that changed after the delivery is the one announced."""
        second = self.conn.execute("SELECT pid FROM projections ORDER BY pid").fetchall()[1][0]
        pids = self.v11(change=second)
        posted = self.scheduled("d3d3d3d3-b3")
        self.assertEqual(len(posted), 1)
        self.assertIn("new: 0 to confirm · 1 missing", posted[0]["text"])
        self.assertEqual([r[0] for r in self.conn.execute(
            "SELECT pid FROM render_states WHERE render_id='legacy-1'")], [pids[0]])

