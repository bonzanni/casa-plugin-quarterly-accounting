"""Diff round d4 (Astra, 26b68ee..29f0f46), the two accepted findings, reproduced through
the real surface (qa_server.TOOLS, a real bank-feed) under Casa's 80-call cut:
- Astra S1a: a mirror unit's bank writes are closed by its record_mirror — since the
  no-budget ruling (2026-10-07) by handing the mirror in chunks of loop.MIRROR_CALLS, so a
  cut repeats at most one chunk;
- Astra S1b: filing membership is decided on the EXACT refs — the gmail probe carries every
  attachment found; the ones no ingest filed join the filing unit's queue and are answered
  as `files` (no clipped list for the model to compare)."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported

CASA_CALLS = 80


class MirrorKeepsItsAck(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_83_payments_mirror_to_completion_and_a_rerun_writes_nothing(self):
        import loop
        drv = JobDriver(self, payments=83)
        for i in range(83):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"INV-{i + 1}")
        drv.casa_cut = CASA_CALLS
        units = drv.run_job("d4d4d4d4-a1")
        self.assertEqual(drv.cuts, len(drv.batch_calls) - 1)      # only Casa ends a batch
        self.assertLessEqual(max(drv.batch_calls), CASA_CALLS, drv.batch_calls)
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 83})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_mirror WHERE"
                                           " job_id='d4d4d4d4-a1' AND state<>'done'"
                                           ).fetchone()[0], 0)
        self.assertIsNotNone(self.conn.execute(
            "SELECT completed_at FROM runs WHERE job_id='d4d4d4d4-a1'").fetchone()[0])
        mirrors = [u for u in units if u["unit"] == "mirror"]
        self.assertGreater(len(mirrors), 1)                   # the mirror spans batches
        for u in mirrors:                                     # a chunk at most
            self.assertLessEqual(len(u["calls"]), loop.MIRROR_CALLS)
        again = drv.run_job("d4d4d4d4-a2", started_by="scheduled")
        self.assertEqual(sum(len(u["calls"]) for u in again if u["unit"] == "mirror"), 0)


class FilingComparesExactRefs(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def long_ref(self, k):
        """Gmail-like refs: identical for their first 250 characters (a clip at 200 would
        make them all one)."""
        return "msg-18c9f0:" + "ANGjdJ8" * 34 + f"-{k:03d}"

    def test_astras_60_long_ref_attachments_file_and_match_to_completion(self):
        drv = JobDriver(self, payments=60)
        for i in range(60):
            drv.gmail.own(1000 * (i + 1), day=drv.DATES[i % 3], number=f"OWN-{i + 1}",
                          ref=self.long_ref(i))
        self.assertGreater(len(self.long_ref(0)), 200)
        drv.casa_cut = CASA_CALLS
        drv.run_job("d4d4d4d4-b1")
        self.assertEqual(drv.cuts, len(drv.batch_calls) - 1)   # no budget: Casa ends a batch
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0], 60)
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT ref FROM operator_refs")), sorted(self.long_ref(i) for i in range(60)))
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 60})
        run = self.conn.execute("SELECT * FROM runs WHERE job_id='d4d4d4d4-b1'").fetchone()
        self.assertIsNotNone(run["completed_at"])
        self.assertFalse(run["partial"])

    def test_the_gmail_probe_answers_the_exact_unfiled_refs_in_order(self):
        drv = JobDriver(self, payments=1)
        refs = [self.long_ref(i) for i in range(30)]
        drv.to_unit("d4d4d4d4-b2", "filing")
        doc = self.doc()                                    # filed and read (h1)
        with db.tx(self.conn):
            for r in refs[:3]:                              # filed already
                self.conn.execute("INSERT INTO operator_refs(ref, source, doc_id, filed_at)"
                                  " VALUES (?, 'manual-email', ?, 'x')", (r, doc))
        out = drv._tool("record_probe", dict(pass_token=drv.token, kind="gmail", ok=True,
                                             data={"refs": refs + [refs[5]]}))
        self.assertEqual(out["files_total"], 27)
        self.assertEqual(out["files"], refs[3:])            # exact, in order
        data = self.conn.execute("SELECT data_json FROM probes WHERE kind='gmail'"
                                 ).fetchone()[0]
        self.assertNotIn(refs[0], data)                     # the list is not stored
