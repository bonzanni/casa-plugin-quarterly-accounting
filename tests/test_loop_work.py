"""Simple loop §2.1–§2.2, rev 18.4 §R18.1/§R18.3: the work list (open, changed facts, a
handover's payments — a paired one included; never reopened by a later mail document, never
pending, never a payment expecting no document, never one the operator left missing),
walked by date one payment at a time; the payment unit's candidates, exact fit and search."""
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

    def test_a_document_filed_later_never_reopens_a_machine_pairing(self):
        """Rev 18.4 §R18.3: a later mail document never reopens a machine match or a machine
        proposal; it stays filed, unmatched."""
        import matches
        pid = self.pay()
        self.machine_match(pid, self.doc(), self.token)
        q = self.pay(who="Zapier", amount=20000)
        matches.propose_match(self.conn, pid=q, doc_id=self.doc(vendor="Zapier",
                                                                amount_minor=20000),
                              expected_revision=self.rev(q), token=self.token,
                              document_date="2026-09-01")
        self.file_later(self.doc())
        self.file_later(self.doc(amount_minor=20000, vendor=None))
        self.assertEqual(self.listed(), [])

    def file_later(self, doc_id):
        """The document as a filing now stamps it (documents.filed_seq, Task 4)."""
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET filed_seq=? WHERE doc_id=?",
                              (db.next_seq(self.conn), doc_id))

    def test_the_list_is_walked_by_date_one_payment_at_a_time(self):
        """Rev 18.4 §R18.1: date order, one payment per hand-out; an undecided one is
        handed again (D8), then the next."""
        z = self.pay(who="Zapier", day="2026-07-05")
        a2 = self.pay(who="Adobe", day="2026-09-05")
        a1 = self.pay(who="Adobe", day="2026-07-01")
        import loop
        loop.build_work(self.conn, self.job_id)
        self.assertEqual(loop.payment_unit(self.conn, self.job_id)["pid"], a1)
        self.assertEqual(loop.payment_unit(self.conn, self.job_id)["pid"], a1)   # again
        self.assertEqual(loop.payment_unit(self.conn, self.job_id)["pid"], z)
        self.assertEqual(loop.payment_unit(self.conn, self.job_id)["pid"], z)
        self.assertEqual(loop.payment_unit(self.conn, self.job_id)["pid"], a2)

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

    def test_a_live_proposals_alternative_is_free_for_another_payment(self):
        """Rev 18.4 §R18.4: the primary is held; an alternative holds nothing."""
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
        self.assertEqual((held[a], held[b]), ("other", None))
        self.assertEqual(loop.exact_fit(self.conn, p2, row, "Adobe", cands), b)
        self.assertFalse(matches.taken_elsewhere(self.conn, b, p2))  # the floor agrees
        self.assertTrue(matches.taken_elsewhere(self.conn, a, p2))

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

    def test_a_handed_document_beyond_the_cap_is_handed_out_first(self):
        """A handover's document (D17) is a must-show candidate: first, uncapped."""
        import asks, loop
        pid = self.pay(who="Adobe", day="2026-09-02")
        for k in range(9):                                   # nine closer extras
            self.doc(vendor="Figma", issuer="Figma", document_date="2026-09-0%d" % (k + 1))
        new = self.doc(vendor=None, document_date="2026-09-25")   # gap 23: last by date
        asks.request_work(self.conn, "handover", "operator", [new])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
        loop.build_work(self.conn, self.job_id, handover_docs=[new])
        unit = loop.payment_unit(self.conn, self.job_id)
        shown = [c["doc_id"] for c in unit["candidates"]]
        self.assertEqual((unit["pid"], shown[0]), (pid, new))
        self.assertEqual(len(shown), 1 + loop.CANDIDATES_MAX)

    def test_a_handover_after_the_payment_was_decided_reopens_it_in_the_run(self):
        import asks, decide, loop
        pid = self.pay(who="Adobe", day="2026-09-02")
        self.listed()
        loop.payment_unit(self.conn, self.job_id)
        decide.decide(self.conn, self.token, [{
            "pid": pid, "outcome": "missing", "reason": "x",
            "expected_revision": self.rev(pid)}])
        self.assertIsNone(loop.payment_unit(self.conn, self.job_id))   # nothing left
        usd = self.doc(currency="USD", amount_minor=11000, vendor=None)
        asks.request_work(self.conn, "handover", "operator", [usd])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
            self.assertEqual(loop.take_handovers(self.conn, self.job_id, [usd]), 1)
        unit = loop.payment_unit(self.conn, self.job_id)
        self.assertEqual((unit["pid"], unit["candidates"][0]["doc_id"]), (pid, usd))

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
        unit = loop.payment_unit(self.conn, self.job_id)
        self.assertEqual(unit["pid"], pid)
        self.assertIn(usd, [c["doc_id"] for c in unit["candidates"]])
        self.assertEqual(tuple(self.conn.execute("SELECT state, pass_id FROM work_requests WHERE"
                                           " request_id=?", (req,)).fetchone()),
                         ("taken", self.pass_id))
        self.assertEqual(loop.run_handover_docs(self.conn, self.job_id), [usd])

    def test_an_undecided_payment_is_handed_at_most_twice(self):
        """Rev 18.4 §R18.5: ATTEMPTS_MAX hand-outs without progress, then the next one;
        each ends "missing · search incomplete" (outcome NULL)."""
        import loop, queues
        pids = [self.pay(who="Adobe", day="2026-08-%02d" % (i + 1)) for i in range(3)]
        loop.build_work(self.conn, self.job_id)
        got = []
        while True:
            u = loop.payment_unit(self.conn, self.job_id)
            if u is None:
                break
            got.append(u["pid"])
        self.assertEqual(got, [p for p in pids for _ in range(queues.ATTEMPTS_MAX)])
        self.assertEqual(tuple(self.conn.execute(
            "SELECT count(*), min(attempts), max(attempts), count(outcome) FROM run_work WHERE"
            " job_id=?", (self.job_id,)).fetchone()), (3, 2, 2, 0))

    def test_a_search_is_the_handed_payments_and_capped(self):
        """Rev 18.4 §R18.1/§R18.5: a job search is recorded for the payment handed out now,
        with its refs, at most SEARCHES_MAX a run; its refs are the payment's owed items."""
        import loop, work
        p1 = self.pay(who="Adobe", day="2026-08-01")
        p2 = self.pay(who="Adobe", day="2026-08-02")
        self.listed()
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET listed_at=? WHERE job_id=?",
                              (db.now(), self.job_id))
        self.assertEqual(loop.payment_unit(self.conn, self.job_id)["pid"], p1)
        with self.assertRaisesRegex(db.Refusal, "the payment handed out now"):
            work.record_search(self.conn, pids=[p2], token=self.token, queries=["x"], refs=[])
        with self.assertRaisesRegex(db.Refusal, "refs"):
            work.record_search(self.conn, pids=[p1], token=self.token, queries=["x"])
        for k in range(loop.SEARCHES_MAX):
            out = work.record_search(self.conn, pids=[p1], token=self.token,
                                     queries=[f"q{k}"], refs=[f"m{k}:a1"])
        self.assertEqual(out["files"], ["m0:a1", "m1:a1", "m2:a1"])
        with self.assertRaisesRegex(db.Refusal, "decide it now"):
            work.record_search(self.conn, pids=[p1], token=self.token, queries=["q9"],
                               refs=[])

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

    def test_changed_facts_list_a_machine_match_again(self):
        changed = self.pay()
        self.machine_match(changed, self.doc(), self.token)
        self.row(1, counterparty="Adobe", amount_minor=10000, booking_date="2026-09-02",
                 value_date="2026-09-02", remittance="corrected by the bank")
        self.settle(changed)
        self.assertEqual(self.listed(), [changed])
        self.assertEqual(self.why_of(changed), "changed")

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
        p = loop.payment_unit(self.conn, self.job_id)
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
        p = loop.payment_unit(self.conn, self.job_id)
        self.assertEqual(p["exact_fit"], fit)
        self.assertEqual(p["candidates"][0]["doc_id"], fit)
        self.assertEqual(len(p["candidates"]), 1 + loop.CANDIDATES_MAX)
        self.assertEqual(p["pid"], pid)

    def test_building_again_never_reopens_a_decided_entry(self):
        """A handover onto a matched payment: the job keeps it (or asks, rev 18.4 §R18.3);
        building the list again in a new batch never reopens the decided entry."""
        import asks, decide, loop
        pid = self.pay()
        a = self.doc(vendor="Adobe", document_date="2026-09-01")
        self.machine_match(pid, a, self.token)
        usd = self.doc(currency="USD", amount_minor=11000, vendor=None)
        asks.request_work(self.conn, "handover", "operator", [usd])
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pass_id)
        self.assertEqual(loop.build_work(self.conn, self.job_id, handover_docs=[usd]), 1)
        self.assertEqual(loop.payment_unit(self.conn, self.job_id)["pid"], pid)
        with self.assertRaisesRegex(AssertionError, "refused"):    # never a re-match
            out = decide.decide(self.conn, self.token, [{
                "pid": pid, "outcome": "match", "doc_id": a, "document_date": "2026-09-01",
                "expected_revision": self.rev(pid)}])
            self.assertEqual(out["refused"], 0, "refused")
        decide.decide(self.conn, self.token, [{"pid": pid, "outcome": "keep",
                                                "expected_revision": self.rev(pid)}])
        self.run_claim(job_id=self.job_id)                     # the same job, a new batch
        self.assertEqual(loop.build_work(self.conn, self.job_id, handover_docs=[usd]), 1)
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work WHERE pid=?",
                                           (pid,)).fetchone()[0], "keep")
        self.assertIsNone(loop.payment_unit(self.conn, self.job_id))

    def test_a_payment_settled_meanwhile_is_not_handed_out(self):
        import loop
        pid = self.pay()
        d = self.doc()
        self.listed()
        rid = self.show(pid)                    # the operator pairs it between hand-outs
        self.operator_pair(pid=pid, doc_id=d, expected_revision=self.rev(pid), render_id=rid)
        self.assertIsNone(loop.payment_unit(self.conn, self.job_id))
        self.assertEqual(tuple(self.conn.execute("SELECT attempts, outcome FROM run_work WHERE"
                                                 " pid=?", (pid,)).fetchone()), (0, "settled"))

    def test_left_missing_between_hand_outs_is_settled(self):
        import loop
        pid = self.pay()
        self.listed()
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state='accepted-missing' WHERE"
                              " pid=?", (pid,))
        self.assertIsNone(loop.payment_unit(self.conn, self.job_id))
        self.assertEqual(self.conn.execute("SELECT outcome FROM run_work WHERE pid=?",
                                           (pid,)).fetchone()[0], "settled")

    def test_the_unit_is_bounded_and_carries_the_vendor_kb_and_its_window(self):
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
        unit = loop.payment_unit(self.conn, self.job_id)
        p = unit
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
