# tests/test_work.py
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


class TestSearchBookkeeping(Base):
    def test_effort_ages_out_after_fruitless_passes_and_revives(self):
        from unittest import mock
        with mock.patch.object(db, "now", lambda: "2026-09-20T08:00:00Z"):
            for _ in range(work.AGE_OUT_PASSES):
                work.record_search(self.conn, pid=self.pid, token=self.token,
                                   queries=["from:adobe"])
                self.token = self.pass_()
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual((p["search_state"], p["status"]), ("aged-out", "open"))
        before = lineage.projection(self.conn, self.pid)["search_json"]
        with mock.patch.object(db, "now", lambda: "2026-09-27T08:00:00Z"):   # a later moment:
            work.record_search(self.conn, pid=self.pid, token=None, revive=True)  # a stamped
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


if __name__ == "__main__":
    unittest.main()
