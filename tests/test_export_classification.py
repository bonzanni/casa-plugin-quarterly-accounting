# tests/test_export_classification.py
"""Issue #1: the import is the classification observation. bank-feed 0.20.0's
export carries each row's tags and tag_revision (casa-specialist-finance#86), so
every row it carries is observed at the import, and the sweep reads only the
lineages that owe bank-feed a write, a read-back, or an erasure check. Against the
REAL bank-feed (component v0.21.0), with a controlled clock for the note window
(design §3, rounds D1 and D2)."""
import csv
import datetime as dt
import io
import re
import unittest
from unittest import mock

from tests import _base  # noqa: F401  (puts server/ on sys.path)
from tests import sim, test_sweep_real
import db  # noqa: E402
import ledger  # noqa: E402
import lineage  # noqa: E402
import passes  # noqa: E402
import steps  # noqa: E402
import sweep  # noqa: E402

T0 = dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.timezone.utc)


class Base(test_sweep_real.Base):
    def setUp(self):
        super().setUp()
        self.now = T0
        p = mock.patch.object(db, "_clock", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)

    def later(self, seconds):
        self.now += dt.timedelta(seconds=seconds)

    def three_rows(self):
        self.bf.fetch([self.bf.row("2026-07-0%d" % (i + 1), ref="R%d" % i, amount=1000 + i,
                                   counterparty="Vendor%d" % i) for i in range(3)])
        rids = [r["row_id"] for r in self.bf.rows(state="active")]
        self.bf.call("tag_transaction", row_ids=rids, tags=["software"])
        return rids

    def settled(self):
        """Three payments mirrored, their notes confirmed outside the window."""
        rids = self.three_rows()
        self.new_pass()
        self.cycle()                         # writes acct::open and the notes
        self.later(3600)
        self.new_pass()
        self.cycle()                         # the read-back was inside the window: read once more
        self.later(3600)
        self.new_pass()
        return rids

    parse = staticmethod(test_sweep_real.TestNoteAsRendered.parse)

    def due(self):
        return sweep._due(self.conn)

    def accounting_notes(self, rid):
        return [n for n in self.bf.notes(rid) if n.startswith("Accounting revision ")]


class TestTheImportObserves(Base):
    def test_every_exported_row_is_fresh_after_the_import_without_a_read(self):
        rids = self.three_rows()
        self.new_pass()
        for rid in rids:
            p = lineage.projection(self.conn, self.pid_of(rid))
            self.assertTrue(lineage.is_fresh(self.conn, p))
            self.assertEqual((p["class_tags_json"], p["read_snapshot"]), ('["software"]', None))
            self.assertEqual(p["exp_kind"], "invoice")

    def test_a_settled_ledger_is_not_read_again(self):
        rids = self.settled()
        self.assertEqual(self.due(), [])
        page = sweep.list_projections(self.conn, token=self.token)
        self.assertEqual((page["projections"], page["remaining_in_cycle"]), ([], 0))
        for rid in rids:
            self.assertEqual(self.owned(rid), ["acct::open"])
            self.assertEqual(len(self.accounting_notes(rid)), 1)     # never restated

    def test_the_pass_report_counts_reads_not_import_stamps(self):
        self.settled()
        self.assertEqual(passes.throughput(self.conn, passes.current_pass(self.conn)["pass_id"]),
                         {"swept_this_pass": 0, "remaining_in_cycle": 0})

    def test_a_reclassification_needs_no_read_to_be_known(self):
        rids = self.settled()
        self.bf.call("untag_transaction", row_ids=[rids[0]], tags=["software"])
        self.bf.call("tag_transaction", row_ids=[rids[0]], tags=["refund"])
        self.later(3600)
        self.new_pass()
        p = lineage.projection(self.conn, self.pid_of(rids[0]))
        self.assertEqual((p["exp_kind"], p["read_snapshot"] == self.snap_id),
                         ("credit-note", False))
        # its tags already say acct::open, but the note changed: due for that write only
        self.assertEqual(self.due(), [self.pid_of(rids[0])])


class TestOwedWritesAreRead(Base):
    def test_an_owned_tag_removed_outside_is_due_and_repaired(self):
        rids = self.settled()
        self.wf(["acct::open"], rids[1], verb="untag_transaction")
        self.later(3600)
        self.new_pass()
        self.assertEqual(self.due(), [self.pid_of(rids[1])])
        self.cycle()
        self.assertEqual(self.owned(rids[1]), ["acct::open"])
        self.assertEqual(self.due(), [])

    def test_an_erasure_that_strips_tags_and_notes_is_repaired(self):
        # purge(user_work=erase) strips every tag and note on the rows it keeps; the
        # tag revision moves, so the notes are re-checked too
        rids = self.settled()
        self.bf.call("purge", before_date="2020-01-01", user_work="erase")
        self.assertEqual([self.bf.tags(r) for r in rids], [[], [], []])
        self.later(3600)
        self.new_pass()
        self.assertEqual(sorted(self.due()), sorted(self.pid_of(r) for r in rids))
        self.cycle()
        for rid in rids:
            self.assertEqual(self.owned(rid), ["acct::open"])
            self.assertEqual(len(self.accounting_notes(rid)), 1)

    def test_an_unrelated_tag_change_rechecks_the_note(self):
        rids = self.settled()
        self.bf.call("tag_transaction", row_ids=[rids[2]], tags=["q3-review"])
        self.later(3600)
        self.new_pass()
        self.assertEqual(self.due(), [self.pid_of(rids[2])])
        before = self.bf.notes(rids[2])
        self.cycle()                                   # read: visible, nothing written
        self.assertEqual(self.bf.notes(rids[2]), before)
        self.later(3600)
        self.new_pass()
        self.assertEqual(self.due(), [])


class TestTheNoteWindow(Base):
    def issue_add_note(self, rid):
        """A read that does not see the current note: the server hands out add_note."""
        page = sweep.list_projections(self.conn, token=self.token)
        item = next(i for i in page["projections"] if i["row_id"] == rid)
        text = self.bf.call("get_transaction", row_id=rid)
        tags, _notes, first_seen = self.parse(text)
        r = sweep.record_observation(self.conn, snapshot_id=self.snap_id, pid=item["pid"],
                                     token=self.token, observed_tags=tags, observed_notes=[],
                                     observed_first_seen=first_seen,
                                     observed_tag_revision=self.bf.tag_revision(rid))
        self.assertIn("add_note", r["instructions"])
        return r["instructions"]

    def record_read(self, rid, text):
        tags, notes, first_seen = self.parse(text)
        rev = int(re.search(r"^Tag revision: (\d+) ", text, re.M)[1])
        return sweep.record_observation(self.conn, snapshot_id=self.snap_id,
                                        pid=self.pid_of(rid), token=self.token,
                                        observed_tags=tags, observed_notes=notes,
                                        observed_first_seen=first_seen,
                                        observed_tag_revision=rev)

    def test_d1_a_note_write_landing_after_the_confirming_read_is_caught(self):
        # round D1 (Astra S2): an add_note carried out after a later read confirmed the
        # note buries it; notes do not move the tag revision
        rids = self.settled()
        rid = rids[0]
        with db.tx(self.conn):          # a later decision will change the note: make it due
            self.conn.execute("UPDATE projections SET observed_revision=NULL WHERE pid=?",
                              (self.pid_of(rid),))
        stale = self.issue_add_note(rid)                    # carried by a delegation...
        self.later(30)
        self.cycle()                                        # ...while another pass confirms
        self.bf.call("add_note", row_ids=[rid], note="Accounting revision 0: stale",
                     author="agent", workflow=stale["workflow"],
                     expected_generation=stale["expected_generation"],
                     expected_ledger=stale["expected_ledger"])   # the late write lands
        self.later(3600)
        self.new_pass()
        self.assertIn(self.pid_of(rid), self.due())          # the confirmation did not stand
        self.cycle()
        self.assertTrue(self.accounting_notes(rid)[-1].startswith(
            lineage.note_text(self.conn, self.pid_of(rid))[:22]))
        self.assertNotIn("stale", self.accounting_notes(rid)[-1])

    def test_d2_a_confirmation_is_dated_by_its_snapshot_not_by_its_recording(self):
        # round D2 (Astra S2): a read taken inside the window and recorded after it
        # must not count as a read taken after it
        rids = self.settled()
        rid = rids[1]
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET observed_revision=NULL WHERE pid=?",
                              (self.pid_of(rid),))
        ins = self.issue_add_note(rid)
        self.bf.call("add_note", row_ids=[rid], note=ins["add_note"], author="agent",
                     workflow=ins["workflow"], expected_generation=ins["expected_generation"],
                     expected_ledger=ins["expected_ledger"])
        text = self.bf.call("get_transaction", row_id=rid)  # read inside the window
        self.later(steps.CEILING_ASSUMED_S + 100)            # recorded after it
        self.assertEqual(self.record_read(rid, text)["instructions"], {})
        p = lineage.projection(self.conn, self.pid_of(rid))
        self.assertFalse(sweep.note_confirmed(p, self.bf.tag_revision(rid)))
        self.later(3600)
        self.new_pass()
        self.assertIn(self.pid_of(rid), self.due())

    def test_a_read_after_the_window_confirms(self):
        rids = self.settled()
        p = lineage.projection(self.conn, self.pid_of(rids[0]))
        self.assertTrue(sweep.note_confirmed(p, self.bf.tag_revision(rids[0])))


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
                                        ledger_instance=self.bf.last_export_instance)
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

    def test_a_read_without_its_tag_revision_is_refused(self):
        rids = self.three_rows()
        self.new_pass()
        text = self.bf.call("get_transaction", row_id=rids[0])
        tags, notes, first_seen = self.parse(text)
        with self.assertRaises(db.Refusal) as cm:
            sweep.record_observation(self.conn, snapshot_id=self.snap_id,
                                     pid=self.pid_of(rids[0]), token=self.token,
                                     observed_tags=tags, observed_notes=notes,
                                     observed_first_seen=first_seen)
        self.assertIn("Tag revision", str(cm.exception))


class TestCatchUpConverges(Base):
    def test_a_quarter_larger_than_one_delegation_finishes_in_a_few_passes(self):
        # the measurement behind issue #1: ~50 rows per delegation. 120 payments: the
        # first passes write (budgeted), then everything settles and a pass reads none
        self.bf.fetch([self.bf.row("2026-07-%02d" % (1 + i % 28), ref="C%d" % i,
                                   amount=1000 + i, counterparty="V%d" % i)
                       for i in range(120)])
        rids = [r["row_id"] for r in self.bf.rows(state="active")]
        self.bf.call("tag_transaction", row_ids=rids, tags=["software"])
        reads = []
        for _ in range(8):
            self.new_pass()
            before = len(self.due())
            sim.sweep_within(self.conn, self.bf, self.token, 50)
            reads.append(before)
            self.later(3600)
            if not before:
                break
        self.assertEqual(reads[-1], 0, reads)
        self.assertLessEqual(len(reads), 7, reads)
        self.assertTrue(all(self.owned(r) == ["acct::open"] for r in rids))


if __name__ == "__main__":
    unittest.main()
