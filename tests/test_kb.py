import unittest

from tests._base import StoreCase
import db  # noqa: E402
import kb  # noqa: E402
import lineage  # noqa: E402


class TestKB(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.row(1, counterparty="BCK*ZAPIER")
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " delivered_at, text, membership_json) VALUES"
                              " ('r1','status','{}','x','x','','[]')")

    def test_pattern_is_exact_and_star_is_literal(self):
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"])
        self.assertEqual(kb.get_counterparty(self.conn, "bck*zapier")["name"], "Zapier")
        self.assertIsNone(kb.get_counterparty(self.conn, "BCK*ZAPIERX"))
        self.assertIsNone(kb.get_counterparty(self.conn, "BCKZAPIER"))

    def test_a_pattern_claimed_by_another_entry_is_refused(self):
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"])
        with self.assertRaises(db.Refusal):
            kb.upsert_counterparty(self.conn, "Other", patterns=["bck*zapier"])

    def test_a_canonical_name_that_is_another_entrys_bank_text_is_refused(self):
        # fix wave B (Astra S1): "BCK*ZAPIER" as a second entry's NAME hid its
        # `none` ruling behind Zapier's pattern — the payment still derived invoice
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"])
        with self.assertRaises(db.Refusal) as cm:
            kb.upsert_counterparty(self.conn, "bck*zapier")
        self.assertIn("Zapier", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 1)
        # the ruling, addressed by the bank text, lands on the one owner and applies
        kb.set_expectation(self.conn, scope_type="counterparty", scope="BCK*ZAPIER",
                           kind="none", author="operator", render_id="r1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 1)
        self.assertEqual(lineage.projection(self.conn, self.pid)["exp_kind"], "none")

    def test_a_pattern_that_is_another_entrys_name_is_refused(self):
        kb.upsert_counterparty(self.conn, "BCK*ZAPIER")
        with self.assertRaises(db.Refusal):
            kb.upsert_counterparty(self.conn, "Zapier", patterns=["bck*zapier"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0], 1)

    def test_an_entry_may_list_its_own_name_as_a_pattern(self):
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"])
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["ZAPIER"], notes="monthly")
        self.assertEqual(kb.get_counterparty(self.conn, "zapier")["notes"], "monthly")

    def test_portal_source_settles_to_portal_tag(self):
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"], source="portal",
                               document_link="https://zapier.example/app/invoices")
        p = lineage.projection(self.conn, self.pid)
        self.assertIn("acct::portal", p["desired_json"])

    def test_counterparty_none_override(self):
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"])
        kb.set_expectation(self.conn, scope_type="counterparty", scope="Zapier", kind="none",
                           author="operator", render_id="r1")
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual((p["exp_kind"], p["exp_row"]), ("none", 2))
        kb.set_expectation(self.conn, scope_type="counterparty", scope="Zapier",
                           kind="default", author="operator", render_id="r1")
        self.assertEqual(lineage.projection(self.conn, self.pid)["exp_row"], 11)

    def test_operator_author_needs_a_delivered_render(self):
        with self.assertRaises(db.Refusal):
            kb.set_expectation(self.conn, scope_type="chain", scope="salary", kind="none",
                               author="operator", render_id="nope")

    def test_chain_overrides_are_operator_only_and_validated(self):
        with self.assertRaises(db.Refusal):
            kb.set_expectation(self.conn, scope_type="chain", scope="salary", kind="none",
                               author="specialist")
        with self.assertRaises(db.Refusal):
            kb.set_expectation(self.conn, scope_type="chain", scope="salary, tax", kind="none",
                               author="operator", render_id="r1")      # a conflicting scope
        with self.assertRaises(db.Refusal):
            kb.set_expectation(self.conn, scope_type="chain", scope="software", kind="invoice",
                               tier=None, author="operator", render_id="r1")  # tier required
        kb.set_expectation(self.conn, scope_type="chain", scope="software", kind="receipt",
                           tier="optional", author="operator", render_id="r1")
        p = lineage.projection(self.conn, self.pid)
        self.assertEqual((p["exp_kind"], p["exp_tier"]), ("receipt", "optional"))

    def test_renaming_the_payee_moves_the_payment_and_its_pairings(self):
        # rounds p6/p7: a rename changes what a line shows, so both revisions move
        import db as _db
        import reducer as R
        d = self.doc()
        with _db.tx(self.conn):
            row = lineage.live_row(self.conn, lineage.projection(self.conn, self.pid))
            mid = self.conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq)"
                                    " VALUES (?,?,0)", (self.pid, d)).lastrowid
            lineage.append(self.conn, self.pid, "pair", "auto", match_id=mid, doc_id=d,
                           fp=R.fingerprint(R.facts_of(row), "invoice"))
            lineage.settle(self.conn, self.pid)
        p0 = lineage.projection(self.conn, self.pid)["revision"]
        m0 = self.conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                               (mid,)).fetchone()[0]
        kb.upsert_counterparty(self.conn, "My accountant", patterns=["BCK*ZAPIER"])
        self.assertEqual(lineage.projection(self.conn, self.pid)["revision"], p0 + 1)
        self.assertEqual(self.conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                                           (mid,)).fetchone()[0], m0 + 1)

    def test_specialist_may_set_a_counterparty_kind(self):
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"])
        kb.set_expectation(self.conn, scope_type="counterparty", scope="Zapier",
                           kind="receipt", tier="required", author="specialist")
        self.assertEqual(lineage.projection(self.conn, self.pid)["exp_kind"], "receipt")

    def test_chain_override_replaces_the_previous_normalized_scope(self):
        # carried ruling (Task 4 review): "refund" and "income, refund" both
        # normalize to ({7}, {refund}); the later ruling wins and derive
        # never raises for anything set through kb.
        self.row(2, counterparty="BCK*OTHER", direction="DBIT")
        pid2 = self.lineage_for(2)
        self.classify(pid2, {"refund"})
        self.settle(pid2)
        kb.set_expectation(self.conn, scope_type="chain", scope="income, refund", kind="none",
                           author="operator", render_id="r1")
        kb.set_expectation(self.conn, scope_type="chain", scope="refund", kind="credit-note",
                           tier="required", author="operator", render_id="r1")
        rows = self.conn.execute("SELECT scope FROM chain_overrides").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope"], "refund")
        p = lineage.projection(self.conn, pid2)
        self.assertEqual((p["exp_kind"], p["exp_tier"]), ("credit-note", "required"))


if __name__ == "__main__":
    unittest.main()
