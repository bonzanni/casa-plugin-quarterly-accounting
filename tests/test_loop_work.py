"""Simple loop §2.1–§2.2: the work list (open, reopened by a newly filed fitting
document, a competitor for a machine proposal, changed facts; never pending, never a
payment expecting no document, never an operator-confirmed pairing, never one the
operator left missing), ordered by vendor then date; the vendor unit's candidates and
exact fit."""
import json
from tests._base import StoreCase
import db                     # server/ is on sys.path once tests._base is imported



class Work(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.n = 0

    def pay(self, who="Adobe", amount=10000, day="2026-09-02", status="BOOK",
            tags=("software",), currency="EUR"):
        self.n += 1
        self.row(self.n, counterparty=who, amount_minor=amount, booking_date=day,
                 value_date=day, status=status, currency=currency)
        pid = self.lineage_for(self.n)
        self.classify(pid, set(tags))
        self.settle(pid)
        return pid

    def listed(self):
        import loop
        loop.build_work(self.conn, self.job_id)
        return [r[0] for r in self.conn.execute(
            "SELECT pid FROM run_work WHERE job_id=? ORDER BY vendor, pid", (self.job_id,))]

    def test_what_needs_work_and_what_never_does(self):
        import matches
        open_ = self.pay()
        pending = self.pay(status="PDNG")
        zero = self.pay(amount=0)                                  # #36: optional
        taxes = self.pay(who="Belastingdienst", tags=("taxes", "vat"))   # row 10
        confirmed = self.pay(who="Zapier")
        d = self.doc(counterparty="Zapier", issuer="Zapier")
        mid = matches.propose_match(self.conn, pid=confirmed, doc_id=d,
                                    expected_revision=self.rev(confirmed), token=self.token,
                                    document_date="2026-09-01")["match_id"]
        rid = self.show(confirmed)     # outside granted's tx: show opens its own
        self.granted(lambda c, grant: matches.confirm_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        left = self.pay(who="Zapier")
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state='accepted-missing' WHERE"
                              " pid=?", (left,))
        self.assertEqual(self.listed(), [open_])

    def test_a_document_filed_after_a_machine_match_reopens_it(self):
        pid = self.pay()
        self.machine_match(pid, self.doc(), self.token)
        self.assertEqual(self.listed(), [])                     # nothing filed after it
        self.file_later(self.doc())
        self.assertEqual(self.listed(), [pid])
        self.assertEqual(self.conn.execute("SELECT why FROM run_work WHERE pid=?",
                                           (pid,)).fetchone()[0], "reopen")

    def file_later(self, doc_id):
        """The document as a filing now stamps it (documents.filed_seq, Task 4)."""
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET filed_seq=? WHERE doc_id=?",
                              (db.next_seq(self.conn), doc_id))

    def test_an_unreviewed_competitor_survives_a_reclaim_until_the_job_decides(self):
        import decide, loop
        pid = self.pay()
        a = self.doc(document_date="2026-09-01")
        self.machine_match(pid, a, self.token)
        self.file_later(self.doc(document_date="2026-09-02"))     # a competitor, unreviewed
        self.run_claim(job_id=self.job_id)                        # the same job, a new batch
        self.assertEqual(self.listed(), [pid])                    # still owed (per payment)
        unit = loop.vendor_unit(self.conn, self.job_id)           # handed out: now seen
        self.assertEqual([p["pid"] for p in unit["payments"]], [pid])
        out = decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "match", "doc_id": a, "document_date": "2026-09-01",
            "expected_revision": self.rev(pid)}])                  # kept: writes nothing
        self.assertEqual(out["results"][0]["wrote"], False)
        self.run_claim()                                           # the next run
        self.assertEqual(self.listed(), [])                        # considered: not again

    def test_the_list_is_ordered_by_vendor_then_date(self):
        z = self.pay(who="Zapier", day="2026-07-05")
        a2 = self.pay(who="Adobe", day="2026-09-05")
        a1 = self.pay(who="Adobe", day="2026-07-01")
        import loop
        loop.build_work(self.conn, self.job_id)
        unit = loop.vendor_unit(self.conn, self.job_id)
        self.assertEqual(unit["vendor"], "Adobe")
        self.assertEqual([p["pid"] for p in unit["payments"]], [a1, a2])
        # nothing was decided: the group is handed once more (D8), then the next vendor
        self.assertEqual(loop.vendor_unit(self.conn, self.job_id)["vendor"], "Adobe")
        third = loop.vendor_unit(self.conn, self.job_id)
        self.assertEqual((third["vendor"], [p["pid"] for p in third["payments"]]),
                         ("Zapier", [z]))

    def test_candidates_and_the_exact_fit(self):
        import loop, lineage
        pid = self.pay(who="Adobe", day="2026-09-02")
        own = self.doc(vendor="Adobe", document_date="2026-08-30")
        other = self.doc(vendor="Figma", issuer="Figma", document_date="2026-09-01")
        usd_vendorless = self.doc(currency="USD", amount_minor=11000, vendor=None)
        usd_other = self.doc(currency="USD", amount_minor=11000, vendor="Figma")
        row = lineage.live_row(self.conn, lineage.projection(self.conn, pid))
        cands = loop.candidates(self.conn, pid, row, "Adobe")
        ids = {c["doc_id"] for c in cands}
        self.assertEqual(ids, {own, other, usd_vendorless})
        self.assertEqual(loop.exact_fit(self.conn, pid, row, "Adobe", cands), own)
        self.doc(vendor="Adobe", document_date="2026-09-03")        # a second fit
        cands = loop.candidates(self.conn, pid, row, "Adobe")
        self.assertIsNone(loop.exact_fit(self.conn, pid, row, "Adobe", cands))

    def test_a_live_proposals_alternative_is_held_for_another_payment(self):
        import loop, lineage, matches
        p1 = self.pay(who="Adobe", day="2026-09-02")
        p2 = self.pay(who="Adobe", day="2026-09-03")
        a = self.doc(vendor="Adobe", document_date="2026-09-01")
        b = self.doc(vendor="Adobe", document_date="2026-09-02")
        matches.propose_match(self.conn, pid=p1, doc_id=a, expected_revision=self.rev(p1),
                              token=self.token, document_date="2026-09-01", alternatives=[b])
        row = lineage.live_row(self.conn, lineage.projection(self.conn, p2))
        cands = loop.candidates(self.conn, p2, row, "Adobe")
        held = {c["doc_id"]: c["held"] for c in cands}
        self.assertEqual((held[a], held[b]), ("other", "other"))
        self.assertIsNone(loop.exact_fit(self.conn, p2, row, "Adobe", cands))
        self.assertTrue(matches.taken_elsewhere(self.conn, b, p2))   # the floor agrees

    def test_uniqueness_and_handover_eligibility_read_every_candidate(self):
        import loop, lineage
        pid = self.pay(who="Adobe", day="2026-09-02")
        first = self.doc(vendor="Adobe", document_date="2026-09-02")          # exact, gap 0
        for k in range(7):                                                   # 7 closer others
            self.doc(vendor="Figma", issuer="Figma", document_date="2026-09-0%d" % (k + 2))
        ninth = self.doc(vendor="Adobe", document_date="2026-09-20")          # exact, gap 18
        row = lineage.live_row(self.conn, lineage.projection(self.conn, pid))
        cands = loop.candidates(self.conn, pid, row, "Adobe")
        self.assertEqual(len(cands), 9)
        self.assertIsNone(loop.exact_fit(self.conn, pid, row, "Adobe", cands))   # two fit
        p = lineage.projection(self.conn, pid)
        self.assertTrue(loop._handover_fits(self.conn, pid, p, row, [ninth]))
        self.assertEqual(len(loop.handed_candidates(cands, [])), loop.CANDIDATES_MAX)
        self.assertEqual({c["doc_id"] for c in cands if c["vendor"] == "Adobe"},
                         {first, ninth})                                     # both exact

    def test_a_trigger_beyond_the_cap_is_handed_out_and_only_then_considered(self):
        import decide, loop
        pid = self.pay(who="Adobe", day="2026-09-02")
        a = self.doc(vendor="Adobe", document_date="2026-09-01")
        self.machine_match(pid, a, self.token)
        for k in range(9):                                   # nine closer extras, not new
            self.doc(vendor="Figma", issuer="Figma", document_date="2026-09-0%d" % (k + 1))
        new = self.doc(vendor=None, document_date="2026-09-25")   # gap 23: last by date
        self.file_later(new)
        self.assertEqual(self.listed(), [pid])               # reopen
        unit = loop.vendor_unit(self.conn, self.job_id)
        shown = [c["doc_id"] for c in unit["payments"][0]["candidates"]]
        self.assertEqual(shown[0], new)                      # the trigger: first, uncapped
        self.assertEqual(len(shown), 1 + loop.CANDIDATES_MAX)
        decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "match", "doc_id": a, "document_date": "2026-09-01",
            "expected_revision": self.rev(pid)}])             # kept, having seen it
        self.run_claim()
        self.assertEqual(self.listed(), [])

    def test_a_handover_after_the_payment_was_decided_reopens_it_in_the_run(self):
        import asks, decide, loop
        pid = self.pay(who="Adobe", day="2026-09-02")
        a = self.doc(vendor="Adobe", document_date="2026-09-01")
        self.listed()
        loop.vendor_unit(self.conn, self.job_id)
        decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "match", "doc_id": a, "document_date": "2026-09-01",
            "expected_revision": self.rev(pid)}])
        self.assertIsNone(loop.vendor_unit(self.conn, self.job_id))   # nothing left
        usd = self.doc(currency="USD", amount_minor=11000, vendor=None)
        asks.request_work(self.conn, "handover", "operator", [usd])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
            self.assertEqual(loop.take_handovers(self.conn, self.job_id, [usd]), 1)
        unit = loop.vendor_unit(self.conn, self.job_id)
        self.assertEqual([p["pid"] for p in unit["payments"]], [pid])
        self.assertEqual(unit["payments"][0]["candidates"][0]["doc_id"], usd)

    def test_a_rejected_pair_is_neither_a_candidate_nor_the_exact_fit(self):
        import loop, lineage, matches
        pid = self.pay()
        d = self.doc(vendor="Adobe")
        mid = matches.propose_match(self.conn, pid=pid, doc_id=d, expected_revision=self.rev(pid),
                                    token=self.token, document_date="2026-09-01")["match_id"]
        rid = self.show(pid)     # outside granted's tx: show opens its own
        self.granted(lambda c, grant: matches.reject_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        row = lineage.live_row(self.conn, lineage.projection(self.conn, pid))
        cands = loop.candidates(self.conn, pid, row, "Adobe")
        self.assertNotIn(d, [c["doc_id"] for c in cands])
        self.assertIsNone(loop.exact_fit(self.conn, pid, row, "Adobe", cands))

    def test_a_handover_entry_is_handed_out_though_ordinary_work_would_skip_it(self):
        import loop
        pid = self.pay()
        self.machine_match(pid, self.doc(), self.token)               # EUR, matched
        usd = self.doc(currency="USD", amount_minor=11000, vendor=None)  # handed over
        import asks
        req = asks.request_work(self.conn, "handover", "operator", [usd])["request_id"]
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)          # the run's own pass takes it
        loop.build_work(self.conn, self.job_id, handover_docs=[usd])
        self.assertEqual(self.conn.execute("SELECT why FROM run_work WHERE pid=?",
                                           (pid,)).fetchone()[0], "handover")
        unit = loop.vendor_unit(self.conn, self.job_id)
        self.assertEqual([p["pid"] for p in unit["payments"]], [pid])
        self.assertIn(usd, [c["doc_id"] for c in unit["payments"][0]["candidates"]])
        self.assertEqual(tuple(self.conn.execute("SELECT state, pass_id FROM work_requests WHERE"
                                           " request_id=?", (req,)).fetchone()),
                         ("taken", self.pass_id))
        self.assertEqual(loop.run_handover_docs(self.conn, self.job_id), [usd])

    def test_a_large_vendor_is_split_and_each_part_handed_at_most_twice(self):
        import loop
        for i in range(loop.GROUP_MAX + 3):
            self.pay(who="Adobe", day="2026-08-%02d" % (i % 28 + 1))
        loop.build_work(self.conn, self.job_id)
        first = loop.vendor_unit(self.conn, self.job_id)
        self.assertEqual(len(first["payments"]), loop.GROUP_MAX)
        again = loop.vendor_unit(self.conn, self.job_id)            # undecided: once more
        self.assertEqual([p["pid"] for p in again["payments"]],
                         [p["pid"] for p in first["payments"]])
        rest = loop.vendor_unit(self.conn, self.job_id)
        self.assertEqual(len(rest["payments"]), 3)
        self.assertFalse({p["pid"] for p in rest["payments"]}
                         & {p["pid"] for p in first["payments"]})
        self.assertEqual(rest["vendor"], "Adobe")
        self.assertEqual(len(loop.vendor_unit(self.conn, self.job_id)["payments"]), 3)
        self.assertIsNone(loop.vendor_unit(self.conn, self.job_id))   # each part twice
        self.assertEqual(tuple(self.conn.execute(
            "SELECT count(*), min(handed), max(handed), count(outcome) FROM run_work WHERE"
            " job_id=?", (self.job_id,)).fetchone()), (loop.GROUP_MAX + 3, 2, 2, 0))

    def test_a_vendors_second_split_group_reuses_the_runs_search(self):
        import decide, loop, work
        for i in range(loop.GROUP_MAX + 3):
            self.pay(who="Adobe", day="2026-08-%02d" % (i % 28 + 1))
        loop.build_work(self.conn, self.job_id)
        first = loop.vendor_unit(self.conn, self.job_id)
        pids = [p["pid"] for p in first["payments"]]
        work.record_search(self.conn, pids=pids, token=self.token, search="hinted",
                           queries=["from:billing@adobe.com after:2026/07/01"])
        decide.decide(self.conn, self.token, [
            {"pid": p, "outcome": "missing", "reason": "x", "expected_revision": self.rev(p)}
            for p in pids])
        second = loop.vendor_unit(self.conn, self.job_id)
        self.assertEqual(second["vendor"], "Adobe")
        self.assertEqual(len(second["payments"]), 3)
        # the hinted search is the run's, not repeated; the plain fallback never ran, so it
        # is still owed to this uncovered group (rev 17 §2.2; plan round 5, Astra S2)
        self.assertEqual(second["searches"], {"hinted": True, "plain": False})
        self.assertIn("from:billing@adobe.com after:2026/07/01", second["vendor_queries"])
        self.assertEqual(second["search_window"], first["search_window"])
        work.record_search(self.conn, pids=[p["pid"] for p in second["payments"]],
                           token=self.token, search="plain", queries=["adobe invoice"])
        import db as _db
        with _db.tx(self.conn):
            flags = self.conn.execute("SELECT min(hinted), min(plain) FROM run_work WHERE"
                                      " job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(tuple(flags), (1, 1))

    # ---- beyond the brief: each reason, and the guards the mutation checks bind ----

    def why_of(self, pid):
        return self.conn.execute("SELECT why FROM run_work WHERE job_id=? AND pid=?",
                                 (self.job_id, pid)).fetchone()[0]

    def test_new_is_a_payment_admitted_at_the_latest_import(self):
        with db.tx(self.conn):
            for _ in range(2):
                self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows,"
                                  " max_row_id) VALUES ('p', 'x', 0, 0)")
        old, new = self.pay(), self.pay(who="Zapier")
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET admitted_snapshot=1 WHERE pid=?", (old,))
            self.conn.execute("UPDATE projections SET admitted_snapshot=2 WHERE pid=?", (new,))
        self.assertEqual(self.listed(), [old, new])
        self.assertEqual((self.why_of(old), self.why_of(new)), ("open", "new"))

    def test_changed_facts_and_a_competitor_for_a_proposal(self):
        import matches
        changed = self.pay()
        self.machine_match(changed, self.doc(), self.token)
        self.row(1, counterparty="Adobe", amount_minor=10000, booking_date="2026-09-02",
                 value_date="2026-09-02", remittance="corrected by the bank")
        self.settle(changed)
        proposed = self.pay(who="Zapier", amount=20000)
        matches.propose_match(self.conn, pid=proposed,
                              doc_id=self.doc(vendor="Zapier", amount_minor=19000),
                              expected_revision=self.rev(proposed), token=self.token,
                              document_date="2026-09-01")
        self.assertEqual(self.listed(), [changed])               # nothing filed after it
        self.file_later(self.doc(amount_minor=20000, vendor=None))
        self.assertEqual(self.listed(), [changed, proposed])
        self.assertEqual((self.why_of(changed), self.why_of(proposed)),
                         ("changed", "competitor"))

    def test_an_aged_out_payment_waits_for_its_rearm(self):
        aged = self.pay()
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state='aged-out', search_json=?"
                              " WHERE pid=?", (json.dumps({"last_counted_at": db.now()}),
                                               aged))
        self.assertEqual(self.listed(), [])

    def test_the_hand_out_judges_the_exact_fit_on_every_candidate(self):
        import loop
        pid = self.pay(who="Adobe", day="2026-09-02")
        self.doc(vendor="Adobe", document_date="2026-09-02")                  # nearest
        for k in range(7):
            self.doc(vendor="Figma", issuer="Figma", document_date="2026-09-0%d" % (k + 2))
        self.doc(vendor="Adobe", document_date="2026-09-20")                  # beyond the cap
        self.assertEqual(self.listed(), [pid])
        p = loop.vendor_unit(self.conn, self.job_id)["payments"][0]
        self.assertEqual((p["candidates_total"], len(p["candidates"])),
                         (9, loop.CANDIDATES_MAX))
        self.assertIsNone(p["exact_fit"])                     # the ninth is a second fit
        cands = [c["doc_id"] for c in p["candidates"]]
        self.assertEqual(len(set(cands)), loop.CANDIDATES_MAX)

    def test_the_exact_fit_is_handed_first_and_named(self):
        import loop
        pid = self.pay(who="Adobe", day="2026-09-02")
        for k in range(9):                                    # closer, same amount, others'
            self.doc(vendor="Figma", issuer="Figma", document_date="2026-09-0%d" % (k + 1))
        fit = self.doc(vendor="adobe ", document_date="2026-09-30")   # kb.norm: same vendor
        self.listed()
        p = loop.vendor_unit(self.conn, self.job_id)["payments"][0]
        self.assertEqual(p["exact_fit"], fit)
        self.assertEqual(p["candidates"][0]["doc_id"], fit)
        self.assertEqual(len(p["candidates"]), 1 + loop.CANDIDATES_MAX)
        self.assertEqual(p["pid"], pid)

    def test_considered_advances_only_past_documents_handed_out(self):
        """An unshown candidate filed after the shown trigger, held then by another
        payment's proposal, reopens the payment once it is released: the decision never
        considered it."""
        import decide, loop, matches
        pid = self.pay(who="Adobe", day="2026-09-02")
        a = self.doc(vendor="Adobe", document_date="2026-09-01")
        self.machine_match(pid, a, self.token)
        for k in range(9):
            self.doc(vendor="Figma", issuer="Figma", document_date="2026-09-0%d" % (k + 1))
        other = self.pay(who="Zapier", amount=20000, day="2026-09-03")
        late = self.doc(vendor=None, document_date="2026-12-30")      # far: beyond the cap
        matches.propose_match(self.conn, pid=other, doc_id=late,
                              expected_revision=self.rev(other), token=self.token,
                              document_date="2026-12-30")
        trigger = self.doc(vendor=None, document_date="2026-09-25")
        self.file_later(trigger)
        self.file_later(late)                                          # filed after it
        self.assertEqual(self.listed(), [pid])
        p = loop.vendor_unit(self.conn, self.job_id)["payments"][0]
        shown = [c["doc_id"] for c in p["candidates"]]
        self.assertEqual(shown[0], trigger)
        self.assertNotIn(late, shown)
        self.assertEqual(p["candidates_total"], 12)                   # late is a candidate
        decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "match", "doc_id": a, "document_date": "2026-09-01",
            "expected_revision": self.rev(pid)}])
        considered = self.conn.execute("SELECT considered_seq FROM projections WHERE pid=?",
                                       (pid,)).fetchone()[0]
        filed = dict(self.conn.execute("SELECT doc_id, filed_seq FROM documents WHERE doc_id"
                                       " IN (?,?)", (trigger, late)).fetchall())
        self.assertEqual(considered, filed[trigger])
        v = self.doc(vendor="Zapier", amount_minor=20000, document_date="2026-09-03")
        matches.record_match(self.conn, pid=other, doc_id=v, author="auto",
                             expected_revision=self.rev(other), token=self.token,
                             document_date="2026-09-03")      # a match replaces outright
        self.assertEqual(matches.holders(self.conn, late), [])
        self.run_claim()
        self.assertEqual(self.listed(), [pid])                # late was never handed out
        self.assertEqual(self.why_of(pid), "reopen")

    def test_building_again_never_reopens_a_decided_entry(self):
        import asks, decide, loop
        pid = self.pay()
        a = self.doc(vendor="Adobe", document_date="2026-09-01")
        self.machine_match(pid, a, self.token)
        usd = self.doc(currency="USD", amount_minor=11000, vendor=None)
        asks.request_work(self.conn, "handover", "operator", [usd])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
        self.assertEqual(loop.build_work(self.conn, self.job_id, handover_docs=[usd]), 1)
        self.assertEqual(loop.vendor_unit(self.conn, self.job_id)["payments"][0]["pid"], pid)
        decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "match", "doc_id": a, "document_date": "2026-09-01",
            "expected_revision": self.rev(pid)}])
        self.run_claim(job_id=self.job_id)                     # the same job, a new batch
        self.assertEqual(loop.build_work(self.conn, self.job_id, handover_docs=[usd]), 1)
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work WHERE pid=?",
                                           (pid,)).fetchone()[0], "match")
        self.assertIsNone(loop.vendor_unit(self.conn, self.job_id))

    def test_a_payment_settled_meanwhile_is_not_handed_out(self):
        import loop
        pid = self.pay()
        d = self.doc()
        self.listed()
        rid = self.show(pid)                    # the operator pairs it between hand-outs
        self.operator_pair(pid=pid, doc_id=d, expected_revision=self.rev(pid), render_id=rid)
        self.assertIsNone(loop.vendor_unit(self.conn, self.job_id))
        self.assertEqual(tuple(self.conn.execute("SELECT handed, outcome FROM run_work WHERE"
                                                 " pid=?", (pid,)).fetchone()), (0, "settled"))

    def test_left_missing_between_hand_outs_is_settled(self):
        import loop
        pid = self.pay()
        self.listed()
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state='accepted-missing' WHERE"
                              " pid=?", (pid,))
        self.assertIsNone(loop.vendor_unit(self.conn, self.job_id))
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work WHERE pid=?",
                                           (pid,)).fetchone()[0], "settled")

    def test_a_reopening_document_another_vendor_took_settles_the_entry(self):
        """Review fix round 1: an own-mail document reopens P and fits Q too; Q's vendor
        goes first and takes it; P is no longer work — settled, never a cut entry."""
        import decide, loop
        p = self.pay(who="Zapier", day="2026-09-02")
        a = self.doc(vendor="Zapier", issuer="Zapier", document_date="2026-09-01")
        self.machine_match(p, a, self.token)
        q = self.pay(who="Adobe", day="2026-09-03")
        d = self.doc(vendor=None, document_date="2026-09-03")
        self.file_later(d)
        self.assertEqual(self.listed(), [q, p])                   # Adobe, then Zapier
        self.assertEqual(self.why_of(p), "reopen")
        unit = loop.vendor_unit(self.conn, self.job_id)
        self.assertEqual([x["pid"] for x in unit["payments"]], [q])
        out = decide.decide(self.conn, self.token, [{
            "pid": q, "outcome": "match", "doc_id": d, "document_date": "2026-09-03",
            "expected_revision": self.rev(q)}])
        self.assertTrue(out["results"][0]["applied"])
        self.assertIsNone(loop.vendor_unit(self.conn, self.job_id))
        self.assertEqual(tuple(self.conn.execute(
            "SELECT why, outcome, handed FROM run_work WHERE pid=?", (p,)).fetchone()),
            ("reopen", "settled", 0))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_work WHERE job_id=? AND"
                                           " outcome IS NULL", (self.job_id,)).fetchone()[0], 0)

    def test_the_unit_is_bounded_and_carries_the_vendor_kb(self):
        import kb, loop
        kb.upsert_counterparty(self.conn, "Adobe", patterns=["adobe"], source="portal",
                               document_link="https://example.invalid/billing",
                               hint_sender="billing@adobe.com", hint_subject="Your invoice")
        self.n += 1
        self.row(self.n, counterparty="Adobe", booking_date="2026-09-02",
                 value_date="2026-09-02", remittance="r" * 300)
        pid = self.lineage_for(self.n)
        self.classify(pid, {"software"})
        self.settle(pid)
        self.doc(vendor="Adobe", issuer="I" * 300, document_date="2026-09-01")
        self.listed()
        unit = loop.vendor_unit(self.conn, self.job_id)
        p = unit["payments"][0]
        self.assertEqual(len(p["remittance"]), 80)
        self.assertEqual(len(p["candidates"][0]["issuer"]), 80)
        self.assertEqual((unit["kb"]["hint_sender"], unit["kb"]["portal"]),
                         ("billing@adobe.com", True))
        self.assertEqual(unit["search_window"], {"after": "2026-07-01", "before": "2026-11-01"})

    def test_add_months_clamps_to_the_month_end(self):
        import dates
        self.assertEqual(dates.add_months("2026-01-31", 1), "2026-02-28")
        self.assertEqual(dates.add_months("2026-10-01", 1), "2026-11-01")
        self.assertEqual(dates.add_months("2026-12-15", 1), "2027-01-15")

    def test_a_document_matched_elsewhere_or_dated_far_is_no_exact_fit(self):
        import loop, lineage
        pid = self.pay(who="Adobe", day="2026-09-02")
        twin = self.pay(who="Adobe", day="2026-09-03")
        taken = self.doc(vendor="Adobe", document_date="2026-09-02")
        self.machine_match(twin, taken, self.token)
        far = self.doc(vendor="Adobe", document_date="2026-10-04")    # 32 days: not near
        row = lineage.live_row(self.conn, lineage.projection(self.conn, pid))
        cands = loop.candidates(self.conn, pid, row, "Adobe")
        self.assertEqual([c["doc_id"] for c in cands], [far])          # taken is not offered
        self.assertIsNone(loop.exact_fit(self.conn, pid, row, "Adobe", cands))
        near = self.doc(vendor="Adobe", document_date="2026-10-03")   # 31 days: near
        cands = loop.candidates(self.conn, pid, row, "Adobe")
        self.assertEqual(loop.exact_fit(self.conn, pid, row, "Adobe", cands), near)
