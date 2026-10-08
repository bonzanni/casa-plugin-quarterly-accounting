"""#60: the vendor card's legend names [Never for X] in the card's own dialect — a vendor
whose bank text carries a formatting mark ("PAYPAL *ACME", "ACME_NL") is shown as the button
reads, never as formatting (Casa's renderer turned "*Acme*" into italics)."""
from tests._base import StoreCase


class NeverLegendIsEscaped(StoreCase):
    def test_a_marked_vendor_name_reads_as_its_button_in_the_legend(self):
        import cards, views
        for name in ("*Acme* BV", "PAYPAL *ACME", "ACME_NL_BV_", "[Acme](x) `b` ~c~ |d|"):
            scope = {"quarter": "2026-Q3", "vendor": name, "page": 1, "pages": [[1]],
                     "missing": True}
            label = next(b[0] for b in cards._buttons("r0", "vendor-page", scope)
                         if b[2].get("action") == "never")
            entry = next(p for p in cards.legend("vendor-page", scope).split(" · ")
                         if "never sends one" in p)
            # every dialect mark in the entry is escaped: what the operator reads is the
            # button's own words, then the vendor field
            self.assertTrue(entry.startswith(views.esc(label) + ": "), (name, entry))
            self.assertEqual(views.unesc(entry).split(": ")[0], label)
