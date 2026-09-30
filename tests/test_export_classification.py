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
        # the store was made before the clock was set: date its epoch (issue #14) a day
        # before T0, as a store installed well before these passes
        with db.tx(self.conn):
            self.conn.execute("UPDATE meta SET value=? WHERE key='store_epoch_at'",
                              ((T0 - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),))

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

    def write(self, rid, ins):
        self.bf.call("add_note", row_ids=[rid], note=ins["add_note"], author="agent",
                     workflow=ins["workflow"], expected_generation=ins["expected_generation"],
                     expected_ledger=ins["expected_ledger"])

    def new_revision(self, rid):
        """The lineage's note text changes (a decision moved it): a new note_seq."""
        pid = self.pid_of(rid)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET note_body='(an older body)',"
                              " observed_revision=NULL WHERE pid=?", (pid,))
            lineage.settle(self.conn, pid)
        return lineage.projection(self.conn, pid)["note_seq"]

    def test_d2_a_held_read_does_not_confirm_while_an_older_revision_may_land(self):
        # round D2 (Astra S2), restated for issue #14: a read taken inside the window
        # and recorded after it must not certify the note while a write of ANOTHER
        # revision, handed out inside the window, can still land on top of it
        rids = self.settled()
        rid, pid = rids[1], self.pid_of(rids[1])
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET observed_revision=NULL WHERE pid=?", (pid,))
        older = self.issue_add_note(rid)                     # revision A, in flight
        issued_a = lineage.projection(self.conn, pid)["note_issued_at"]
        self.later(20)
        seq_b = self.new_revision(rid)
        ins = self.issue_add_note(rid)                       # revision B, written at once
        self.assertIn(f"Accounting revision {seq_b}:", ins["add_note"])
        self.write(rid, ins)
        text = self.bf.call("get_transaction", row_id=rid)  # read inside the window
        self.later(steps.CEILING_ASSUMED_S + 100)            # recorded after it
        self.assertEqual(self.record_read(rid, text)["instructions"], {})
        p = lineage.projection(self.conn, pid)
        # NULL-safe (design round D2, Terra S1): A's issue is kept as the "other" one
        self.assertEqual(p["note_other_issued_at"], issued_a)
        self.assertEqual(p["note_issued_seq"], seq_b)
        self.assertFalse(sweep.note_confirmed(p, self.bf.tag_revision(rid),
                                                epoch=db.epoch(self.conn)))
        self.write(rid, older)                               # A lands late, on top
        self.later(3600)
        self.new_pass()
        self.assertIn(pid, self.due())
        self.cycle()
        self.assertIn(f"Accounting revision {seq_b}:", self.accounting_notes(rid)[-1])

    def test_14_a_read_back_confirms_the_note_it_wrote(self):
        # issue #14: one add_note, read back in the pass that wrote it, settles the row
        rids = self.three_rows()
        self.new_pass()
        self.cycle()
        self.assertTrue(all(len(self.accounting_notes(r)) == 1 for r in rids))
        self.later(3600)
        self.new_pass()
        self.assertEqual(self.due(), [])

    def test_14_nothing_confirms_within_a_ceiling_of_the_store_epoch(self):
        # a store just created, reset or upgraded: a write an earlier generation handed
        # out may still land, so the read-back is checked once more
        with db.tx(self.conn):
            db.set_epoch(self.conn)
        rids = self.three_rows()
        self.new_pass()
        self.cycle()
        self.later(3600)
        self.new_pass()
        self.assertEqual(sorted(self.due()), sorted(self.pid_of(r) for r in rids))
        self.cycle()                                   # read: visible, nothing written
        self.later(3600)
        self.new_pass()
        self.assertEqual(self.due(), [])

    def test_14_reset_store_sets_the_epoch(self):
        import binding
        self.later(7200)
        binding.reset_store(self.conn)
        self.assertEqual(db.epoch(self.conn), db.now())

    def test_a_read_after_the_window_confirms(self):
        rids = self.settled()
        p = lineage.projection(self.conn, self.pid_of(rids[0]))
        self.assertTrue(sweep.note_confirmed(p, self.bf.tag_revision(rids[0]),
                                               epoch=db.epoch(self.conn)))


class TestNoteConfirmedRule(unittest.TestCase):
    """Issue #14's rule (3) as a pure function: only an add_note of ANOTHER note text,
    or the store's epoch, bounds a confirmation — strictly (design round D1)."""
    E = "2026-09-01T00:00:00Z"

    def proj(self, **over):
        p = {"note_body": "b", "note_seq": 7, "note_seen_seq": 7, "note_seen_rev": 3,
             "note_seen_at": "2026-09-02T10:00:00Z", "note_issued_at": "2026-09-02T10:05:00Z",
             "note_issued_seq": 7, "note_other_issued_at": None}
        p.update(over)
        return p

    def ok(self, **over):
        return sweep.note_confirmed(self.proj(**over), 3, epoch=self.E)

    def test_a_same_text_issue_after_the_read_does_not_bound_it(self):
        self.assertTrue(self.ok())

    def test_another_texts_issue_bounds_it_strictly(self):
        self.assertFalse(self.ok(note_other_issued_at="2026-09-02T09:50:00Z"))
        self.assertFalse(self.ok(note_other_issued_at="2026-09-02T09:50:00Z",
                                 note_seen_at="2026-09-02T10:00:00Z"))   # exactly +600: not yet
        self.assertTrue(self.ok(note_other_issued_at="2026-09-02T09:49:59Z"))

    def test_an_issue_of_an_older_text_is_another_texts(self):
        # the last issue carried seq 6, the current note is 7
        self.assertFalse(self.ok(note_issued_seq=6))
        self.assertFalse(self.ok(note_issued_seq=None))          # migrated: unknown seq
        self.assertTrue(self.ok(note_issued_seq=6, note_issued_at="2026-09-02T09:00:00Z"))

    def test_the_epoch_bounds_it(self):
        self.assertFalse(sweep.note_confirmed(self.proj(), 3, epoch="2026-09-02T09:55:00Z"))

    def test_rules_1_and_2_are_unchanged(self):
        self.assertFalse(self.ok(note_seen_seq=6))
        self.assertFalse(sweep.note_confirmed(self.proj(), 4, epoch=self.E))
        self.assertTrue(self.ok(note_body=None, note_seen_seq=None))


class TestMergeCarriesNoteIssues(_base.StoreCase):
    def test_the_losers_issues_bound_the_survivors_confirmation(self):
        # issue #14: a merged-in lineage's handed-out note writes target the survivor's
        # row and carry none of its note texts
        for r in (1, 2):
            self.row(r)
        a, b = self.lineage_for(1), self.lineage_for(2)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET note_issued_at='2026-09-02T10:00:00Z',"
                              " note_other_issued_at='2026-09-02T09:00:00Z' WHERE pid=?", (b,))
            self.conn.execute("UPDATE projections SET note_other_issued_at='2026-09-02T08:00:00Z'"
                              " WHERE pid=?", (a,))
            ledger.merge(self.conn, a, b)
        self.assertEqual(lineage.projection(self.conn, a)["note_other_issued_at"],
                         "2026-09-02T10:00:00Z")


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
        # issue #14: a read-back confirms the note it wrote, so each pass takes the next
        # 50 and none is read twice (0.3.5: [120, 120, 120, 90, 40, 0])
        self.assertEqual(reads, [120, 70, 20, 0])
        self.assertTrue(all(self.owned(r) == ["acct::open"] for r in rids))


if __name__ == "__main__":
    unittest.main()
