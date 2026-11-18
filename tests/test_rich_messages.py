# tests/test_rich_messages.py
"""Casa (v0.376) posts every text of this plugin as a Telegram rich message: GitHub-flavoured
Markdown, with real tables. These pins hold the plugin to what Casa takes in one message, to
the tables its lists are drawn as, and to what a reply quoting such a post reads back."""
import re
import unittest

from tests._base import StoreCase
import db  # noqa: E402
import tools  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402


class CasaLimits(unittest.TestCase):
    """The per-message budget comes from Casa's result broker, not from the old 4,096-unit
    page: MAX_MESSAGE_CHARS 12,000 for a body, and one rich message (32,000 prepared bytes
    less the settle reserve) for a card or a page under its label line."""

    def test_the_budget_is_casa_s(self):
        self.assertEqual(views.CASA_MESSAGE_CHARS, 12_000)
        self.assertEqual(views.CASA_CARD_BYTES, 32_000 - (2 + 3 + 1 + 5 * 32 + 3))
        self.assertLess(views.BODY_LIMIT, views.CASA_MESSAGE_CHARS)
        self.assertGreater(views.BODY_LIMIT, 11_000)
        self.assertFalse(hasattr(views, "TELEGRAM_LIMIT"))

    def test_a_long_plain_body_fits_one_message(self):
        body = "\n".join(["Adobe · EUR 12.00 · 14 Sep"] * 400)       # ~10,800 characters
        self.assertGreater(len(body), 4096)
        self.assertTrue(views.fits_body(body))
        self.assertTrue(views.fits_proposal(body))
        self.assertFalse(views.fits_body("x" * (views.BODY_LIMIT + 1)))

    def test_the_prepared_bytes_bound_a_body_too(self):
        # Casa sends "_" as "&#95;" (five bytes): 7,000 of them are 35,000 prepared bytes,
        # more than one rich message holds, though only 7,000 characters
        self.assertGreaterEqual(views.prepared_bytes("_" * 7000), 35_000)
        self.assertFalse(views.fits_body("_" * 7000))
        self.assertTrue(views.fits_body("_" * 6000))

    def test_the_fit_fills_to_the_new_budget(self):
        lines = ["%04d Adobe · EUR 12.00 · 14 Sep" % i for i in range(1000)]
        out, whole = views.fit_lines(lines, "The rest did not fit.")
        text = "\n".join(out)
        self.assertTrue(views.fits_body(text))
        self.assertGreater(len(text), views.BODY_LIMIT - 100)
        self.assertEqual(out[-1], "The rest did not fit.")

    def test_a_closing_line_never_joins_a_table(self):
        lines = ["**Missing**", "", "| Payment | Amount |", "| --- | ---: |"] + \
            ["| Adobe %04d | EUR 12.00 |" % i for i in range(2000)]
        out, _ = views.fit_lines(lines, "The rest did not fit.")
        self.assertEqual(out[-2:], ["", "The rest did not fit."])
        self.assertTrue(views.fits_body("\n".join(out)))

    def test_the_tool_boundary_takes_what_casa_takes(self):
        ok = {"text": "x" * 9000}
        self.assertIs(tools._deliverable("build_review", ok), ok)
        with self.assertRaises(tools.Undeliverable):
            tools._deliverable("build_review", {"text": "x" * (views.BODY_LIMIT + 1)})


class Escaping(unittest.TestCase):
    def test_markers_that_still_format_stay_escaped(self):
        for ch in "*[|#`":
            self.assertEqual(views.esc(f"a{ch}b"), f"a\\{ch}b")
        # == is Telegram's highlight; escaped like the rest, and read back as itself
        self.assertEqual(views.esc("a==b"), "a\\=\\=b")
        self.assertEqual(views.unesc(views.esc("a==b")), "a==b")


class ReadBack(unittest.TestCase):
    """What a reply quoting a rich post carries (Casa: the post's blocks flattened — a table
    row's cells joined with " | ", a list item without its number) is what `displayed`
    predicts, so the quote binds."""

    def test_a_table_reads_back_as_its_cells(self):
        text = ("**Missing · Q3 2026** · 15 Sep 12:00:00\n\n"
                "| Payment | Amount | Date | Note |\n| --- | ---: | --- | --- |\n"
                "| A\\|B | EUR 1.00 | 1 Jul | x |\n| C | EUR 2.00 | 2 Jul |   |\n\nAfter.")
        shown = views.displayed(text)
        self.assertIn("Payment | Amount | Date | Note", shown)
        self.assertIn("A|B | EUR 1.00 | 1 Jul | x", shown)
        self.assertIn("C | EUR 2.00 | 2 Jul | ", shown)
        self.assertNotIn("---", shown)
        self.assertNotIn("**", shown)

    def test_a_numbered_line_reads_back_without_its_number(self):
        shown = views.displayed("**Confirm these matches?**\n1. Adobe · 14 Sep\n2. Zapier")
        self.assertEqual(shown.split("\n")[1:], ["Adobe · 14 Sep", "Zapier"])
        self.assertEqual(views.displayed("1\\. Ltd"), "1. Ltd")      # an escaped one is text

    def test_casa_s_reply_clip_is_undone(self):
        # Casa quotes at most REPLY_QUOTE_CHARS (600) characters, the last three "[…]"
        quote = ("x" * 700)[:597] + "[…]"
        self.assertEqual(views._qnorm(quote), "x" * 597)
        self.assertEqual(views._qnorm("ends with […]"), views._bnorm("ends with […]"))

    def test_a_quote_compares_without_whitespace(self):
        # however Telegram joins the lines it gives back, the words are the same
        self.assertEqual(views._bnorm("a b\nc"), views._bnorm("ab c"))


class Quotes(StoreCase):
    """Casa's label heads a labelled post; Telegram may give the label back on its own line
    or run into the title (a single line break reads as a space). Either way the quote binds."""

    def setUp(self):
        super().setUp()
        self.seed_payments([{"counterparty": "Adobe"}, {"counterparty": "Zapier"}])
        out = views.build_review(self.conn, view="missing", quarter="2026-Q3")
        views.mark_rendering_delivered(self.conn, out["render_id"])
        self.rid, self.shown = out["render_id"], views.displayed(out["text"])

    def test_the_label_on_its_own_line(self):
        quote = "\U0001f4ca Finance\n" + self.shown
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], self.rid)

    def test_the_label_run_into_the_title(self):
        quote = "\U0001f4ca Finance " + self.shown.replace("\n", " ")
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], self.rid)

    def test_no_label_in_the_agent_s_own_chat(self):
        self.assertEqual(views.bound_rendering(self.conn, self.shown)["render_id"], self.rid)

    def test_a_quote_clipped_by_casa(self):
        long = "\U0001f4ca Finance\n" + self.shown + "\n" + "x" * 700
        quote = long[:597] + "[\u2026]"
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], self.rid)

    def test_another_text_does_not_bind(self):
        with self.assertRaises(views.QuoteRefusal):
            views.bound_rendering(self.conn, "\U0001f4ca Finance\nSomething else entirely")


class PagesOfACard(StoreCase):
    """A card and the pages it brings are one deposit, which Casa takes up to 64 KiB
    (result_broker.MAX_VALUE_BYTES): pages past that wait for the card's "more"."""

    def test_the_pages_stay_within_the_deposit(self):
        from unittest import mock
        import posting
        from tests.fakebroker import FakeBroker
        import matches
        self.seed_payments([{"counterparty": f"Vendor {i:03d} Holdings International BV",
                             "amount_minor": 1000 + i} for i in range(700)])
        # one match to confirm: the list carries a button, so it is a card with pages
        p = self.conn.execute("SELECT pid FROM projections ORDER BY pid DESC").fetchone()[0]
        matches.propose_match(self.conn, pid=p, doc_id=self.doc(amount_minor=1699),
                              expected_revision=self.rev(p), token=self.pass_(),
                              document_date="2026-09-14")
        with mock.patch.object(views, "PAGES_VALUE_BYTES", 20_000), FakeBroker() as b:
            out = posting.show_view(self.conn, view="missing", quarter="2026-Q3")
        prop = b.proposal()
        self.assertEqual(len(prop["pages"]), 1)            # a second page would pass 20,000
        self.assertIn(views.SAY_MORE, prop["text"])         # the rest is the card's "more"
        self.assertIsNotNone(out["next"])
        with FakeBroker() as b:
            posting.show_view(self.conn, view="missing", quarter="2026-Q3")
        value = b.deposits[-1]["value"].encode("utf-8")
        self.assertLessEqual(len(value), views.CASA_VALUE_BYTES)


class Tables(StoreCase):
    def setUp(self):
        super().setUp()
        self.pids = self.seed_payments([{"counterparty": n, "amount_minor": 1000 + i}
                                        for i, n in enumerate(("Adobe", "Zap|ier", "Notion"))])

    def test_the_missing_list_is_a_table_with_right_aligned_amounts(self):
        out = views.build_review(self.conn, view="missing", quarter="2026-Q3")
        lines = out["text"].split("\n")
        # no payment here has a note, so the table has no Note column (kept narrow)
        head = lines.index("| Payment | Amount | Date |")
        self.assertEqual(lines[head - 1], "")
        self.assertEqual(lines[head + 1], "| --- | ---: | --- |")
        rows = [ln for ln in lines if ln.startswith("| ") and "EUR" in ln]
        self.assertEqual(len(rows), 3)
        self.assertIn("| Zap\\|ier | EUR 10.01 | 14 Sep |", out["text"])
        # a line after the table is a paragraph of its own, never a row
        end = head + 2 + len(rows)
        self.assertTrue(end == len(lines) or lines[end] == "", lines[end:end + 2])
        self.assertEqual(out["printed"], 3)                   # every row binds its payment

    def test_a_quote_of_the_table_binds_its_rendering(self):
        out = views.build_review(self.conn, view="missing", quarter="2026-Q3")
        views.mark_rendering_delivered(self.conn, out["render_id"])
        flat = []           # Telegram's read-back: blocks one per line, cells joined " | "
        for ln in views.deposit_safe(out["text"]).split("\n"):
            if re.fullmatch(r"\|( *:?-+:? *\|)+", ln):
                continue
            if ln.startswith("|"):
                ln = " | ".join(c.strip() for c in re.split(r"(?<!\\)\|", ln.strip())[1:-1])
            flat.append(views.unesc(ln.replace("**", "")))
        quote = "\U0001f4ca Finance\n" + "\n".join(x for x in flat if x)
        self.assertEqual(views.bound_rendering(self.conn, quote)["render_id"], out["render_id"])

    def test_the_status_sheet_draws_its_missing_section_as_a_table(self):
        text = views.build_review(self.conn, view="status", quarter="2026-Q3")["text"]
        self.assertIn("**Missing**\n\n| Payment | Amount | Date |", text)

    def test_a_note_gets_its_own_column(self):
        import kb
        kb.upsert_counterparty(self.conn, "Adobe", source="portal",
                               document_link="https://adobe.example/invoices")
        text = views.build_review(self.conn, view="missing", quarter="2026-Q3")["text"]
        self.assertIn("| Payment | Amount | Date | Note |\n| --- | ---: | --- | --- |", text)
        self.assertIn("| Adobe | EUR 10.00 | 14 Sep | Where to download: "
                      "https://adobe.example/invoices |", text)
        self.assertIn("| Notion | EUR 10.02 | 14 Sep |   |", text)


if __name__ == "__main__":
    unittest.main()
