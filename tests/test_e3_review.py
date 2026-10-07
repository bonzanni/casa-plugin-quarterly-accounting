"""Diff round e3 (26b68ee..cd1a37d, rev 18.4), each accepted finding reproduced through the
real surface:
- Astra S1: a batch Casa cuts after persisting work, re-claimed, reports its own progress —
  the report is judged per claim, never latched across a cut;
- Astra S2a: attempts count CONSECUTIVE unproductive hand-outs (a search in between resets);
- Astra S2b: record_match / propose_match pass the same job checks as decide."""
from tests._base import StoreCase
from tests.sim_job import CasaCut, JobDriver
import db                     # server/ is on sys.path once tests._base is imported


class ProgressPerClaim(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_cut_batches_that_filed_documents_keep_reporting_and_finish(self):
        drv = JobDriver(self, payments=60)
        for i in range(60):
            drv.gmail.invoice("Zapier", 1000 * (i + 1), "EUR", drv.DATES[i % 3], f"ZAP-{i + 1}")
        real, n = drv._file_vendor, {}

        def cut_after_five(token, vendor, refs):
            out = []
            for ref in refs:
                if n.get(token, 0) >= 5:
                    raise CasaCut()                 # Casa ends the batch mid-unit
                out += real(token, vendor, [ref])
                n[token] = n.get(token, 0) + 1
            return out
        drv._file_vendor = cut_after_five
        drv.casa_cut = 80                           # Casa's idle guard is enforced
        drv.run_job("e3e3e3e3-a1")
        self.assertTrue(all(drv.batch_reported), drv.batch_reported)
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 60})


class ConsecutiveAttempts(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_a_search_between_two_idle_hand_outs_resets_the_count(self):
        drv = JobDriver(self, payments=1)
        ref = drv.gmail.invoice("Zapier", 1000, "EUR", drv.DATES[0], "ZAP-1")
        real, n = drv._payment, [0]

        def idle_search_idle(u, token):
            n[0] += 1
            if n[0] == 2:                           # a search with its find, then cut
                drv._tool("record_search", {"pass_token": token, "pids": [u["pid"]],
                                            "search": "plain", "queries": ["Zapier"],
                                            "refs": [ref]})
                return None
            if n[0] in (1, 3):
                return None                         # cut, nothing persisted
            return real(u, token)
        drv._payment = idle_search_idle
        drv.run_job("e3e3e3e3-b1")
        self.assertEqual(self.conn.execute("SELECT status FROM projections").fetchone()[0],
                         "matched")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_items WHERE"
                                           " state='given_up'").fetchone()[0], 0)


class SingleDecisionsAreGuarded(StoreCase):
    def test_record_match_of_a_handed_document_onto_a_paired_payment_is_refused(self):
        import asks, loop, qa_server, tools  # noqa: F401
        self.bind()
        self.token = self.run_claim()
        self.row(1, counterparty="Adobe", booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        a = self.doc(vendor="Adobe", document_date="2026-09-01", document_number="A1")
        self.machine_match(pid, a, self.token)
        b = self.doc(vendor=None, document_date="2026-09-02", document_number="B2")
        asks.request_work(self.conn, "handover", "operator", [b])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        loop.build_work(self.conn, self.job_id, handover_docs=[b])
        u = loop.payment_unit(self.conn, self.job_id)
        for tool in ("record_match", "propose_match"):
            args = {"pass_token": self.token, "pid": pid, "doc_id": b,
                    "expected_revision": u["revision"], "document_date": "2026-09-02"}
            if tool == "record_match":
                args["author"] = "auto"
            with self.assertRaisesRegex(db.Refusal, "already has a document"):
                qa_server.TOOLS[tool]["fn"](args)
        cur = self.conn.execute("SELECT s.doc_id FROM projections p JOIN match_state s ON"
                                " s.match_id=p.current_match WHERE p.pid=?",
                                (pid,)).fetchone()[0]
        self.assertEqual(cur, a)
