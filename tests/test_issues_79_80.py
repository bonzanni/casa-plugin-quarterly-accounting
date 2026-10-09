"""0.11.12: issues #79 and #80 (casa-test PLAY of 0.11.10): a matched payment's card said
"matched" twice; "To check" opened with one quarter's "Not checked yet."; each view gets
one plain line in the desk skill and the show_view description (the model picks the view)."""
import pathlib

from tests._base import LoopCase

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

    def test_a_quarter_view_keeps_it(self):
        import views
        self.pay()
        text = views.build_review(self.conn, view="missing", quarter="2026-Q3")["text"]
        self.assertIn("Not checked yet.", text)


class ViewsDescribed(LoopCase):
    def test_each_view_has_its_line_in_the_skill_and_the_tool(self):
        import qa_server, tools  # noqa: F401
        skill = (ROOT / "skills/quarterly-accounting/SKILL.md").read_text()
        desc = qa_server.TOOLS["show_view"]["description"]
        for view in ("status", "missing", "check", "rest", "older", "all", "quarter", "item"):
            self.assertIn(f"- `{view}`", skill)
            self.assertIn(f"{view} (", desc)
        self.assertIn("every quarter's suggested matches waiting for a yes or no", skill)
        self.assertIn("every quarter's suggested matches waiting for a yes or no", desc)
