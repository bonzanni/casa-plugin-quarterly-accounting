# tests/test_export_classification.py
"""Issue #1: the import is the classification observation. bank-feed 0.20.0's
export carries each row's tags and tag_revision (casa-specialist-finance#86), so
every row it carries is observed at the import, with no read of the row. Against the
REAL bank-feed (component v0.21.0)."""
import csv
import io
import unittest

from tests._base import StoreCase
from tests import bankfeed
import db  # noqa: E402
import ledger  # noqa: E402
import lineage  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bf = bankfeed.Ledger(self.tmp / "bankfeed")
        self.addCleanup(self.bf.close)
        self.bf.account()
        self.bind(account=bankfeed.Ledger.ACCOUNT)

    def new_pass(self):
        """A run's pass (pass_) with bank-feed's own ledger state, and its import under the
        run's acquisition."""
        self.token = self.pass_(generation=self.bf.generation(), registered=self.bf.registered(),
                                instance=self.bf.instance())
        out = ledger.import_ledger_export(self.conn, path=self.bf.export(), token=self.token,
                                          ledger_instance=self.bf.last_export_instance,
                                          acq=self.acq)
        self.snap_id = out["snapshot"]
        return out

    def three_rows(self):
        self.bf.fetch([self.bf.row("2026-07-0%d" % (i + 1), ref="R%d" % i, amount=1000 + i,
                                   counterparty="Vendor%d" % i) for i in range(3)])
        rids = [r["row_id"] for r in self.bf.rows(state="active")]
        self.bf.call("tag_transaction", row_ids=rids, tags=["software"])
        return rids

    def pid_of(self, row_id):
        return self.conn.execute("SELECT pid FROM aliases WHERE row_id=?", (row_id,)).fetchone()[0]


class TestTheImportObserves(Base):
    def test_every_exported_row_is_fresh_after_the_import_without_a_read(self):
        rids = self.three_rows()
        self.new_pass()
        for rid in rids:
            p = lineage.projection(self.conn, self.pid_of(rid))
            self.assertTrue(lineage.is_fresh(self.conn, p))
            self.assertEqual((p["class_tags_json"], p["class_observed_snapshot"]),
                             ('["software"]', self.snap_id))
            self.assertEqual(p["exp_kind"], "invoice")

    def test_a_reclassification_needs_no_read_to_be_known(self):
        rids = self.three_rows()
        self.new_pass()
        self.end_live_pass()
        self.bf.call("untag_transaction", row_ids=[rids[0]], tags=["software"])
        self.bf.call("tag_transaction", row_ids=[rids[0]], tags=["refund"])
        self.new_pass()
        p = lineage.projection(self.conn, self.pid_of(rids[0]))
        self.assertEqual((p["exp_kind"], p["class_observed_snapshot"]),
                         ("credit-note", self.snap_id))
        self.assertTrue(lineage.is_fresh(self.conn, p))


class TestTheExportFloor(Base):
    def csv_export(self, header, rows):
        import casa_handoff
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return casa_handoff.publish("bank-feed", "ledger-export-test.csv",
                                    data=buf.getvalue().encode())["path"]

    def test_an_export_without_tags_is_refused_whole(self):
        self.three_rows()
        full = self.bf.export()
        import casa_handoff
        name, data = casa_handoff.capture(full)
        rows = list(csv.DictReader(io.StringIO(data.decode())))
        header = [h for h in rows[0] if h not in ("tags", "tag_revision")]
        path = self.csv_export(header, [{k: r[k] for k in header} for r in rows])
        self.token = self.pass_(generation=self.bf.generation(), registered=self.bf.registered(),
                                instance=self.bf.instance())
        with self.assertRaises(db.Refusal) as cm:
            ledger.import_ledger_export(self.conn, path=path, token=self.token,
                                        ledger_instance=self.bf.last_export_instance,
                                        acq=self.acq)
        self.assertIn("below this plugin's floor (0.20.0", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0)

    def test_a_malformed_tag_is_refused_whole(self):
        for bad in ({"tags": "software,Not A Tag", "tag_revision": "3"},
                    {"tags": "software", "tag_revision": "x"},
                    {"tags": "software", "tag_revision": "-1"}):
            with self.assertRaises(db.Refusal):
                ledger._tags_of({"row_id": 1, **bad})

    def test_jsonl_and_csv_tags_parse_alike(self):
        self.assertEqual(ledger._tags_of({"tags": ["b", "a"], "tag_revision": 4}), (["a", "b"], 4))
        self.assertEqual(ledger._tags_of({"tags": "b,a", "tag_revision": "4"}), (["a", "b"], 4))
        self.assertEqual(ledger._tags_of({"tags": "", "tag_revision": "0"}), ([], 0))


if __name__ == "__main__":
    unittest.main()
