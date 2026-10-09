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
        self.assertIn("also when it already shows that name: a vendor not `named` only borrows "
                      "its invoice's name until renamed.", s)
        import tools  # noqa: F401
        desc = {n: " ".join(qa_server.TOOLS[n]["description"].split())
                for n in ("rename_vendor", "list_vendors")}
        self.assertIn("also when its cards already show that name", desc["rename_vendor"])
        self.assertIn("a rename asked for still keeps it", desc["list_vendors"])
