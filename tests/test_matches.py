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
        matches.set_exemption(self.conn, pid=self.pid, exempt=True,
                              expected_revision=self.rev(self.pid), render_id=rid)
        r = self.auto(doc_id=self.doc())
        self.assertFalse(r["applied"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM residue WHERE reason='exempt-doc'")
                         .fetchone()[0], 1)

    def test_unknown_none_and_wrong_kind_are_refused(self):
        self.classify(self.pid, set())
        self.settle(self.pid)
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=self.doc())
        self.classify(self.pid, {"internal-transfer"})
        self.settle(self.pid)
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=self.doc())
        self.classify(self.pid, {"software"})
        self.settle(self.pid)
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=self.doc(kind="payslip"))

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

    def test_issuer_number_collision_refuses_acceptance_but_allows_a_proposal(self):
        a = self.doc(document_number="X-9")
        self.doc(document_number="X-9", sha256="f" * 64)
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=a)
        self.assertEqual(self.auto(doc_id=a, kind="propose")["state"], "proposed")

    def test_resolves_must_name_exactly_the_conflicted_set(self):
        a = self.auto(doc_id=self.doc())["match_id"]
        b = self.auto(doc_id=self.doc())["match_id"]           # collision: both conflicted
        self.assertEqual((self.state(a), self.state(b)), ("conflicted", "conflicted"))
        c_doc = self.doc()
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=c_doc, kind="propose", resolves=[a])
        r = self.auto(doc_id=c_doc, kind="propose", resolves=[a, b])
        self.assertEqual((self.state(a), self.state(b), r["state"]),
                         ("rejected", "rejected", "proposed"))

    def test_a_machine_write_on_the_operators_own_pairing_is_refused(self):
        d = self.doc()
        rid = self.show(self.pid)
        mid = matches.record_match(self.conn, pid=self.pid, doc_id=d, author="operator",
                                   expected_revision=self.rev(self.pid), render_id=rid)["match_id"]
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=d, kind="propose")
        self.assertEqual(self.conn.execute("SELECT state, author FROM match_state WHERE"
                                           " match_id=?", (mid,)).fetchone()[:],
                         ("matched", "operator"))

    def test_resolves_after_the_operator_confirmed_one_is_refused_whole(self):
        a = self.auto(doc_id=self.doc())["match_id"]
        b = self.auto(doc_id=self.doc())["match_id"]
        rid = self.show(self.pid)
        matches.confirm_match(self.conn, match_id=b, expected_revision=self.rev(match_id=b),
                              render_id=rid)
        with self.assertRaises(db.Refusal):
            self.auto(doc_id=self.doc(), kind="propose", resolves=[a, b])
        self.assertEqual(self.state(b), "matched")


class TestOperatorWrites(Base):
    def test_an_item_never_shown_cannot_be_decided(self):
        with self.assertRaises(authorship.NotShown):
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="operator",
                                 expected_revision=self.rev(self.pid), render_id="nope")

    def test_a_changed_item_is_refused_as_stale(self):
        rid = self.show(self.pid)
        shown_rev = self.rev(self.pid)
        self.auto(doc_id=self.doc())                            # the pass moved it
        with self.assertRaises(authorship.Stale):
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="operator",
                                 expected_revision=shown_rev, render_id=rid)

    def test_the_current_revision_with_an_old_render_is_refused(self):
        # the shown-revision comparison is what fails here: the caller passes the
        # CURRENT revision, as a caller that resolved against live state would
        d = self.doc()
        mid = self.auto(doc_id=d, kind="propose")["match_id"]
        rid = self.show(self.pid)
        matches.relabel_match(self.conn, match_id=mid, labels=("guessed",), token=self.token)
        with self.assertRaises(authorship.Stale):
            matches.set_exemption(self.conn, pid=self.pid, exempt=True,
                                  expected_revision=self.rev(self.pid), render_id=rid)
        with self.assertRaises(authorship.Stale):
            matches.reject_match(self.conn, match_id=mid,
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
            matches.confirm_match(self.conn, match_id=mid, expected_revision=shown, render_id=rid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE author='operator'")
                         .fetchone()[0], 0)

    def test_a_render_id_for_another_item_is_refused(self):
        self.row(2)
        other = self.lineage_for(2)
        self.settle(other)
        rid = self.show(other)
        with self.assertRaises(authorship.NotShown):
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="operator",
                                 expected_revision=self.rev(self.pid), render_id=rid)

    def test_operator_record_match_on_an_exempt_lineage_lifts_then_pairs(self):
        rid = self.show(self.pid)
        matches.set_exemption(self.conn, pid=self.pid, exempt=True,
                              expected_revision=self.rev(self.pid), render_id=rid)
        rid = self.show(self.pid)
        r = matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="operator",
                                 expected_revision=self.rev(self.pid), render_id=rid)
        self.assertEqual(r["status"], "matched")
        kinds = [k[0] for k in self.conn.execute("SELECT kind FROM log WHERE pid=? AND"
                                                 " author='operator' ORDER BY seq", (self.pid,))]
        self.assertEqual(kinds, ["exempt", "lift", "pair"])

    def test_kind_guard_is_evaluated_after_the_lift_and_rolls_back_whole(self):
        self.classify(self.pid, {"transport", "fuel"})
        self.settle(self.pid)
        rid = self.show(self.pid)
        matches.set_exemption(self.conn, pid=self.pid, exempt=True,
                              expected_revision=self.rev(self.pid), render_id=rid)
        kb.upsert_counterparty(self.conn, "Adobe")
        kb.set_expectation(self.conn, scope_type="counterparty", scope="Adobe", kind="none",
                           author="specialist")
        rid = self.show(self.pid)
        with self.assertRaises(db.Refusal) as caught:
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="operator",
                                 expected_revision=self.rev(self.pid), render_id=rid)
        self.assertIn("Adobe", str(caught.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE kind='lift'")
                         .fetchone()[0], 0)

    def test_confirm_refuses_a_wrong_kind_until_the_kind_is_corrected_and_reshown(self):
        # round-27/28: no confirmation cures a kind mismatch; correcting the
        # document's kind moves the item, so the old shown revision is stale.
        slip = self.doc(kind="payslip")
        self.classify(self.pid, {"income", "salary"})
        self.settle(self.pid)
        rid = self.show(self.pid)
        mid = matches.record_match(self.conn, pid=self.pid, doc_id=slip, author="operator",
                                   expected_revision=self.rev(self.pid),
                                   render_id=rid)["match_id"]
        self.classify(self.pid, {"software"})              # the classifier now wants an invoice
        self.assertEqual(self.settle(self.pid).status, "proposed")   # shown, not retired
        rid = self.show(self.pid)
        shown = self.rev(match_id=mid)
        with self.assertRaises(db.Refusal) as caught:
            matches.confirm_match(self.conn, match_id=mid, expected_revision=shown, render_id=rid)
        self.assertNotIsInstance(caught.exception, authorship.Stale)
        self.assertIn("payslip", str(caught.exception))
        documents.update_document_metadata(self.conn, slip, kind="invoice")
        with self.assertRaises(authorship.Stale):
            matches.confirm_match(self.conn, match_id=mid, expected_revision=shown, render_id=rid)
        rid = self.show(self.pid)
        r = matches.confirm_match(self.conn, match_id=mid,
                                  expected_revision=self.rev(match_id=mid), render_id=rid)
        self.assertEqual(r["status"], "matched")

    def test_unpairing_a_conflicted_candidate_leaves_the_accepted_pairing(self):
        p_doc, q_doc = self.doc(), self.doc()
        rid = self.show(self.pid)
        p = matches.record_match(self.conn, pid=self.pid, doc_id=p_doc, author="operator",
                                 expected_revision=self.rev(self.pid), render_id=rid)["match_id"]
        q = self.auto(doc_id=q_doc, kind="propose")["match_id"]  # lands conflicted beside P
        self.assertEqual(self.state(q), "conflicted")
        rid = self.show(self.pid)
        matches.reject_match(self.conn, match_id=q, expected_revision=self.rev(match_id=q),
                             render_id=rid)
        self.assertEqual((self.state(p), self.state(q)), ("matched", "rejected"))

    def test_confirming_a_conflicted_candidate_whose_document_moved_is_refused(self):
        d = self.doc()
        a = self.auto(doc_id=d)["match_id"]
        self.auto(doc_id=self.doc())                            # a and b collide
        self.row(2)
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        self.settle(other)
        self.auto(pid=other, doc_id=d)                          # d is free (a conflicted) -> active on other
        rid = self.show(self.pid)
        with self.assertRaises(db.Refusal):
            matches.confirm_match(self.conn, match_id=a, expected_revision=self.rev(match_id=a),
                                  render_id=rid)

    def test_exemption_rejects_the_pairing_and_says_so(self):
        mid = self.auto(doc_id=self.doc(), kind="propose")["match_id"]
        rid = self.show(self.pid)
        r = matches.set_exemption(self.conn, pid=self.pid, exempt=True,
                                  expected_revision=self.rev(self.pid), render_id=rid)
        self.assertEqual((r["status"], self.state(mid)), ("exempt", "rejected"))
        self.assertIn(f"unpaired {mid}", r["effects"])
        rid = self.show(self.pid)
        matches.set_exemption(self.conn, pid=self.pid, exempt=False,
                              expected_revision=self.rev(self.pid), render_id=rid)
        rid = self.show(self.pid)
        with self.assertRaises(db.Refusal):                   # nothing stands to lift
            matches.set_exemption(self.conn, pid=self.pid, exempt=False,
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
        self.assertEqual(sorted(r[0] for r in results), ["ok", "ok"], results)
        self.assertEqual(sorted(r[2] for r in results), ["conflicted", "matched"])
        loser = next(r[1] for r in results if r[2] == "conflicted")
        winner = next(r[1] for r in results if r[2] == "matched")
        seq = dict(self.conn.execute("SELECT match_id, max(seq) FROM log WHERE kind IN"
                                     " ('pair','propose') GROUP BY match_id").fetchall())
        self.assertGreater(seq[loser], seq[winner])       # the later-serialized write loses
        self.assertEqual(self.conn.execute("SELECT cause FROM log WHERE kind='retire' AND"
                                           " match_id=?", (loser,)).fetchone()[0], "occupied")


class TestFixRound1(Base):
    def residue(self, reason="exempt-doc"):
        return self.conn.execute("SELECT COUNT(*) FROM residue WHERE reason=?",
                                 (reason,)).fetchone()[0]

    def test_relabelling_a_rejected_pairing_is_refused_and_moves_nothing(self):
        mid = self.auto(doc_id=self.doc(), kind="propose")["match_id"]
        rid = self.show(self.pid)
        matches.reject_match(self.conn, match_id=mid, expected_revision=self.rev(match_id=mid),
                             render_id=rid)
        rid = self.show(self.pid)
        shown = self.rev(self.pid)
        with self.assertRaises(db.Refusal):
            matches.relabel_match(self.conn, match_id=mid, labels=("guessed",), token=self.token)
        self.assertEqual(self.rev(self.pid), shown)
        r = matches.set_exemption(self.conn, pid=self.pid, exempt=True,
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
        matches.set_exemption(self.conn, pid=self.pid, exempt=True,
                              expected_revision=self.rev(self.pid), render_id=rid)
        irrelevant = self.doc()
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET irrelevant=1 WHERE doc_id=?", (irrelevant,))
        for bad in (99999, irrelevant, self.doc(kind="payslip")):
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
                matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(),
                                     author="operator", expected_revision=rev, render_id=rid)
            with self.assertRaises(authorship.NotShown):
                matches.set_exemption(self.conn, pid=self.pid, exempt=True,
                                      expected_revision=rev, render_id=rid)
        for fn in (matches.confirm_match, matches.reject_match):
            with self.assertRaises(authorship.NotShown):
                fn(self.conn, match_id=mid, expected_revision=mrev, render_id=rid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE author='operator'")
                         .fetchone()[0], 0)

    def test_a_snapshot_of_a_row_no_longer_active_is_refused(self):
        snap = dict(self.snapshot(self.pid), state="superseded")
        with self.assertRaises(db.Refusal) as caught:
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="auto",
                                 expected_revision=self.rev(self.pid), row_snapshot=snap,
                                 token=self.token)
        self.assertIn("snapshot", str(caught.exception))

    def test_an_older_render_after_a_newer_delivered_one_is_not_shown(self):
        old = self.show(self.pid)
        self.show(self.pid)
        with self.assertRaises(authorship.NotShown):
            matches.record_match(self.conn, pid=self.pid, doc_id=self.doc(), author="operator",
                                 expected_revision=self.rev(self.pid), render_id=old)


if __name__ == "__main__":
    unittest.main()
