"""0.11.2 §B (operator rulings 2026-10-08): a quarter's vendor card lists only that quarter's
payments; the vendor's payments in other quarters are a frozen, plainly stated count; one
switch, [Apply to all quarters], makes the card's next answer cover the other quarters'
missing ones too (a tap like [Next page]: a short receipt and the same card switched on); the
answers refuse when a counted payment moved; [Never] binds the shown pages plus the stated
others. The Review legend counts the quarter's own missing payments."""
from tests.test_taps_next import _Tapping
from tests._base import untag
import db


class VendorCardQuarter(_Tapping):
    def setUp(self):
        super().setUp()
        with db.tx(self.conn):
            self.conn.execute("UPDATE runs SET quarter='2026-Q3' WHERE job_id=?",
                              (self.job_id,))

    def card(self, label="Review"):
        return self.tap(self.end(), label)["next"]

    def labels(self, dep):
        return [b["label"] for b in dep["buttons"]]

    def state(self, pid):
        return self.conn.execute("SELECT search_state FROM projections WHERE pid=?",
                                 (pid,)).fetchone()[0]

    def test_the_card_lists_only_its_quarter_and_counts_the_others(self):
        q3 = self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Twilio", 2100, "2026-10-14")
        dep = self.card()
        lines = untag(dep["text"]).split("\n")
        self.assertIn("EUR 20.00 · 14 Aug", lines)          # PLAY 0.11.2: no payee
        self.assertNotIn("21.00", dep["text"])
        self.assertIn("Also missing in other quarters: 1 (Q4)", lines)
        self.assertEqual(self.labels(dep), ["No invoice needed for these", "Never for Twilio",
                                            "Leave missing", "Apply to all quarters",
                                            "Leave for now"])
        self.assertTrue(lines[-1].endswith("Apply to all quarters: your next answer here also "
                                           "covers the 1 in other quarters · Leave for now: "
                                           "decide later"), lines[-1])
        self.assertEqual(self.scope_of(dep)["others_missing"], [q4])
        self.assertNotIn(q4, self.scope_of(dep)["missing"])
        del q3

    def test_no_switch_without_other_quarters(self):
        self.pay("Twilio", 2000, "2026-08-14")
        dep = self.card()
        self.assertNotIn("Apply to all quarters", self.labels(dep))
        self.assertNotIn("other quarters", dep["text"])

    def test_the_switch_answers_one_line_and_the_same_card_switched_on(self):
        self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Twilio", 2100, "2026-10-14")
        out = self.tap(self.card(), "Apply to all quarters")
        self.assertEqual(out["receipt"], "All quarters on.")
        on = out["next"]
        self.assertIn("Also missing in other quarters: 1 (Q4) · answers will cover them",
                      untag(on["text"]))
        self.assertIn("Only this quarter", self.labels(on))
        self.assertEqual(self.scope_of(on)["others_missing"], [q4])
        back = self.tap(on, "Only this quarter")
        self.assertEqual(back["receipt"], "This quarter only.")
        self.assertIn("Apply to all quarters", self.labels(back["next"]))

    def test_leave_missing_switched_on_covers_the_other_quarters(self):
        q3 = self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Twilio", 2100, "2026-10-14")
        on = self.tap(self.card(), "Apply to all quarters")["next"]
        out = self.tap(on, "Leave missing")
        self.assertEqual((self.state(q3), self.state(q4)), ("accepted-missing",) * 2)
        self.assertEqual(out["receipt"], "Left missing (Twilio): EUR 20.00 · 14 Aug; and 1 "
                                         "payment in other quarters.")

    def test_leave_missing_switched_off_covers_only_the_quarter(self):
        q3 = self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Twilio", 2100, "2026-10-14")
        self.tap(self.card(), "Leave missing")
        self.assertEqual(self.state(q3), "accepted-missing")
        self.assertNotEqual(self.state(q4), "accepted-missing")

    def test_no_invoice_needed_switched_on_exempts_both(self):
        q3 = self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Twilio", 2100, "2026-10-14")
        on = self.tap(self.card(), "Apply to all quarters")["next"]
        self.tap(on, "No invoice needed for these")
        st = {r[0]: r[1] for r in self.conn.execute(
            "SELECT pid, status FROM projections WHERE pid IN (?, ?)", (q3, q4))}
        self.assertEqual(st, {q3: "exempt", q4: "exempt"})

    def test_a_counted_payment_that_moved_refuses_the_answer(self):
        import work
        q3 = self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Twilio", 2100, "2026-10-14")
        on = self.tap(self.card(), "Apply to all quarters")["next"]
        self.granted(work.leave_missing_in_tx, [q4])         # answered meanwhile elsewhere
        rev = self.rev(q3)
        out = self.tap(on, "Leave missing")
        self.assertIn("changed since it was shown", out["receipt"])
        self.assertEqual(self.rev(q3), rev)
        self.assertNotEqual(self.state(q3), "accepted-missing")

    def test_never_states_and_binds_the_other_quarters_it_changes(self):
        """d2 (Astra + Terra S1): a matched Q4 payment Never would change is stated."""
        self.pay("Twilio", 2000, "2026-08-14")
        q4 = self.pay("Twilio", 2100, "2026-10-14")
        self.machine_entry(q4, self.doc(issuer="Twilio", document_number="TW-9",
                                        amount_minor=2100,
                                        document_date="2026-10-01"))
        dep = self.card()
        self.assertIn("Also in other quarters: 1 payment (Q4)", untag(dep["text"]))
        self.assertNotIn("Apply to all quarters", self.labels(dep))
        out = self.tap(dep, "Never for Twilio")
        self.assertIn("never needs an invoice: 2 payments changed", out["receipt"])

    def test_never_refuses_a_payment_that_arrived_in_another_quarter(self):
        self.pay("Twilio", 2000, "2026-08-14")
        dep = self.card()
        self.pay("Twilio", 2200, "2026-11-14")               # after the card was shown
        out = self.tap(dep, "Never for Twilio")
        self.assertIn("changed since it was shown", out["receipt"])

    def test_the_review_legend_counts_the_quarters_missing(self):
        for amount in (2000, 2100):
            self.pay("Twilio", amount, "2026-08-14")
        self.pay("Twilio", 2200, "2026-10-14")
        end = self.end()
        # PLAY 0.11.2: the counts line carries the number; the legend names no count
        self.assertEqual(untag(end["text"]).split("\n")[1], "2 missing")
        self.assertIn("Review: go through the missing invoices, one at a time", end["text"])


class OfferedDropsSetAside(_Tapping):
    def test_an_irrelevant_alternative_is_not_offered(self):
        """d2 (Terra S2): a stored alternative later set aside leaves the card."""
        import cards, documents
        p = self.pay()
        alt = self.doc(document_number="INV-91")
        self.propose(p, alternatives=[alt], document_number="INV-88")
        documents.mark_irrelevant(self.conn, alt)
        import work
        with db.tx(self.conn):
            offered = cards._offered(self.conn, work.describe(self.conn, p))
        self.assertNotIn(alt, [c["doc"]["doc_id"] for c in offered])


class R5Fixes(VendorCardQuarter):
    def test_a_full_vendor_walk_with_other_quarters_shows_every_payment(self):
        """r5 Astra S2: the count line is sized into the pages, so none drops off."""
        name = "V*_[](){}-!|#<>~" * 4                     # escapes double its width
        pids = [self.pay(name, 100000 + i, "2026-08-14") for i in range(48)]
        self.pay(name, 500000, "2026-10-14")
        import cards, views
        self.patch(cards, "LEGEND_MAX", 0)                    # isolate the count line's room
        for limit in range(1500, 1700, 9):                    # some page ends up tight
            self.patch(views, "BODY_LIMIT", limit)
            dep, seen = self.card(), set()
            while True:
                seen |= set(self.c_items(dep))
                if "Next page" not in self.labels(dep):
                    break
                dep = self.tap(dep, "Next page")["next"]
            self.assertEqual(len(seen & set(pids)), len(pids), limit)
        self.assertEqual(seen & set(pids), set(pids))

    def c_items(self, dep):
        rid = dep["buttons"][0]["call"]["arguments"]["render_id"]
        sc = self.scope_of(dep)
        return [p for p in (r[0] for r in self.conn.execute(
            "SELECT pid FROM render_items WHERE render_id=?", (rid,)))
                if str(p) not in (sc.get("others") or {})]

    def test_the_legend_counts_what_the_vendor_card_answers(self):
        """r5 Astra S2: a left-missing payment is on the card and answered, so it counts."""
        import work
        a = self.pay("Twilio", 2000, "2026-08-14")
        self.pay("Twilio", 2100, "2026-08-15")
        self.granted(work.leave_missing_in_tx, [a])
        end = self.end()
        self.assertEqual(untag(end["text"]).split("\n")[1], "2 missing")
        self.assertIn("Review: go through the missing invoices, one at a time", end["text"])
