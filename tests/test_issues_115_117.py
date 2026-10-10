"""#115: a payment classified after the quarter's last bank import is still asked about — the
desk's answer to "that's wage tax" now runs the check (the only path that re-reads the
ledger's tags), never "Run the check now?". #117: small inconsistencies — a status sheet that
lists open items never says "Everything matched cleanly."; a payment still pending at the bank
is "waiting on the bank" on the views as on the cards, never "what is it?"; "what's missing"
is the missing list; no card prescribes words to type."""
import pathlib
import re

from tests.test_issue_89 import SKILL
from tests.test_issue_111 import _Uncl, flat
import cards
import db
import views

ROOT = pathlib.Path(__file__).resolve().parent.parent


class ClassifyRunsTheCheck(_Uncl):
    def test_the_desk_starts_the_check_after_tagging(self):
        s = flat(SKILL)
        self.assertNotIn("Run the check now?", s)
        self.assertIn("classify-transactions does (even if already tagged), then the check ask "
                      "below, with its quarter.", s)
        self.assertLessEqual(len(SKILL), 10_000)

    def test_what_is_missing_is_the_missing_list(self):
        s = flat(SKILL)
        self.assertIn("what is open, whether it is done", s)
        self.assertIn("`missing`: one quarter's payments still missing a document (\"what's "
                      "missing?\")", s)


class Bucketing(_Uncl):
    def pending_parked(self):
        p = self.parked()
        with db.tx(self.conn):
            self.conn.execute("UPDATE bank_rows SET status='PDNG' WHERE row_id=?", (self.n,))
        self.settle(p)
        return p

    def test_a_pending_payment_waits_on_the_bank_everywhere(self):
        self.pending_parked()
        for view in ("status", "missing", "quarter"):
            text = views.displayed(views.build_review(self.conn, view=view,
                                                      quarter="2026-Q3")["text"])
            self.assertNotIn("what is it?", text, (view, text))
            self.assertNotIn("Not classified yet", text, (view, text))
        text = views.displayed(views.build_review(self.conn, view="status",
                                                  quarter="2026-Q3")["text"])
        lines = text.split("\n")
        self.assertIn("Waiting on the bank", lines)
        self.assertTrue(lines[lines.index("Waiting on the bank") + 1].startswith(
            "Loonadministratie NL · EUR 1,480.00"), text)
        with db.tx(self.conn):
            rid = cards.compose_open(self.conn, "2026-Q3")
        card = views.displayed(self.render_text(rid))
        self.assertIn("1 waiting on the bank", card)
        self.assertNotIn("what is it?", card)

    def test_everything_matched_only_when_nothing_else_is_open(self):
        done = self.pay("Adobe", 1000, "2026-09-03")
        self.machine_entry(done, self.doc(amount_minor=1000, document_date="2026-09-03"))
        text = views.displayed(views.build_review(self.conn, view="status",
                                                  quarter="2026-Q3")["text"])
        self.assertIn("Everything matched cleanly.", text)
        self.parked()               # one open item now on the sheet
        text = views.displayed(views.build_review(self.conn, view="status",
                                                  quarter="2026-Q3")["text"])
        self.assertNotIn("Everything matched cleanly.", text)
        self.assertIn("Everything else matched cleanly.", text)

    def test_older_open_items_are_not_everything(self):
        done = self.pay("Adobe", 1000, "2026-09-03")
        self.machine_entry(done, self.doc(amount_minor=1000, document_date="2026-09-03"))
        with db.tx(self.conn):
            self.conn.execute("UPDATE binding SET watermark='2026-04-01'")
        self.n += 1
        self.row(self.n, counterparty=None, remittance="Old one", amount_minor=500,
                 booking_date="2026-04-02", value_date="2026-04-02")
        old = self.lineage_for(self.n)
        self.classify(old, set())
        self.settle(old)
        text = views.displayed(views.build_review(self.conn, view="status",
                                                  quarter="2026-Q3")["text"])
        self.assertIn("older not classified yet", text)
        self.assertNotIn("Everything matched cleanly.", text)


class NoPrescribedWords(_Uncl):
    """#117 item 5: the operator speaks freely; a card or notice never quotes words to type."""
    MODULES = ("alerts", "cards", "delivery", "naming", "taps", "views")
    SAY = re.compile(r"""[Ss]ay \\?["“']""")

    def test_no_operator_text_quotes_a_phrase(self):
        for m in self.MODULES:
            src = (ROOT / "server" / f"{m}.py").read_text()
            code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
            hits = [ln for ln in code if self.SAY.search(ln)]
            self.assertEqual(hits, [], m)

    def test_the_status_card_names_no_phrase(self):
        for _ in range(5):
            self.parked()
        with db.tx(self.conn):
            rid = cards.compose_open(self.conn, "2026-Q3")
        text = views.displayed(self.render_text(rid))
        self.assertIn("+2 more not shown", text)
        self.assertNotIn('say "', text.lower())
