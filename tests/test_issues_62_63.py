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

    def test_a_period_between_sentences_still_splits(self):
        import reply
        self.assertEqual(reply._clauses("Zapier is fine.Adobe is wrong"),
                         ["zapier is fine", "adobe is wrong"])
        # r1 (Astra, Terra): a capitalised word after a period is a new sentence
        for text in ("Zapier is fine.De Bijenkorf is wrong", "Zapier is fine.AI is wrong",
                     "Zapier is fine.Io is wrong"):
            self.assertEqual(len(reply._clauses(text)), 2, text)
        self.assertEqual(reply._clauses("no invoices ever for TWILIO.COM"),
                         ["no invoices ever for twilio.com"])
        self.assertEqual(reply._clauses("fsprg.nl via Checkout.com is wrong. Elevenlabs.io too"),
                         ["fsprg.nl via checkout.com is wrong", "elevenlabs.io too"])


class CreditNoteLegend(StoreCase):
    def test_the_exempt_legend_entry_starts_with_its_buttons_words(self):
        import cards
        for noun, label in (("invoice", "No invoice needed for these"),
                            ("credit note", "No document needed for these")):
            scope = {"quarter": "2026-Q2", "vendor": "AMAZON", "page": 1, "pages": [[1]],
                     "noun": noun, "missing": True}
            legend = cards.legend("vendor-page", scope)
            labels = [b[0] for b in cards._buttons("r0", "vendor-page", scope)]
            self.assertIn(label, labels)
            first = legend.split(" · ")[0]
            self.assertTrue(label.startswith(first.split(":")[0]), (first, label))
