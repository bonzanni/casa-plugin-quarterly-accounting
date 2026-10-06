"""Simple loop §2 "The floor": same currency and an exactly equal amount to match; a
different currency only proposed (FX screen kept, #35); taken counts proposals and a
live proposal's alternatives; a payment's own machine pairing is not taken against its own
re-decision, which replaces it; a re-decision with the same outcome and document writes
nothing; an operator rejection refuses both writes; an operator pairing is never reopened."""
from tests._base import StoreCase
import json
import db                     # server/ is on sys.path once tests._base is imported


class Floor(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.run_claim()
        self.p1 = self._payment(1, "Adobe", 10000)
        self.p2 = self._payment(2, "Adobe", 10000)

    def _payment(self, row_id, who, amount, currency="EUR"):
        self.row(row_id, counterparty=who, amount_minor=amount, currency=currency,
                 booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(row_id)
        self.classify(pid, {"software"})
        self.settle(pid)
        return pid

    def write(self, kind, pid, doc_id, **kw):
        import matches
        fn = matches.record_match if kind == "pair" else matches.propose_match
        extra = {"author": "auto"} if kind == "pair" else {}
        return fn(self.conn, pid=pid, doc_id=doc_id, expected_revision=self.rev(pid),
                  token=self.token, document_date="2026-09-01", **extra, **kw)

    def test_a_match_needs_the_same_currency_and_the_exact_amount(self):
        usd = self.doc(currency="USD", amount_minor=10000)
        with self.assertRaisesRegex(db.Refusal, "different currency is only ever proposed"):
            self.write("pair", self.p1, usd)
        off = self.doc(amount_minor=10001)
        with self.assertRaisesRegex(db.Refusal, "amounts differ"):
            self.write("pair", self.p1, off)
        self.assertTrue(self.write("propose", self.p1, usd)["wrote"])   # no bank rate: may be

    def test_a_proposal_takes_the_document_from_every_other_payment(self):
        d = self.doc()
        self.write("propose", self.p1, d)
        for kind in ("pair", "propose"):
            with self.assertRaisesRegex(db.Refusal, "taken"):
                self.write(kind, self.p2, d)

    def test_an_alternative_of_a_live_proposal_holds_nothing(self):
        """Rev 18.4 §R18.4 (r2 Astra S1 #2): only a match or a proposal's primary holds."""
        a, b = self.doc(), self.doc()
        self.write("propose", self.p1, a, alternatives=[b])
        self.assertTrue(self.write("pair", self.p2, b)["applied"])
        with self.assertRaisesRegex(db.Refusal, "taken"):
            self.write("pair", self.p2, a)                # the primary stays held

    def test_reopening_replaces_the_own_machine_match_and_A_stays_taken_for_others(self):
        a = self.doc()
        self.write("pair", self.p1, a)
        b = self.doc()                                    # filed later, same amount
        out = self.write("propose", self.p1, b, alternatives=[a])
        self.assertTrue(out["wrote"])
        states = dict(self.conn.execute("SELECT doc_id, state FROM match_state WHERE pid=?",
                                        (self.p1,)).fetchall())
        self.assertEqual(states[b], "proposed")          # the new proposal, b chosen
        self.assertEqual(states[a], "rejected")          # the own machine match, replaced
        # a is only the proposal's alternative: it holds nothing (rev 18.4 §R18.4)
        self.assertTrue(self.write("pair", self.p2, a)["applied"])

    def test_the_same_outcome_and_document_writes_nothing(self):
        d = self.doc()
        self.write("pair", self.p1, d)
        rev = self.rev(self.p1)
        seq = self.conn.execute("SELECT max(seq) FROM log").fetchone()[0]
        out = self.write("pair", self.p1, d)
        self.assertEqual((out["wrote"], self.rev(self.p1)), (False, rev))
        self.assertEqual(self.conn.execute("SELECT max(seq) FROM log").fetchone()[0], seq)

    def test_a_proposal_replacing_the_own_match_keeps_it_as_an_alternative(self):
        a = self.write("pair", self.p1, self.doc())["match_id"]
        doc_a = self.conn.execute("SELECT doc_id FROM matches WHERE match_id=?",
                                  (a,)).fetchone()[0]
        b = self.doc()
        out = self.write("propose", self.p1, b)                  # no alternatives named
        self.assertEqual((out["state"], out["status"]), ("proposed", "proposed"))
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT alternatives_json FROM matches WHERE match_id=?",
            (out["match_id"],)).fetchone()[0]), [doc_a])
        import matches
        self.assertEqual(matches.holders(self.conn, doc_a), [])     # rev 18.4 §R18.4
        self.assertTrue(self.write("pair", self.p2, doc_a)["applied"])

    def test_the_kept_alternative_counts_against_the_cap(self):
        self.write("pair", self.p1, self.doc())
        with self.assertRaisesRegex(db.Refusal, "stays as an alternative: name at most 2"):
            self.write("propose", self.p1, self.doc(),
                       alternatives=[self.doc() for _ in range(3)])
        out = self.write("propose", self.p1, self.doc(),
                         alternatives=[self.doc() for _ in range(2)])
        self.assertEqual(len(json.loads(self.conn.execute(
            "SELECT alternatives_json FROM matches WHERE match_id=?",
            (out["match_id"],)).fetchone()[0])), 3)

    def test_a_match_replaces_the_own_proposal_outright(self):
        self.write("propose", self.p1, self.doc())
        out = self.write("pair", self.p1, self.doc())
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT alternatives_json FROM matches WHERE match_id=?",
            (out["match_id"],)).fetchone()[0]), [])

    def test_a_noop_redecision_stamps_a_missing_date_read(self):
        d = self.doc()
        self.write("pair", self.p1, d)
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET date_read_at=NULL WHERE doc_id=?", (d,))
        rev = self.rev(self.p1)
        out = self.write("pair", self.p1, d)                      # same date: a no-op
        self.assertEqual((out["wrote"], self.rev(self.p1)), (False, rev))
        self.assertIsNotNone(self.conn.execute(
            "SELECT date_read_at FROM documents WHERE doc_id=?", (d,)).fetchone()[0])

    def test_a_redecision_with_a_corrected_date_is_written(self):
        d = self.doc()
        self.write("pair", self.p1, d)
        import matches
        out = matches.record_match(self.conn, pid=self.p1, doc_id=d, author="auto",
                                   expected_revision=self.rev(self.p1), token=self.token,
                                   document_date="2026-09-03")
        self.assertTrue(out["wrote"])
        self.assertEqual(self.conn.execute("SELECT document_date FROM documents WHERE doc_id=?",
                                           (d,)).fetchone()[0], "2026-09-03")

    def test_a_redecision_after_the_payment_changed_is_written(self):
        d = self.doc()
        self.write("pair", self.p1, d)
        with db.tx(self.conn):
            self.conn.execute("UPDATE bank_rows SET remittance='INV 42' WHERE row_id=1")
        self.settle(self.p1)
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (self.p1,)).fetchone()[0], "proposed")
        out = self.write("pair", self.p1, d)
        self.assertTrue(out["wrote"])
        self.assertEqual(out["status"], "matched")

    def test_a_reproposal_after_the_payment_changed_is_written_and_refingerprinted(self):
        d = self.doc()
        mid = self.write("propose", self.p1, d)["match_id"]
        with db.tx(self.conn):
            self.conn.execute("UPDATE bank_rows SET remittance='INV 42' WHERE row_id=1")
        self.settle(self.p1)
        out = self.write("propose", self.p1, d)
        self.assertEqual((out["wrote"], out["match_id"]), (True, mid))
        fp = self.conn.execute("SELECT fp FROM log WHERE match_id=? ORDER BY seq DESC LIMIT 1",
                               (mid,)).fetchone()[0]
        self.assertEqual(json.loads(fp)["facts"]["remittance"], "INV 42")

    def test_an_operator_rejection_refuses_both_writes(self):
        import matches
        d = self.doc()
        mid = self.write("propose", self.p1, d)["match_id"]
        rid = self.show(self.p1)     # outside granted's tx: show opens its own
        self.granted(lambda c, grant: matches.reject_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        for kind in ("pair", "propose"):
            with self.assertRaisesRegex(db.Refusal, "operator rejected"):
                self.write(kind, self.p1, d)

    def test_an_operator_pairing_is_never_reopened(self):
        import matches
        d = self.doc()
        mid = self.write("propose", self.p1, d)["match_id"]
        rid = self.show(self.p1)     # outside granted's tx: show opens its own
        self.granted(lambda c, grant: matches.confirm_in_tx(
            c, grant=grant, match_id=mid, expected_revision=self.rev(match_id=mid),
            render_id=rid, bind="rendered"))
        with self.assertRaisesRegex(db.Refusal, "never reopened"):
            self.write("propose", self.p1, self.doc())

    def test_deleted_gates_no_longer_refuse(self):
        self.classify(self.p1, set())                    # unclassified
        self.settle(self.p1)
        r = self.doc(kind="receipt", document_number="X-1", issuer="Adobe")
        self.doc(kind="invoice", document_number="X-1", issuer="Adobe")    # a duplicate
        self.assertEqual(self.write("pair", self.p1, r)["state"], "matched")

    def test_no_document_expected_still_refuses(self):
        import kb
        kb.set_expectation(self.conn, scope_type="counterparty", scope="Adobe", kind="none",
                           author="specialist", token=self.token)
        with self.assertRaisesRegex(db.Refusal, "no document is expected"):
            self.write("pair", self.p1, self.doc())

    def test_an_edited_alternative_moves_the_proposal_revision(self):
        import documents
        a, b = self.doc(), self.doc()
        mid = self.write("propose", self.p1, a, alternatives=[b])["match_id"]
        revs = (self.rev(self.p1), self.rev(match_id=mid))
        documents.update_document_metadata(self.conn, b, token=self.token, amount_minor=9000)
        self.assertEqual((self.rev(self.p1), self.rev(match_id=mid)),
                         (revs[0] + 1, revs[1] + 1))

    def test_alternatives_go_with_a_proposal_and_are_floored_too(self):
        a, b = self.doc(), self.doc()
        with self.assertRaisesRegex(db.Refusal, "never with a match"):
            self.write("pair", self.p1, a, alternatives=[b])
        with self.assertRaisesRegex(db.Refusal, "up to 3 other documents"):
            self.write("propose", self.p1, a, alternatives=[self.doc() for _ in range(4)])
        with self.assertRaisesRegex(db.Refusal, "up to 3 other documents"):
            self.write("propose", self.p1, a, alternatives=[a])
        self.write("propose", self.p2, b)
        with self.assertRaisesRegex(db.Refusal, f"document #{b} is taken"):
            self.write("propose", self.p1, a, alternatives=[b])

    def test_the_tools_take_alternatives_and_no_resolves(self):
        import qa_server
        import tools  # noqa: F401 — registers the tools
        schema = {n: t["schema"]["properties"] for n, t in qa_server.TOOLS.items()}
        for name in ("record_match", "propose_match"):
            self.assertNotIn("resolves", schema[name])  # removed-name: asserted absent
        self.assertIn("alternatives", schema["propose_match"])
        self.assertNotIn("alternatives", schema["record_match"])
        a, b = self.doc(), self.doc()
        out = qa_server.TOOLS["propose_match"]["fn"]({
            "pid": self.p1, "doc_id": a, "expected_revision": self.rev(self.p1),
            "alternatives": [b], "document_date": "2026-09-01", "pass_token": self.token})
        self.assertEqual(json.loads(self.conn.execute(
            "SELECT alternatives_json FROM matches WHERE match_id=?",
            (out["match_id"],)).fetchone()[0]), [b])
