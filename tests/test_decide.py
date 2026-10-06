# tests/test_decide.py
"""Simple loop §2.2 step 4: decide applies the floor to each entry on its own, in order; a
refused entry is reported and the others apply; a document an earlier entry took is taken
for the later ones; no `not-needed` outcome; record_missing; the run's outcome and the
batch's progress (§2.2 `progressed`)."""
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported


class Decide(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.pids = []
        for n in (1, 2, 3):
            self.row(n, counterparty="Adobe", amount_minor=10000,
                     booking_date="2026-09-0%d" % n, value_date="2026-09-0%d" % n)
            pid = self.lineage_for(n)
            self.classify(pid, {"software"})
            self.settle(pid)
            self.pids.append(pid)

    def entry(self, pid, outcome, doc_id=None, **kw):
        e = {"pid": pid, "outcome": outcome, "expected_revision": self.rev(pid), **kw}
        if doc_id is not None:
            e.update(doc_id=doc_id, document_date="2026-09-01")
        return e

    def test_a_refused_entry_leaves_the_others_applied(self):
        import decide
        eur, usd = self.doc(), self.doc(currency="USD", amount_minor=11000)
        out = decide.decide(self.conn, self.token, [
            self.entry(self.pids[0], "match", eur),
            self.entry(self.pids[1], "match", usd),            # different currency
            self.entry(self.pids[2], "missing", reason="no invoice found")])
        got = [(r["pid"], r["applied"]) for r in out["results"]]
        self.assertEqual(got, [(self.pids[0], True), (self.pids[1], False),
                               (self.pids[2], True)])
        self.assertIn("only ever proposed", out["results"][1]["refused"])
        self.assertEqual(self.conn.execute("SELECT state FROM match_state WHERE doc_id=?",
                                           (eur,)).fetchone()[0], "matched")

    def test_a_document_an_earlier_entry_took_is_taken_for_the_later_ones(self):
        import decide
        d = self.doc()
        out = decide.decide(self.conn, self.token, [self.entry(self.pids[0], "propose", d),
                                                     self.entry(self.pids[1], "match", d)])
        self.assertEqual([r["applied"] for r in out["results"]], [True, False])
        self.assertIn("taken", out["results"][1]["refused"])

    def test_not_needed_is_never_the_jobs(self):
        import decide
        out = decide.decide(self.conn, self.token, [self.entry(self.pids[0], "not-needed")])
        self.assertFalse(out["results"][0]["applied"])
        self.assertIn("operator", out["results"][0]["refused"])

    def test_one_payment_twice_in_one_call_is_refused_the_second_time(self):
        import decide
        out = decide.decide(self.conn, self.token, [
            self.entry(self.pids[0], "missing", reason="x"),
            self.entry(self.pids[0], "missing", reason="x")])
        self.assertIn("earlier in this call", out["results"][1]["refused"])

    def test_a_refused_entry_may_be_decided_again_in_the_same_call(self):
        import decide
        out = decide.decide(self.conn, self.token, [
            self.entry(self.pids[0], "match", self.doc(amount_minor=1)),
            self.entry(self.pids[0], "missing", reason="nothing fits")])
        self.assertEqual([r["applied"] for r in out["results"]], [False, True])

    def test_missing_refuses_a_payment_that_holds_a_pairing(self):
        import decide
        d = self.doc()
        decide.decide(self.conn, self.token, [self.entry(self.pids[0], "match", d)])
        with self.assertRaisesRegex(db.Refusal, "holds a pairing"):
            decide.record_missing(self.conn, self.token, self.pids[0], self.rev(self.pids[0]),
                                  "x")

    def test_the_outcome_lands_on_the_runs_work_list_and_marks_progress(self):
        import decide
        self.work_rows([self.pids[2]])         # the run's work list (the fixture helper)
        decide.decide(self.conn, self.token, [self.entry(self.pids[2], "missing",
                                                          reason="nothing in mail")])
        self.assertEqual(tuple(self.conn.execute(
            "SELECT outcome, reason FROM run_work WHERE pid=?", (self.pids[2],)).fetchone()),
            ("missing", "nothing in mail"))
        self.assertEqual(self.conn.execute("SELECT progressed FROM claims WHERE gen=?",
                                           (self.token,)).fetchone()[0], 1)

    def test_ingest_records_the_vendor_and_a_filing_sequence(self):
        import documents
        path = self.publish("inv.pdf", b"%PDF-1.4 adobe")
        out = documents.ingest_document(self.conn, source_path=path, kind="invoice",
                                        source="gmail", extraction_author="specialist",
                                        vendor="Adobe", amount_minor=10000, currency="EUR",
                                        token=self.token)
        row = self.conn.execute("SELECT vendor, filed_seq FROM documents WHERE doc_id=?",
                                (out["doc_id"],)).fetchone()
        self.assertEqual(row["vendor"], "Adobe")
        self.assertIsNotNone(row["filed_seq"])

    def test_a_vendor_search_records_every_payment_and_needs_no_chunk(self):
        import work
        work.record_search(self.conn, pids=self.pids, token=self.token,
                           queries=["from:billing@adobe.com after:2026/07/01"])
        for pid in self.pids:
            d = work.describe(self.conn, pid)
            self.assertEqual(d["search"]["queries"], ["from:billing@adobe.com after:2026/07/01"])

    def test_the_learned_hint_is_stored_and_read_back(self):
        import kb
        kb.upsert_counterparty(self.conn, "Adobe", hint_sender="billing@adobe.com",
                               hint_subject="Your Adobe invoice", token=self.token)
        cp = kb.get_counterparty(self.conn, "Adobe")
        self.assertEqual((cp["hint_sender"], cp["hint_subject"]),
                         ("billing@adobe.com", "Your Adobe invoice"))

    # --- beyond the brief's nine -------------------------------------------------------

    def progressed(self):
        return self.conn.execute("SELECT progressed FROM claims WHERE gen=?",
                                 (self.token,)).fetchone()[0]

    def test_a_refused_entry_rolls_back_alone_and_writes_nothing(self):
        import decide
        d = self.doc()
        seq = self.conn.execute("SELECT coalesce(max(seq), 0) FROM log").fetchone()[0]
        rev = self.rev(self.pids[1])
        out = decide.decide(self.conn, self.token, [
            self.entry(self.pids[0], "match", d),
            dict(self.entry(self.pids[1], "match", self.doc(amount_minor=999)))])
        self.assertEqual((out["applied"], out["refused"]), (1, 1))
        self.assertIn("amounts differ", out["results"][1]["refused"])
        self.assertEqual(self.rev(self.pids[1]), rev)
        # exactly the first entry's one log entry was appended
        self.assertEqual([r[0] for r in self.conn.execute(
            "SELECT pid FROM log WHERE seq>?", (seq,))], [self.pids[0]])

    def test_a_refusal_after_a_write_rolls_the_entry_back(self):
        import decide
        d = self.doc()            # filed 2026-07-02; the entry reads 2026-09-01 on it
        out = decide.decide(self.conn, self.token,
                            [self.entry(self.pids[0], "match", d, labels=["bogus"])])
        self.assertIn("labels are", out["results"][0]["refused"])
        # machine_in_tx wrote the date read before the labels refused: the savepoint undid it
        self.assertEqual(tuple(self.conn.execute(
            "SELECT document_date, date_read_at FROM documents WHERE doc_id=?",
            (d,)).fetchone()), ("2026-07-02", None))

    def test_a_stale_revision_refuses_that_entry_only(self):
        import decide
        e = self.entry(self.pids[0], "missing", reason="x")
        e["expected_revision"] += 1
        out = decide.decide(self.conn, self.token, [e, self.entry(self.pids[1], "missing",
                                                                   reason="y")])
        self.assertEqual([r["applied"] for r in out["results"]], [False, True])
        self.assertIn("changed since it was handed out", out["results"][0]["refused"])

    def test_a_match_or_proposal_needs_the_date_read(self):
        import decide
        e = self.entry(self.pids[0], "match", self.doc())
        del e["document_date"]
        out = decide.decide(self.conn, self.token, [e])
        self.assertIn("document_date", out["results"][0]["refused"])

    def test_malformed_entries_are_refused_alone_never_the_call(self):
        import decide
        alt_bad = self.entry(self.pids[1], "propose", self.doc(), alternatives=["x"])
        out = decide.decide(self.conn, self.token, [
            "not an entry", alt_bad, {"pid": self.pids[2], "outcome": "missing"},
            self.entry(self.pids[0], "missing", reason="none")])
        self.assertEqual([r["applied"] for r in out["results"]], [False, False, False, True])
        self.assertIn("alternatives", out["results"][1]["refused"])
        self.assertIn("expected_revision", out["results"][2]["refused"])

    def test_the_entries_are_bounded_and_the_token_is_required(self):
        import decide
        for bad in ([], [self.entry(self.pids[0], "missing")] * (decide.ENTRIES_MAX + 1),
                    "x"):
            with self.assertRaisesRegex(db.Refusal, "entries is a list"):
                decide.decide(self.conn, self.token, bad)
        with self.assertRaisesRegex(db.Refusal, "pass_token"):
            decide.decide(self.conn, None, [self.entry(self.pids[0], "missing")])
        with self.assertRaisesRegex(db.Refusal, "no longer the current one"):
            decide.decide(self.conn, self.token + 1000, [self.entry(self.pids[0], "missing")])

    def test_an_exempt_payment_keeps_its_residue_in_a_decide(self):
        import decide
        import matches
        rid = self.show(self.pids[0])
        self.granted(matches.set_exemption_in_tx, pid=self.pids[0], exempt=True,
                     expected_revision=self.rev(self.pids[0]), render_id=rid)
        out = decide.decide(self.conn, self.token,
                            [self.entry(self.pids[0], "match", self.doc())])
        self.assertFalse(out["results"][0]["applied"])
        self.assertIn("exempted", out["results"][0]["refused"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM residue WHERE pid=? AND"
                                           " reason='exempt-doc'", (self.pids[0],)
                                           ).fetchone()[0], 1)

    def test_a_noop_redecision_lands_on_the_work_list_and_writes_nothing(self):
        import decide
        d = self.doc()
        self.work_rows([self.pids[0]])
        decide.decide(self.conn, self.token, [self.entry(self.pids[0], "match", d)])
        rev = self.rev(self.pids[0])
        with db.tx(self.conn):
            self.conn.execute("UPDATE run_work SET outcome=NULL, handed_upto=77 WHERE pid=?",
                              (self.pids[0],))
        out = decide.decide(self.conn, self.token, [self.entry(self.pids[0], "match", d)])
        self.assertEqual((out["results"][0]["applied"], out["results"][0]["wrote"]),
                         (True, False))
        self.assertEqual(self.rev(self.pids[0]), rev)
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work WHERE pid=?",
                                           (self.pids[0],)).fetchone()[0], "match")
        # §2.1: the decision considered the documents handed out for it, up to handed_upto
        self.assertEqual(self.conn.execute("SELECT considered_seq FROM projections WHERE"
                                           " pid=?", (self.pids[0],)).fetchone()[0], 77)

    def test_a_refused_entry_leaves_the_work_list_undecided(self):
        import decide
        self.work_rows([self.pids[0]])
        decide.decide(self.conn, self.token,
                      [self.entry(self.pids[0], "match", self.doc(amount_minor=1))])
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work WHERE pid=?",
                                           (self.pids[0],)).fetchone()[0], None)
        self.assertEqual(self.progressed(), 0)

    def test_single_decisions_land_on_the_work_list_too(self):
        import matches
        self.work_rows(self.pids[:2])
        matches.record_match(self.conn, pid=self.pids[0], doc_id=self.doc(), author="auto",
                             expected_revision=self.rev(self.pids[0]), token=self.token,
                             document_date="2026-09-01")
        matches.propose_match(self.conn, pid=self.pids[1], doc_id=self.doc(),
                              expected_revision=self.rev(self.pids[1]), token=self.token,
                              document_date="2026-09-01")
        self.assertEqual(dict(self.conn.execute("SELECT pid, outcome FROM run_work")),
                         {self.pids[0]: "match", self.pids[1]: "propose"})
        self.assertEqual(self.progressed(), 1)

    # --- fix round 1: missing meets the floor's own preconditions -----------------------

    def outcome(self, pid):
        return self.conn.execute("SELECT outcome FROM run_work WHERE pid=?",
                                 (pid,)).fetchone()[0]

    def test_an_exempt_payment_is_never_missing(self):
        import decide
        import matches
        self.work_rows([self.pids[0]])
        rid = self.show(self.pids[0])
        self.granted(matches.set_exemption_in_tx, pid=self.pids[0], exempt=True,
                     expected_revision=self.rev(self.pids[0]), render_id=rid)
        out = decide.decide(self.conn, self.token, [
            self.entry(self.pids[0], "match", self.doc()),
            self.entry(self.pids[0], "missing", reason="x")])
        self.assertEqual([r["applied"] for r in out["results"]], [False, False])
        self.assertIn("exempted", out["results"][1]["refused"])
        self.assertIsNone(self.outcome(self.pids[0]))

    def test_a_no_document_payment_is_never_missing(self):
        import decide
        import kb
        self.work_rows([self.pids[0]])
        kb.set_expectation(self.conn, scope_type="counterparty", scope="Adobe", kind="none",
                           author="specialist", token=self.token)
        out = decide.decide(self.conn, self.token,
                            [self.entry(self.pids[0], "missing", reason="x")])
        self.assertIn("no document is expected", out["results"][0]["refused"])
        self.assertIsNone(self.outcome(self.pids[0]))

    def test_a_pending_payment_is_never_missing(self):
        import decide
        self.row(9001, counterparty="Adobe", status="PDNG", booking_date="2026-09-05",
                 value_date="2026-09-05")
        pid = self.lineage_for(9001)
        self.classify(pid, {"software"})
        self.settle(pid)
        out = decide.decide(self.conn, self.token, [self.entry(pid, "missing", reason="x")])
        self.assertIn("pending payment", out["results"][0]["refused"])

    def test_a_payment_not_in_the_latest_import_is_never_missing(self):
        import decide
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET class_observed_snapshot=NULL WHERE"
                              " pid=?", (self.pids[0],))
        out = decide.decide(self.conn, self.token,
                            [self.entry(self.pids[0], "missing", reason="x")])
        self.assertIn("latest bank import", out["results"][0]["refused"])

    def test_the_work_row_of_a_merged_away_payment_takes_the_outcome(self):
        import decide
        self.row(9002, counterparty="Adobe", booking_date="2026-09-06",
                 value_date="2026-09-06")
        loser = self.lineage_for(9002)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET merged_into=? WHERE pid=?",
                              (self.pids[1], loser))
        self.work_rows([loser])
        with db.tx(self.conn):
            self.conn.execute("UPDATE run_work SET handed_upto=55 WHERE pid=?", (loser,))
        out = decide.decide(self.conn, self.token,
                            [self.entry(self.pids[1], "missing", reason="none")])
        self.assertTrue(out["results"][0]["applied"])
        self.assertEqual(self.outcome(loser), "missing")
        self.assertEqual(self.conn.execute("SELECT considered_seq FROM projections WHERE"
                                           " pid=?", (self.pids[1],)).fetchone()[0], 55)

    def test_record_missing_is_one_missing_entry(self):
        import decide
        self.work_rows([self.pids[1]])
        out = decide.record_missing(self.conn, self.token, self.pids[1],
                                    self.rev(self.pids[1]), "r" * 300)
        self.assertEqual((out["applied"], out["wrote"]), (True, False))
        self.assertEqual(self.conn.execute("SELECT reason FROM run_work WHERE pid=?",
                                           (self.pids[1],)).fetchone()[0], "r" * 200)

    def test_the_tools_decide_and_record_missing(self):
        import json
        import qa_server
        import tools  # noqa: F401 — registers the tools
        d = self.doc()
        out = qa_server.TOOLS["decide"]["fn"]({"pass_token": self.token, "entries": [
            self.entry(self.pids[0], "match", d), self.entry(self.pids[1], "match", d)]})
        self.assertEqual((out["applied"], out["refused"]), (1, 1))
        out = qa_server.TOOLS["record_missing"]["fn"]({
            "pass_token": self.token, "pid": self.pids[2],
            "expected_revision": self.rev(self.pids[2]), "reason": "nothing"})
        self.assertTrue(out["applied"])
        reply = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "record_missing", "arguments": {
                                      "pass_token": self.token, "pid": self.pids[0],
                                      "expected_revision": self.rev(self.pids[0])}}})
        self.assertIn("holds a pairing", json.dumps(reply))


class Filing(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()

    def ingest(self, data, **kw):
        import documents
        return documents.ingest_document(self.conn, source_path=self.publish("inv.pdf", data),
                                         kind="invoice", source="gmail",
                                         extraction_author="specialist", **kw)

    def test_a_refiling_names_a_vendor_only_where_none_was_recorded(self):
        first = self.ingest(b"%PDF-1.4 one", token=self.token)
        self.assertIsNone(self.conn.execute("SELECT vendor FROM documents WHERE doc_id=?",
                                            (first["doc_id"],)).fetchone()[0])
        seq = self.conn.execute("SELECT filed_seq FROM documents WHERE doc_id=?",
                                (first["doc_id"],)).fetchone()[0]
        again = self.ingest(b"%PDF-1.4 one", token=self.token, vendor="Adobe")
        self.assertEqual((again["doc_id"], again["created"]), (first["doc_id"], False))
        third = self.ingest(b"%PDF-1.4 one", token=self.token, vendor="Zapier")
        self.assertEqual(third["doc_id"], first["doc_id"])
        row = self.conn.execute("SELECT vendor, filed_seq FROM documents WHERE doc_id=?",
                                (first["doc_id"],)).fetchone()
        self.assertEqual((row["vendor"], row["filed_seq"]), ("Adobe", seq))

    def test_the_stored_vendor_compares_as_the_work_list_does(self):
        import kb
        out = self.ingest(b"%PDF-1.4 sp", token=self.token, vendor="  Adobe   Systems ")
        stored = self.conn.execute("SELECT vendor FROM documents WHERE doc_id=?",
                                   (out["doc_id"],)).fetchone()[0]
        self.assertEqual((stored, kb.norm(stored)), ("Adobe Systems", kb.norm("adobe systems")))

    def test_a_vendor_is_bounded(self):
        with self.assertRaisesRegex(db.Refusal, "vendor"):
            self.ingest(b"%PDF-1.4 two", token=self.token, vendor="v" * 81)

    def test_a_token_bearing_filing_marks_progress_and_a_desk_filing_does_not(self):
        progressed = lambda: self.conn.execute("SELECT progressed FROM claims WHERE gen=?",
                                               (self.token,)).fetchone()[0]
        self.ingest(b"%PDF-1.4 desk")
        self.assertEqual(progressed(), 0)
        a = self.ingest(b"%PDF-1.4 job", token=self.token)
        self.assertEqual(progressed(), 1)
        b = self.ingest(b"%PDF-1.4 later", token=self.token)
        self.assertGreater(
            self.conn.execute("SELECT filed_seq FROM documents WHERE doc_id=?",
                              (b["doc_id"],)).fetchone()[0],
            self.conn.execute("SELECT filed_seq FROM documents WHERE doc_id=?",
                              (a["doc_id"],)).fetchone()[0])


class VendorSearch(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.pids = []
        for n, who in ((1, "Adobe"), (2, "Adobe"), (3, "Adobe"), (4, "Zapier")):
            self.row(n, counterparty=who, booking_date="2026-09-0%d" % n,
                     value_date="2026-09-0%d" % n)
            pid = self.lineage_for(n)
            self.classify(pid, {"software"})
            self.settle(pid)
            self.pids.append(pid)
        self.work_rows(self.pids[:3], vendor="Adobe")
        self.work_rows(self.pids[3:], vendor="Zapier")

    def flags(self):
        return {r["pid"]: (r["hinted"], r["plain"]) for r in self.conn.execute(
            "SELECT pid, hinted, plain FROM run_work")}

    def test_a_vendor_search_marks_the_vendor_for_the_run_kind_by_kind(self):
        import work
        a1, a2, a3, z = self.pids
        # one split group of Adobe searched: the whole vendor, its later group included
        work.record_search(self.conn, pids=[a1, a2], token=self.token, search="hinted",
                           queries=["from:billing@adobe.com"])
        self.assertEqual(self.flags(), {a1: (1, 0), a2: (1, 0), a3: (1, 0), z: (0, 0)})
        work.record_search(self.conn, pids=[a3], token=self.token, search="plain",
                           queries=["Adobe invoice"])
        self.assertEqual(self.flags(), {a1: (1, 1), a2: (1, 1), a3: (1, 1), z: (0, 0)})
        work.record_search(self.conn, pid=z, token=self.token, queries=["Zapier"])
        self.assertEqual(self.flags()[z], (0, 0))           # a per-payment search marks none
        self.assertEqual(work.describe(self.conn, a3)["search"]["queries"], ["Adobe invoice"])

    def test_a_search_marks_progress_and_the_kind_is_checked(self):
        import work
        with self.assertRaisesRegex(db.Refusal, "search is hinted"):
            work.record_search(self.conn, pids=self.pids[:1], token=self.token,
                               search="vendor", queries=["q"])
        with self.assertRaisesRegex(db.Refusal, "pids is the list"):
            work.record_search(self.conn, pids=[], token=self.token, queries=["q"])
        with self.assertRaisesRegex(db.Refusal, "not both"):
            work.record_search(self.conn, pids=self.pids[:1], pid=self.pids[1],
                               token=self.token, queries=["q"])
        # an identity question or a bare `incomplete` is no search: no flag, no progress
        work.record_search(self.conn, pids=self.pids[:1], token=self.token, search="hinted",
                           identity_unknown=True, incomplete=True)
        self.assertEqual(self.flags()[self.pids[1]], (0, 0))
        self.assertEqual(self.conn.execute("SELECT progressed FROM claims WHERE gen=?",
                                           (self.token,)).fetchone()[0], 0)
        work.record_search(self.conn, pid=self.pids[0], token=self.token, queries=["q"])
        self.assertEqual(self.conn.execute("SELECT progressed FROM claims WHERE gen=?",
                                           (self.token,)).fetchone()[0], 1)

    def test_the_tool_takes_pids_and_search(self):
        import qa_server
        import tools  # noqa: F401
        props = qa_server.TOOLS["record_search"]["schema"]["properties"]
        self.assertIn("pids", props)
        self.assertIn("counts as the vendor's search only when it carries queries",
                      qa_server.TOOLS["record_search"]["description"])
        with self.assertRaisesRegex(db.Refusal, "not both"):
            qa_server.TOOLS["record_search"]["fn"]({"pids": self.pids[:1], "pid": self.pids[1],
                                                    "pass_token": self.token})
        self.assertIn("search", props)
        qa_server.TOOLS["record_search"]["fn"]({"pids": self.pids[:2], "search": "plain",
                                                "pass_token": self.token,
                                                "queries": ["Adobe"]})
        self.assertEqual(self.flags()[self.pids[2]], (0, 1))


class LearnedHint(StoreCase):
    def test_a_hint_is_bounded_and_only_a_given_value_updates(self):
        import kb
        kb.upsert_counterparty(self.conn, "Adobe", hint_sender="billing@adobe.com",
                               hint_subject="Your invoice")
        kb.upsert_counterparty(self.conn, "Adobe", hint_subject="Adobe receipt")
        cp = kb.get_counterparty(self.conn, "Adobe")
        self.assertEqual((cp["hint_sender"], cp["hint_subject"]),
                         ("billing@adobe.com", "Adobe receipt"))
        for field in ("hint_sender", "hint_subject"):
            with self.assertRaisesRegex(db.Refusal, "learned hint"):
                kb.upsert_counterparty(self.conn, "Adobe", **{field: "x" * 201})

    def test_the_tool_passes_the_hint(self):
        import qa_server
        import tools  # noqa: F401
        qa_server.TOOLS["upsert_counterparty"]["fn"]({"name": "Adobe",
                                                      "hint_sender": "a@adobe.com"})
        import kb
        self.assertEqual(kb.get_counterparty(self.conn, "Adobe")["hint_sender"],
                         "a@adobe.com")
