"""#89 (OPERATOR DECISION): naming a vendor posts its own outcome — the model adds nothing.
rename_vendor finds one vendor from the operator's words (a name, part of it, a bank text, the
name on its invoice) and posts "<old> is now called <new>." before the vendor's card;
rename_vendors_to_invoice_names renames every vendor to the name on its invoice and posts one
short summary. upsert_counterparty no longer renames."""
import json
import pathlib

from tests.fakebroker import FakeBroker
from tests.test_issues_57_59 import _Q3
import db
import kb
import views
import work

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = (ROOT / "skills/quarterly-accounting/SKILL.md").read_text()
INFO = "Informatique via Stichting Mollie Payments"
INFO_ISSUER = "Informatique computers en componenten B.V."


def call(name, args):
    import qa_server
    import tools  # noqa: F401
    return qa_server.TOOLS[name]["fn"](args)


class _Case(_Q3):
    def entries(self):
        return self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0]

    def entry(self, name):
        return self.conn.execute("SELECT * FROM counterparties WHERE name=?",
                                 (name,)).fetchone()

    def informatique(self):
        done = self.pay(INFO, 129900, "2026-07-20")
        self.matched_to(done, INFO_ISSUER, amount_minor=129900, document_date="2026-07-20")
        self.kb(INFO)
        return self.pay(INFO, 4900, "2026-08-20")


class OneVendor(_Case):
    def test_part_of_a_name_takes_the_name_on_its_invoice_and_posts_line_and_card(self):
        latest = self.informatique()
        with FakeBroker() as b:
            out = call("rename_vendor", {"vendor": "informatique"})
        self.assertEqual(len(b.deposits), 1)
        dep = b.deposits[0]
        self.assertEqual(dep["slot"], "view")
        value = json.loads(dep["value"])
        self.assertEqual([views.unesc(p) for p in value["pages"]],
                         [f"{INFO} is now called {INFO_ISSUER}"])
        self.assertIn(INFO_ISSUER, views.unesc(value["text"]))
        self.assertTrue(value["buttons"])
        card = self.conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                 (out["render_id"],)).fetchone()[0]
        self.assertEqual(json.loads(card).get("pid"), latest)
        # the result carries no words to retell
        self.assertNotIn("line", out)
        self.assertNotIn(INFO_ISSUER, json.dumps(out))
        self.assertIn("<silent/>", out["note"])
        e = self.entry(INFO_ISSUER)
        self.assertIsNotNone(e["named_at"])
        self.assertIn(INFO, json.loads(e["patterns_json"]))
        self.assertEqual(self.entries(), 1)

    def test_a_given_name_by_bank_text(self):
        self.pay("Elevenlabs.io", 2200, "2026-08-03")
        self.kb("Elevenlabs.io")
        with FakeBroker() as b:
            call("rename_vendor", {"vendor": "Elevenlabs.io", "new_name": "ElevenLabs"})
        self.assertEqual(json.loads(b.deposits[0]["value"])["pages"],
                         ["Elevenlabs.io is now called ElevenLabs."])
        self.assertEqual(self.entry("ElevenLabs")["patterns_json"], '["Elevenlabs.io"]')

    def test_a_case_only_name_is_applied(self):
        self.pay("LINKEDIN", 5784, "2026-09-15")
        with FakeBroker() as b:
            call("rename_vendor", {"vendor": "linkedin", "new_name": "LinkedIn"})
        self.assertEqual(json.loads(b.deposits[0]["value"])["pages"],
                         ["LINKEDIN is now called LinkedIn."])
        self.assertIsNotNone(self.entry("LinkedIn"))

    def test_the_old_name_still_finds_a_renamed_vendor(self):
        self.informatique()
        with FakeBroker():
            call("rename_vendor", {"vendor": "Informatique"})
        with FakeBroker() as b:
            call("rename_vendor", {"vendor": INFO, "new_name": "Informatique"})
        self.assertEqual(json.loads(b.deposits[0]["value"])["pages"],
                         [f"{views.field(INFO_ISSUER)} is now called Informatique."])
        self.assertEqual(self.entries(), 1)

    def test_already_that_name_posts_the_card_and_writes_nothing(self):
        self.pay("Coolblue B.V.", 40900, "2026-08-30")
        self.kb("Coolblue B.V.")
        with FakeBroker() as b:
            call("rename_vendor", {"vendor": "coolblue", "new_name": "Coolblue B.V."})
        self.assertEqual(json.loads(b.deposits[0]["value"])["pages"],
                         ["Coolblue B.V. already has that name."])
        self.assertIsNone(self.entry("Coolblue B.V.")["named_at"])

    def test_several_vendors_fit_is_a_refusal_naming_them(self):
        for t in ("Ryanair H2n0", "Ryanair Mtw0", "Ryanair Zgx0"):
            self.pay(t, 5000, "2026-08-03")
        with FakeBroker() as b:
            out = call("rename_vendor", {"vendor": "ryanair", "new_name": "Ryanair"})
        self.assertEqual(b.deposits, [])
        self.assertIsNone(out["view"])
        self.assertIn("fits 3 vendors: Ryanair H2n0, Ryanair Mtw0, Ryanair Zgx0", out["refused"])
        self.assertEqual(self.entries(), 0)

    def test_an_exact_bank_text_wins_over_containment(self):
        self.pay("Amazon", 100, "2026-08-03")
        self.pay("Amazon EU SARL", 200, "2026-08-04")
        with FakeBroker() as b:
            call("rename_vendor", {"vendor": "amazon", "new_name": "Amazon.nl"})
        self.assertEqual(json.loads(b.deposits[0]["value"])["pages"],
                         ["Amazon is now called Amazon.nl."])

    def test_no_vendor_fits(self):
        self.pay("Zapier", 100, "2026-08-03")
        with FakeBroker() as b:
            out = call("rename_vendor", {"vendor": "Adobe", "new_name": "Adobe Inc"})
        self.assertEqual(b.deposits, [])
        self.assertIn("no vendor's name or bank text contains 'Adobe'", out["refused"])

    def test_no_matched_invoice_never_guesses(self):
        self.pay("OPENAI *CHATGPT", 2000, "2026-08-05")
        with FakeBroker() as b:
            out = call("rename_vendor", {"vendor": "openai"})
        self.assertEqual(b.deposits, [])
        self.assertIn("has no matched invoice yet", out["refused"])
        self.assertEqual(self.entries(), 0)

    def test_another_vendors_name_is_refused_and_nothing_posts(self):
        self.pay("Anthropic", 100, "2026-08-03")
        self.kb("Anthropic, PBC")
        self.pay("Claude.ai Subscription", 200, "2026-08-04")
        with FakeBroker() as b:
            out = call("rename_vendor", {"vendor": "Claude.ai", "new_name": "Anthropic, PBC"})
        self.assertEqual(b.deposits, [])
        self.assertIn("already belongs to", out["refused"])

    def test_upsert_counterparty_no_longer_renames(self):
        self.pay("Aws Emea", 1300, "2026-08-03")
        self.kb("Aws Emea")
        with self.assertRaises(db.Refusal) as cm:
            call("upsert_counterparty", {"name": "Aws Emea", "new_name": "AWS"})
        self.assertIn("rename_vendor", str(cm.exception))
        self.assertIsNotNone(self.entry("Aws Emea"))


class InvoiceName(_Case):
    def test_a_sales_invoice_names_its_recipient_and_a_payslip_names_nobody(self):
        sale = self.pay("Klant BV", 50000, "2026-08-03")
        self.machine_entry(sale, self.doc(kind="sales-invoice", issuer="Lesina Holding B.V.",
                                          recipient="Klant Holding B.V.", amount_minor=50000,
                                          document_date="2026-08-03"))
        slip = self.pay("Jan Jansen", 300000, "2026-08-25")
        self.machine_entry(slip, self.doc(kind="payslip", issuer="Lesina Holding B.V.",
                                          amount_minor=300000, document_date="2026-08-25"))
        with db.tx(self.conn):
            got = {v["name"]: v["invoice_name"] for v in work.vendors(self.conn)}
        self.assertEqual(got, {"Jan Jansen": None, "Klant BV": "Klant Holding B.V."})


class AllVendors(_Case):
    def test_one_pass_and_one_short_summary(self):
        self.informatique()
        for t in ("Ryanair H2n0", "Ryanair Mtw0"):
            p = self.pay(t, 5000, "2026-07-03")
            self.matched_to(p, "Ryanair DAC", amount_minor=5000, document_date="2026-07-03")
        p = self.pay("Elevenlabs.io", 2200, "2026-07-03")
        self.matched_to(p, "Eleven Labs Inc.", amount_minor=2200, document_date="2026-07-03")
        self.kb("Elevenlabs.io")
        kb.upsert_counterparty(self.conn, "Elevenlabs.io", new_name="ElevenLabs")
        self.pay("OPENAI *CHATGPT", 2000, "2026-08-05")
        self.pay("LINKEDIN", 5784, "2026-09-15")
        before = self.entries()
        with FakeBroker() as b:
            out = call("rename_vendors_to_invoice_names", {})
        self.assertEqual(len(b.deposits), 1)
        self.assertEqual(b.deposits[0]["slot"], "results")
        self.assertEqual(views.unesc(b.deposits[0]["value"]),
                         "Renamed 2 vendors to the name on their invoice. 1 keeps the bank "
                         "name: the invoice name belongs to another vendor (Ryanair Mtw0). "
                         "Kept the names you gave: ElevenLabs. No invoice yet: LINKEDIN, "
                         "OPENAI *CHATGPT.")
        self.assertEqual(out["renamed"], 2)
        self.assertIn("<silent/>", out["note"])
        self.assertIsNotNone(self.entry(INFO_ISSUER))
        self.assertIsNotNone(self.entry("Ryanair DAC"))
        self.assertIsNone(self.entry("Ryanair Mtw0"))
        self.assertIsNotNone(self.entry("ElevenLabs"))
        # Ryanair H2n0 had no entry: one was made; nothing else was created
        self.assertEqual(self.entries(), before + 1)

    def test_nothing_to_do_still_says_so(self):
        self.pay("Zapier", 100, "2026-08-03")
        with FakeBroker() as b:
            call("rename_vendors_to_invoice_names", {})
        self.assertEqual(b.deposits[0]["value"],
                         "No vendor needed a new name. No invoice yet: Zapier.")

    def test_a_refused_rename_leaves_nothing_half_done(self):
        # the second Ryanair is refused after the first took the name; its savepoint rolls
        # back whatever it wrote, the first's rename stays
        for t in ("Ryanair H2n0", "Ryanair Mtw0"):
            p = self.pay(t, 5000, "2026-07-03")
            self.matched_to(p, "Ryanair DAC", amount_minor=5000, document_date="2026-07-03")
            self.kb(t)
        with FakeBroker():
            call("rename_vendors_to_invoice_names", {})
        names = sorted(r[0] for r in self.conn.execute("SELECT name FROM counterparties"))
        self.assertEqual(names, ["Ryanair DAC", "Ryanair Mtw0"])
        self.assertEqual(json.loads(self.entry("Ryanair Mtw0")["patterns_json"]), [])


class Skill(_Case):
    def naming(self):
        start = SKILL.index("## Naming a vendor")
        return " ".join(SKILL[start:SKILL.index("## Asks", start)].split())

    def test_the_skill_names_the_two_tools_and_adds_nothing(self):
        s = self.naming()
        for phrase in ("`rename_vendor(vendor=<their words for it>, new_name=<the name they "
                       "give; left out for the name on its invoice>)`",
                       "`rename_vendors_to_invoice_names()`, once",
                       "add nothing, your whole reply is `<silent/>`",
                       "Never rename unasked."):
            self.assertIn(phrase, s, phrase)
        self.assertNotIn("upsert_counterparty", s)

    def test_the_desk_skill_stays_within_its_budget(self):
        self.assertLessEqual(len(SKILL), 10_000)
