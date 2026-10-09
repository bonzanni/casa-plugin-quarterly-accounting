"""#90: the operator merges one vendor into another through the desk. merge_vendors finds both
vendors from the operator's words (refusing, with the candidates, when they fit several), makes
every text X stood for one of Y's bank texts in ONE transaction (X's payments, documents, rules
and a running job's work resolve to Y), pins Y's name, and posts "<X> is now part of <Y>."
before the merged vendor's card; the model adds nothing."""
import json

from tests.fakebroker import FakeBroker
from tests.test_issue_89 import SKILL, _Case, call
import db
import kb
import loop
import views
import work


class _Merge(_Case):
    def ryanair(self):
        """s74: H2n0's invoice name made it the entry "Ryanair DAC"; Mtw0's invoice prints
        the same name, so it kept its bank text."""
        pids = {}
        for t, day in (("Ryanair H2n0", "2026-07-03"), ("Ryanair Mtw0", "2026-08-03")):
            pids[t] = self.pay(t, 5000, day)
            self.matched_to(pids[t], "Ryanair DAC", amount_minor=5000, document_date=day)
        kb.upsert_counterparty(self.conn, "Ryanair H2n0", new_name="Ryanair DAC")
        return pids

    def merge(self, vendor, into):
        with FakeBroker() as b:
            out = call("merge_vendors", {"vendor": vendor, "into": into})
        return out, b.deposits

    def refused(self, vendor, into):
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM counterparties")]
        with FakeBroker() as b:
            out = call("merge_vendors", {"vendor": vendor, "into": into})
        self.assertEqual(b.deposits, [])
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM counterparties")],
                         before)
        return out["refused"]

    def vendors(self):
        with db.tx(self.conn):
            return {v["name"]: v for v in work.vendors(self.conn)}


class Merge(_Merge):
    def test_s74_ryanair_joins_the_entry_with_one_line_and_the_card(self):
        pids = self.ryanair()
        out, deps = self.merge("Ryanair Mtw0", "Ryanair DAC")
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["slot"], "view")
        value = json.loads(deps[0]["value"])
        self.assertEqual([views.unesc(p) for p in value["pages"]],
                         ["Ryanair Mtw0 is now part of Ryanair DAC."])
        self.assertIn("Ryanair DAC", views.unesc(value["text"]))
        self.assertNotIn("Mtw0", views.unesc(value["text"]))
        card = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                            (out["render_id"],)).fetchone()[0])
        self.assertEqual(card.get("pid"), max(pids.values()))
        self.assertIn("<silent/>", out["note"])
        self.assertNotIn("Ryanair DAC", json.dumps(out))
        vs = self.vendors()
        self.assertEqual(list(vs), ["Ryanair DAC"])
        self.assertEqual(vs["Ryanair DAC"]["texts"], ["Ryanair H2n0", "Ryanair Mtw0"])
        self.assertEqual(sorted(vs["Ryanair DAC"]["pids"]), sorted(pids.values()))
        self.assertEqual(self.entries(), 1)
        listed = call("list_vendors", {})["vendors"]
        self.assertEqual([(v["name"], v["shown"], v["named"]) for v in listed],
                         [("Ryanair DAC", "Ryanair DAC", True)])

    def test_into_an_entry_with_no_payment_of_its_own(self):
        self.pay("Ryanair Mtw0", 5000, "2026-08-03")
        self.kb("Ryanair DAC")
        out, deps = self.merge("mtw0", "ryanair dac")
        self.assertEqual(views.unesc(json.loads(deps[0]["value"])["pages"][0]),
                         "Ryanair Mtw0 is now part of Ryanair DAC.")
        e = self.entry("Ryanair DAC")
        self.assertEqual(json.loads(e["patterns_json"]), ["Ryanair Mtw0"])
        self.assertIsNotNone(e["named_at"])
        self.assertEqual(self.entries(), 1)

    def test_two_bank_texts_without_entries_make_one(self):
        self.pay("Anthropic", 9000, "2026-07-10")
        self.pay("Claude.ai Subscription", 2000, "2026-08-10")
        self.merge("anthropic", "claude.ai")
        e = self.entry("Claude.ai Subscription")
        self.assertEqual(json.loads(e["patterns_json"]), ["Anthropic"])
        self.assertEqual(list(self.vendors()), ["Claude.ai Subscription"])

    def test_x_matched_invoice_never_names_the_merged_vendor(self):
        # d1 (Astra S2): Y's name is pinned, so X's matched invoice's issuer does not show
        x = self.pay("Megekko via Mollie", 1300, "2026-07-05")
        self.matched_to(x, "Megekko B.V.", amount_minor=1300, document_date="2026-07-05")
        self.pay("Megekko", 4000, "2026-08-05")
        self.kb("Megekko")
        self.pay("Megekko via Mollie", 2200, "2026-09-05")
        out, deps = self.merge("via mollie", "Megekko")
        value = json.loads(deps[0]["value"])
        self.assertNotIn("Megekko B.V.", views.unesc(value["text"]))
        listed = call("list_vendors", {})["vendors"]
        self.assertEqual([(v["name"], v["shown"]) for v in listed], [("Megekko", "Megekko")])

    def test_x_rules_fill_what_y_lacks_and_y_keeps_its_own(self):
        self.pay("Aws Emea", 5000, "2026-07-03")
        self.pay("Amazon Web Services", 7000, "2026-08-03")
        self.kb("Aws Emea", source="portal", document_link="https://example.invalid/x",
                window_days=40, hint_sender="aws@example.invalid", hint_subject="Invoice")
        self.kb("Amazon Web Services", source="email", window_days=12)
        with db.tx(self.conn):
            self.conn.execute("UPDATE counterparties SET exp_kind='none', exp_author='operator'"
                              " WHERE name='Aws Emea'")
        self.merge("aws emea", "amazon web services")
        e = self.entry("Amazon Web Services")
        self.assertEqual((e["source"], e["document_link"]), ("email", None))   # y's own
        self.assertEqual((e["exp_kind"], e["exp_author"]), ("none", "operator"))
        self.assertEqual((e["hint_sender"], e["hint_subject"]),
                         ("aws@example.invalid", "Invoice"))
        self.assertEqual(e["window_days"], 40)
        self.assertEqual(self.entries(), 1)
        # x's ruling now applies to y's payments, through the one entry
        for pid in self.vendors()["Amazon Web Services"]["pids"]:
            self.assertEqual(self.conn.execute("SELECT exp_kind FROM projections WHERE pid=?",
                                               (pid,)).fetchone()[0], "none")

    def test_x_documents_and_work_still_resolve_to_the_merged_vendor(self):
        self.ryanair()
        self.merge("Ryanair Mtw0", "Ryanair DAC")
        self.assertTrue(kb.same_vendor(self.conn, "Ryanair Mtw0", "Ryanair DAC"))
        # a document filed under X's old name: a candidate of the merged vendor's payment
        doc = self.doc(issuer="Ryanair DAC", counterparty="Ryanair Mtw0", vendor="Ryanair Mtw0",
                       amount_minor=None, currency=None, document_date="2026-09-03")
        pid = self.pay("Ryanair Mtw0", 6100, "2026-09-03")
        with db.tx(self.conn):
            row = self.conn.execute("SELECT b.* FROM projections p JOIN bank_rows b ON"
                                    " b.row_id=p.dest_row_id WHERE p.pid=?", (pid,)).fetchone()
            self.assertEqual(loop.vendor_of(self.conn, dict(row)), "Ryanair DAC")
            got = [c["doc_id"] for c in loop.candidates(self.conn, pid, dict(row),
                                                        "Ryanair DAC")]
        self.assertIn(doc, got)

    def test_a_payment_shown_before_the_merge_changes_revision(self):
        pids = self.ryanair()
        rev = self.conn.execute("SELECT revision FROM projections WHERE pid=?",
                                (pids["Ryanair Mtw0"],)).fetchone()[0]
        self.merge("Ryanair Mtw0", "Ryanair DAC")
        self.assertGreater(self.conn.execute("SELECT revision FROM projections WHERE pid=?",
                                             (pids["Ryanair Mtw0"],)).fetchone()[0], rev)


class Refusals(_Merge):
    def test_several_fit_names_them_and_merges_nothing(self):
        for t in ("Ryanair H2n0", "Ryanair Mtw0", "Ryanair Zgx0"):
            self.pay(t, 5000, "2026-08-03")
        msg = self.refused("ryanair", "Ryanair Zgx0")
        self.assertIn("fits 3 vendors", msg)
        self.assertIn("Nothing was merged", msg)

    def test_the_target_is_named_by_its_name_before_an_invoice_that_prints_it(self):
        # Mtw0 and Zgx0 both print "Ryanair DAC" on their invoices; the entry's name wins
        self.ryanair()
        z = self.pay("Ryanair Zgx0", 5000, "2026-09-03")
        self.matched_to(z, "Ryanair DAC", amount_minor=5000, document_date="2026-09-03")
        self.merge("Ryanair Zgx0", "Ryanair DAC")
        self.assertIn("Ryanair Zgx0", json.loads(self.entry("Ryanair DAC")["patterns_json"]))

    def test_one_vendor_already(self):
        self.ryanair()
        self.assertIn("already one vendor", self.refused("Ryanair H2n0", "ryanair dac"))

    def test_x_own_invoice_name_is_no_target_to_guess_from(self):
        # r1 (Astra S1): "into" = X's invoice name, borne by no other vendor: refused, never a
        # containment match on "Megekko B.V. Hardware"
        x = self.pay("Megekko via Mollie", 1300, "2026-07-05")
        self.matched_to(x, "Megekko B.V.", amount_minor=1300, document_date="2026-07-05")
        self.pay("Megekko B.V. Hardware", 4000, "2026-08-05")
        self.assertIn("no other vendor is called", self.refused("via mollie", "Megekko B.V."))

    def test_no_such_vendor(self):
        self.pay("Zapier", 100, "2026-08-03")
        self.assertIn("nothing was merged", self.refused("zapier", "Make.com"))


class Skill(_Case):
    def test_the_skill_names_merge_vendors(self):
        start = SKILL.index("## Naming a vendor")
        s = " ".join(SKILL[start:SKILL.index("## Asks", start)].split())
        self.assertIn('("merge X into Y"): `merge_vendors(vendor=<X>, into=<Y>)`', s)
        self.assertIn("Never rename or merge unasked.", s)


class PlainWhenNothingToAct(_Merge):
    def test_a_merged_card_with_nothing_to_act_on_goes_plain(self):
        # #93: the merged vendor's latest payment was exempted by the operator's tap: its
        # card has nothing to tap, so the line and the card go plain, through post_results
        import posting, qa_server, tools  # noqa: F401
        self.pay("Ryanair Mtw0", 5000, "2026-08-03")
        y = self.pay("Ryanair DAC", 6000, "2026-09-03")
        self.kb("Ryanair DAC")
        with FakeBroker() as b:
            posting.show_view(self.conn, view="item", pid=y)
        tap = next(x["call"] for x in b.proposal()["buttons"]
                   if x["label"] == "No invoice needed")
        qa_server.TOOLS[tap["tool"]]["fn"](dict(tap["arguments"]))
        with FakeBroker() as b:
            out = call("merge_vendors", {"vendor": "Ryanair Mtw0", "into": "Ryanair DAC"})
        self.assertEqual((out["view"], b.deposits), (None, []))
        self.assertEqual(len(out["post"]), 2)              # the line, then the card
        line = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (out["post"][0][0],)).fetchone()[0]
        self.assertEqual(views.unesc(line), "Ryanair Mtw0 is now part of Ryanair DAC.")
