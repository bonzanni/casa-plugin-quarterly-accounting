"""0.11.4: #63 (a credit-note vendor card's legend names the button it explains). #62's
dotted-name tests pinned the phrase grammar's clause splitting, gone in #121."""
from tests._base import StoreCase


class CreditNoteButton(StoreCase):
    def test_a_credit_note_vendor_card_says_document_on_its_button(self):
        # #99: no legend; the button's own words say it
        import cards
        for noun, label in (("invoice", "No invoice needed for these"),
                            ("credit note", "No document needed for these")):
            scope = {"quarter": "2026-Q2", "vendor": "AMAZON", "page": 1, "pages": [[1]],
                     "noun": noun, "missing": True}
            self.assertIn(label, [b[0] for b in cards._buttons("r0", "vendor-page", scope)])
