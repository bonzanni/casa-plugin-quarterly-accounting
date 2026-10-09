"""#86: a vendor named by part of its name ("call Informatique by the name on its invoice")
was looked up exactly, missed, and the desk answered from an earlier conversation; the desk
skill now starts every naming request with a fresh list_vendors, and get_counterparty's miss
points there. #87: a rename answers with one plain sentence (`line`) for the desk to say
before the vendor's card. #89 moved the naming steps into tools that post their outcome
(tests/test_issue_89.py); the store's `line` stays."""
from tests.test_issues_57_59 import _Q3
import kb


class RenameLine(_Q3):
    def test_a_rename_of_an_entry_says_its_old_and_new_name(self):
        self.pay("Informatique via Stichting Mollie Payments", 129900, "2026-08-20")
        self.kb("Informatique via Stichting Mollie Payments")
        out = kb.upsert_counterparty(self.conn, "Informatique via Stichting Mollie Payments",
                                     new_name="Informatique computers en componenten B.V.")
        self.assertEqual(out["line"], "Informatique via Stichting Mollie Payments is now "
                                      "called Informatique computers en componenten B.V.")

    def test_a_rename_by_a_bank_text_says_the_entrys_name(self):
        kb.upsert_counterparty(self.conn, "Google Workspace", patterns=["GOOGLE*WS"])
        out = kb.upsert_counterparty(self.conn, "google*ws", new_name="Google")
        self.assertEqual(out["line"], "Google Workspace is now called Google.")

    def test_a_rename_with_no_entry_says_the_bank_text(self):
        self.pay("LINKEDIN", 5784, "2026-09-15")
        out = kb.upsert_counterparty(self.conn, "LINKEDIN", new_name="LinkedIn")
        self.assertEqual(out["line"], "LINKEDIN is now called LinkedIn.")

    def test_the_line_keeps_the_stored_name_whole(self):
        # r1 (Terra S2): a trailing period of the name itself is kept
        self.pay("RAW", 100, "2026-08-03")
        out = kb.upsert_counterparty(self.conn, "RAW", new_name="Acme B.V..")
        self.assertEqual(out["name"], "Acme B.V..")
        self.assertEqual(out["line"], "RAW is now called Acme B.V..")
        out = kb.upsert_counterparty(self.conn, "Acme B.V..", new_name="Acme")
        self.assertEqual(out["line"], "Acme B.V.. is now called Acme.")

    def test_an_upsert_without_a_rename_has_no_line(self):
        out = kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"])
        self.assertNotIn("line", out)


class Lookup(_Q3):
    def test_a_miss_points_to_list_vendors(self):
        import qa_server
        import tools  # noqa: F401
        self.kb("Informatique via Stichting Mollie Payments")
        out = qa_server.TOOLS["get_counterparty"]["fn"]({"text": "Informatique"})
        self.assertFalse(out["found"])
        self.assertIn("list_vendors", out["note"])
        self.assertIn("list_vendors", qa_server.TOOLS["get_counterparty"]["description"])
