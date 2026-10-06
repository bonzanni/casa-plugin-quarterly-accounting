"""The server-owned per-run work queues (operator ruling A; docs queues-design.md q6), each
design round's finding pinned through the real surface (qa_server.TOOLS, a real bank-feed):
- q1 Astra: a model's write enqueues only into the handed unit; the server's producers (a
  taken handover) are exempt — a payment reopened during the mirror is worked before the post;
- q1/q2/q3 (rule 5): a run that gave up any upstream item (an attachment, the own-mail
  search, an erase check) counts every payment it decided missing as "search incomplete";
- q4 (rule 5's latch): given_up is terminal — a re-found ref filed later never clears it;
- q5: a job's record_search carries refs; a zero-result search is progress;
- rule 3: decide waits for the vendor's found attachments; rule 2: a mirror call never
  reported is given up as failed; set_aside closes an erase candidate bank-feed still has."""
from tests._base import StoreCase
from tests.sim_job import JobDriver
import db                     # server/ is on sys.path once tests._base is imported


def items(conn, job_id, **where) -> list:
    sql = "SELECT unit, kind, key, state FROM run_items WHERE job_id=?"
    args = [job_id]
    for k, v in where.items():
        sql += f" AND {k}=?"
        args.append(v)
    return [tuple(r) for r in conn.execute(sql + " ORDER BY seq", args)]


class Queues(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()

    def test_a_ref_goes_only_into_the_handed_unit(self):
        drv = JobDriver(self, payments=1)
        u = drv.to_unit("aaaa0001-1", "payment")
        pid = u["pid"]
        out = drv._tool("record_search", {"pass_token": drv.token, "pids": [pid],
                                          "search": "plain", "queries": ["Zapier invoice"],
                                          "refs": ["m1:a1"]})
        self.assertEqual((out["files"], out["files_total"]), (["m1:a1"], 1))
        self.assertEqual(items(self.conn, "aaaa0001-1", kind="ref"),
                         [(f"payment:{pid}", "ref", "m1:a1", "queued")])
        with self.assertRaises(db.Refusal):           # refs never go with a vendor's probe
            drv._tool("record_probe", {"pass_token": drv.token, "kind": "gmail", "ok": True,
                                       "data": {"refs": ["m2:a1"]}})
        with self.assertRaises(db.Refusal):           # a job search carries its refs (q5)
            drv._tool("record_search", {"pass_token": drv.token, "pids": [pid],
                                        "search": "payment", "queries": ["x"]})

    def test_decide_waits_for_the_vendors_found_attachments(self):
        drv = JobDriver(self, payments=1)
        u = drv.to_unit("aaaa0002-1", "payment")
        p = u
        drv._tool("record_search", {"pass_token": drv.token, "pids": [p["pid"]],
                                    "search": "plain", "queries": ["q"], "refs": ["m1:a1"]})
        with self.assertRaises(db.Refusal):
            drv._tool("decide", {"pass_token": drv.token, "entries": [{
                "pid": p["pid"], "outcome": "missing", "reason": "x",
                "expected_revision": p["revision"]}]})
        drv._tool("set_aside", {"pass_token": drv.token, "items": [{"ref": "m1:a1"}],
                                "reason": "terms and conditions"})
        out = drv._tool("decide", {"pass_token": drv.token, "entries": [{
            "pid": p["pid"], "outcome": "missing", "reason": "x",
            "expected_revision": p["revision"]}]})
        self.assertEqual(out["applied"], 1)

    def test_a_zero_result_search_is_progress(self):
        """q5 (Terra): a vendor hand-out that records a search finding nothing, then is cut,
        progressed — the payment is handed again, not given up after two such cuts."""
        drv = JobDriver(self, payments=1)
        real, n = drv._payment, [0]

        def cut_after_search(u, token):
            n[0] += 1
            if n[0] <= 2:
                kind = ("plain", "payment")[n[0] - 1]
                drv._tool("record_search", {"pass_token": token, "search": kind,
                                            "pids": [u["pid"]],
                                            "queries": [f"q{n[0]}"], "refs": []})
                return None
            return real(u, token)
        drv._payment = cut_after_search
        drv.run_job("aaaa0003-1")
        run = self.conn.execute("SELECT partial FROM runs WHERE job_id='aaaa0003-1'"
                                ).fetchone()
        self.assertEqual(run["partial"], 0)
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work").fetchone()[0],
                         "missing")

    def test_a_given_up_own_mail_attachment_makes_every_missing_search_incomplete(self):
        """q2 (Astra), rule 5: an own-mail attachment never filed (twice handed, never
        reached) — the payment decided missing later reads "missing · search incomplete"."""
        drv = JobDriver(self, payments=1)
        drv.gmail.own(55500, day=drv.DATES[0], number="OWN-X")    # fits no payment
        def probe_only(u, token):
            if u["search"]:                             # the probe, then cut every time
                drv._tool("record_probe", {"pass_token": token, "kind": "gmail", "ok": True,
                                           "data": {"refs": [m["ref"] for m in
                                                             drv.gmail.own_mail]}})
            return None                                 # never reaches the attachment
        drv._filing = probe_only
        drv.run_job("aaaa0004-1")
        self.assertEqual(items(self.conn, "aaaa0004-1", kind="ref"),
                         [("filing", "ref", "own-msg-001:att-1", "given_up")])
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work").fetchone()[0],
                         "missing")
        text = drv.posted_end("aaaa0004-1")["text"]
        self.assertIn("1 missing · search incomplete", text)
        self.assertIn("1 attachment found but not filed", text)
        self.assertEqual(self.conn.execute("SELECT partial FROM runs WHERE"
                                           " job_id='aaaa0004-1'").fetchone()[0], 1)

    def test_a_given_up_own_mail_search_makes_every_missing_search_incomplete(self):
        """q2 (Terra), rule 5: the own-mail search never recorded (two cut hand-outs)."""
        drv = JobDriver(self, payments=1)
        drv._filing = lambda u, token: None
        drv.run_job("aaaa0005-1")
        self.assertEqual(items(self.conn, "aaaa0005-1", kind="search"),
                         [("filing", "search", "own-mail", "given_up")])
        text = drv.posted_end("aaaa0005-1")["text"]
        self.assertIn("1 missing · search incomplete", text)
        self.assertIn("Your own mail was not read", text)

    def test_given_up_is_terminal_a_refound_ref_filed_later_keeps_the_latch(self):
        """q3 (Astra), q4: rA given up in filing; vendor B's search re-finds it and files it;
        the given-up item stays given up, so A's missing still reads search incomplete."""
        drv = JobDriver(self, payments=0)
        drv._add_rows([("Adobe", 1000, "2026-07-05", "software"),
                       ("Zapier", 2000, "2026-07-06", "software")])
        ra = drv.gmail.invoice("Zapier", 99900, "EUR", "2026-07-06", "ZAP-X")
        def filing(u, token):
            if u["search"]:
                drv._tool("record_probe", {"pass_token": token, "kind": "gmail", "ok": True,
                                           "data": {"refs": [ra]}})
            return None
        drv._filing = filing
        drv.run_job("aaaa0006-1")
        self.assertIn(("filing", "ref", ra, "given_up"), items(self.conn, "aaaa0006-1"))
        zapier = self.conn.execute("SELECT pid FROM run_work WHERE vendor='Zapier'").fetchone()[0]
        self.assertIn((f"payment:{zapier}", "ref", ra, "done"), items(self.conn, "aaaa0006-1"))
        self.assertIn("missing · search incomplete", drv.posted_end("aaaa0006-1")["text"])

    def test_a_handover_taken_during_the_mirror_is_worked_before_the_post(self):
        """q1 (Astra): the taken handover reopens a decided payment of an earlier vendor
        (a server producer: exempt from the handed-unit rule); the cursor walks back to
        the vendors before the post."""
        import asks, job
        drv = JobDriver(self, payments=1)
        drv.to_unit("aaaa0007-1", "mirror")
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work").fetchone()[0],
                         "missing")
        doc = drv.file_document(vendor="Zapier", amount_minor=1000, document_date="2026-07-05")
        asks.request_work(self.conn, "handover", "operator", [doc])
        rest = []
        while not rest or rest[-1]["unit"] != "complete":
            rest.append(job.next_unit(self.conn, drv.token, drv.calls))
            if rest[-1]["unit"] not in ("complete", "end-batch"):
                with drv._broker():
                    drv.do(rest[-1], drv.token)
        kinds = [u["unit"] for u in rest]
        self.assertLess(kinds.index("payment"), kinds.index("view"))
        self.assertEqual(self.conn.execute("SELECT status FROM projections").fetchone()[0],
                         "matched")

    def test_a_mirror_call_never_reported_is_given_up_failed(self):
        drv = JobDriver(self, payments=1)
        drv._mirror = lambda u, token: None              # runs nothing, reports nothing
        units = drv.run_job("aaaa0008-1")
        import queues
        self.assertEqual(sum(u["unit"] == "mirror" for u in units), queues.ATTEMPTS_MAX)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_mirror WHERE"
                                           " state='failed' AND error='not reported'"
                                           ).fetchone()[0],
                         self.conn.execute("SELECT count(*) FROM run_mirror").fetchone()[0])
        self.assertEqual(units[-1]["unit"], "complete")

    def test_an_erase_candidate_still_in_bank_feed_is_set_aside(self):
        drv = JobDriver(self, payments=1)
        self.row(9001, booking_date="2026-09-10", value_date="2026-09-10")
        pid = self.lineage_for(9001)
        orig = drv._bank

        def bank(tool, **args):
            if tool == "get_transaction" and args.get("row_id") == 9001:
                drv._spend()
                return "Transaction #9001: …"
            return orig(tool, **args)
        drv._bank = bank
        drv.run_job("aaaa0009-1")
        self.assertEqual(items(self.conn, "aaaa0009-1", kind="erase"),
                         [("erasures", "erase", str(pid), "done")])
        self.assertEqual(self.conn.execute("SELECT reason FROM run_items WHERE kind='erase'"
                                           ).fetchone()[0], "still in bank-feed")
        import lineage
        self.assertIsNone(lineage.projection(self.conn, pid)["ended"])


class RewalkMissing(StoreCase):
    """Rev 18.3 (r3 Astra S2 = Terra S1): a later payment's search files an earlier missing
    payment's invoice; that payment is walked again before the post and decided again."""

    def setUp(self):
        super().setUp()
        self.bind()

    def test_a_missing_payment_gets_the_invoice_a_later_search_filed(self):
        drv = JobDriver(self, payments=2)                   # Zapier EUR 10.00 Jul, 20.00 Aug
        drv.gmail.invoice("Zapier", 1000, "EUR", drv.DATES[0], "ZAP-1")
        drv.gmail.invoice("Zapier", 2000, "EUR", drv.DATES[1], "ZAP-2")
        real, first = drv._payment, []

        def skip_first(u, token):
            if not first:                                   # decided missing, unsearched
                first.append(u["pid"])
                drv._tool("decide", {"pass_token": token, "entries": [{
                    "pid": u["pid"], "outcome": "missing", "reason": "none yet",
                    "expected_revision": u["revision"]}]})
                return None
            return real(u, token)
        drv._payment = skip_first
        units = drv.run_job("aaaa000a-1")
        handed = [u["pid"] for u in units if u["unit"] == "payment"]
        self.assertEqual(handed.count(first[0]), 2)           # walked again
        self.assertEqual(dict(self.conn.execute(
            "SELECT status, count(*) FROM projections GROUP BY status").fetchall()),
            {"matched": 2})
        self.assertEqual(self.conn.execute("SELECT partial FROM runs WHERE"
                                           " job_id='aaaa000a-1'").fetchone()[0], 0)
