# tests/test_dates_read.py
"""Issue #22: the date that names a file in a package was read on the document, or the
package says it was not. The store marks a date read by a machine pairing or a
confirmation; a package round's judge is handed the quarter's pairings whose date was
never read; the caption and notes.md count what is left (design
docs/superpowers/specs/2026-09-30-issues-21-22-design.md, Part B)."""
import unittest

from tests.test_package import Base
import db  # noqa: E402
import documents  # noqa: E402
import matches  # noqa: E402
import work  # noqa: E402


class Dates(Base):
    def read_at(self, doc):
        return self.conn.execute("SELECT date_read_at FROM documents WHERE doc_id=?",
                                 (doc,)).fetchone()[0]

    def paired(self, date=None, how="record"):
        # each payment and its invoice distinct: identical payments are one identity
        n = getattr(self, "k", 0) + 1
        self.k = n
        pid = self.line(amount_minor=10000 + n, remittance=f"INV{n}")
        doc = self.file_doc(amount_minor=10000 + n)
        fn = matches.record_match if how == "record" else matches.propose_match
        kw = {"author": "auto"} if how == "record" else {}
        fn(self.conn, pid=pid, doc_id=doc, expected_revision=self.rev(pid),
           row_snapshot=self.snapshot(pid), token=self.token, document_date=date, **kw)
        return pid, doc


class TestTheMark(Dates):
    def test_a_filed_document_is_unread(self):
        self.assertIsNone(self.read_at(self.file_doc()))

    def test_a_machine_pairing_that_states_the_date_marks_it_read_even_unchanged(self):
        for how in ("record", "propose"):
            with self.subTest(how):
                _, doc = self.paired(date="2026-07-02", how=how)     # the filed date, confirmed
                self.assertIsNotNone(self.read_at(doc))

    def test_a_pairing_without_a_date_leaves_it_unread(self):
        # the operator's pairing path carries no date; at this layer a machine one may not
        _, doc = self.paired(date=None)
        self.assertIsNone(self.read_at(doc))

    def test_a_date_corrected_or_confirmed_marks_it_other_fields_do_not(self):
        doc = self.file_doc()
        documents.update_document_metadata(self.conn, doc, token=self.token, issuer="Adobe Inc")
        self.assertIsNone(self.read_at(doc))
        documents.update_document_metadata(self.conn, doc, token=self.token,
                                           document_date="2026-07-02")
        self.assertIsNotNone(self.read_at(doc))

    def test_a_date_cleared_is_no_date_read(self):
        # D2 (Astra S2): the file then falls back to the payment's date, unread
        doc = self.file_doc()
        documents.update_document_metadata(self.conn, doc, token=self.token,
                                           document_date="2026-07-02")
        for cleared in ("", None):
            documents.update_document_metadata(self.conn, doc, token=self.token,
                                               document_date="2026-07-02")
            documents.update_document_metadata(self.conn, doc, token=self.token,
                                               document_date=cleared)
            self.assertIsNone(self.read_at(doc), cleared)


class TestTheListing(Dates):
    def test_only_the_quarters_current_pairings_with_an_unread_date_are_listed(self):
        unread, _ = self.paired(date=None)
        self.paired(date="2026-07-02")                              # read
        self.line()                                                  # no pairing
        prop, _ = self.paired(date=None, how="propose")             # a proposal ships too
        out = work.list_quarter_state(self.conn, "2026-Q3", unread_dates=True)
        self.assertEqual([i["pid"] for i in out["dates_unread"]], sorted([unread, prop]))
        self.assertEqual(out["total"], 2)
        self.assertFalse(out["dates_unread"][0]["current"]["document"]["date_read"])
        self.assertEqual(work.list_quarter_state(self.conn, "2026-Q4",
                                                 unread_dates=True)["total"], 0)

    def test_it_names_one_quarter(self):
        for kw in ({}, {"triage_only": True, "quarter": "2026-Q3"}):
            with self.assertRaises(db.Refusal):
                work.list_quarter_state(self.conn, unread_dates=True, **kw)


class TestThePackageSays(Dates):
    def test_a_file_named_by_an_unread_date_is_counted_and_listed(self):
        self.paired(date=None)
        out, z = self.build()
        # simple loop §1: the caption is one line; the count lives in notes.md
        self.assertNotIn("not yet read", out["caption"])
        notes = z.read("notes.md").decode()
        self.assertIn("## Dates not yet read from the document\n\n- invoices/"
                      "2026-07-02_Adobe_100.01.pdf — named by the date it was filed with", notes)

    def test_a_package_whose_dates_were_all_read_says_nothing_of_it(self):
        self.paired(date="2026-07-02")
        out, z = self.build()
        self.assertNotIn("not yet read", out["caption"])
        self.assertNotIn("Dates not yet read", z.read("notes.md").decode())


if __name__ == "__main__":
    unittest.main()
