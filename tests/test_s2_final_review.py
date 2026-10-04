"""S2 final review (fix wave): a handover whose judgment never covers it ends the job
through the no-progress guard (FW-I1; under the credit rule, test_s2_credits.NeverWhole),
a ledger restored between two bank reads of one pass stops it (FW-I2), and the guards
whose obvious mutant survived (FW-I3, minors)."""
import json

from tests._base import StoreCase
from tests.sim_job import JobDriver

A = "aaaaaaaa-1"


class JudgeEpoch(StoreCase):
    """FW-I1 (a), under the credit rule (spec §15): a judge page earns
    `judge:<epoch>:<its start>`; a judgment started again for a bounded cause (a handover
    taken) moves the epoch on, so its pages earn again; the never-covered loop itself is
    pinned in test_s2_credits.NeverWhole."""

    def test_a_page_earns_its_start_and_a_handover_restart_moves_the_epoch(self):
        import asks, db, job
        self.bind()
        drv = JobDriver(self, payments=10)              # two triage pages
        asks.request_work(self.conn, "check", "cron")
        u = drv.run_until(A, "judge")
        t = drv.token
        epoch = job.live_job_pass(self.conn)["judge_epoch"]
        keys = lambda: [r[0] for r in self.conn.execute(
            "SELECT key FROM credits WHERE key LIKE 'judge:%' ORDER BY rowid")]
        u = job.next_unit(self.conn, t, judged=drv.do(u, t))           # page 1 of 2
        self.assertEqual((u["unit"], u["after"] is not None), ("judge", True))
        self.assertEqual(keys(), [f"judge:{epoch}:0"])
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        with db.tx(self.conn):                           # taken mid-judgment
            job._take(self.conn, t, job.live_job_pass(self.conn))
        p = job.live_job_pass(self.conn)
        self.assertEqual(p["judge_epoch"], epoch + 1)
        self.assertIsNone(p["judge_after"])
        u = drv.next_until(t, "judge")
        t = drv.token
        job.next_unit(self.conn, t, judged=drv.do(u, t))
        self.assertEqual(keys(), [f"judge:{epoch}:0", f"judge:{epoch + 1}:0"])


class HandoverVerdicts(StoreCase):
    """FW-I1 (b): the answer that finishes a judgment gives each handed-over document
    its verdict; (c) a handover names filed documents only."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=2)

    def test_the_reviewers_omitted_verdict_is_refused_then_taken(self):
        import asks, db, job, views
        doc = self.doc()
        rid = asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])["request_id"]
        u = self.drv.run_until(A, "judge")
        t = self.drv.token
        self.assertEqual(u["documents_first"], [doc])
        good = self.drv.do(u, t)
        self.assertIsNone(good["page_next"])             # one page: this answer finishes
        before = [tuple(r) for r in self.conn.execute(
            "SELECT p.judge_epoch, p.judge_after, s.finished_at, s.started_seq, w.verdicts_json"
            " FROM passes p JOIN pass_steps s ON s.pass_id=p.pass_id AND s.step='judge'"
            " JOIN work_requests w ON w.pass_id=p.pass_id")]
        for bad in ({**good, "documents": {}}, {k: v for k, v in good.items()
                                                if k != "documents"},
                    {**good, "documents": {str(doc + 1000): "no-payment-yet"}}):
            with self.assertRaises(db.Refusal) as cm:
                job.next_unit(self.conn, t, judged=bad)
            text = str(cm.exception)
            self.assertIn(f"document {doc},", text)
            for word in views.FORBIDDEN:
                self.assertNotIn(word, text)
            after = [tuple(r) for r in self.conn.execute(
                "SELECT p.judge_epoch, p.judge_after, s.finished_at, s.started_seq,"
                " w.verdicts_json FROM passes p JOIN pass_steps s ON s.pass_id=p.pass_id AND"
                " s.step='judge' JOIN work_requests w ON w.pass_id=p.pass_id")]
            self.assertEqual(after, before)              # nothing written
        job.next_unit(self.conn, t, judged=good)         # with the verdict: taken
        self.drv.next_until(t, "complete")
        r = self.conn.execute("SELECT state, outcome FROM work_requests WHERE request_id=?",
                              (rid,)).fetchone()
        # S7 §5: the job posted the result and its receipt was marked
        self.assertEqual((r["state"], r["outcome"]), ("reported", "complete"))

    def test_a_verdict_recorded_earlier_in_the_judgment_counts(self):
        """A document judged on an earlier page of the same judgment needs no verdict
        again on the last page."""
        import asks, db, job
        doc = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])
        u = self.drv.run_until(A, "judge")
        t = self.drv.token
        with db.tx(self.conn):
            p = job.live_job_pass(self.conn)
            asks.record_verdicts(self.conn, p["pass_id"], {str(doc): "no-payment-yet"})
        judged = {**self.drv.do(u, t), "documents": {}}
        job.next_unit(self.conn, t, judged=judged)
        self.drv.next_until(t, "complete")
        self.assertEqual(self.conn.execute("SELECT outcome FROM work_requests").fetchone()[0],
                         "complete")

    def test_two_missing_documents_are_both_named(self):
        import asks, db, job
        d1, d2 = self.doc(), self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d1, d2])
        u = self.drv.run_until(A, "judge")
        t = self.drv.token
        judged = {**self.drv.do(u, t), "documents": {str(d1): "no-payment-yet"}}
        with self.assertRaises(db.Refusal) as cm:
            job.next_unit(self.conn, t, judged=judged)
        self.assertIn(f"document {d2},", str(cm.exception))
        self.assertNotIn(f"{d1},", str(cm.exception))

    def test_a_handover_of_an_unfiled_document_is_refused(self):
        import asks, db
        d = self.doc()
        with self.assertRaises(db.Refusal) as cm:
            asks.request_work(self.conn, "handover", "operator", doc_ids=[d, d + 5000, d + 6000])
        text = str(cm.exception)
        self.assertIn(f"documents {d + 5000}, {d + 6000} are not", text)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0],
                         0)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])     # filed: taken

    def test_a_non_number_document_key_is_a_refusal(self):
        import asks, db, job
        doc = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])
        u = self.drv.run_until(A, "judge")
        t = self.drv.token
        judged = self.drv.do(u, t)
        judged["documents"] = {**judged["documents"], "the invoice": "matched"}
        with self.assertRaises(db.Refusal) as cm:
            job.next_unit(self.conn, t, judged=judged)
        self.assertIn("document id", str(cm.exception))


class RestoreMidPass(StoreCase):
    """FW-I2: against the real bank-feed (tests/bankfeed.py)."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=3)

    def test_a_restore_between_two_reads_of_one_pass_stops_it(self):
        import asks, datetime as _dt, db, job, version
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "judge")
        bf = self.drv.bf
        remembered = self.conn.execute("SELECT ledger_generation FROM binding").fetchone()[0]
        snaps = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        bf.call("restore_backup", backup_id=bf.registered()[version.WORKFLOW])
        self.assertNotEqual(bf.generation(), remembered)
        later = db._clock() + _dt.timedelta(seconds=job.W_S * 2)
        with self.patch_clock(later):
            t = job.claim(self.conn, A)
            u = job.next_unit(self.conn, t)
            self.assertEqual(u["unit"], "probes")       # W: the bank is read again
            self.drv.do(u, t)
            u = job.next_unit(self.conn, t)
            self.assertEqual(u["unit"], "snapshot")
            with self.assertRaises(db.Refusal) as cm:
                self.drv.do(u, t)
            self.assertIn("nothing was imported", str(cm.exception))
            self.assertEqual(self.conn.execute("SELECT ledger_generation FROM binding"
                                               ).fetchone()[0], remembered)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0],
                             snaps)
            u = job.next_unit(self.conn, t)             # the pass stops
            self.assertEqual(u["unit"], "post")         # S7 §5: its stop line, not marked
            self.assertIsNone(job.live_job_pass(self.conn))
            r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
            self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))
            # the next pass's own gate names what happened
            asks.request_work(self.conn, "check", "operator")
            self.drv.run_job(A)
        gr = json.loads(self.conn.execute("SELECT value FROM meta WHERE key='gate_refusal'"
                                          ).fetchone()[0])
        self.assertEqual(gr["kind"], "restored")
        self.assertEqual(self.conn.execute("SELECT ledger_generation FROM binding"
                                           ).fetchone()[0], remembered)
        last = self.conn.execute("SELECT outcome FROM work_requests ORDER BY request_id DESC"
                                 " LIMIT 1").fetchone()[0]
        self.assertEqual(last, "stopped")


class LateStop(StoreCase):
    """Minor: a job pass's step reads no wall-clock age (INV-J3): a stop after
    SWEEP_STOP_S is taken as given, never refused as a time-out."""

    def test_a_job_pass_stop_is_never_refused_as_late(self):
        import datetime as _dt, db, job, steps
        self.bind()
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        steps.start(self.conn, t, "sweep", {})
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                              " VALUES (?, ?, 0, 0)", (pid, db.now()))
        later = db._clock() + _dt.timedelta(seconds=steps.SWEEP_STOP_S + 60)
        with self.patch_clock(later):
            steps.finish(self.conn, t, "sweep", counts={"remaining_in_cycle": 3},
                         stopped="the bank sync failed")
        fin = json.loads(self.conn.execute("SELECT finish_json FROM pass_steps WHERE"
                                           " step='sweep'").fetchone()[0])
        self.assertEqual(fin.get("stopped"), "the bank sync failed")
