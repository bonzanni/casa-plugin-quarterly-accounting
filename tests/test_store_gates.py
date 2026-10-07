"""Simple loop §2 table: the kind gate's store-side twins and the classification gate
are deleted; `taxes` is a statement chain (row 10); a joint machine proposal is shown as
a proposal (D3)."""
import json
from tests._base import StoreCase


class StoreGates(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.row(1, counterparty="Adobe", amount_minor=10000)
        self.pid = self.lineage_for(1)

    def _auto(self, kind, doc_id):
        import db, lineage
        import reducer as R
        with db.tx(self.conn):
            mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                    " VALUES (?,?,0)", (self.pid, doc_id)).lastrowid
            row = lineage.live_row(self.conn, lineage.projection(self.conn, self.pid))
            lineage.append(self.conn, self.pid, kind, "auto", match_id=mid, doc_id=doc_id,
                           fp=R.fingerprint(R.facts_of(row), "invoice"))
            return mid, lineage.settle(self.conn, self.pid)

    def test_a_receipt_paired_to_an_invoice_payment_stays_matched(self):
        self.classify(self.pid, {"software"})                 # wants an invoice
        _, red = self._auto("pair", self.doc(kind="receipt"))
        self.assertEqual(red.status, "matched")
        self.assertEqual(sorted(red.desired), ["acct::matched"])

    def test_an_unclassified_payment_can_hold_a_machine_match(self):
        self.classify(self.pid, set())                        # workable: kind unknown
        _, red = self._auto("pair", self.doc())
        self.assertEqual(red.status, "matched")
        self.assertNotIn("unclassified", red.reasons)

    def test_two_machine_candidates_reduce_to_one_proposal(self):
        self.classify(self.pid, {"software"})
        self._auto("propose", self.doc())
        _, red = self._auto("propose", self.doc())
        self.assertEqual((red.status, red.current), ("proposed", None))
        self.assertEqual(sorted(red.desired), ["acct::proposed"])
        self.assertIn("conflicted", red.reasons)

    def test_taxes_is_a_statement_chain(self):
        import expectation as ex
        for tags in (["taxes"], ["taxes", "vat"], ["corporate-tax", "recurring", "taxes"]):
            e = ex.derive("DBIT", tags)
            self.assertEqual((e.kind, e.tier, e.row), ("statement", "optional", 10), tags)

    def test_the_operator_rejection_retires_an_unclassified_machine_pairing(self):
        import db, matches
        self.classify(self.pid, set())
        mid, _ = self._auto("pair", self.doc())
        rid = self.show(self.pid)     # outside granted's tx: show opens its own
        self.granted(lambda c, grant: matches.reject_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        self.assertEqual(self.conn.execute("SELECT state FROM match_state WHERE match_id=?",
                                           (mid,)).fetchone()[0], "rejected")
