import json
import unittest
from unittest import mock

from tests._base import StoreCase
import db  # noqa: E402
import lineage  # noqa: E402
import reducer as R  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})

    def machine_pair(self, pid, doc_id, kind="pair"):
        with db.tx(self.conn):
            row = lineage.live_row(self.conn, lineage.projection(self.conn, pid))
            cur = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                    " VALUES (?, ?, 0)", (pid, doc_id))
            mid = cur.lastrowid
            lineage.append(self.conn, pid, kind, "auto", match_id=mid, doc_id=doc_id,
                           fp=R.fingerprint(R.facts_of(row), "invoice"))
            lineage.settle(self.conn, pid)
        return mid

    def operator_pair(self, pid, doc_id, kind_at="invoice"):
        with db.tx(self.conn):
            row = lineage.live_row(self.conn, lineage.projection(self.conn, pid))
            mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                    " VALUES (?, ?, 0)", (pid, doc_id)).lastrowid
            lineage.append(self.conn, pid, "pair", "operator", match_id=mid, doc_id=doc_id,
                           fp=R.fingerprint(R.facts_of(row), kind_at))
            lineage.settle(self.conn, pid)
        return mid

    def state(self, mid):
        return self.conn.execute("SELECT state FROM match_state WHERE match_id=?",
                                 (mid,)).fetchone()[0]

    def doc_status(self, doc_id):
        return self.conn.execute("SELECT status FROM document_status WHERE doc_id=?",
                                 (doc_id,)).fetchone()[0]

    def proj(self, pid=None):
        return lineage.projection(self.conn, pid or self.pid)


class TestSettle(Base):
    def test_unpaired_required_is_open(self):
        red = self.settle(self.pid)
        self.assertEqual((red.status, red.desired), ("open", frozenset({"acct::open"})))
        self.assertEqual(json.loads(self.proj()["desired_json"]), ["acct::open"])

    def test_machine_pair_materializes_and_holds_the_document(self):
        d = self.doc()
        mid = self.machine_pair(self.pid, d)
        self.assertEqual(self.state(mid), "matched")
        self.assertEqual(self.doc_status(d), "matched")
        self.assertEqual(self.proj()["status"], "matched")

    def test_occupancy_retires_the_later_activation_and_names_the_other_payment(self):
        d = self.doc()
        self.machine_pair(self.pid, d)
        self.row(2, amount_minor=10000, booking_date="2026-07-04")
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        mid2 = self.machine_pair(other, d)
        self.assertEqual(self.state(mid2), "conflicted")
        retire = self.conn.execute("SELECT cause FROM log WHERE kind='retire' AND match_id=?",
                                   (mid2,)).fetchone()
        self.assertEqual(retire[0], "occupied")
        res = self.conn.execute("SELECT detail FROM residue WHERE pid=? AND reason='occupied'",
                                (other,)).fetchone()
        self.assertIn(str(self.pid), res[0])

    def test_unique_index_backstop_becomes_a_recorded_conflict(self):
        d = self.doc()
        self.machine_pair(self.pid, d)
        self.row(2)
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        with mock.patch.object(lineage, "_occupied", lambda conn, pid: (lambda d_, m_: False)):
            mid2 = self.machine_pair(other, d)
        self.assertEqual(self.state(mid2), "conflicted")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM match_state WHERE doc_id=? AND state IN ('matched','proposed')",
            (d,)).fetchone()[0], 1)

    def test_machine_kind_mismatch_is_retired_and_frees_the_document(self):
        d = self.doc()
        mid = self.machine_pair(self.pid, d)
        self.classify(self.pid, {"internal-transfer"})
        red = self.settle(self.pid)
        self.assertEqual(self.state(mid), "rejected")
        self.assertEqual(self.doc_status(d), "unmatched")
        self.assertEqual(red.desired, frozenset({"acct::no-document-expected"}))
        cause = self.conn.execute("SELECT cause FROM log WHERE kind='retire' AND match_id=?",
                                  (mid,)).fetchone()[0]
        self.assertEqual(cause, "kind-mismatch")

    def test_vendor_set_to_none_retires_a_machine_pairing(self):   # plan §D8
        d = self.doc()
        mid = self.machine_pair(self.pid, d)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO counterparties(name, patterns_json, exp_kind,"
                              " updated_at) VALUES ('Adobe', '[]', 'none', 'x')")
        self.settle(self.pid)
        self.assertEqual(self.state(mid), "rejected")

    def test_operator_kind_mismatch_is_proposed_not_retired(self):
        d = self.doc()
        mid = self.operator_pair(self.pid, d)
        self.classify(self.pid, {"income", "salary"})
        red = self.settle(self.pid)
        self.assertEqual(self.state(mid), "matched")
        self.assertEqual(red.desired, frozenset({"acct::proposed"}))
        self.assertIn("kind-mismatch", red.reasons)

    def test_unknown_keeps_the_machine_match_and_the_last_known_kind(self):
        d = self.doc()
        mid = self.machine_pair(self.pid, d)
        self.classify(self.pid, set())                 # purge(user_work=erase) stripped tags
        red = self.settle(self.pid)
        self.assertEqual((self.state(mid), red.desired), ("matched", frozenset({"acct::matched"})))
        self.assertEqual(self.proj()["last_known_kind"], "invoice")

    def test_an_ended_lineage_retires_everything_including_the_operators(self):
        d1, d2 = self.doc(), self.doc()
        m1 = self.operator_pair(self.pid, d1)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET ended='erased' WHERE pid=?", (self.pid,))
        red = self.settle(self.pid)
        self.assertEqual((self.state(m1), red.desired, red.status),
                         ("rejected", frozenset(), "ended"))
        self.assertEqual(self.doc_status(d1), "unmatched")
        self.assertIn("row-ended", [r[0] for r in self.conn.execute(
            "SELECT cause FROM log WHERE kind='retire'")])
        del d2

    def test_an_ended_lineage_retires_its_conflicted_candidates(self):
        a, b = self.machine_pair(self.pid, self.doc()), self.machine_pair(self.pid, self.doc())
        self.assertEqual((self.state(a), self.state(b)), ("conflicted", "conflicted"))
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET ended='vanished' WHERE pid=?", (self.pid,))
        self.settle(self.pid)
        self.assertEqual((self.state(a), self.state(b)), ("rejected", "rejected"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE kind='retire' AND"
                                           " cause='row-ended'").fetchone()[0], 2)

    def test_revisions_move_only_with_the_proposition(self):
        d = self.doc()
        mid = self.machine_pair(self.pid, d)
        r0 = self.proj()["revision"]
        m0 = self.conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                               (mid,)).fetchone()[0]
        self.settle(self.pid)
        self.assertEqual(self.proj()["revision"], r0)
        self.row(1, amount_minor=9000)                 # in-place correction
        self.settle(self.pid)
        self.assertEqual(self.proj()["revision"], r0 + 1)
        self.assertEqual(self.conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                                           (mid,)).fetchone()[0], m0 + 1)

    def test_note_revision_comes_from_the_store_sequence_and_moves_with_status(self):
        self.settle(self.pid)
        n1 = self.proj()["note_seq"]
        self.settle(self.pid)
        self.assertEqual(self.proj()["note_seq"], n1)
        self.machine_pair(self.pid, self.doc())
        n2 = self.proj()["note_seq"]
        self.assertGreater(n2, n1)
        self.assertTrue(lineage.note_text(self.conn, self.pid).startswith(
            "Accounting revision %d: " % n2))

    def test_ineligible_before_the_watermark_desires_nothing(self):
        self.row(1, booking_date="2026-06-30", value_date="2026-06-30")
        red = self.settle(self.pid)
        self.assertEqual((red.status, red.desired), ("ineligible", frozenset()))


class TestCarriedFindings(Base):
    def counts(self):
        return (self.conn.execute("SELECT COUNT(*) FROM log").fetchone()[0],
                self.conn.execute("SELECT COUNT(*) FROM residue").fetchone()[0])

    def unpair(self, pid, mid):
        with db.tx(self.conn):
            lineage.append(self.conn, pid, "unpair", "operator", match_id=mid)
            lineage.settle(self.conn, pid)

    def released_to_another_lineage(self):
        d = self.doc()
        m1 = self.machine_pair(self.pid, d)
        self.unpair(self.pid, m1)
        self.assertEqual((self.state(m1), self.doc_status(d)), ("rejected", "unmatched"))
        self.row(2, booking_date="2026-07-04")
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        m2 = self.machine_pair(other, d)
        self.assertEqual(self.state(m2), "matched")
        return d, m1, m2

    def test_refold_records_no_occupancy_retirement_for_a_long_unpaired_activation(self):
        # fold review: the re-fold answers occupied(d) with TODAY's store at the
        # historical pair entry; [pair m1 d, unpair m1] with d now legitimately on
        # another lineage must settle with no new log entry and no residue line.
        d, m1, m2 = self.released_to_another_lineage()
        before = self.counts()
        red = self.settle(self.pid)
        self.assertEqual(self.counts(), before)
        self.assertEqual((self.state(m1), self.state(m2)), ("rejected", "matched"))
        self.assertEqual(red.status, "open")
        self.assertNotIn("conflicted", red.reasons)

    def test_a_new_activation_of_that_pairing_is_still_retired_occupied(self):
        d, m1, m2 = self.released_to_another_lineage()
        with db.tx(self.conn):
            row = lineage.live_row(self.conn, lineage.projection(self.conn, self.pid))
            act = lineage.append(self.conn, self.pid, "pair", "operator", match_id=m1, doc_id=d,
                                 fp=R.fingerprint(R.facts_of(row), "invoice"))
            lineage.settle(self.conn, self.pid)
        self.assertEqual((self.state(m1), self.state(m2)), ("conflicted", "matched"))
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT retire_activation, cause FROM log WHERE kind='retire' AND match_id=?",
            (m1,))], [(act, "occupied")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM residue WHERE pid=? AND"
                                           " reason='occupied'", (self.pid,)).fetchone()[0], 1)

    def test_machine_proposal_of_another_kind_is_retired_before_the_reducer(self):
        # reducer review: the reducer alone would show proposed/kind-mismatch
        d = self.doc(kind="receipt")
        mid = self.machine_pair(self.pid, d, kind="propose")
        red = self.settle(self.pid)
        self.assertEqual((self.state(mid), self.doc_status(d)), ("rejected", "unmatched"))
        self.assertEqual((red.status, red.desired, red.current),
                         ("open", frozenset({"acct::open"}), None))
        self.assertNotIn("kind-mismatch", red.reasons)
        self.assertEqual([r[0] for r in self.conn.execute(
            "SELECT cause FROM log WHERE kind='retire' AND match_id=?", (mid,))],
            ["kind-mismatch"])


if __name__ == "__main__":
    unittest.main()
