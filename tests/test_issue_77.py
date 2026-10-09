"""0.11.10: issue #77 (casa-test PLAY of 0.11.9): the reading stored no recipient, so a
company-addressed reissue was called "already filed"; a "To check · Q4" list held Q2/Q3
items; one vendor read under two names on one card."""
import pathlib

from tests._base import LoopCase
import db

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Reading(LoopCase):
    def test_the_reading_asks_for_the_recipient(self):
        import qa_server, tools  # noqa: F401
        job = " ".join((ROOT / "skills/quarterly-job/SKILL.md").read_text().split())
        self.assertIn("document_number, recipient, pass_token)` (`recipient`: the \"Bill to\" "
                      "name)", job)
        desc = qa_server.TOOLS["update_document_metadata"]["description"]
        self.assertIn('recipient (the "Bill to" / customer name', desc)

    def test_a_reissue_differing_only_in_recipient_is_no_copy(self):
        import documents
        a = self.doc(recipient="Nicola Bonzanni", document_number="FG-2", issuer="OpenAI")
        b = self.doc(recipient="Lesina Holding B.V.", document_number="FG-2", issuer="OpenAI")
        self.assertIsNone(documents.duplicate_of(self.conn, b))
        with db.tx(self.conn):
            import cards
            self.assertEqual(cards._differs(self.conn, a, b),
                             "Differs: recipient Nicola Bonzanni → Lesina Holding B.V.")


class Heading(LoopCase):
    def test_the_to_check_list_names_no_quarter(self):
        import views
        self.assertEqual(views.view_title("check", "2026-Q4"), "To check")
