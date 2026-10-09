"""#92: "call Informatique by the name on its invoice" did nothing live, because the vendor's
cards already showed its invoice's name and Finance read that as done. A vendor that is not
`named` only borrows its invoice's name; a rename asked for still pins it (named_at). Fix =
steering: the desk skill and the tool descriptions say so."""
from tests.fakebroker import FakeBroker
from tests.test_issue_89 import INFO, INFO_ISSUER, SKILL, _Case, call
import qa_server


class ShownIsNotNamed(_Case):
    def vendor(self):
        return next(v for v in call("list_vendors", {})["vendors"] if v["name"] == INFO)

    def test_a_shown_invoice_name_is_not_a_given_name_and_the_rename_still_pins_it(self):
        self.informatique()
        v = self.vendor()
        self.assertEqual((v["shown"], v["named"]), (INFO_ISSUER, False))
        with FakeBroker() as b:
            call("rename_vendor", {"vendor": "Informatique"})
        self.assertEqual(len(b.deposits), 1)
        self.assertIsNotNone(self.entry(INFO_ISSUER)["named_at"])
        after = next(v for v in call("list_vendors", {})["vendors"]
                     if v["name"] == INFO_ISSUER)
        self.assertTrue(after["named"])

    def test_the_skill_and_the_tools_say_a_shown_name_is_still_renamed(self):
        start = SKILL.index("## Naming a vendor")
        s = " ".join(SKILL[start:SKILL.index("## Asks", start)].split())
        self.assertIn("also when it already shows that name (the rename pins it).", s)
        import tools  # noqa: F401
        desc = {n: " ".join(qa_server.TOOLS[n]["description"].split())
                for n in ("rename_vendor", "list_vendors")}
        self.assertIn("also when its cards already show that name: the rename pins it", desc["rename_vendor"])
        self.assertIn("when false, a rename asked for still runs", desc["list_vendors"])

    def test_asked_for_the_name_it_already_has_the_name_is_pinned(self):
        # r1 (Astra S2): the invoice prints the stored name itself; the rename still pins it
        done = self.pay("Acme Tools", 5000, "2026-07-20")
        self.matched_to(done, "Acme Tools", amount_minor=5000, document_date="2026-07-20")
        with FakeBroker() as b:
            call("rename_vendor", {"vendor": "acme"})
        self.assertEqual(len(b.deposits), 1)
        self.assertIn("already has that name", b.deposits[0]["value"])
        self.assertIsNotNone(self.entry("Acme Tools")["named_at"])
        with FakeBroker():
            call("rename_vendor", {"vendor": "acme"})       # a second time: nothing new
        self.assertEqual(self.entries(), 1)
