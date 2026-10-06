import unittest

from tests._base import TempEnv  # noqa: F401
import expectation as ex  # noqa: E402
import fold as F  # noqa: E402
import reducer as R  # noqa: E402

FACTS = {"account_id": "acc-biz", "direction": "DBIT", "currency": "EUR",
         "amount_minor": 10000, "status": "BOOK", "booking_date": "2026-07-03",
         "counterparty": "Adobe", "remittance": "", "needs_review": 0, "review_reason": None}
INVOICE = ex.derive("DBIT", {"software"})
PAYSLIP = ex.derive("DBIT", {"income", "salary"})
UNKNOWN = ex.derive("DBIT", set())


def fp(facts=FACTS, kind="invoice"):
    return R.fingerprint(facts, kind)


def inputs(entries, facts=FACTS, exp=INVOICE, last_known="invoice", doc_kinds=None,
           eligible=True, ended=None, portal=False):
    return R.Inputs(ended=ended, eligible=eligible, fold=F.fold(entries), facts=facts,
                    expectation=exp, last_known_kind=last_known,
                    doc_kinds=doc_kinds or {1: "invoice", 2: "invoice"}, portal=portal)


def op_pair(seq, mid, f=None):
    return F.Entry(seq, "pair", "operator", mid, mid, fp=f or fp())


def auto(seq, mid, kind="pair", f=None):
    return F.Entry(seq, kind, "auto", mid, mid, fp=f or fp())


class TestDesiredSet(unittest.TestCase):
    def test_no_pairing_required(self):
        r = R.reduce(inputs([]))
        self.assertEqual((r.desired, r.status), (frozenset({"acct::open"}), "open"))

    def test_optional_missing_has_no_tag(self):
        r = R.reduce(inputs([], exp=PAYSLIP, last_known="payslip"))
        self.assertEqual((r.desired, r.status), (frozenset(), "optional"))

    def test_none_expected(self):
        r = R.reduce(inputs([], exp=ex.derive("DBIT", {"internal-transfer"})))
        self.assertEqual(r.desired, frozenset({"acct::no-document-expected"}))

    def test_exemption_beats_everything_and_keeps_portal(self):
        r = R.reduce(inputs([F.Entry(1, "exempt", "operator")], portal=True))
        self.assertEqual(r.desired, frozenset({"acct::no-document-expected", "acct::portal"}))
        self.assertEqual(r.status, "exempt")

    def test_portal_unpaired_is_open_and_portal(self):
        r = R.reduce(inputs([], portal=True))
        self.assertEqual(r.desired, frozenset({"acct::open", "acct::portal"}))

    def test_ineligible_and_ended_desire_nothing(self):
        self.assertEqual(R.reduce(inputs([op_pair(1, 1)], eligible=False, portal=True)).desired,
                         frozenset())
        r = R.reduce(inputs([op_pair(1, 1)], ended="erased", portal=True))
        self.assertEqual((r.desired, r.status), (frozenset(), "ended"))

    def test_unknown_desires_open(self):
        r = R.reduce(inputs([], exp=UNKNOWN, last_known=None))
        self.assertEqual(r.desired, frozenset({"acct::open"}))
        self.assertNotIn("unclassified", r.reasons)       # the classification gate is gone

    def test_classification_conflict_reason(self):
        conflict = ex.derive("DBIT", {"internal-transfer", "refund"})
        self.assertTrue(conflict.unknown and conflict.conflict)
        r = R.reduce(inputs([], exp=conflict, last_known=None))
        self.assertEqual(r.desired, frozenset({"acct::open"}))
        self.assertNotIn("classification-conflict", r.reasons)
        self.assertNotIn("unclassified", r.reasons)

    def test_status_tags_are_exclusive(self):
        for entries in ([], [op_pair(1, 1)], [auto(1, 1, "propose")]):
            r = R.reduce(inputs(entries))
            self.assertEqual(len(r.desired & {"acct::open", "acct::matched", "acct::proposed",
                                              "acct::no-document-expected"}), 1)


class TestValidity(unittest.TestCase):
    def test_operator_pairing_invalidated_by_an_in_place_correction(self):
        corrected = dict(FACTS, amount_minor=9000)
        r = R.reduce(inputs([op_pair(1, 1)], facts=corrected))
        self.assertEqual((r.desired, r.current), (frozenset({"acct::proposed"}), 1))
        self.assertIn("facts-changed", r.reasons)

    def test_reverted_correction_restores_the_acceptance(self):
        self.assertEqual(R.reduce(inputs([op_pair(1, 1)], facts=dict(FACTS))).desired,
                         frozenset({"acct::matched"}))

    def test_confirmation_against_new_facts_restores_matched(self):
        corrected = dict(FACTS, amount_minor=9000)
        r = R.reduce(inputs([op_pair(1, 1), op_pair(2, 1, f=fp(corrected))], facts=corrected))
        self.assertEqual(r.desired, frozenset({"acct::matched"}))

    def test_round14_A_then_B_then_revert_keeps_B_current(self):
        at90 = dict(FACTS, amount_minor=9000)
        entries = [F.Entry(1, "pair", "operator", 1, 1, fp=fp()),
                   F.Entry(2, "pair", "operator", 2, 2, fp=fp(at90))]
        r = R.reduce(inputs(entries, facts=FACTS))
        self.assertEqual((r.current, r.desired), (2, frozenset({"acct::proposed"})))

    def test_exemption_survives_a_correction(self):
        r = R.reduce(inputs([F.Entry(1, "exempt", "operator")],
                            facts=dict(FACTS, amount_minor=1)))
        self.assertEqual(r.desired, frozenset({"acct::no-document-expected"}))

    def test_auto_proposal_stays_proposed_after_correction(self):
        r = R.reduce(inputs([auto(1, 1, "propose")], facts=dict(FACTS, amount_minor=10500)))
        self.assertEqual(r.desired, frozenset({"acct::proposed"}))

    def test_tier_only_change_leaves_matched(self):
        optional_invoice = ex.Expectation("invoice", "optional", 11)
        self.assertEqual(R.reduce(inputs([auto(1, 1)], exp=optional_invoice)).desired,
                         frozenset({"acct::matched"}))

    def test_conflicted_only_is_a_proposal(self):
        # D3: a joint machine set is one proposal awaiting the operator's pick
        r = R.reduce(inputs([auto(1, 1), auto(2, 2)]))
        self.assertEqual((r.desired, r.status), (frozenset({"acct::proposed"}), "proposed"))
        self.assertIn("conflicted", r.reasons)

    def test_machine_proposal_stays_proposed_when_unconfirmed(self):
        # S2 (Astra round on 61bcbaa): step 5's `ok` requires `m.state ==
        # "matched"`. An auto `propose` entry (never confirmed to a `pair`)
        # is state "proposed" even with unchanged facts; dropping this guard would
        # emit acct::matched for it.
        r = R.reduce(inputs([auto(1, 1, "propose")]))
        self.assertEqual(r.desired, frozenset({"acct::proposed"}))
        self.assertNotIn("facts-changed", r.reasons)

    def test_machine_pair_invalidated_by_facts_changing_after_pairing(self):
        # S2: step 5's `ok` also requires `row_ok`. A machine `pair`
        # (state "matched") whose material facts changed post-pairing
        # (amount 10000 -> 9000) must fall back to acct::proposed; dropping
        # `row_ok` from `ok` would emit acct::matched with stale facts.
        r = R.reduce(inputs([auto(1, 1)], facts=dict(FACTS, amount_minor=9000)))
        self.assertEqual(r.desired, frozenset({"acct::proposed"}))
        self.assertIn("facts-changed", r.reasons)


class TestFixedPoint(unittest.TestCase):
    def test_reaches_desired_from_any_start_and_keeps_foreign_tags(self):
        desired = frozenset({"acct::open", "acct::portal"})
        starts = [set(), {"acct::matched"}, {"acct::matched", "acct::proposed", "food"},
                  {"acct::custom", "acct-matched", "acct::open"}, set(R.OWNED)]
        for s in starts:
            got = R.apply_fixed_point(set(s), desired)
            self.assertEqual(got & set(R.OWNED), set(desired))
            self.assertEqual(got - set(R.OWNED), s - set(R.OWNED))

    def test_owned_tags_satisfy_bank_feed_grammar(self):
        import re
        g = re.compile(r"^(?:[a-z][a-z0-9-]{0,15}::)?[a-z0-9][a-z0-9-]{0,31}$")
        for t in R.OWNED:
            self.assertTrue(g.match(t) and t.startswith("acct::"), t)


if __name__ == "__main__":
    unittest.main()
