"""0.11.4: #62 (a vendor named like a domain, "Twilio.com", stays whole in a reply) and #63
(a credit-note vendor card's legend names the button it explains)."""
from tests._base import StoreCase
from tests.fakebroker import FakeBroker


class DomainNames(StoreCase):
    def setUp(self):
        super().setUp()
        import datetime as dt
        cm = self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc))
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def propose(self, text):
        import posting
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, text, None)
        return out, (b.proposal() if b.deposits else None)

    def test_a_domain_name_stays_whole_in_a_reply(self):
        self.sheet_fixture(payee="Twilio.com", guesses=1)
        out, prop = self.propose("no invoices ever for Twilio.com")
        self.assertIsNotNone(prop, out)

    def test_a_known_dotted_name_stays_whole_in_any_case(self):
        import reply
        keep = ["twilio.com", "fsprg.nl via checkout.com", "elevenlabs.io"]
        self.assertEqual(reply._clauses("no invoices ever for TWILIO.COM", keep),
                         ["no invoices ever for twilio.com"])
        self.assertEqual(reply._clauses("fsprg.nl via Checkout.com is wrong. Elevenlabs.io too",
                                        keep),
                         ["fsprg.nl via checkout.com is wrong", "elevenlabs.io too"])

    def test_a_period_between_sentences_splits_as_in_0_11_3(self):
        """r1/r2 (Astra, Terra): no rule guesses a domain from the text."""
        import reply
        keep = ["twilio.com"]
        for text in ("Zapier is fine.De Bijenkorf is wrong", "Zapier is fine.AI is wrong",
                     "Zapier is OK.AI is wrong", "Zapier is fine.de Bijenkorf is wrong",
                     "Zapier is BAD.AI is wrong", "Zapier is fine.Com is wrong"):
            self.assertEqual(len(reply._clauses(text, keep)), 2, text)
        # an unknown dotted word is split exactly as 0.11.3 split it
        self.assertEqual(reply._clauses("no invoices ever for Twilio.com"),
                         ["no invoices ever for twilio", "com"])

    def test_the_store_supplies_the_kept_names(self):
        import reply
        self.sheet_fixture(payee="Twilio.com", guesses=1)
        self.assertIn("twilio.com", reply._dotted_names(self.conn, reply._open_items(self.conn)))


class CreditNoteButton(StoreCase):
    def test_a_credit_note_vendor_card_says_document_on_its_button(self):
        # #99: no legend; the button's own words say it
        import cards
        for noun, label in (("invoice", "No invoice needed for these"),
                            ("credit note", "No document needed for these")):
            scope = {"quarter": "2026-Q2", "vendor": "AMAZON", "page": 1, "pages": [[1]],
                     "noun": noun, "missing": True}
            self.assertIn(label, [b[0] for b in cards._buttons("r0", "vendor-page", scope)])
