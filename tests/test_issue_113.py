"""#113: a payment the bank gave no payee for is named by the name field its SEPA description
carries — on the cards (no clipped bank text with a hash), in notes.md and in ledger.csv,
whose row also carries the whole bank description."""
import csv
import io
import unittest
import zipfile

from tests.test_issue_111 import _Uncl
import cards
import db
import kb
import package
import views

SEPA = "SEPA Overboeking Naam: Loonadministratie NL Omschrijving: LH 2026-09 LESINA"


class BankName(unittest.TestCase):
    def test_the_name_field_else_the_text(self):
        self.assertEqual(kb.bank_name(SEPA), "Loonadministratie NL")
        self.assertEqual(kb.bank_name("/TRTP/SEPA OVERBOEKING/NAME/Belastingdienst/REMI/x"),
                         "Belastingdienst")
        self.assertEqual(kb.bank_name("Company Free plan fee"), "Company Free plan fee")
        self.assertEqual(kb.bank_name("Naam: Omschrijving: x"), "Naam: Omschrijving: x")
        self.assertEqual(kb.bank_name("  "), kb.UNKNOWN)
        self.assertEqual(kb.bank_name(None), kb.UNKNOWN)


class NamedEverywhere(_Uncl):
    def _package(self):
        blob = package._render(package._freeze(self.conn, "2026-Q3"), "2026-Q3",
                               "2026-10-10")[0]
        z = zipfile.ZipFile(io.BytesIO(blob))
        return z.read("notes.md").decode(), list(csv.DictReader(
            io.StringIO(z.read("ledger.csv").decode())))

    def test_the_card_leads_with_the_name(self):
        self.parked(remittance=SEPA)
        with db.tx(self.conn):
            rid = cards.compose_open(self.conn, "2026-Q3")
        text = views.displayed(self.render_text(rid))
        self.assertIn("Loonadministratie NL · EUR 1,480.00 · 2 Sep — what is it?", text)
        self.assertNotIn("SEPA Overboeking", text)
        self.assertNotIn("…", text)

    def test_notes_and_ledger_name_it_alike(self):
        self.parked(remittance=SEPA)
        self.parked(remittance="Company Free plan fee")
        notes, rows = self._package()
        self.assertIn("- Loonadministratie NL · EUR 1,480.00 · 2 Sep", notes)
        self.assertNotIn("Unknown payee", notes)
        by_vendor = {r["vendor"]: r for r in rows}
        self.assertEqual(set(by_vendor), {"Loonadministratie NL", "Company Free plan fee"})
        self.assertEqual(by_vendor["Loonadministratie NL"]["counterparty"], "")
        self.assertEqual(by_vendor["Loonadministratie NL"]["notes"], f"bank: {SEPA}")
        # a description that is its own name is not repeated
        self.assertEqual(by_vendor["Company Free plan fee"]["notes"], "")

    def test_a_payee_keeps_its_name(self):
        self.pay("Payroll Co", 5000)
        _, rows = self._package()
        self.assertEqual([(r["counterparty"], r["vendor"], r["notes"]) for r in rows],
                         [("Payroll Co", "Payroll Co", "")])
