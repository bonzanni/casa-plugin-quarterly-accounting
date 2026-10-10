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
        self.assertIn('Words saying what a payment is or what its document needs ("that\'s '
                      'wage tax", "wage tax is nice to have") skip `propose_reading`', self.sec)
        self.assertIn("the payment under discussion", self.sec)
        self.assertIn("ask which only when several fit", self.sec)

    def test_it_asks_for_the_check_of_its_quarter_unasked(self):
        self.assertIn("Then the check ask for its quarter, unasked.", self.sec)
        self.assertNotIn("Run the check now?", DESK)

    def test_the_document_it_names_is_the_one_derived(self):
        """The skill's mapping, sentence by sentence, against expectation.derive (a debit
        classified by those tags alone, no override)."""
        m = re.search(r"Its tags decide its document: (.+?)\. Then", self.sec)
        self.assertIsNotNone(m)
        words = {"a statement": "statement", "a payslip": "payslip", "an invoice": "invoice"}
        tiers = {"nice to have": "optional", "required": "required"}
        parts = m.group(1).split("; ")
        self.assertEqual(len(parts), 3)
        for part in parts:
            if part.startswith("else "):
                tags, doc = [("software",), ("food", "dining")], part[len("else "):]
            else:
                lhs, doc = part.split(" → ")
                tags = [(t,) for t in lhs.split(" or ")]
            what, tier = doc.split(", ")
            for t in tags:
                e = ex.derive("DBIT", set(t))
                self.assertEqual((e.kind, e.tier), (words[what], tiers[tier]), (t, part))

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
