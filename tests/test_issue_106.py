"""#106: wording after 0.11.18 — the not-classified line names the payment and asks what it
is; the bank ledger says "nice to have" as the cards do; a posted view is the whole answer
(its result says so); "that's wage tax" offers a check; a short document number is shown."""
from tests._base import LoopCase
from tests.fakebroker import FakeBroker
import db
import views


class Wording(LoopCase):
    def test_the_not_classified_line_names_the_payment_and_asks(self):
        p = self.pay("Loonadministratie", 148000)
        self.classify(p, set())
        self.settle(p)
        r = views.build_review(self.conn, "status", quarter="2026-Q3")
        lines = r["text"].split("\n")
        at = lines.index("**Not classified yet**")
        self.assertTrue(lines[at + 1].startswith("Loonadministratie · EUR 1,480.00"), lines)
        self.assertIn("Not classified yet — tell me what it is (\"that's wage tax\").", lines)
        # r1 (Astra, Terra): the named payment is bound to the view, so a reply about it binds
        self.assertIn(p, views.render_items(self.conn, r["render_id"]))

    def test_the_ledger_says_nice_to_have(self):
        import mirror
        p = self.pay("Payroll Co", 5000)
        self.classify(p, {"income", "salary"})
        self.settle(p)
        self.assertEqual(self.conn.execute("SELECT status FROM projections WHERE pid=?",
                                           (p,)).fetchone()[0], "optional")
        self.assertEqual(mirror.note_text(self.conn, p),
                         "Accounting: nice to have (quarterly check)")

    def test_a_posted_view_says_it_is_the_whole_answer(self):
        import posting
        p = self.pay()
        self.propose(p)
        with FakeBroker():
            out = posting.show_view(self.conn, view="check", quarter="2026-Q3")
        self.assertIn("never describe it", out["note"])
        self.assertIn("<silent/>", out["note"])

    def test_a_short_document_number_is_on_its_line_a_long_id_is_not(self):
        import cards
        a = self.pay("Adobe", 1000)
        self.propose(a, amount_minor=1000, document_number="INV-88")
        b = self.pay("Runpod", 2000)
        self.propose(b, amount_minor=2000, document_number="pi_3UKZabcdefghijklmnop")
        with db.tx(self.conn):
            rid = cards.compose_end(self.conn, self.job_id, scheduled=False)
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (rid,)).fetchone()[0]
        self.assertIn("↔ invoice INV\\-88 · EUR 10.00", text)
        self.assertNotIn("pi\\_3UKZ", text)
