"""The decision table of spec §"Document expectation" as an executable
oracle. Every row is exercised; the round-25/26/27 cases are named."""
import unittest

from tests._base import TempEnv
import expectation as ex  # noqa: E402
from expectation import Expectation as E  # noqa: E402

D, C = "DBIT", "CRDT"
REQ, OPT = "required", "optional"

# (direction, tags, kwargs, expected (kind, tier, row, conflict))
ORACLE = [
    (D, {"transport", "fuel"}, {"exempt": True}, ("none", None, 1, False)),
    (D, {"awaiting-operator"}, {"counterparty_override": ("none", None)}, ("none", None, 2, False)),
    (D, set(), {"counterparty_override": ("receipt", OPT)}, ("receipt", OPT, 2, False)),
    (D, {"unclassifiable"}, {}, ("invoice", REQ, 3, False)),
    (D, {"unclassifiable", "awaiting-operator"}, {}, ("invoice", REQ, 3, False)),
    (C, {"unclassifiable"}, {}, ("sales-invoice", REQ, 3, False)),
    (D, set(), {}, (None, REQ, 4, False)),
    (C, set(), {}, (None, REQ, 4, False)),
    (D, {"awaiting-operator", "transport"}, {}, (None, REQ, 4, False)),
    (D, {"acct::open"}, {}, (None, REQ, 4, False)),
    (D, {"salary", "fees"}, {}, (None, REQ, 5, True)),
    (D, {"internal-transfer", "refund"}, {}, (None, REQ, 5, True)),
    (D, {"income", "consulting", "internal-transfer"}, {}, ("none", None, 6, False)),
    (C, {"income", "consulting", "internal-transfer"}, {}, ("none", None, 6, False)),
    (D, {"cash-withdrawal"}, {}, ("none", None, 6, False)),
    (D, {"refund"}, {}, ("credit-note", REQ, 7, False)),
    (D, {"income", "refund"}, {}, ("credit-note", REQ, 7, False)),      # D9
    (D, {"transport", "fuel", "refund"}, {}, ("credit-note", REQ, 7, False)),
    (C, {"refund"}, {}, ("credit-note", REQ, 7, False)),
    (C, {"income", "refund"}, {}, ("credit-note", REQ, 7, False)),
    (D, {"reimbursement"}, {}, ("receipt", OPT, 8, False)),
    (C, {"reimbursement"}, {}, ("receipt", OPT, 8, False)),
    (D, {"income", "salary"}, {}, ("payslip", OPT, 9, False)),
    (D, {"payroll"}, {}, ("payslip", OPT, 9, False)),
    (D, {"fees"}, {}, ("statement", OPT, 10, False)),
    (D, {"tax"}, {}, ("statement", OPT, 10, False)),
    (D, {"interest"}, {}, ("statement", OPT, 10, False)),
    (D, {"transport", "fuel"}, {}, ("invoice", REQ, 11, False)),
    (D, {"invoice-missing", "software"}, {}, ("invoice", REQ, 11, False)),
    (C, {"income", "interest"}, {}, ("none", None, 12, False)),
    (C, {"income", "dividend"}, {}, ("none", None, 12, False)),
    (C, {"income", "consulting"}, {}, ("sales-invoice", REQ, 13, False)),
    (C, {"income", "salary"}, {}, ("sales-invoice", REQ, 13, False)),
]


class TestDecisionTable(unittest.TestCase):
    def test_oracle(self):
        for direction, tags, kw, want in ORACLE:
            with self.subTest(direction=direction, tags=sorted(tags), kw=kw):
                got = ex.derive(direction, tags, **kw)
                self.assertEqual((got.kind, got.tier, got.row, got.conflict), want)

    def test_unknown_and_seeks(self):
        self.assertTrue(ex.derive(D, set()).unknown)
        self.assertFalse(ex.derive(D, {"transport"}).unknown)
        self.assertTrue(ex.derive(D, {"transport"}).seeks_document)
        self.assertFalse(ex.derive(D, {"internal-transfer"}).seeks_document)

    @staticmethod
    def ov(scope, kind, tier):
        rows, key = ex.normalize_scope(frozenset(scope))
        return (rows, key, kind, tier)

    def test_chain_override_for_income_refund_applies_to_every_refund_row(self):
        ov = [self.ov({"income", "refund"}, "receipt", OPT)]
        for direction, tags in ((C, {"refund"}), (C, {"income", "refund"}), (D, {"refund"})):
            got = ex.derive(direction, tags, chain_overrides=ov)
            self.assertEqual((got.kind, got.tier, got.row), ("receipt", OPT, 7), (direction, tags))

    def test_payslips_dont_matter(self):
        ov = [self.ov({"salary"}, "none", None)]
        got = ex.derive(D, {"income", "salary"}, chain_overrides=ov)
        self.assertEqual((got.kind, got.row), ("none", 9))
        # a CRDT carrying salary is decided at row 13, where a row-9 override never applies
        self.assertEqual(ex.derive(C, {"income", "salary"}, chain_overrides=ov).kind, "sales-invoice")

    def test_most_specific_chain_override_wins(self):
        ov = [self.ov({"transport"}, "receipt", OPT),
              self.ov({"transport", "fuel"}, "none", None)]
        self.assertEqual(ex.derive(D, {"transport", "fuel"}, chain_overrides=ov).kind, "none")
        self.assertEqual(ex.derive(D, {"transport", "train"}, chain_overrides=ov).kind, "receipt")

    def test_precedence_exemption_over_counterparty_over_chain(self):
        ov = [self.ov({"transport"}, "receipt", OPT)]
        cp = ("none", None)
        self.assertEqual(ex.derive(D, {"transport"}, counterparty_override=cp,
                                   chain_overrides=ov).row, 2)
        self.assertEqual(ex.derive(D, {"transport"}, exempt=True, counterparty_override=cp,
                                   chain_overrides=ov).row, 1)

    def test_scope_normalization(self):
        self.assertEqual(ex.normalize_scope(frozenset({"salary"})), (frozenset({9}), frozenset({"salary"})))
        self.assertEqual(ex.normalize_scope(frozenset({"income", "refund"}))[0], frozenset({7}))
        self.assertEqual(ex.normalize_scope(frozenset({"transport", "fuel"}))[0], frozenset({11, 13}))
        self.assertIsNone(ex.normalize_scope(frozenset({"salary", "tax"})))

    def test_chain_overrides_never_reach_rows_3_to_5(self):
        ov = [self.ov({"salary"}, "none", None)]
        self.assertEqual(ex.derive(D, {"unclassifiable"}, chain_overrides=ov).row, 3)
        self.assertEqual(ex.derive(D, {"awaiting-operator", "salary"}, chain_overrides=ov).row, 4)
        self.assertEqual(ex.derive(D, {"salary", "tax"}, chain_overrides=ov).row, 5)

    def test_colliding_normalized_scopes_raise(self):
        # `refund` and `income, refund` both normalize to ({7}, {refund}):
        # decisive() keys row 7 on the flow tag alone, so the extra `income`
        # chain tag on the second scope is dropped and the two collide.
        refund_pair = [self.ov({"refund"}, "credit-note", REQ),
                       self.ov({"income", "refund"}, "none", None)]
        with self.assertRaises(ValueError):
            ex.derive(D, {"refund"}, chain_overrides=refund_pair)

        # `bank, fees` and `card, fees` both normalize to ({10}, {fees}): row
        # 10 is keyed on the statement marker alone, so the differing
        # `bank`/`card` tags are dropped and the two collide.
        fees_pair = [self.ov({"bank", "fees"}, "statement", OPT),
                     self.ov({"card", "fees"}, "none", None)]
        with self.assertRaises(ValueError):
            ex.derive(D, {"fees"}, chain_overrides=fees_pair)

    def test_scope_with_no_classification_tag_is_not_an_override(self):
        # Workflow markers and owner::name tags carry no classification
        # content; a scope reduced to nothing must not become a catch-all
        # for rows 11/13 (an empty key is a subset of every override key).
        self.assertIsNone(ex.normalize_scope(frozenset({"awaiting-operator"})))
        self.assertIsNone(ex.normalize_scope(frozenset({"acct::open"})))
        self.assertIsNone(ex.normalize_scope(frozenset()))


class TestParityWithBankFeed(TempEnv):
    def test_classification_state_matches_bank_feed(self):
        from tests import bankfeed
        bankfeed.load()
        import rules
        cases = [[], ["acct::open"], ["awaiting-operator"], ["unclassifiable"],
                 ["unclassifiable", "awaiting-operator"], ["food"], ["food", "acct::matched"],
                 ["awaiting-operator", "food"], ["owner::x"]]
        for tags in cases:
            self.assertEqual(ex.classification_state(tags), rules.classification_state(tags), tags)
            for t in tags:
                self.assertEqual(ex.is_classification_tag(t), rules.is_classification_tag(t), t)


if __name__ == "__main__":
    unittest.main()
