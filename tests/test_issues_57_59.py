"""0.11.3: issue #57 (an [Invoice links] button on the quarter's card: the same card, in
place, with where to download each missing invoice), issue #59 (cards read right: a
readable vendor name, a labelled link, a credit note not called an invoice, an explained
other-quarters line) and Casa #1339 (a tap that only changes the card's own view asks Casa
to edit the tapped card in place)."""
from tests.test_taps_next import _Tapping
from tests._base import untag
import db


class _Q3(_Tapping):
    def setUp(self):
        super().setUp()
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET quarter='2026-Q3' WHERE job_id=?",
                              (self.job_id,))

    def labels(self, dep):
        return [b["label"] for b in dep["buttons"]]

    def card(self):
        return self.tap(self.end(), "Review")["next"]

    def matched_to(self, pid, issuer, **doc):
        self.machine_entry(pid, self.doc(issuer=issuer, **doc))

    def kb(self, name, patterns=(), **over):
        import kb
        kb.upsert_counterparty(self.conn, name, patterns=list(patterns), **over)


class ReadableName(_Q3):
    RAW = "Google*workspace Lesin"

    def test_the_issuer_of_a_matched_document_names_the_vendor(self):
        done = self.pay(self.RAW, 1200, "2026-07-03")
        self.matched_to(done, "Google Cloud EMEA", amount_minor=1200,
                        document_date="2026-07-03")
        self.pay(self.RAW, 1300, "2026-08-03")              # missing
        dep = self.card()
        head = untag(dep["text"]).split("\n")[0]
        self.assertEqual(head, "Card 1 of 1 · missing invoices · Google Cloud EMEA")
        self.assertIn("Never for Google Cloud EMEA", self.labels(dep))
        self.assertNotIn("workspace", dep["text"])
        # the rule still lands on the bank's payee (display only)
        out = self.tap(dep, "Never for Google Cloud EMEA")
        self.assertIn("Google Cloud EMEA never needs an invoice", out["receipt"])
        self.assertEqual(self.conn.execute(
            "SELECT exp_kind FROM counterparties WHERE name=?", (self.RAW,)).fetchone()[0],
            "none")

    def test_a_name_given_in_the_kb_wins(self):
        self.kb("Google Workspace", patterns=[self.RAW])
        done = self.pay(self.RAW, 1200, "2026-07-03")
        self.matched_to(done, "Google Cloud EMEA", amount_minor=1200,
                        document_date="2026-07-03")
        self.pay(self.RAW, 1300, "2026-08-03")
        self.assertIn("· Google Workspace", untag(self.card()["text"]).split("\n")[0])

    def test_the_bank_text_when_nothing_better_is_known(self):
        self.pay(self.RAW, 1300, "2026-08-03")
        self.assertIn(f"· {self.RAW}", untag(self.card()["text"]).split("\n")[0]
                      .replace("\\", ""))

    def test_a_kb_entry_named_with_the_bank_text_is_no_given_name(self):
        self.kb(self.RAW, patterns=[self.RAW])
        done = self.pay(self.RAW, 1200, "2026-07-03")
        self.matched_to(done, "Google Cloud EMEA", amount_minor=1200,
                        document_date="2026-07-03")
        self.pay(self.RAW, 1300, "2026-08-03")
        self.assertIn("· Google Cloud EMEA", untag(self.card()["text"]).split("\n")[0])

    def test_a_payments_own_pairing_never_names_it(self):
        import work
        p = self.pay(self.RAW, 1200, "2026-07-03")
        self.matched_to(p, "Somebody Else Ltd", amount_minor=1200, document_date="2026-07-03")
        with db.tx(self.conn):
            self.assertEqual(work.describe(self.conn, p)["readable"], self.RAW)
        q = self.pay(self.RAW, 1300, "2026-08-03")
        with db.tx(self.conn):
            self.assertEqual(work.describe(self.conn, q)["readable"], "Somebody Else Ltd")

    def test_proposal_lines_and_headlines_read_the_same_name(self):
        done = self.pay(self.RAW, 1200, "2026-07-03")
        self.matched_to(done, "Google Cloud EMEA", amount_minor=1200,
                        document_date="2026-07-03")
        p = self.pay(self.RAW, 1300, "2026-08-03")
        self.propose(p, amount_minor=1300, issuer="Google Cloud EMEA")
        end = untag(self.end()["text"])
        self.assertIn("1. Google Cloud EMEA · 3 Aug · EUR 13.00", end)
        card = self.card()
        self.assertEqual(untag(card["text"]).split("\n")[1],
                         "Google Cloud EMEA · EUR 13.00 · 3 Aug")


class LinkAndNoun(_Q3):
    def test_the_link_line_says_what_it_is(self):
        self.kb("Twilio", document_link="https://console.twilio.com/billing")
        self.pay("Twilio", 2000, "2026-08-14")
        lines = untag(self.card()["text"]).replace("\\", "").split("\n")
        self.assertIn("Where to download: https://console.twilio.com/billing", lines)

    def test_a_missing_credit_note_is_not_called_an_invoice(self):
        p = self.pay("Belastingdienst", -14300, "2026-08-03")
        self.classify(p, {"refund"})
        self.settle(p)
        dep = self.card()
        lines = untag(dep["text"]).split("\n")
        self.assertEqual(lines[0], "Card 1 of 1 · missing credit notes · Belastingdienst")
        self.assertIn("No document needed for these", self.labels(dep))
        # #63: the legend names the button that is there
        self.assertIn("No document needed: these need none", lines[-1])
        out = self.tap(dep, "No document needed for these")
        self.assertTrue(out["receipt"].startswith("No credit note needed (Belastingdienst): "),
                        out["receipt"])

    def test_invoices_keep_their_words(self):
        self.pay("Twilio", 2000, "2026-08-14")
        dep = self.card()
        self.assertIn("missing invoices", dep["text"])
        self.assertIn("No invoice needed for these", self.labels(dep))
        self.assertIn("No invoice needed: these need none", dep["text"])

    def test_the_other_quarters_line_says_why_it_is_there(self):
        self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Twilio", 2100, "2026-10-14")
        self.machine_entry(q4, self.doc(issuer="Twilio", document_number="TW-9",
                                        amount_minor=2100))
        self.assertIn("Also in other quarters: 1 payment (Q4) · Never for Twilio would also "
                      "change it", untag(self.card()["text"]))


class InvoiceLinks(_Q3):
    def test_the_button_shows_the_links_on_the_same_card_in_place(self):
        self.kb("OpenAI", patterns=["Openai *chatgpt Subscr"],
                document_link="https://platform.openai.com/settings/billing")
        self.pay("Openai *chatgpt Subscr", 2000, "2026-08-14")
        self.pay("Runpod", 3000, "2026-08-20")
        end = self.end()
        self.assertEqual(self.labels(end), ["Review", "Invoice links", "Get package"])
        self.assertIn("Invoice links: where to download each missing invoice", end["text"])
        out = self.tap(end, "Invoice links")
        self.assertIs(out["in_place"], True)
        self.assertTrue(out["receipt"])
        lines = untag(out["next"]["text"]).replace("\\", "").split("\n")
        at = lines.index("Where to download the missing invoices:")
        self.assertEqual(lines[at + 1:at + 3],
                         ["OpenAI: https://platform.openai.com/settings/billing",
                          "Runpod: no link known"])
        self.assertEqual(self.labels(out["next"]), ["Review", "Get package"])
        # the card's other buttons still work
        self.assertIn("Card 1 of 2", self.tap(out["next"], "Review")["next"]["text"])

    def test_no_button_without_missing_invoices(self):
        p = self.pay()
        self.propose(p)
        self.assertNotIn("Invoice links", self.labels(self.end()))

    def test_answered_meanwhile_says_so(self):
        import work
        p = self.pay("Runpod", 3000, "2026-08-20")
        end = self.end()
        self.granted(work.leave_missing_in_tx, [p])
        out = self.tap(end, "Invoice links")
        self.assertIn("Nothing is missing in Q3 any more.", untag(out["next"]["text"]))

    def test_many_vendors_fit_and_the_rest_is_counted(self):
        for i in range(60):
            self.kb("Vendor %02d" % i, document_link="https://example.com/" + "x" * 150 + str(i))
            self.pay("Vendor %02d" % i, 1000 + i, "2026-08-14")
        out = self.tap(self.end(), "Invoice links")
        text = untag(out["next"]["text"])
        self.assertRegex(text, r"… and \d+ more vendors")


class InPlace(_Q3):
    def test_a_page_turn_and_the_switch_are_in_place(self):
        for i in range(30):
            self.pay("Adobe", 100 + i, "2026-08-%02d" % (i % 28 + 1))
        self.pay("Adobe", 999, "2026-10-14")
        page1 = self.card()
        out = self.tap(page1, "Next page")
        self.assertIs(out["in_place"], True)
        on = self.tap(out["next"], "Apply to all quarters")
        self.assertIs(on["in_place"], True)

    def test_answers_that_act_or_move_on_keep_their_receipt(self):
        p = self.pay()
        self.propose(p)
        self.pay("Twilio")
        card = self.card()
        self.assertNotIn("in_place", self.tap(card, "Leave for now"))
        card = self.card()
        self.assertNotIn("in_place", self.tap(card, "Confirm"))
        self.assertNotIn("in_place", self.tap(self.end(), "Review"))
