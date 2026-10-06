# tests/test_issues_34_36.py
"""Issues #34–#36, the v0.7.0 live retest (design
docs/superpowers/specs/2026-10-02-issues-34-36-design.md): an operator's rejection sticks
(G), a 0.00 row wants its document optionally (Z), and a foreign-currency document is
screened by the bank's own rate (R)."""
import unittest

from tests._base import StoreCase
import db  # noqa: E402
import documents  # noqa: E402
import expectation as ex  # noqa: E402
import fx  # noqa: E402
import ledger  # noqa: E402
import lineage  # noqa: E402
import matches  # noqa: E402
import reducer as R  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)

    def machine(self, doc_id, kind="propose", pid=None):
        pid = pid or self.pid
        fn = matches.record_match if kind == "record" else matches.propose_match
        extra = {"author": "auto"} if kind == "record" else {}
        return fn(self.conn, pid=pid, doc_id=doc_id, expected_revision=self.rev(pid),
                  row_snapshot=self.snapshot(pid), token=self.token, **extra)

    def reject(self, mid, pid=None):
        rid = self.show(pid or self.pid)
        self.granted(matches.reject_in_tx, match_id=mid, expected_revision=self.rev(match_id=mid),
                     render_id=rid)


class TestARejectionSticks(Base):
    def test_the_same_pairing_is_refused_for_a_proposal_and_a_pairing(self):
        doc = self.doc()
        self.reject(self.machine(doc)["match_id"])
        for kind in ("propose", "record"):
            with self.assertRaises(db.Refusal) as cm:
                self.machine(doc, kind)
            self.assertIn(f"the operator rejected document #{doc} for this payment",
                          str(cm.exception))

    def test_a_change_to_the_payment_lifts_it(self):
        doc = self.doc()
        self.reject(self.machine(doc)["match_id"])
        self.row(1, remittance="INV-42")                 # the bank corrected the row
        self.settle(self.pid)
        self.assertEqual(self.machine(doc)["state"], "proposed")

    def test_a_change_to_the_documents_amount_lifts_it_but_a_date_read_stamp_does_not(self):
        doc = self.doc()
        self.reject(self.machine(doc)["match_id"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE documents SET date_read_at=? WHERE doc_id=?",
                              (db.now(), doc))
        with self.assertRaises(db.Refusal):
            self.machine(doc)
        documents.update_document_metadata(self.conn, doc, token=self.token, amount_minor=9999)
        self.row(1, amount_minor=9999)                 # and a payment it now fits
        self.settle(self.pid)
        self.assertEqual(self.machine(doc)["state"], "proposed")

    def test_a_rejection_from_before_0_8_does_not_block(self):
        # D1 (Astra S1): it recorded neither side, so a corrected document could never
        # lift it — 0.7.0's behaviour: the operator is asked again, and that answer sticks
        doc = self.doc()
        self.reject(self.machine(doc)["match_id"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE log SET detail=NULL, fp=NULL WHERE kind='unpair'")
        self.assertEqual(self.machine(doc)["state"], "proposed")
        self.reject(self.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                      (self.pid,)).fetchone()[0])
        with self.assertRaises(db.Refusal):
            self.machine(doc)

    def test_the_rejection_binds_the_facts_the_operator_saw(self):
        # D1 (Astra S1): proposed at 100.00, the payment corrected to 90.00, then rejected
        doc = self.doc()
        mid = self.machine(doc)["match_id"]
        self.row(1, amount_minor=9000)
        self.settle(self.pid)
        self.reject(mid)
        with self.assertRaises(db.Refusal):
            self.machine(doc)

    def test_a_merge_keeps_the_rejection(self):
        # D1 (Astra S1, Terra S1): the rejected id and another id for the same document
        doc = self.doc()
        self.reject(self.machine(doc)["match_id"])
        self.row(2)                                          # identical facts
        twin = self.lineage_for(2)
        self.classify(twin, {"software"})
        self.settle(twin)
        self.machine(doc, pid=twin)
        with db.tx(self.conn):
            ledger.merge(self.conn, twin, self.pid)
            lineage.settle(self.conn, twin)
        row = lineage.live_row(self.conn, lineage.projection(self.conn, twin))
        self.assertIsNotNone(matches.rejected_by_operator(
            self.conn, twin, documents._doc(self.conn, doc), R.facts_of(row), "invoice",
            matches.row_fx(row)))

    def test_a_date_read_in_the_same_call_lifts_it(self):
        # C1 (Terra S1, Astra S1): the specialist's documented calling pattern
        doc = self.doc(document_date="2026-07-02")
        self.reject(self.machine(doc)["match_id"])
        with self.assertRaises(db.Refusal):
            self.machine(doc)
        out = matches.propose_match(self.conn, pid=self.pid, doc_id=doc,
                                    expected_revision=self.rev(self.pid),
                                    row_snapshot=self.snapshot(self.pid), token=self.token,
                                    document_date="2026-07-03")
        self.assertEqual(out["state"], "proposed")

    def test_a_merge_does_not_bring_back_a_rejected_pairing(self):
        # C1 (Astra S1): the twin's machine proposal for the rejected document is retired
        doc = self.doc()
        self.reject(self.machine(doc)["match_id"])
        self.row(2)
        twin = self.lineage_for(2)
        self.classify(twin, {"software"})
        self.settle(twin)
        mid = self.machine(doc, pid=twin)["match_id"]
        with db.tx(self.conn):
            ledger.merge(self.conn, twin, self.pid)
            lineage.settle(self.conn, twin)
        self.assertEqual(self.conn.execute("SELECT state FROM match_state WHERE match_id=?",
                                           (mid,)).fetchone()[0], "rejected")
        self.assertNotEqual(lineage.projection(self.conn, twin)["status"], "proposed")

    def test_a_merge_does_not_offer_a_rejected_document_as_a_candidate(self):
        # C2 (Astra S1): the collision makes it conflicted before the rule looks
        rejected = self.doc(document_number="REJECTED")
        self.reject(self.machine(rejected)["match_id"])
        self.machine(self.doc(document_number="OTHER"))
        self.row(2)
        twin = self.lineage_for(2)
        self.classify(twin, {"software"})
        self.settle(twin)
        mid = self.machine(rejected, pid=twin)["match_id"]
        with db.tx(self.conn):
            ledger.merge(self.conn, twin, self.pid)
            lineage.settle(self.conn, twin)
        self.assertEqual(self.conn.execute("SELECT state FROM match_state WHERE match_id=?",
                                           (mid,)).fetchone()[0], "rejected")
        d = work.describe(self.conn, twin)
        self.assertNotIn(rejected, [c["document"]["doc_id"] for c in d["candidates"]])
        self.assertNotIn("REJECTED", " ".join(views.evidence(d)))

    def test_wrong_on_merged_duplicates_sets_them_all_aside(self):
        # C3 (Astra S1): rejecting the first must not move the revision the second is
        # bound to — every rejection is recorded, then the payment settles once
        import fold as F
        doc = self.doc()
        first = self.machine(doc)["match_id"]
        self.row(2)
        twin = self.lineage_for(2)
        self.classify(twin, {"software"})
        self.settle(twin)
        # two lineages that each paired `doc` (what a merge brings together): the floor
        # refuses the second write ("taken"), so it is laid down directly
        second = self.machine_entry(twin, doc, kind="propose")     # occupied: conflicted
        with db.tx(self.conn):
            ledger.merge(self.conn, self.pid, twin)
            st = lineage.fold_of(self.conn, self.pid)
            c = st.cands[first]
            lineage._record_retirement(self.conn, self.pid,
                                       F.Retirement(first, c.activation, "conflicted", "test"))
            lineage.settle(self.conn, self.pid)
        d = work.describe(self.conn, self.pid)
        self.assertEqual(sorted(c["match_id"] for c in d["candidates"]), sorted([first, second]))
        rid = self.show(self.pid)
        # S7: reply._set_aside_all's write is reject_all_in_tx under a tap's grant
        self.granted(matches.reject_all_in_tx, self.pid, [(first, rid), (second, rid)])
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT match_id FROM log WHERE kind='unpair' AND author='operator'")),
            sorted([first, second]))

    def test_the_same_rate_written_differently_is_the_same_evidence(self):
        # C1 (Astra S1)
        self.row(1, fx_rate="1.10", fx_unit="EUR")
        self.settle(self.pid)
        doc = self.doc(currency="USD", amount_minor=11000)
        self.reject(self.machine(doc)["match_id"])
        self.row(1, fx_rate="1.100", fx_unit="EUR")
        self.settle(self.pid)
        with self.assertRaises(db.Refusal):
            self.machine(doc)

    def test_a_changed_rate_moves_the_revisions_an_old_review_binds(self):
        # C1 (Astra S1): the converted amount is shown, so a new rate is a new question
        self.row(1, fx_rate="1.10", fx_unit="EUR")
        self.settle(self.pid)
        mid = self.machine(self.doc(currency="USD", amount_minor=11000))["match_id"]
        before = (self.rev(self.pid), self.rev(match_id=mid))
        self.row(1, fx_rate="1.12", fx_unit="EUR")
        self.settle(self.pid)
        after = (self.rev(self.pid), self.rev(match_id=mid))
        self.assertTrue(after[0] > before[0] and after[1] > before[1], (before, after))

    def test_no_rate_moves_no_revision(self):
        mid = self.machine(self.doc())["match_id"]
        before = (self.rev(self.pid), self.rev(match_id=mid))
        self.settle(self.pid)
        self.assertEqual((self.rev(self.pid), self.rev(match_id=mid)), before)

    def test_a_corrected_exchange_rate_lifts_it(self):
        # D1 (Terra S1): the rate is evidence for #35's screen
        self.row(1, fx_rate="1.10", fx_unit="EUR")
        self.settle(self.pid)
        doc = self.doc(currency="USD", amount_minor=11000)
        self.reject(self.machine(doc)["match_id"])
        with self.assertRaises(db.Refusal):
            self.machine(doc)
        self.row(1, fx_rate="1.11", fx_unit="EUR")
        self.settle(self.pid)
        self.assertEqual(self.machine(doc)["state"], "proposed")

    def test_another_payment_may_take_the_document(self):
        doc = self.doc()
        self.reject(self.machine(doc)["match_id"])
        self.row(2, booking_date="2026-07-04")
        other = self.lineage_for(2)
        self.classify(other, {"software"})
        self.settle(other)
        self.assertEqual(self.machine(doc, pid=other)["state"], "proposed")

    def test_the_operator_can_still_pair_it_and_then_the_machine_may_again(self):
        doc = self.doc()
        mid = self.machine(doc)["match_id"]
        self.reject(mid)
        rid = self.show(self.pid)
        self.operator_pair(pid=self.pid, doc_id=doc, expected_revision=self.rev(self.pid),
                           render_id=rid)
        self.assertIsNone(matches.rejected_by_operator(
            self.conn, self.pid, documents._doc(self.conn, doc),
            R.facts_of(self.snapshot(self.pid)), "invoice", None))

    def test_a_blocked_document_makes_no_payment_judge_due(self):
        doc = self.doc()
        self.assertTrue(work.describe(self.conn, self.pid)["fresh"])
        self.assertIn(self.pid, work.judge_due_state(self.conn))      # it fits: due
        self.reject(self.machine(doc)["match_id"])
        self.assertNotIn(self.pid, work.judge_due_state(self.conn))   # rejected: not due
        self.doc(document_number="OTHER")                              # another that fits
        self.assertIn(self.pid, work.judge_due_state(self.conn))


class TestZeroRows(StoreCase):
    def test_a_zero_amount_wants_its_document_optionally(self):
        e = ex.derive("DBIT", {"software"}, zero=True)
        self.assertEqual((e.kind, e.tier), ("invoice", "optional"))
        self.assertEqual(ex.derive("DBIT", {"software"}).tier, "required")
        self.assertEqual(ex.derive("DBIT", {"unclassifiable"}, zero=True).tier, "optional")

    def test_explicit_and_unknown_expectations_are_kept(self):
        e = ex.derive("DBIT", {"software"}, zero=True,
                      counterparty_override=("invoice", "required"))
        self.assertEqual((e.kind, e.tier, e.row), ("invoice", "required", 2))
        self.assertEqual(ex.derive("DBIT", set(), zero=True).kind, None)
        self.assertEqual(ex.derive("DBIT", {"internal-transfer"}, zero=True).kind, "none")
        self.assertEqual(ex.derive("DBIT", {"software"}, exempt=True, zero=True).kind, "none")

    def test_a_zero_row_is_not_missing_and_its_document_still_pairs(self):
        self.bind()
        token = self.pass_()
        self.row(1, amount_minor=0)
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        d = work.describe(self.conn, pid)
        self.assertEqual(d["expectation"]["tier"], "optional")
        doc = self.doc(amount_minor=0)
        out = matches.record_match(self.conn, pid=pid, doc_id=doc, author="auto",
                                   expected_revision=self.rev(pid),
                                   row_snapshot=self.snapshot(pid), token=token)
        self.assertEqual(out["state"], "matched")


class TestTheBanksRate(Base):
    FX = {"rate": "1.1608109839149589", "unit": "EUR"}

    def test_the_expected_amount_both_ways(self):
        self.assertEqual(fx.expected(self.FX, 948, "EUR", "USD"), 1100)
        self.assertEqual(fx.expected({"rate": "0.8615", "unit": "USD"}, 948, "EUR", "USD"), 1100)
        self.assertIsNone(fx.expected({"rate": "1.2", "unit": "GBP"}, 948, "EUR", "USD"))
        self.assertIsNone(fx.expected(None, 948, "EUR", "USD"))
        self.assertIsNone(fx.expected(self.FX, 948, "EUR", "EUR"))

    def test_the_screen(self):
        self.assertIsNone(fx.screen(self.FX, 948, "EUR", 1100, "USD"))
        self.assertIsNone(fx.screen(self.FX, 948, "EUR", 1111, "USD"))       # 1% off
        self.assertIn("cannot be the EUR 9.52 payment",
                      fx.screen(self.FX, 952, "EUR", 718, "USD"))          # 35% off
        self.assertIsNone(fx.screen(None, 952, "EUR", 718, "USD"))         # no rate

    def test_the_tolerance_boundaries(self):
        # C1 (Astra: mutants survived): 3% of the expected amount, never less than 0.02
        fx_ = {"rate": "1.105", "unit": "EUR"}                        # 1000 -> 1105
        self.assertIsNone(fx.screen(fx_, 1000, "EUR", 1105 + 34, "USD"))
        self.assertIsNotNone(fx.screen(fx_, 1000, "EUR", 1105 + 35, "USD"))
        self.assertIsNone(fx.screen(fx_, 1000, "EUR", 1105 - 34, "USD"))
        small = {"rate": "1", "unit": "EUR"}                          # 30 -> 30
        self.assertIsNone(fx.screen(small, 30, "EUR", 32, "USD"))
        self.assertIsNotNone(fx.screen(small, 30, "EUR", 33, "USD"))

    def test_judge_due_follows_the_rate(self):
        # C1 (Astra: mutant survived): a document the rate rules out makes nothing due
        self.fx_row()
        self.doc(amount_minor=718, currency="USD", document_date="2026-07-02")
        self.assertNotIn(self.pid, work.judge_due_state(self.conn))
        self.doc(amount_minor=1105, currency="USD", document_date="2026-07-02")
        self.assertIn(self.pid, work.judge_due_state(self.conn))

    def test_the_pair_is_kept_only_valid(self):
        self.assertEqual(fx.pair("1.16", "EUR"), ("1.16", "EUR"))
        for rate, unit in (("1.16", None), (None, "EUR"), ("0", "EUR"), ("-1", "EUR"),
                           ("1e3", "EUR"), ("1.16", "eur"), ("1" * 16, "EUR"),
                           ("1." + "1" * 21, "EUR")):
            self.assertIsNone(fx.pair(rate, unit), (rate, unit))

    def test_parse_reads_the_columns_and_does_without_them(self):
        head = ("row_id,account_id,first_seen,booking_date,value_date,amount_minor,currency,"
                "direction,status,counterparty,remittance,state,superseded_by,needs_review,"
                "review_reason,tags,tag_revision")
        line = "1,a,2026-07-01T00:00:00Z,2026-07-03,2026-07-03,948,EUR,DBIT,BOOK,X,,active,,0,,,0"
        rows = ledger.parse("e.csv", (head + "\n" + line + "\n").encode())
        self.assertEqual((rows[0]["fx_rate"], rows[0]["fx_unit"]), (None, None))
        rows = ledger.parse("e.csv", (head + ",exchange_rate,exchange_unit_currency\n" + line
                                      + ",1.1608,EUR\n").encode())
        self.assertEqual((rows[0]["fx_rate"], rows[0]["fx_unit"]), ("1.1608", "EUR"))
        rows = ledger.parse("e.csv", (head + ",exchange_rate,exchange_unit_currency\n" + line
                                      + ",1.1608,\n").encode())
        self.assertEqual((rows[0]["fx_rate"], rows[0]["fx_unit"]), (None, None))

    def fx_row(self):
        self.row(1, amount_minor=952, fx_rate="1.1608109839149589", fx_unit="EUR")
        self.settle(self.pid)

    def test_a_document_the_rate_rules_out_is_refused_and_one_it_allows_is_proposed(self):
        before = R.facts_of(self.snapshot(self.pid))
        self.fx_row()
        # the rate is not a fact: no fingerprint moves because bank-feed exposes it
        self.assertEqual(R.facts_of(self.snapshot(self.pid)), dict(before, amount_minor=952))
        bad = self.doc(amount_minor=718, currency="USD")
        with self.assertRaises(db.Refusal) as cm:
            self.machine(bad)
        self.assertIn("at the bank's rate that payment is USD 11.05", str(cm.exception))
        good = self.doc(amount_minor=1105, currency="USD")
        self.assertEqual(self.machine(good)["state"], "proposed")

    def test_the_review_shows_the_banks_conversion(self):
        self.fx_row()
        self.machine(self.doc(amount_minor=1105, currency="USD"))
        d = work.describe(self.conn, self.pid)
        self.assertIn("The invoice is in USD 11.05; the payment is EUR 9.52 (USD 11.05 at the "
                      "bank's rate).", views.evidence(d))


if __name__ == "__main__":
    unittest.main()
