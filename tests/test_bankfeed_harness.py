# tests/test_bankfeed_harness.py
"""The harness is only worth having if it IS bank-feed. These pin the
upstream behaviours the whole plan relies on, at the floor tree."""
import unittest

from tests._base import ROOT, TempEnv
from tests import bankfeed


class TestHarness(TempEnv):
    def setUp(self):
        super().setUp()
        self.bf = bankfeed.Ledger(self.tmp / "bankfeed")
        self.addCleanup(self.bf.close)
        self.bf.account()

    def test_floor_tree_is_bank_feed_0_20_0(self):
        import json
        m = json.loads((bankfeed.plugin_root() / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(m["version"], "0.20.0")

    def test_the_floor_export_carries_tags_and_a_tag_revision(self):
        # issue #1 rests on casa-specialist-finance#86: the export names each row's tags
        # and a revision that moves on every tag change, removals included
        import ledger
        import casa_handoff
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        rid = self.bf.rows()[0]["row_id"]
        self.bf.call("tag_transaction", row_ids=[rid], tags=["software"])
        rows = ledger.parse(*casa_handoff.capture(self.bf.export()))
        self.assertEqual((rows[0]["tags"], rows[0]["tag_revision"] > 0), (["software"], True))
        r1 = rows[0]["tag_revision"]
        self.bf.call("untag_transaction", row_ids=[rid], tags=["software"])
        rows = ledger.parse(*casa_handoff.capture(self.bf.export()))
        self.assertEqual(rows[0]["tags"], [])
        self.assertNotEqual(rows[0]["tag_revision"], r1)
        self.assertEqual(rows[0]["tag_revision"], self.bf.tag_revision(rid))

    def test_pending_to_booked_is_a_supersession(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1", status="PDNG")])
        first = self.bf.rows()[0]["row_id"]
        stats = self.bf.fetch([self.bf.row("2026-07-06", ref="R1", status="BOOK")])
        self.assertEqual((stats["inserted"], stats["superseded"]), (1, 1))
        rows = {r["row_id"]: r for r in self.bf.rows()}
        self.assertEqual(rows[first]["state"], "superseded")
        self.assertIn(rows[first]["superseded_by"], rows)

    def test_first_seen_survives_an_in_place_update(self):
        # D4 relies on it: `first_seen` is written at insert and never rewritten.
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1", amount=10000)])
        before = self.bf.rows()[0]
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1", amount=9000)])
        after = self.bf.rows()[0]
        self.assertEqual(after["row_id"], before["row_id"])
        self.assertEqual(after["amount_minor"], 9000)       # corrected in place
        self.assertEqual(after["first_seen"], before["first_seen"])

    def test_export_carries_every_state_and_value_date(self):
        import csv, io
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1", status="PDNG")])
        self.bf.fetch([self.bf.row("2026-07-06", ref="R1", status="BOOK")])
        path = self.bf.export()
        import casa_handoff
        _, data = casa_handoff.capture(path)
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
        self.assertEqual(sorted(r["state"] for r in rows), ["active", "superseded"])
        for col in ("row_id", "account_id", "first_seen", "booking_date", "value_date",
                    "state", "superseded_by", "needs_review", "review_reason",
                    "direction", "status", "amount_minor", "currency",
                    "counterparty", "remittance"):
            self.assertIn(col, rows[0], col)
        self.assertNotIn("raw_json", rows[0])

    def test_acct_tag_requires_workflow_and_generation(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        rid = self.bf.rows()[0]["row_id"]
        refused = self.bf.call("tag_transaction", row_ids=[rid], tags=["acct::open"])
        self.assertNotIn("acct::open", self.bf.tags(rid), refused)
        ok = self.bf.call("tag_transaction", row_ids=[rid], tags=["acct::open"],
                          workflow="acct@0.1.0", expected_generation=self.bf.generation())
        self.assertIn("acct::open", self.bf.tags(rid), ok)
        self.assertIn("acct@0.1.0", self.bf.registered())

    def test_acct_tag_keeps_the_row_in_the_classifier_queue(self):
        # spec §Testing round 9/10: pinned against real untagged_only / queue_totals.
        import rules
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        rid = self.bf.rows()[0]["row_id"]
        self.bf.call("tag_transaction", row_ids=[rid], tags=["acct::open"],
                     workflow="acct@0.1.0", expected_generation=self.bf.generation())
        workable, parked = rules.queue_totals(self.bf.conn)
        self.assertEqual((workable, parked), (1, 0))
        listed = self.bf.call("list_transactions", untagged_only=True)
        self.assertIn("#%d" % rid, listed)

    def test_casa_handoff_is_the_same_file_in_both_trees(self):
        up = bankfeed.plugin_root() / "server/casa_handoff.py"
        self.assertEqual((ROOT / "server/casa_handoff.py").read_bytes(), up.read_bytes())

    def test_below_floor_tree_runs_in_a_subprocess(self):
        out = bankfeed.run_below_floor("import json, pathlib;"
                                       "print(json.loads((PLUGIN_ROOT/'.claude-plugin/plugin.json').read_text())['version'])")
        self.assertEqual(out.strip(), "0.12.2")


if __name__ == "__main__":
    unittest.main()
