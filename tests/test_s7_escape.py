# tests/test_s7_escape.py
"""S7 §12/§7.6: every dynamic field passes ONE escape; control characters and in-field
newlines become spaces; whole bodies are made deposit-safe; every rendering fits a
proposal page (BODY_LIMIT) after escaping."""
import re
import unicodedata
from tests._base import StoreCase

HOSTILE = ["*Acme*", "_x_", "`y`", "[a](b)", "| t |", "1. Ltd", "a<b>c", "www.evil.example",
           "x://y", "ACME\x01Corp", "two\nlines", "Z" * 2100, "café ☕ 𝔘"]


class Escape(StoreCase):
    def test_esc_neutralises_every_marker_and_unesc_restores_the_text(self):
        import views
        for raw in HOSTILE:
            e = views.esc(raw)
            self.assertNotIn("\n", e)
            self.assertFalse(any(unicodedata.category(c) == "Cc" for c in e), raw)
            flat = raw.replace("\n", " ").replace("\x01", " ")
            self.assertEqual(views.unesc(e), flat, raw)
        self.assertEqual(views.esc("*Acme*"), "\\*Acme\\*")
        self.assertEqual(views.esc("1. Ltd"), "1\\. Ltd")
        self.assertEqual(views.esc("v1.2"), "v1.2")           # a dot inside is no list marker
        self.assertEqual(views.esc("a\x01b\nc", plain=True), "a b c")

    def test_field_clips_then_escapes_and_field_raw_does_not_escape(self):
        import views
        self.assertEqual(views.field_raw("*A*"), "*A*")
        self.assertEqual(views.field("*A*"), "\\*A\\*")
        long = views.field("_" * 500)
        self.assertLessEqual(views.utf16_len(long), 2 * views.FIELD_MAX)

    def test_deposit_safe_and_caption_safe(self):
        import views
        self.assertEqual(views.deposit_safe("a\x01b\n\tc\x7f"), "a b\n\tc ")
        self.assertEqual(views.caption_safe("a\u2028b\tc"), "a b c")

    def test_every_view_fits_a_proposal_page_with_hostile_payees(self):
        import views
        self.seed_payments([{"counterparty": h, "amount_minor": 1000 + i}
                            for i, h in enumerate(HOSTILE * 8)])
        for view in ("status", "missing", "check", "rest", "older", "all", "quarter"):
            out = views.build_review(self.conn, view=view)
            self.assertLessEqual(views.utf16_len(out["text"]), views.BODY_LIMIT, view)
            self.assertLessEqual(len(out["text"]), 4000, view)

    def test_templates_carry_no_marker(self):
        """Every fixed template, composed with empty fields, is its own display text:
        nothing in it is a dialect marker (the Casa gate renders them for real)."""
        import alerts, asks, job, views
        for t in (alerts.STOPPED, job.RUN_FINISHED, *asks.LINES.values(),
                  views.MORE_LINE, views.FIT_CLOSING, *alerts.PACKAGE.values()):
            self.assertEqual(views.unesc(t), t, t)
            # every dialect marker is escaped, or absent (unesc alone passes "*bold*")
            self.assertIsNone(re.search(r"(?<!\\)[*_`\[|~]", t), t)
            self.assertIsNone(re.match(r"\s*(#|\d+\.)", t), t)

    def test_composers_that_bypassed_field_now_escape(self):
        import delivery, views
        self.assertEqual(delivery.offer_lines("a*b_c.zip")[0],
                         "a\\*b\\_c.zip may not have arrived — I can send it again.")
        self.assertEqual(delivery.offer_lines("a*b.zip", "failed")[0],
                         "a\\*b.zip didn't go out — I can send it again.")
        # a scope["names"] value is stored as the operator's words, unescaped
        self.assertEqual(views.field_raw("*Acme*"), "*Acme*")
