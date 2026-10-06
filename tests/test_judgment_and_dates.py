# tests/test_judgment_and_dates.py
"""Issue #19: a machine pairing states the date printed on the document, and the package
names the file by it (design docs/superpowers/specs/2026-09-30-issues-17-18-19-design.md,
Part C)."""
import unittest

from tests.test_ledger import RealLedger


class TestTheDateReadOnTheDocument(RealLedger):
    def judged(self, **match):
        """A Q3 payment of 2026-07-05 and its invoice, filed with the email's date; the
        run pairs them through the tool layer."""
        bf = self.bf
        bf.fetch([bf.row("2026-07-05", ref="R0", amount=1000, counterparty="Adobe")])
        self.tag(self.active()[0]["row_id"], "software")
        self.first_pass()
        t = self.begin()
        doc = self.file(amount_minor=1000, document_date="2026-07-20")   # the email's date
        item = self.call("list_quarter_state", triage=True, quarter="2026-Q3",
                         pass_token=t)["triage"][0]
        args = dict(pid=item["pid"], doc_id=doc, expected_revision=item["revision"],
                    row_digest=item["row_digest"], pass_token=t, **match)
        return t, doc, item, args

    def date_of(self, doc):
        return self.conn.execute("SELECT document_date FROM documents WHERE doc_id=?",
                                 (doc,)).fetchone()[0]

    def test_a_machine_pairing_without_the_date_is_refused_and_writes_nothing(self):
        t, doc, item, args = self.judged()
        for tool, extra in (("record_match", {"author": "auto"}), ("propose_match", {})):
            out = self.text(tool, **args, **extra)
            self.assertTrue(out.startswith("refused: pass document_date: the date printed on "
                                           "the document"), (tool, out))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
        self.assertEqual(self.date_of(doc), "2026-07-20")
        out = self.text("record_match", **args, author="auto", document_date="5 July")
        self.assertEqual(out, "refused: document_date is YYYY-MM-DD")

    def test_the_date_read_replaces_the_filed_one_and_names_the_file(self):
        t, doc, item, args = self.judged()
        out = self.call("record_match", **args, author="auto", document_date="2026-07-05")
        self.assertEqual(out["state"], "matched")
        self.assertEqual(self.date_of(doc), "2026-07-05")
        self.end_live_pass()
        pkg, _, _, z = self.zip_of()
        names = z.namelist()
        self.assertIn("invoices/2026-07-05_Adobe_10.00.pdf", names, names)
        self.assertFalse(any("2026-07-20" in n for n in names), names)

    def test_a_proposal_states_it_too(self):
        t, doc, item, args = self.judged()
        out = self.call("propose_match", **args, document_date="2026-07-04")
        self.assertEqual(out["state"], "proposed")
        self.assertEqual(self.date_of(doc), "2026-07-04")

    def test_the_filed_date_confirmed_changes_nothing(self):
        t, doc, item, args = self.judged()
        self.call("record_match", **args, author="auto", document_date="2026-07-20")
        self.assertEqual(self.date_of(doc), "2026-07-20")


if __name__ == "__main__":
    unittest.main()
