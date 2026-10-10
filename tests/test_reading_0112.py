"""0.11.2, PLAY's live check (2026-10-08): five reading problems. 1) one thing, one number:
the legend and the Review receipt never print a number the counts line does not; 2) a
vendor card has [Leave for now]; 3) a vendor card's subject is its missing payments, the
others one summary line, and its receipts name the payments acted on; 4) the document's own
kind, "documents fit", "Reject the suggested document" on a proposal, no switch label
starting with a mark, "waiting on the bank", one payee form per vendor card; 5) a plain pick
legend."""
from tests.test_taps_next import _Tapping
from tests._base import untag
import db


class Reading(_Tapping):
    def setUp(self):
        super().setUp()
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET quarter='2026-Q3' WHERE job_id=?",
                              (self.job_id,))

    def labels(self, dep):
        return [b["label"] for b in dep["buttons"]]

    # 1 ------------------------------------------------------------------------------------
    def test_the_legend_and_the_review_receipt_print_no_other_missing_count(self):
        p = self.pay("Zapier", 1958, "2026-09-01")
        self.propose(p, issuer="Zapier", document_number="ZAP-114", amount_minor=1958)
        self.pay("Twilio", 2000, "2026-08-14")
        self.pay("Twilio", 2100, "2026-08-15")
        end = self.end()
        lines = untag(end["text"]).split("\n")
        self.assertEqual(lines[1], "1 to confirm · 2 missing")
        self.assertNotIn("Review: go through", end["text"])         # #99: no legend
        out = self.tap(end, "Review")
        self.assertEqual(out["receipt"], "Reviewing the 1 to confirm, then the missing invoices.")

    # 2 ------------------------------------------------------------------------------------
    def test_a_vendor_card_can_be_left_for_now(self):
        a = self.pay("Twilio", 2000, "2026-08-14")
        card = self.tap(self.end(), "Review")["next"]
        self.assertIn("Leave for now", self.labels(card))
        rev = self.rev(a)
        out = self.tap(card, "Leave for now")
        self.assertEqual(out["receipt"], "Left for now: Twilio.")
        self.assertEqual(self.rev(a), rev)

    # 3 ------------------------------------------------------------------------------------
    def test_the_vendor_card_lists_its_missing_payments_and_sums_up_the_rest(self):
        import matches
        miss = self.pay("Twilio", 2000, "2026-08-14")
        done = self.pay("Twilio", 2100, "2026-08-15")
        self.machine_entry(done, self.doc(issuer="Twilio", document_number="TW-1",
                                          amount_minor=2100))
        card = self.tap(self.end(), "Review")["next"]
        lines = untag(card["text"]).split("\n")
        self.assertIn("EUR 20.00 · 14 Aug", lines)
        self.assertNotIn("21.00", "\n".join(lines[:-2]))
        self.assertIn("Never for Twilio would also change 1 more payment of this quarter.",
                      lines)
        self.assertFalse(any("other quarters" in ln for ln in lines), lines)   # not twice
        out = self.tap(card, "Leave missing")
        self.assertEqual(out["receipt"], "Left missing (Twilio): EUR 20.00 · 14 Aug.")
        del miss, matches

    def test_one_payee_form_on_a_vendor_card(self):
        self.pay("Belastingdienst", 2000, "2026-08-14")
        self.pay("BELASTINGDIENST", 2100, "2026-08-15")
        card = self.tap(self.end(), "Review")["next"]
        body = untag(card["text"]).split("\n")[1:3]
        self.assertEqual(body, ["EUR 20.00 · 14 Aug", "EUR 21.00 · 15 Aug"])

    # 4 ------------------------------------------------------------------------------------
    def test_a_review_card_carries_no_legend(self):
        p = self.pay("Elevenlabs.io", 1899, "2026-09-01")
        self.propose(p, kind="receipt", document_number="2635-8754-8667",
                     amount_minor=1899)
        card = self.tap(self.end(), "Review")["next"]
        self.assertNotIn("Confirm: this", card["text"])           # #99: the buttons say it
        self.assertIn("Confirm", self.labels(card))

    def test_two_purchases_are_two_documents_not_two_invoices(self):
        p = self.pay("Hanabi", 2271, "2026-07-16")
        rec = self.doc(kind="receipt", issuer="Hanabi", document_number="2570-9321",
                       amount_minor=2271)
        self.propose(p, alternatives=[rec], issuer="Hanabi", document_number="SX8S3VEU-0002",
                     amount_minor=2271)
        self.assertIn("2 documents fit; chose SX8S3VEU\\-0002", self.end()["text"])

    def test_a_proposal_is_rejected_not_unmatched(self):
        import views
        from tests._base import apply_now
        p = self.pay("Zapier", 1958, "2026-09-01")
        self.propose(p, issuer="Zapier", document_number="ZAP-114", amount_minor=1958)
        card = self.tap(self.end(), "Review")["next"]
        out = self.tap(card, "Wrong")
        self.assertTrue(out["receipt"].startswith(
            "Rejected the suggested document for Zapier · EUR 19.58 · 1 Sep"), out["receipt"])
        q = self.pay("Notion", 900, "2026-09-02")
        self.propose(q, issuer="Notion", document_number="NO-1", amount_minor=900)
        rid = self.end()["buttons"][0]["call"]["arguments"]["render_id"]
        views.mark_rendering_delivered(self.conn, rid)
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?", (rid,)).fetchone()[0]
        res = apply_now(self.conn, [("reject", q)],   # #121: ops
                        "\U0001f4ca Alex\n" + views.displayed(text))
        self.assertIn("Reject the suggested document for Notion · EUR 9.00 · 2 Sep.",
                      res["proposal"])

    def test_the_switch_back_label_starts_with_no_mark(self):
        self.pay("Twilio", 2000, "2026-08-14")
        self.pay("Twilio", 2100, "2026-10-14")
        on = self.tap(self.tap(self.end(), "Review")["next"], "Apply to all quarters")["next"]
        self.assertIn("Only this quarter", self.labels(on))

    def test_pending_says_at_the_bank(self):
        import cards, collections
        c = collections.Counter(matched=1, pending=1)
        self.assertEqual(cards._counts_line(c), "1 matched · 1 waiting on the bank")

    # 5 ------------------------------------------------------------------------------------
    def test_a_pick_card_carries_no_legend(self):
        p = self.pay("AWS", 149, "2026-07-02")
        alt = self.doc(issuer="AWS", document_number="EUINNL26-664958", amount_minor=149)
        self.propose(p, alternatives=[alt], issuer="AWS", document_number="EUINNL26-429716",
                     amount_minor=149)
        card = self.tap(self.end(), "Review")["next"]
        self.assertNotIn("A document button", card["text"])      # #99: no legend


class R7Fixes(Reading):
    def test_an_answered_middle_page_does_not_end_the_vendors_walk(self):
        """r7 Astra S1: page 2's payments matched meanwhile; Next page goes on to page 3."""
        pids = [self.pay("Adobe", 100 + i, "2026-08-14") for i in range(60)]
        card = self.tap(self.end(), "Review")["next"]
        pages = self.scope_of(card)["pages"]
        self.assertEqual(len(pages), 3)
        for p in pages[1]:
            self.machine_entry(p, self.doc(issuer="Adobe", document_number=f"A-{p}",
                                           amount_minor=100 + pids.index(p)))
        out = self.tap(card, "Next page")
        self.assertIn("page 3 of 3", out["next"]["text"])

    def test_the_question_stands_above_the_list_confirm_all_answers(self):
        # #99: the question on its own line, its list right under it; no legend
        p = self.pay("Elevenlabs.io", 1899, "2026-09-01")
        self.propose(p, kind="receipt", document_number="REC-1", amount_minor=1899)
        end = self.end()
        lines = end["text"].split("\n")
        i = lines.index("**Confirm these matches?**")
        self.assertEqual(lines[i - 1], "")
        self.assertTrue(lines[i + 1].startswith("1. Elevenlabs.io"))
        self.assertIn("receipt REC\\-1 · EUR 18.99", end["text"])   # #106: a short number
        self.assertNotIn("Confirm all:", end["text"])
        self.assertIn("Confirm all", self.labels(end))


class R8Fixes(Reading):
    def test_two_lines_that_read_the_same_without_the_payee_are_told_apart(self):
        """r8 Astra S2: Belastingdienst / BELASTINGDIENST, same amount and day."""
        self.pay("Belastingdienst", 2000, "2026-08-14")
        self.pay("BELASTINGDIENST", 2000, "2026-08-14")
        card = self.tap(self.end(), "Review")["next"]
        body = untag(card["text"]).split("\n")[1:3]
        self.assertEqual(len(set(body)), 2, body)
        self.assertTrue(all(b.startswith("EUR 20.00 · 14 Aug · ref ") for b in body), body)

    def test_a_full_vendor_receipt_names_every_payment(self):
        """r8 Astra S2: the receipt names the vendor once and every payment whole."""
        name = "*_" * 30
        for i in range(25):
            self.pay(name, 100000000 + i, "2026-08-14")
        card = self.tap(self.end(), "Review")["next"]
        out = self.tap(card, "Leave missing")
        self.assertTrue(out["receipt"].startswith("Left missing ("), out["receipt"][:80])
        for i in range(25):
            self.assertIn(f"EUR 1,000,000.{i:02d}", out["receipt"])
        self.assertEqual(out["receipt"].count(name.replace("*", "\\*").replace("_", "\\_")), 1)
