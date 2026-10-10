"""0.11.12: issues #79 and #80 (casa-test PLAY of 0.11.10): a matched payment's card said
"matched" twice; "To check" opened with one quarter's "Not checked yet."; each view gets
one plain line in the desk skill and the show_view description (the model picks the view)."""
import pathlib

from tests._base import LoopCase
from tests.test_taps_next import _Tapping

ROOT = pathlib.Path(__file__).resolve().parents[1]


class MatchedOnce(LoopCase):
    def test_a_matched_payment_says_its_document_once(self):
        import matches, views
        p = self.pay()
        d = self.doc(document_number="INV-5004", document_date="2026-09-02")
        matches.record_match(self.conn, pid=p, doc_id=d, author="auto",
                             expected_revision=self.rev(p), token=self.token,
                             document_date=self.stored_date(d))
        text = views.build_review(self.conn, view="item", pid=p)["text"]
        self.assertIn("Matched to", text)
        self.assertNotIn("is filed with it", text)
        self.assertEqual(text.count("5004"), 1, text)

    def test_a_proposed_payment_says_its_document_once(self):
        import views
        p = self.pay()
        self.propose(p, document_number="INV-88", document_date="2026-09-02")
        text = views.build_review(self.conn, view="item", pid=p)["text"]
        self.assertIn("Suggested:", text)
        self.assertEqual(text.count("88"), 1, text)


class ToCheckHead(LoopCase):
    def test_the_to_check_list_has_no_coverage_line(self):
        import views
        p = self.pay()
        self.propose(p, document_number="INV-88", document_date="2026-09-02")
        text = views.build_review(self.conn, view="check", quarter="2026-Q3")["text"]
        self.assertNotIn("Not checked yet.", text)
        self.assertNotIn("Bank checked through", text)
        self.assertIn("Suggested:", text)

    def test_a_quarter_sheet_keeps_it_and_a_list_has_none(self):
        # #111: the Missing and Nice-to-have lists opened with a bare "Not checked yet."
        import views
        self.pay()
        text = views.build_review(self.conn, view="status", quarter="2026-Q3")["text"]
        self.assertIn("bank not checked yet ·", text)
        for view in ("missing", "rest", "older"):
            text = views.build_review(self.conn, view=view, quarter="2026-Q3")["text"]
            self.assertNotIn("checked yet", text.split("\n")[1], view)
            self.assertNotIn("Bank checked through", text, view)


class ViewsDescribed(LoopCase):
    def test_each_view_has_its_line_in_the_skill_and_the_tool(self):
        import qa_server, tools  # noqa: F401
        skill = (ROOT / "skills/quarterly-accounting/SKILL.md").read_text()
        desc = qa_server.TOOLS["show_view"]["description"]
        for view in ("status", "missing", "check", "rest", "older", "all", "quarter", "item"):
            self.assertIn(f"- `{view}`", skill)
            self.assertIn(f"{view} (", desc)
        line = "suggested matches waiting for a yes or no, in the quarter and every earlier one"
        self.assertIn(line, skill)
        self.assertIn(line, desc)


class OwedCardFollows(_Tapping):
    def test_a_switch_on_an_answered_page_re_posts_page_one(self):
        """r2 Astra S1: page 2 answered meanwhile, then its quarter switch: the vendor's
        page 1 still holds payments, so it follows the receipt (not the walk's end)."""
        import matches
        for i in range(30):                         # the walk's quarter (the run's: Q4)
            self.pay("Adobe", 100 + i, "2026-10-%02d" % (i % 8 + 1))
        self.pay("Adobe", 999, "2026-08-05")        # another quarter: the switch is offered
        page1 = self.tap(self.end(), "Review")["next"]
        page2 = self.tap(page1, "Next page")["next"]
        for p in self.scope_of(page2)["pages"][1]:
            amount = self.conn.execute("SELECT amount_minor FROM bank_rows b JOIN projections p"
                                       " ON p.dest_row_id=b.row_id WHERE p.pid=?",
                                       (p,)).fetchone()[0]
            matches.record_match(self.conn, pid=p, author="auto",
                                 doc_id=self.doc(document_date="2026-10-01", amount_minor=amount),
                                 expected_revision=self.rev(p), token=self.token)
        out = self.tap(page2, "Apply to all quarters")
        self.assertEqual(out["receipt"], "Nothing is left on this card: answered meanwhile.")
        self.assertIn("Adobe", out["next"]["text"])
