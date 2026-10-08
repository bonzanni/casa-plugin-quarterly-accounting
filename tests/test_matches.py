import multiprocessing
import unittest

from tests._base import StoreCase
from tests import _procs
import authorship  # noqa: E402
import db  # noqa: E402
import documents  # noqa: E402
import kb  # noqa: E402
import lineage  # noqa: E402
import matches  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)

    def auto(self, pid=None, doc_id=None, kind="record", **kw):
        pid = pid or self.pid
        fn = matches.record_match if kind == "record" else matches.propose_match
        extra = {"author": "auto"} if kind == "record" else {}
        return fn(self.conn, pid=pid, doc_id=doc_id, expected_revision=self.rev(pid),
                  row_snapshot=self.snapshot(pid), token=self.token, **extra, **kw)

    def state(self, mid):
        return self.conn.execute("SELECT state FROM match_state WHERE match_id=?",
                                 (mid,)).fetchone()[0]


class TestMachineWrites(Base):
    def test_record_match_lands_matched_with_its_labels(self):
        r = self.auto(doc_id=self.doc(), labels=("guessed", "recipient?"),
                      runners_up=["8712 (10 Sep)"])
        self.assertEqual((r["state"], r["status"]), ("matched", "matched"))
        self.assertEqual(self.conn.execute("SELECT label FROM matches WHERE match_id=?",
                                           (r["match_id"],)).fetchone()[0], "guessed,recipient?")

    def test_machine_write_needs_a_pass_token(self):
        with self.assertRaises(db.Refusal):
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="auto",
                                 expected_revision=self.rev(self.pid),
                                 row_snapshot=self.snapshot(self.pid), token=None)

    def test_exempt_lineage_refuses_and_leaves_a_residue_line(self):
        rid = self.show(self.pid)
        self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=True,
                     expected_revision=self.rev(self.pid), render_id=rid)
        r = self.auto(doc_id=self.doc())
        self.assertFalse(r["applied"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM residue WHERE reason='exempt-doc'")
                         .fetchone()[0], 1)

    def test_a_stale_row_snapshot_is_refused(self):
        snap = dict(self.snapshot(self.pid), amount_minor=9000)
        with self.assertRaises(db.Refusal):
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="auto",
                                 expected_revision=self.rev(self.pid), row_snapshot=snap,
                                 token=self.token)

    def test_a_pending_row_is_not_auto_matched(self):
        self.row(1, status="PDNG")
        self.settle(self.pid)
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=self.doc())

    def test_a_machine_redecision_replaces_its_own_pairing(self):
        # design rev 17 §2.2: what the payment holds by the machine is replaced by its own
        # re-decision (the server computes `resolves`), never collides with it
        a = self.auto(doc_id=self.doc())["match_id"]
        b = self.auto(doc_id=self.doc())["match_id"]
        self.assertEqual((self.state(a), self.state(b)), ("rejected", "matched"))
        r = self.auto(doc_id=self.doc(), kind="propose")
        self.assertEqual((self.state(b), r["state"], r["status"]),
                         ("rejected", "proposed", "proposed"))

    def test_a_machine_write_on_the_operators_own_pairing_is_refused(self):
        d = self.doc()
        rid = self.show(self.pid)
        mid = self.operator_pair(pid=self.pid, doc_id=d, expected_revision=self.rev(self.pid),
                                 render_id=rid)["match_id"]
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=d, kind="propose")
        self.assertEqual(self.conn.execute("SELECT state, author FROM match_state WHERE"
                                           " match_id=?", (mid,)).fetchone()[:],
                         ("matched", "operator"))

    def test_a_machine_write_after_the_operator_confirmed_is_refused_whole(self):
        b = self.auto(doc_id=self.doc())["match_id"]
        rid = self.show(self.pid)
        self.granted(matches.confirm_in_tx, match_id=b, expected_revision=self.rev(match_id=b),
                     render_id=rid)
        seq = self.conn.execute("SELECT max(seq) FROM log").fetchone()[0]
        with self.assertRaisesRegex(db.Refusal, "never reopened"):
            self.auto(doc_id=self.doc(), kind="propose")
        self.assertEqual(self.state(b), "matched")
        self.assertEqual(self.conn.execute("SELECT max(seq) FROM log").fetchone()[0], seq)


class TestOperatorWrites(Base):
    def test_an_item_never_shown_cannot_be_decided(self):
        with self.assertRaises(authorship.NotShown):
            self.operator_pair(pid=self.pid, doc_id=self.doc(),
                               expected_revision=self.rev(self.pid), render_id="nope")

    def test_a_changed_item_is_refused_as_stale(self):
        rid = self.show(self.pid)
        shown_rev = self.rev(self.pid)
        self.auto(doc_id=self.doc())                            # the pass moved it
        with self.assertRaises(authorship.Stale):
            self.operator_pair(pid=self.pid, doc_id=self.doc(), expected_revision=shown_rev,
                               render_id=rid)

    def test_the_current_revision_with_an_old_render_is_refused(self):
        # the shown-revision comparison is what fails here: the caller passes the
        # CURRENT revision, as a caller that resolved against live state would
        d = self.doc()
        mid = self.auto(doc_id=d, kind="propose")["match_id"]
        rid = self.show(self.pid)
        matches.relabel_match(self.conn, match_id=mid, labels=("guessed",), token=self.token)
        with self.assertRaises(authorship.Stale):
            self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=True,
                         expected_revision=self.rev(self.pid), render_id=rid)
        with self.assertRaises(authorship.Stale):
            self.granted(matches.reject_in_tx, match_id=mid,
                         expected_revision=self.rev(match_id=mid), render_id=rid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE author='operator'")
                         .fetchone()[0], 0)

    def test_successive_corrections_each_move_the_pairing(self):
        mid = self.auto(doc_id=self.doc())["match_id"]
        self.row(1, amount_minor=9000)
        self.settle(self.pid)
        rid = self.show(self.pid)
        shown = self.rev(match_id=mid)
        self.row(1, amount_minor=8000)
        self.settle(self.pid)
        self.assertGreater(self.rev(match_id=mid), shown)
        with self.assertRaises(authorship.Stale):
            self.granted(matches.confirm_in_tx, match_id=mid, expected_revision=shown,
                         render_id=rid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE author='operator'")
                         .fetchone()[0], 0)

    def test_a_render_id_for_another_item_is_refused(self):
        self.row(2)
        other = self.lineage_for(2)
        self.settle(other)
        rid = self.show(other)
        with self.assertRaises(authorship.NotShown):
            self.operator_pair(pid=self.pid, doc_id=self.doc(),
                               expected_revision=self.rev(self.pid), render_id=rid)

    def test_operator_record_match_on_an_exempt_lineage_lifts_then_pairs(self):
        rid = self.show(self.pid)
        self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=True,
                     expected_revision=self.rev(self.pid), render_id=rid)
        rid = self.show(self.pid)
        r = self.operator_pair(pid=self.pid, doc_id=self.doc(),
                               expected_revision=self.rev(self.pid), render_id=rid)
        self.assertEqual(r["status"], "matched")
        kinds = [k[0] for k in self.conn.execute("SELECT kind FROM log WHERE pid=? AND"
                                                 " author='operator' ORDER BY seq", (self.pid,))]
        self.assertEqual(kinds, ["exempt", "lift", "pair"])

    def test_unpairing_a_conflicted_candidate_leaves_the_accepted_pairing(self):
        p_doc, q_doc = self.doc(), self.doc()
        q = self.auto(doc_id=q_doc, kind="propose")["match_id"]
        rid = self.show(self.pid)
        p = self.operator_pair(pid=self.pid, doc_id=p_doc, expected_revision=self.rev(self.pid),
                               render_id=rid)["match_id"]       # sets Q aside beside P
        self.assertEqual(self.state(q), "conflicted")
        rid = self.show(self.pid)
        self.granted(matches.reject_in_tx, match_id=q, expected_revision=self.rev(match_id=q),
                     render_id=rid)
        self.assertEqual((self.state(p), self.state(q)), ("matched", "rejected"))

    def test_confirming_a_conflicted_candidate_whose_document_moved_is_refused(self):
        d = self.doc()
        a = self.machine_entry(self.pid, d)                     # a joint machine set:
        self.machine_entry(self.pid, self.doc())                # a and b both conflicted
        self.assertEqual(self.state(a), "conflicted")
        self.row(2)
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        self.settle(other)
        # d then paired with another payment (the floor refuses this write as taken now;
        # a pre-floor store or a merge can still hold it)
        moved = self.machine_entry(other, d)
        self.assertEqual(self.state(moved), "matched")
        rid = self.show(self.pid)
        with self.assertRaisesRegex(db.Refusal, "since been matched"):
            self.granted(matches.confirm_in_tx, match_id=a, expected_revision=self.rev(match_id=a),
                         render_id=rid)

    def test_confirming_a_document_another_proposal_names_as_an_alternative_is_allowed(self):
        """Rev 18.4 §R18.4 (r2 Astra S1 #2): P2's live proposal names B only as an
        alternative, which holds nothing — P1's own pairing of B is confirmed."""
        self.row(2)
        p2 = self.lineage_for(2)
        self.classify(p2, {"software"})
        self.settle(p2)
        b = self.doc()
        self.auto(pid=p2, doc_id=self.doc(), kind="propose", alternatives=[b])
        self.assertEqual(matches.holders(self.conn, b), [])
        mine = self.auto(doc_id=b, kind="propose")["match_id"]
        rid = self.show(self.pid)
        self.granted(matches.confirm_in_tx, match_id=mine,
                     expected_revision=self.rev(match_id=mine), render_id=rid)
        self.assertEqual(self.state(mine), "matched")

    def test_exemption_rejects_the_pairing_and_says_so(self):
        mid = self.auto(doc_id=self.doc(), kind="propose")["match_id"]
        rid = self.show(self.pid)
        r = self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=True,
                         expected_revision=self.rev(self.pid), render_id=rid)
        self.assertEqual((r["status"], self.state(mid)), ("exempt", "rejected"))
        self.assertIn(f"unpaired {mid}", r["effects"])
        rid = self.show(self.pid)
        self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=False,
                     expected_revision=self.rev(self.pid), render_id=rid)
        rid = self.show(self.pid)
        with self.assertRaises(db.Refusal):                   # nothing stands to lift
            self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=False,
                         expected_revision=self.rev(self.pid), render_id=rid)


class TestRace(Base):
    def test_two_processes_one_invoice_two_lineages(self):
        self.row(2)
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        self.settle(other)
        d = self.doc()
        path = str(self.data / db.DB_NAME)
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_procs.machine_pair,
                             args=(path, pid, d, self.token, self.rev(pid), self.snapshot(pid), q))
                 for pid in (self.pid, other)]
        for p in procs:
            p.start()
        results = [q.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(60)
        # design rev 17 §2 "The floor": the later-serialized write finds the document taken
        # and is refused whole; it never lands conflicted beside the winner
        self.assertEqual(sorted(r[0] for r in results), ["error", "ok"], results)
        err = next(r for r in results if r[0] == "error")
        self.assertEqual(err[1], "Refusal", results)
        self.assertIn(f"document #{d} is taken", err[2])
        self.assertEqual(next(r[2] for r in results if r[0] == "ok"), "matched")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE kind='pair'")
                         .fetchone()[0], 1)

    def test_two_processes_same_revision_different_documents_one_wins(self):
        # round C1 (Astra S2): the machine CAS guard (`_machine`'s revision check) is what
        # stops a second writer that captured a now-stale revision from landing at all. Two
        # real processes race to pair the SAME lineage, at the SAME captured revision, with
        # two DIFFERENT documents (no doc-level occupancy collision to catch this instead):
        # exactly one write should land; the other must be refused as stale before it ever
        # appends anything.
        d1, d2 = self.doc(document_number="A"), self.doc(document_number="B")
        expected = self.rev(self.pid)
        snap = self.snapshot(self.pid)
        path = str(self.data / db.DB_NAME)
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_procs.machine_pair,
                             args=(path, self.pid, d, self.token, expected, snap, q))
                 for d in (d1, d2)]
        for p in procs:
            p.start()
        results = [q.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(60)
        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "error"]
        self.assertEqual((len(oks), len(errs)), (1, 1), results)
        self.assertEqual(errs[0][1], "Stale", results)
        self.assertEqual(oks[0][2], "matched", results)
        active = self.conn.execute("SELECT COUNT(*) FROM match_state WHERE state='matched'"
                                   ).fetchone()[0]
        conflicted = self.conn.execute("SELECT COUNT(*) FROM match_state WHERE state='conflicted'"
                                       ).fetchone()[0]
        pairs = self.conn.execute("SELECT COUNT(*) FROM log WHERE kind='pair'").fetchone()[0]
        self.assertEqual((active, conflicted, pairs), (1, 0, 1))


class TestFixRound1(Base):
    def residue(self, reason="exempt-doc"):
        return self.conn.execute("SELECT COUNT(*) FROM residue WHERE reason=?",
                                 (reason,)).fetchone()[0]

    def test_relabelling_a_rejected_pairing_is_refused_and_moves_nothing(self):
        mid = self.auto(doc_id=self.doc(), kind="propose")["match_id"]
        rid = self.show(self.pid)
        self.granted(matches.reject_in_tx, match_id=mid, expected_revision=self.rev(match_id=mid),
                     render_id=rid)
        rid = self.show(self.pid)
        shown = self.rev(self.pid)
        with self.assertRaises(db.Refusal):
            matches.relabel_match(self.conn, match_id=mid, labels=("guessed",), token=self.token)
        self.assertEqual(self.rev(self.pid), shown)
        r = self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=True,
                         expected_revision=shown, render_id=rid)
        self.assertEqual(r["status"], "exempt")

    def test_a_standing_pairing_is_still_relabelled(self):
        mid = self.auto(doc_id=self.doc())["match_id"]
        r = matches.relabel_match(self.conn, match_id=mid, labels=("no-ref",), token=self.token)
        self.assertEqual(r["state"], "matched")
        self.assertEqual(self.conn.execute("SELECT label FROM matches WHERE match_id=?",
                                           (mid,)).fetchone()[0], "no-ref")

    def test_exempt_residue_names_only_a_valid_document_once(self):
        rid = self.show(self.pid)
        self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=True,
                     expected_revision=self.rev(self.pid), render_id=rid)
        irrelevant = self.doc()
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET irrelevant=1 WHERE doc_id=?", (irrelevant,))
        for bad in (99999, irrelevant):       # a payslip is a real document now (kind gate gone)
            for _ in range(2):
                with self.assertRaises(db.Refusal):
                    self.auto(doc_id=bad)
        self.assertEqual(self.residue(), 0)
        d = self.doc()
        for _ in range(3):
            self.assertFalse(self.auto(doc_id=d)["applied"])
        self.assertEqual(self.residue(), 1)
        self.assertFalse(self.auto(doc_id=self.doc())["applied"])     # another document: a line
        self.assertEqual(self.residue(), 2)

    def test_two_ids_for_one_document_reuse_the_latest_never_mint_a_third(self):
        # what a merge of two lineages that each paired d leaves behind (plan §D12)
        d = self.doc()
        with db.tx(self.conn):
            ids = []
            for act in (5, 9):
                mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                        " VALUES (?,?,0)", (self.pid, d)).lastrowid
                self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state, author,"
                                  " activation) VALUES (?,?,?,'rejected','auto',?)",
                                  (mid, self.pid, d, act))
                ids.append(mid)
        before = self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        with db.tx(self.conn):
            self.assertEqual(matches._match_id_for(self.conn, self.pid, d), ids[1])
        r = self.auto(doc_id=d, kind="propose")
        self.assertEqual(r["match_id"], ids[1])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], before)

    def test_operator_writes_naming_a_merged_pid_are_not_shown(self):
        # the loser and the survivor on ONE delivered render; then the fan-in
        # merges the loser. A write naming the loser must not be bound to the
        # survivor's shown record, even with the survivor's revision in hand.
        mid = self.auto(doc_id=self.doc(), kind="propose")["match_id"]
        self.row(2)
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        self.settle(other)
        rid = self.show(self.pid, other)
        mrev = self.rev(match_id=mid)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET merged_into=? WHERE pid=?",
                              (other, self.pid))
        for rev in (self.rev(other), self.rev(self.pid)):   # survivor first: the hazard
            with self.assertRaises(authorship.NotShown):
                self.operator_pair(pid=self.pid, doc_id=self.doc(),
                                   expected_revision=rev, render_id=rid)
            with self.assertRaises(authorship.NotShown):
                self.granted(matches.set_exemption_in_tx, pid=self.pid, exempt=True,
                             expected_revision=rev, render_id=rid)
        for fn in (matches.confirm_in_tx, matches.reject_in_tx):
            with self.assertRaises(authorship.NotShown):
                self.granted(fn, match_id=mid, expected_revision=mrev, render_id=rid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE author='operator'")
                         .fetchone()[0], 0)

    def test_a_snapshot_of_a_row_no_longer_active_is_refused(self):
        snap = dict(self.snapshot(self.pid), state="superseded")
        with self.assertRaises(db.Refusal) as caught:
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="auto",
                                 expected_revision=self.rev(self.pid), row_snapshot=snap,
                                 token=self.token)
        self.assertIn("snapshot", str(caught.exception))

    def test_a_write_binds_the_rows_of_the_rendering_it_names(self):
        # binding §2 #12: the `shown` pointer no longer binds — a write is checked against
        # the render_items row of the rendering it names, older or not; a rendering that
        # did not show the payment, or a revision changed since, refuses
        old = self.show(self.pid)
        self.show(self.pid)
        self.row(2)
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        self.settle(other)
        elsewhere = self.show(other)
        with self.assertRaises(authorship.NotShown):
            self.operator_pair(pid=self.pid, doc_id=self.doc(),
                               expected_revision=self.rev(self.pid), render_id=elsewhere)
        with self.assertRaises(authorship.Stale):
            self.operator_pair(pid=self.pid, doc_id=self.doc(),
                               expected_revision=self.rev(self.pid) - 1, render_id=old)
        self.operator_pair(pid=self.pid, doc_id=self.doc(),
                           expected_revision=self.rev(self.pid), render_id=old)


if __name__ == "__main__":
    unittest.main()
