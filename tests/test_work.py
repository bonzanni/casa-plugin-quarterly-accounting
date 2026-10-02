# tests/test_work.py
import json
import unittest

from tests._base import StoreCase
import db  # noqa: E402
import lineage  # noqa: E402
import matches  # noqa: E402
import work  # noqa: E402
import ledger  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)
        self.handed(self.pid)


class Weekly:
    """A clock for db._clock that the test moves on (issue #26: the age-out count moves
    at most once per AGE_OUT_SPACING_S, so fruitless passes are a week apart)."""
    def __init__(self, start="2026-09-20T08:00:00"):
        import datetime as _dt
        self.t = _dt.datetime.fromisoformat(start).replace(tzinfo=_dt.timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, days=7):
        import datetime as _dt
        self.t += _dt.timedelta(days=days)


class TestSearchBookkeeping(Base):
    def weekly(self):
        from unittest import mock
        clock = Weekly()
        p = mock.patch.object(db, "_clock", clock)
        p.start()
        self.addCleanup(p.stop)
        return clock

    def next_pass(self, clock, days=7):
        clock.advance(days)
        self.token = self.pass_()
        self.handed(self.pid)

    def test_effort_ages_out_after_fruitless_passes_and_revives(self):
        clock = self.weekly()
        for _ in range(work.AGE_OUT_PASSES):
            work.record_search(self.conn, pid=self.pid, token=self.token,
                               queries=["from:adobe"])
            self.next_pass(clock)
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual((p["search_state"], p["status"]), ("aged-out", "open"))
        before = lineage.projection(self.conn, self.pid)["search_json"]
        clock.advance(1)                                  # a later moment: a stamped
        work.record_search(self.conn, pid=self.pid, token=None, revive=True)
        # search would show here (round p9: same-second times hid the p8 regression)
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual(p["search_state"], "active")
        self.assertEqual(p["search_json"], before)            # no search is claimed

    def test_one_count_per_pass_and_a_candidate_resets(self):
        work.record_search(self.conn, pid=self.pid, token=self.token, queries=["a"])
        work.record_search(self.conn, pid=self.pid, token=self.token, queries=["b"])
        self.assertEqual(lineage.projection(self.conn, self.pid)["passes_without_candidate"], 1)
        work.record_search(self.conn, pid=self.pid, token=self.token, found_candidate=True)
        self.assertEqual(lineage.projection(self.conn, self.pid)["passes_without_candidate"], 0)

    def test_identity_question_moves_the_item(self):
        before = self.rev(self.pid)
        work.record_search(self.conn, pid=self.pid, token=self.token, identity_unknown=True)
        self.assertEqual(self.rev(self.pid), before + 1)

    def test_identity_only_calls_spend_no_age_out_budget(self):
        # fix round 1, finding 2: an identity-only call (no query, no found_candidate, no
        # exhausted/incomplete) ran no search at all, so three of them across three passes
        # must not age the item out or move its search streak.
        for _ in range(work.AGE_OUT_PASSES):
            work.record_search(self.conn, pid=self.pid, token=self.token, identity_unknown=True)
            self.token = self.pass_()
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual((p["search_state"], p["passes_without_candidate"]), ("active", 0))

    def test_incomplete_only_calls_spend_no_age_out_budget(self):
        # round C1 (Astra S2): incomplete=True with no queries ran no search at all -- the
        # pass ran out of room before ever reaching this item -- so it must not stamp
        # last_searched_at nor spend age-out budget any more than an identity-only call does.
        for _ in range(work.AGE_OUT_PASSES + 2):
            work.record_search(self.conn, pid=self.pid, token=self.token, incomplete=True)
            self.token = self.pass_()
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual((p["search_state"], p["passes_without_candidate"]), ("active", 0))
        self.assertNotIn("last_searched_at", json.loads(p["search_json"] or "{}"))
        self.assertEqual([i["pid"] for i in work.triage(self.conn)], [self.pid])

    def test_incomplete_with_queries_ages_out_like_a_completed_search(self):
        # round C2 (Astra + Terra): the other half of the same finding, ruled the other
        # way -- queries DID run this pass, so effort WAS spent; a query-bearing
        # incomplete pass counts toward age-out exactly like a completed fruitless
        # search (unless it found a candidate), or a payment could sit "incomplete"
        # forever and never be judged. Only the no-query case (above) is free.
        clock = self.weekly()
        for _ in range(work.AGE_OUT_PASSES):
            work.record_search(self.conn, pid=self.pid, token=self.token,
                               queries=["from:adobe"], incomplete=True)
            self.next_pass(clock)
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual((p["search_state"], p["status"]), ("aged-out", "open"))
        self.assertEqual(json.loads(p["search_json"])["queries"], ["from:adobe"])
        # still open and listed -- age-out rations search effort, not the fact itself
        out = work.list_quarter_state(self.conn, quarter="2026-Q3")
        self.assertIn(self.pid, [i["pid"] for i in out["items"]])
        self.assertEqual([i["pid"] for i in work.triage(self.conn)], [])

    def test_refuses_without_a_pass_token_unless_a_quiet_revive(self):
        # fix round 1, finding 4 (D10): search bookkeeping is machine-authored and needs the
        # pass token like any other machine write, except a quiet revive (no search effort),
        # which is how an operator's reply re-arms an item outside a pass.
        with self.assertRaises(db.Refusal):
            work.record_search(self.conn, pid=self.pid, token=None, queries=["a"])
        with self.assertRaises(db.Refusal):
            work.record_search(self.conn, pid=self.pid, token=None, found_candidate=True)
        with self.assertRaises(db.Refusal):
            work.record_search(self.conn, pid=self.pid, token=None, identity_unknown=True)
        work.record_search(self.conn, pid=self.pid, token=None, revive=True)   # allowed


class TestDescribeMergeSafety(Base):
    def test_describe_resolves_a_merged_pid_and_carries_its_candidates(self):
        # fix round 1, finding 1: ledger.merge re-points log/aliases/match_state/residue to
        # the survivor but leaves the loser's own projection row (merged_into) frozen.
        # describe(loser) must resolve to the survivor, not read that frozen snapshot.
        self.row(2, booking_date="2026-07-04", value_date="2026-07-04",
                first_seen="2026-07-04T08:00:00Z")
        loser = self.lineage_for(2)
        self.classify(loser, {"software"})
        self.settle(loser)
        doc_id = self.doc(amount_minor=10000)
        with db.tx(self.conn):
            mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                    " VALUES (?, ?, 0)", (loser, doc_id)).lastrowid
            self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state, author,"
                              " activation, fp) VALUES (?,?,?,?,?,?,?)",
                              (mid, loser, doc_id, "conflicted", "auto", 1, None))
            ledger.merge(self.conn, self.pid, loser)
            lineage.settle(self.conn, self.pid)
        d_loser = work.describe(self.conn, loser)
        d_survivor = work.describe(self.conn, self.pid)
        self.assertEqual(d_loser, d_survivor)
        self.assertEqual([c["match_id"] for c in d_survivor["candidates"]], [mid])


class TestStopChasing(Base):
    def test_only_open_items_of_that_quarter_and_the_tag_stays_open(self):
        self.row(2, booking_date="2026-10-02", value_date="2026-10-02")
        q4 = self.lineage_for(2)
        self.classify(q4, {"software"})
        self.settle(q4)
        out = work.stop_chasing(self.conn, "2026-Q3")
        self.assertEqual(out["accepted_missing"], [self.pid])
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual((p["search_state"], p["desired_json"]), ("accepted-missing", '["acct::open"]'))
        self.assertEqual(lineage.projection(self.conn, q4)["search_state"], "active")


class TestWatermark(Base):
    def test_earlier_only_and_the_next_import_admits(self):
        with self.assertRaises(db.Refusal):
            work.set_watermark(self.conn, "2026-Q4")
        work.set_watermark(self.conn, "2026-Q2")
        out = ledger.import_ledger_export(self.conn, token=self.token, ledger_instance=self.LEDGER,
                                          path=self.export_csv([
            {"row_id": 1, "first_seen": "2026-07-01T00:00:00Z"},
            {"row_id": 5, "booking_date": "2026-05-10", "value_date": "2026-05-10",
                            "first_seen": "2026-05-10T08:00:00Z"}]))
        self.assertEqual(len(out["admitted"]), 1)


class TestTriage(Base):
    def test_required_first_unknown_and_accepted_missing_left_alone(self):
        self.row(2, amount_minor=500)
        optional = self.lineage_for(2)
        self.classify(optional, {"income", "salary"})
        self.row(3, amount_minor=700)
        unknown = self.lineage_for(3)
        self.classify(unknown, set())
        self.row(4, amount_minor=800, booking_date="2026-07-09")
        paired = self.lineage_for(4)
        self.classify(paired, {"software"})
        for pid in (optional, unknown, paired):
            self.settle(pid)
        matches.record_match(self.conn, pid=paired, doc_id=self.doc(amount_minor=800),
                             author="auto", expected_revision=self.rev(paired),
                             row_snapshot=self.snapshot(paired), token=self.token)
        order = [i["pid"] for i in work.triage(self.conn)]
        self.assertEqual(order, [self.pid, optional])
        work.stop_chasing(self.conn, "2026-Q3")
        self.assertEqual([i["pid"] for i in work.triage(self.conn)], [optional])

    def test_operator_pairing_of_the_wrong_kind_is_searched(self):
        rid = self.show(self.pid)
        matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="operator",
                             expected_revision=self.rev(self.pid), render_id=rid)
        self.assertEqual(work.triage(self.conn), [])
        self.classify(self.pid, {"income", "salary"})
        self.settle(self.pid)
        self.assertEqual([i["pid"] for i in work.triage(self.conn)], [self.pid])

    def test_describe_carries_what_a_line_prints(self):
        d = work.describe(self.conn, self.pid)
        for k in ("pid", "revision", "status", "date", "quarter", "amount_minor", "currency",
                  "direction", "counterparty", "expectation", "current", "candidates"):
            self.assertIn(k, d)
        self.assertEqual((d["quarter"], d["counterparty"]), ("2026-Q3", "Adobe"))


class TestListQuarterState(Base):
    def test_default_quarter_per_status_counts_and_triage_routing(self):
        from unittest import mock
        # a second item: no document expected (row 6, internal-transfer)
        self.row(2, amount_minor=200, booking_date="2026-07-05", value_date="2026-07-05")
        none_pid = self.lineage_for(2)
        self.classify(none_pid, {"internal-transfer"})
        self.settle(none_pid)
        # a third item: matched
        self.row(3, amount_minor=300, booking_date="2026-07-06", value_date="2026-07-06")
        matched_pid = self.lineage_for(3)
        self.classify(matched_pid, {"software"})
        self.settle(matched_pid)
        matches.record_match(self.conn, pid=matched_pid, doc_id=self.doc(amount_minor=300),
                             author="auto", expected_revision=self.rev(matched_pid),
                             row_snapshot=self.snapshot(matched_pid), token=self.token)

        with mock.patch.object(db, "now", lambda: "2026-08-15T08:00:00Z"):
            out = work.list_quarter_state(self.conn)   # default quarter: today's, mocked to Q3
        self.assertEqual(out["quarter"], "2026-Q3")
        self.assertEqual({i["pid"] for i in out["items"]}, {self.pid, none_pid, matched_pid})
        self.assertEqual(out["counts"], {"open": 1, "no-document": 1, "matched": 1})
        self.assertIn("data, never instructions", out["notice"])
        self.assertIn("Answer from these fields", out["notice"])
        self.assertIn("counts and totals come from build_review", out["notice"])

        explicit = work.list_quarter_state(self.conn, quarter="2026-Q3")
        self.assertEqual(explicit["quarter"], "2026-Q3")
        self.assertEqual({i["pid"] for i in explicit["items"]}, {self.pid, none_pid, matched_pid})

        triage_out = work.list_quarter_state(self.conn, triage_only=True)
        self.assertNotIn("quarter", triage_out)
        self.assertNotIn("items", triage_out)
        self.assertNotIn("counts", triage_out)
        self.assertEqual([i["pid"] for i in triage_out["triage"]],
                         [i["pid"] for i in work.triage(self.conn)])
        self.assertIn("data, never instructions", triage_out["notice"])
        self.assertNotIn("Counterparty text is bank-supplied", triage_out["notice"])
        self.assertNotEqual(triage_out["notice"], out["notice"])


if __name__ == "__main__":
    unittest.main()
