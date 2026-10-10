"""#116: words saying what a payment is ("yes classify wage tax as nice to have") are no
reading: the desk classifies the payment under discussion and asks for the check of its
quarter, and a card about where a quarter stands names the quarter being talked about. The
document the desk says a classification gives is the one the plugin derives."""
import pathlib
import re
import unittest

from tests._base import StoreCase  # noqa: F401 — puts server/ on sys.path
import expectation as ex

DESK = (pathlib.Path(__file__).resolve().parents[1]
        / "skills/quarterly-accounting/SKILL.md").read_text()


def flat(s):
    return " ".join(s.split())


def section(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


class Classify(unittest.TestCase):
    def setUp(self):
        self.sec = flat(section(DESK, "## What a payment is", "## The operator's words"))

    def test_words_about_a_payment_are_no_reading(self):
        # before the propose_reading section, so it is read first
        self.assertLess(DESK.index("## What a payment is"),
                        DESK.index("## The operator's words about the books"))
        self.assertIn('Words saying what a payment is ("that\'s wage tax", "classify wage tax as '
                      'nice to have") skip `propose_reading`', self.sec)
        self.assertIn("the payment under discussion; ask which only when several fit", self.sec)

    def test_what_a_payment_needs_stays_a_reading(self):
        """r1 (Astra, Terra S1): a document rule is the reply grammar's (`exempt`, `never`),
        never a classification."""
        self.assertIn('What a payment needs ("needs no invoice") stays a reading.', self.sec)
        self.assertIn('"no invoices ever for Adobe"',
                      flat(section(DESK, "## The operator's words", "## Naming")))

    def test_it_asks_for_the_check_of_its_quarter_unasked(self):
        self.assertIn("Then the check ask for its quarter, unasked.", self.sec)
        self.assertNotIn("Run the check now?", DESK)

    def test_the_documents_it_names_are_the_derived_defaults(self):
        """The skill's defaults against expectation.derive, for a payment out with no
        operator rule; r1 (Terra S1): payroll and taxes together conflict, and the skill says
        the check judges it."""
        m = re.search(r"By default a payment out tagged (.+?) needs a statement, (.+?) a "
                      r"payslip, both nice to have \(both together: the check judges\)\.",
                      self.sec)
        self.assertIsNotNone(m)
        stmt = re.split(r", | or ", m.group(1))
        slip = re.split(r", | or ", m.group(2))
        self.assertEqual(stmt, ["taxes", "interest", "fees"])
        self.assertEqual(slip, ["salary", "payroll"])
        for tags, kind in [(stmt, "statement"), (slip, "payslip")]:
            for t in tags:
                e = ex.derive("DBIT", {t})
                self.assertEqual((e.kind, e.tier), (kind, "optional"), t)
        for a in stmt:
            for b in slip:
                e = ex.derive("DBIT", {a, b})
                self.assertTrue(e.conflict and e.kind is None, (a, b))
        # the prod wage-tax chain
        e = ex.derive("DBIT", {"taxes", "recurring", "wage-tax"})
        self.assertEqual((e.kind, e.tier), ("statement", "optional"))

    def test_tagging_is_not_forbidden_by_the_setup_line(self):
        self.assertNotIn("never change anything about bank-feed", flat(DESK))


class Quarter(unittest.TestCase):
    def test_the_open_card_names_the_quarter_being_talked_about(self):
        stands = flat(section(DESK, "**Where a quarter stands**", "**Get a quarter done**"))
        self.assertIn('quarter=<the quarter named, else the one talked about, e.g. '
                      '"2026-Q3">)`; no quarter in play: `show_view(view="open")`.', stands)
        self.assertNotIn("with no quarter named", stands)


if __name__ == "__main__":
    unittest.main()
