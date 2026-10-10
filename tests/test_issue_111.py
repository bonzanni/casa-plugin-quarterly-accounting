"""#111: residuals after 0.11.23 (casa-test PLAY s78). A merge posts the card of a payment that
shows the vendor and never replaces another payment's open card; a payment not classified yet
is asked about on its own card, on the lists and on the status card, and is named by its bank
description when the bank gave no payee; notes.md prints no "None"; the lists carry no bare
coverage line; the invoice-names summary is one line per part and names vendors that read as
one; "that's wage tax" is answered with one question."""
import json

from tests.fakebroker import FakeBroker
from tests.test_issue_89 import SKILL, call
from tests.test_issue_90 import _Merge
import cards
import db
import posting
import views


def flat(text):
    return " ".join(text.split())


class _Uncl(_Merge):
    def parked(self, remittance="Loonadministratie NL", counterparty=None):
        """A payment the bank gave no payee for, its classification parked (no kind)."""
        self.n += 1
        self.row(self.n, counterparty=counterparty, remittance=remittance, amount_minor=148000,
                 booking_date="2026-09-02", value_date="2026-09-02")
        pid = self.lineage_for(self.n)
        self.classify(pid, set())
        self.settle(pid)
        return pid


class NotClassified(_Uncl):
    def test_its_own_card_asks_what_it_is(self):
        p = self.parked()
        text = views.displayed(views.build_review(self.conn, view="item", pid=p)["text"])
        self.assertIn("Not classified yet — what is it?", text)
        self.assertNotIn("Not searched yet", text)
        self.assertNotIn("nothing was searched", text)

    def test_the_bank_description_names_a_payment_with_no_payee(self):
        p = self.parked()
        text = views.displayed(views.build_review(self.conn, view="item", pid=p)["text"])
        self.assertTrue(text.startswith("Loonadministratie NL · EUR 1,480.00"), text)
        self.assertNotIn("Unknown payee", text)
        # with neither, it stays unknown
        q = self.parked(remittance="")
        text = views.displayed(views.build_review(self.conn, view="item", pid=q)["text"])
        self.assertTrue(text.startswith("Unknown payee"), text)

    def test_the_lists_say_it_once(self):
        self.parked()
        for view in ("status", "missing", "quarter"):
            text = views.displayed(views.build_review(self.conn, view=view,
                                                      quarter="2026-Q3")["text"])
            self.assertEqual(text.count("Not classified yet"), 1, (view, text))
            self.assertIn("Loonadministratie NL · EUR 1,480.00 · 2 Sep — what is it?", text)

    def test_the_status_card_names_the_payment(self):
        p = self.parked()
        with db.tx(self.conn):
            rid = cards.compose_open(self.conn, "2026-Q3")
        text = views.displayed(self.render_text(rid))
        self.assertIn("1 not classified yet", text)
        lines = text.split("\n")
        at = lines.index("Not classified yet")
        self.assertEqual(lines[at + 1], "Loonadministratie NL · EUR 1,480.00 · 2 Sep — what is "
                                        "it?")
        self.assertIsNotNone(p)

    def test_a_reply_about_the_named_payment_binds(self):
        # r1 (Astra S2): the card's not-classified line binds as the views' does
        from tests._base import apply_now
        p = self.parked()
        with FakeBroker():
            out = posting.show_view(self.conn, view="open", quarter="2026-Q3")
        self.assertIn(p, views.render_items(self.conn, out["render_id"]))
        res = apply_now(self.conn, "Loonadministratie NL needs no invoice",
                        quoted=self.render_text(out["render_id"]))
        self.assertIsNotNone(res["proposal"], res)
        self.assertIn("Loonadministratie NL", views.displayed(res["proposal"]))

    def test_a_scheduled_end_card_names_the_new_one(self):
        # r1 (Terra S2): the scheduled run's card names it too
        self.parked()
        with db.tx(self.conn):
            rid = cards.compose_end(self.conn, self.job_id, scheduled=True)
        text = views.displayed(self.render_text(rid))
        self.assertIn("1 not classified yet", text)
        self.assertIn("Loonadministratie NL · EUR 1,480.00 · 2 Sep — what is it?", text)

    def test_a_crowded_card_cuts_the_lines_and_still_posts(self):
        # r2 (Astra S2): receipts that fill the card cut the not-classified lines; the card
        # is still stored, binding only what it prints whole
        p = self.parked()
        head = ["Q3 checked"] + [f"Receipt vendor {n:02d} Services International BV · 3 Sep"
                                 for n in range(120)]
        with db.tx(self.conn):
            uncl = cards.state(self.conn)["unclassified"]
            rid = cards._summary(self.conn, "end", "2026-Q3", head, [], [], [], {},
                                 scheduled=False, unclassified=uncl)
        self.assertNotIn(p, views.render_items(self.conn, rid))
        self.assertNotIn("what is it?", self.render_text(rid))

    def test_the_status_card_counts_past_three(self):
        for n in range(5):
            self.parked(remittance=f"Loon {n}")
        with db.tx(self.conn):
            rid = cards.compose_open(self.conn, "2026-Q3")
        text = views.displayed(self.render_text(rid))
        self.assertEqual(text.count("— what is it?"), 3)
        self.assertIn("+2 more not shown", text)


class PackageNotes(_Uncl):
    def test_a_nice_to_have_with_no_kind_names_none(self):
        import package
        p = self.pay("Payroll Co", 5000)
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET status='optional', exp_kind=NULL,"
                              " exp_tier='optional' WHERE pid=?", (p,))
        import io
        import zipfile
        blob = package._render(package._freeze(self.conn, "2026-Q3"), "2026-Q3",
                               "2026-10-10")[0]
        notes = zipfile.ZipFile(io.BytesIO(blob)).read("notes.md").decode()
        nice = notes.split("## Nice to have, not found")[1].split("##")[0]
        self.assertIn("Payroll Co · EUR 50.00", nice)
        self.assertNotIn("None", notes)


class ItemCardRevision(_Uncl):
    def test_one_payments_card_never_replaces_anothers(self):
        a, b = self.pay("Adobe", 1000), self.pay("Zapier", 2000)
        with FakeBroker() as fb:
            posting.show_view(self.conn, view="item", pid=a)
            posting.show_view(self.conn, view="item", pid=b)
            posting.show_view(self.conn, view="item", pid=a)
        revs = [json.loads(d["value"])["revision"] for d in fb.deposits]
        self.assertNotEqual(revs[0], revs[1])
        self.assertEqual(revs[0], revs[2])


class MergeCard(_Uncl):
    def test_the_merged_vendors_card_is_a_payment_with_an_amount(self):
        a = self.pay("Anthropic", 2000, "2026-07-03")
        self.pay("Claude.ai Subscription", 0, "2026-09-03")
        out, deps = self.merge("Claude.ai Subscription", "Anthropic")
        card = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                            (out["render_id"],)).fetchone()[0])
        self.assertEqual(card.get("pid"), a)


class InvoiceNamesSummary(_Uncl):
    def test_one_line_per_part_and_vendors_that_read_as_one(self):
        self.pay("Anthropic", 2000, "2026-07-03")
        p = self.pay("Claude.ai Subscription", 2100, "2026-08-03")
        self.matched_to(p, "Anthropic, PBC", amount_minor=2100, document_date="2026-08-03")
        self.pay("Zapier", 100, "2026-08-04")
        with FakeBroker() as b:
            call("rename_vendors_to_invoice_names", {})
        self.assertEqual(views.displayed(b.deposits[0]["value"]).split("\n"), [
            "Renamed 1 vendor to the name on their invoice.",
            "No invoice yet: Anthropic, Zapier.",
            "Anthropic and Anthropic, PBC may be one vendor — ask me to merge them if so."])


class WageTaxReply(_Uncl):
    def test_the_desk_answers_with_one_question(self):
        # #115: superseded — the desk runs the check (tests/test_issues_115_117.py)
        self.assertIn("then the check ask below, with its quarter.", flat(SKILL))
        self.assertLessEqual(len(SKILL), 10_000)
