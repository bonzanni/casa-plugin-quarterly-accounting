"""#60: a vendor whose bank text carries a formatting mark ("PAYPAL *ACME", "ACME_NL") is
shown as the button reads, never as formatting (Casa's renderer turned "*Acme*" into
italics). #99: the legend is gone (the buttons say it); the vendor card's bold title carries
the name, escaped inside the bold."""
from tests._base import StoreCase


class MarkedVendorNameReadsLiterally(StoreCase):
    def test_a_marked_vendor_name_reads_literally_in_the_bold_title(self):
        import cards, views
        for name in ("*Acme* BV", "PAYPAL *ACME", "ACME_NL_BV_", "[Acme](x) `b` ~c~ |d|"):
            title = views.title(f"Card 1 of 1 · missing invoices · {views.field(name)}")
            self.assertEqual(views.bold_spans(title),
                             [f"Card 1 of 1 · missing invoices · {name}"], name)
            self.assertEqual(views.displayed(title),
                             f"Card 1 of 1 · missing invoices · {name}", name)
        self.assertFalse(hasattr(cards, "legend"))
