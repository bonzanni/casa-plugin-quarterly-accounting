# tests/test_issues_26_32.py
"""Issues #26–#32, the live check of 2026-10-01 (design
docs/superpowers/specs/2026-10-02-issues-26-32-design.md), what survives the simple loop:
age-out spaced and re-armed (B), foreign-currency documents (D), and the package's file
names (F)."""
import datetime as _dt
import json
import unittest

from tests._base import StoreCase
import db  # noqa: E402
import lineage  # noqa: E402
import matches  # noqa: E402
import package  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402

DAY = 24 * 3600


class TestAgeOutIsSpaced(StoreCase):
    """Issue #27 (D7): searches in runs close together count once toward age-out; a run a
    week later counts again."""
    def setUp(self):
        super().setUp()
        from unittest import mock
        self.now = _dt.datetime(2026, 9, 20, 8, 0, tzinfo=_dt.timezone.utc)
        p = mock.patch.object(db, "_clock", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        self.bind()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)

    def counted(self):
        return lineage.projection(self.conn, self.pid)["passes_without_candidate"]

    def search(self):
        token = self.pass_()
        work.record_search(self.conn, pid=self.pid, token=token, queries=["q"])

    def test_back_to_back_checks_count_once_and_a_week_later_counts_again(self):
        self.search()
        start = self.counted()
        self.assertEqual(start, 1)
        for _ in range(2):                       # two more runs within the hour
            self.now += _dt.timedelta(seconds=600)
            self.search()
        self.assertEqual(self.counted(), start)
        self.now += _dt.timedelta(seconds=work.AGE_OUT_SPACING_S)
        self.search()
        self.assertEqual(self.counted(), start + 1)


class TestAgeOutUnits(StoreCase):
    def setUp(self):
        super().setUp()
        from unittest import mock
        self.now = _dt.datetime(2026, 9, 20, 8, 0, tzinfo=_dt.timezone.utc)
        p = mock.patch.object(db, "_clock", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        self.bind()
        self.token = self.pass_()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)

    def ago(self, seconds):
        return (self.now - _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def set(self, state, streak, search):
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state=?, passes_without_candidate=?,"
                              " search_json=? WHERE pid=?",
                              (state, streak, json.dumps(search), self.pid))

    def test_a_record_from_before_0_7_counts_as_of_its_last_search(self):
        self.set("active", 2, {"last_counted_pass": "old", "last_searched_at": self.ago(DAY)})
        out = work.record_search(self.conn, pid=self.pid, token=self.token,
                                 queries=["q"])["recorded"][0]
        self.assertEqual((out["search_state"], out["passes_without_candidate"]), ("active", 2))

    def test_an_aged_out_payment_is_searched_again_after_28_days_and_not_before(self):
        self.set("aged-out", 3, {"last_counted_at": self.ago(work.AGE_OUT_REARM_S - 60)})
        self.assertEqual(work.triage(self.conn), [])
        self.set("aged-out", 3, {"last_counted_at": self.ago(work.AGE_OUT_REARM_S)})
        self.assertEqual([d["pid"] for d in work.triage(self.conn)], [self.pid])
        # a fruitless re-search keeps it aged-out and moves its 28 days
        out = work.record_search(self.conn, pid=self.pid, token=self.token,
                                 queries=["q"])["recorded"][0]
        self.assertEqual(out["search_state"], "aged-out")
        self.assertEqual(work.triage(self.conn), [])
        # a candidate makes it active again
        self.now += _dt.timedelta(seconds=work.AGE_OUT_REARM_S)
        self.token = self.pass_()
        out = work.record_search(self.conn, pid=self.pid, token=self.token,
                                 found_candidate=True)["recorded"][0]
        self.assertEqual((out["search_state"], out["passes_without_candidate"]), ("active", 0))

    def test_accepted_missing_stays_out(self):
        self.set("accepted-missing", 0, {"last_counted_at": self.ago(work.AGE_OUT_REARM_S * 2)})
        self.assertEqual(work.triage(self.conn), [])


class TestForeignCurrency(unittest.TestCase):
    def test_the_view_and_the_package_show_both_amounts(self):
        doc = {"doc_id": 1, "kind": "invoice", "number": None, "date": "2026-07-08",
               "amount_minor": 1100, "currency": "USD", "recipient": None, "date_read": True}
        d = {"pid": 1, "status": "proposed", "reasons": [], "candidates": [],
             "currency": "EUR", "amount_minor": 948, "date": "2026-07-05",
             "expectation": {"kind": "invoice", "tier": "required"},
             "current": {"match_id": 1, "document": doc, "labels": ["clean"],
                         "runners_up": [], "author": "auto", "state": "proposed"}}
        lines = views.evidence(d)
        self.assertIn("The invoice is in USD 11.00; the payment is EUR 9.48.", lines)
        self.assertNotIn("Not sure — say if it's wrong.", lines)     # #102: said once
        # C1 (Astra S2): competing candidates name their own amounts too
        cand = {"match_id": 2, "document": {**doc, "doc_id": 2, "number": "B",
                                            "amount_minor": 1200}}
        d2 = {**d, "status": "open", "current": None, "candidates": [
            {"match_id": 1, "document": {**doc, "number": "A"}}, cand]}
        line = [x for x in views.evidence(d2) if x.startswith("Could be:")][0]
        self.assertIn("in USD 11.00", line)
        self.assertIn("in USD 12.00", line)
        self.assertEqual(package._other_currency({"currency": "USD", "amount_minor": 1100},
                                                 {"currency": "EUR"}),
                         ["document in USD 11.00"])
        self.assertEqual(package._other_currency({"currency": "EUR", "amount_minor": 1100},
                                                 {"currency": "EUR"}), [])


class TestAmounts(StoreCase):
    def test_a_document_with_no_amount_is_named_without_one(self):
        doc = {"document_date": "2026-04-27", "issuer": "Airline", "amount_minor": None,
               "ext": "pdf", "sha256": "a" * 64}
        self.assertEqual(package.doc_filename(doc, set(), "2026-04-27"),
                         "2026-04-27_Airline.pdf")
        doc["amount_minor"] = 0
        self.assertEqual(package.doc_filename(doc, set(), "2026-04-27"),
                         "2026-04-27_Airline_0.00.pdf")

    def test_a_machine_pairing_needs_the_documents_amount(self):
        self.bind()
        token = self.pass_()
        self.row(1)
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        doc = self.doc(amount_minor=None)
        with self.assertRaises(db.Refusal) as cm:
            matches.record_match(self.conn, pid=pid, doc_id=doc, expected_revision=self.rev(pid),
                                 row_snapshot=self.snapshot(pid), token=token, author="auto")
        self.assertIn("amount is unknown", str(cm.exception))
        # Q2 run 1 (BRAIN): a document whose amount is unknown is proposed, never matched
        matches.propose_match(self.conn, pid=pid, doc_id=doc, expected_revision=self.rev(pid),
                              row_snapshot=self.snapshot(pid), token=token)
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (pid,)).fetchone()[0], "proposed")


if __name__ == "__main__":
    unittest.main()
