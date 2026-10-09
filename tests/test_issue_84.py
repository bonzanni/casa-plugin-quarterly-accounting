"""#84 (the ruling on #77 item 3, A + C): the operator renames a vendor through the desk — a
literal name, the name on its invoice, or the invoice names for all vendors. A vendor whose
knowledge-base entry already exists is renamed in place (upsert_counterparty's new_name), and
list_vendors shows the model every vendor with its current name and its matched invoice's
issuer."""
import json

from tests.test_issues_57_59 import _Q3
from tests._base import apply_now, untag
import db
import kb
import lineage
import loop
import work


class Rename(_Q3):
    RAW = "Elevenlabs.io"

    def entry(self):
        rows = self.conn.execute("SELECT * FROM counterparties").fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_an_existing_entry_is_renamed_in_place_and_keeps_what_it_knows(self):
        self.kb(self.RAW, hint_sender="invoice@example.com", hint_subject="Your receipt",
                window_days=12)
        p = self.pay(self.RAW, 2200, "2026-08-03")
        with db.tx(self.conn):
            kb.set_expectation_in_tx(self.conn, scope_type="counterparty", scope=self.RAW,
                                     kind="receipt", tier="optional", author="specialist")
        before = self.entry()
        kb.upsert_counterparty(self.conn, self.RAW, new_name="ElevenLabs")
        after = self.entry()
        self.assertEqual(after["cp_id"], before["cp_id"])
        self.assertEqual(after["name"], "ElevenLabs")
        self.assertEqual(json.loads(after["patterns_json"]), [self.RAW])
        for k in ("hint_sender", "hint_subject", "window_days", "exp_kind", "exp_tier",
                  "exp_author"):
            self.assertEqual(after[k], before[k], k)
        # every text that found the entry still finds it
        for t in (self.RAW, "elevenlabs.io", "ElevenLabs"):
            self.assertEqual(kb.counterparty_for(self.conn, t)["cp_id"], before["cp_id"], t)
        with db.tx(self.conn):
            self.assertEqual(work.describe(self.conn, p)["readable"], "ElevenLabs")

    def test_a_bank_text_names_the_entry_to_rename(self):
        kb.upsert_counterparty(self.conn, "Google Workspace", patterns=["GOOGLE*WS"])
        kb.upsert_counterparty(self.conn, "google*ws", new_name="Google")
        e = self.entry()
        self.assertEqual(e["name"], "Google")
        self.assertEqual(sorted(json.loads(e["patterns_json"])),
                         ["GOOGLE*WS", "Google Workspace"])

    def test_no_entry_yet_makes_one_under_the_new_name(self):
        self.pay("LINKEDIN", 5784, "2026-09-15")
        kb.upsert_counterparty(self.conn, "LINKEDIN", new_name="LinkedIn")
        e = self.entry()
        self.assertEqual(e["name"], "LinkedIn")
        self.assertEqual(kb.counterparty_for(self.conn, "LINKEDIN")["cp_id"], e["cp_id"])

    def test_a_name_differing_only_in_case_wins_over_the_invoice_issuer(self):
        # the ruled phrasing "call LINKEDIN 'LinkedIn'": a case-blind comparison took the
        # given name for the bank's own text and the matched invoice's issuer won
        done = self.pay("LINKEDIN", 5784, "2026-07-15")
        self.matched_to(done, "LinkedIn Ireland Unlimited Company", amount_minor=5784,
                        document_date="2026-07-15")
        self.pay("LINKEDIN", 5784, "2026-08-15")
        self.kb("LINKEDIN")
        kb.upsert_counterparty(self.conn, "LINKEDIN", new_name="LinkedIn")
        self.assertTrue(untag(self.card()["text"]).split("\n")[0].endswith("· LinkedIn"))

    def test_renaming_back_to_an_earlier_name_shows_it(self):
        # d1 (Astra S2): the earlier given name stayed a pattern, so it read as a bank text
        # and the matched invoice's issuer took over on the other payments
        a = self.pay("GOOGLE*PAY", 1200, "2026-07-03")
        self.matched_to(a, "Google Cloud EMEA", amount_minor=1200, document_date="2026-07-03")
        b = self.pay("GOOGLE*SECOND", 1300, "2026-08-03")
        kb.upsert_counterparty(self.conn, "Google Workspace",
                               patterns=["GOOGLE*PAY", "GOOGLE*SECOND"])
        kb.upsert_counterparty(self.conn, "Google Workspace", new_name="Google Services")
        kb.upsert_counterparty(self.conn, "Google Services", new_name="Google Workspace")
        with db.tx(self.conn):
            self.assertEqual([work.describe(self.conn, p)["readable"] for p in (a, b)],
                             ["Google Workspace", "Google Workspace"])
        for t in ("GOOGLE*PAY", "GOOGLE*SECOND", "Google Services", "Google Workspace"):
            self.assertEqual(kb.counterparty_for(self.conn, t)["name"], "Google Workspace")

    def test_a_rename_onto_another_vendors_name_is_refused_and_changes_nothing(self):
        kb.upsert_counterparty(self.conn, "Suno", patterns=["SUNO INC."])
        kb.upsert_counterparty(self.conn, self.RAW)
        with self.assertRaises(db.Refusal) as cm:
            kb.upsert_counterparty(self.conn, self.RAW, new_name="suno")
        self.assertIn("new_name", str(cm.exception))
        self.assertEqual(sorted(r[0] for r in self.conn.execute(
            "SELECT name FROM counterparties")), [self.RAW, "Suno"])

    def test_a_document_filed_under_the_old_name_stays_the_vendors(self):
        # the vendor group a document was filed under (documents.vendor) keeps the old name
        p = self.pay(self.RAW, 2200, "2026-08-03")
        d = self.doc(issuer="Eleven Labs Inc.", amount_minor=None, currency=None,
                     document_date="2026-08-03", vendor=self.RAW)
        kb.upsert_counterparty(self.conn, self.RAW, new_name="ElevenLabs")
        with db.tx(self.conn):
            proj = self.conn.execute("SELECT * FROM projections WHERE pid=?", (p,)).fetchone()
            row = loop.lineage.live_row(self.conn, proj)
            vendor = loop.vendor_of(self.conn, row)
            self.assertEqual(vendor, "ElevenLabs")
            self.assertIn(d, [c["doc_id"] for c in loop.candidates(self.conn, p, row, vendor)])

    def test_a_reply_quoting_the_old_name_binds_after_the_rename(self):
        p = self.pay(self.RAW, 2200, "2026-08-03")
        self.kb(self.RAW)
        self.show(p)
        kb.upsert_counterparty(self.conn, self.RAW, new_name="ElevenLabs")
        out = apply_now(self.conn, f"the {self.RAW} one needs no invoice")
        # it binds to the payment by its old name; a renamed payee is a changed line (lineage
        # p6), so the first answer re-shows the card under its new name and applies nothing
        self.assertEqual(out["reshow"], [p])
        self.assertIn("ElevenLabs", out["receipt"])
        self.assertEqual(out["applied"], [])
        self.show(p)
        out = apply_now(self.conn, f"the {self.RAW} one needs no invoice")
        self.assertTrue(out["applied"], out)
        self.assertEqual(lineage.projection(self.conn, p)["status"], "exempt")

    def test_a_rename_with_no_entry_onto_another_vendors_name_is_refused(self):
        # r1 (Astra + Terra S1): with no entry for the bank text, the new name found the
        # other vendor's entry and merged the two (its ruling then covered both)
        kb.upsert_counterparty(self.conn, "Vendor A", patterns=["BANK A"])
        self.pay("BANK B", 2200, "2026-08-03")
        with self.assertRaises(db.Refusal):
            kb.upsert_counterparty(self.conn, "BANK B", new_name="vendor a")
        rows = self.conn.execute("SELECT name, patterns_json FROM counterparties").fetchall()
        self.assertEqual([(r[0], json.loads(r[1])) for r in rows], [("Vendor A", ["BANK A"])])

    def test_an_entry_nobody_renamed_reads_as_before_in_other_capitals(self):
        # r1 (Astra + Terra S2): the bank's "Linkedin" for an entry named "LINKEDIN" is the
        # bank's text, not a given name: the matched invoice's issuer names it
        self.kb("LINKEDIN")
        done = self.pay("LINKEDIN", 5784, "2026-07-15")
        self.matched_to(done, "LinkedIn Ireland Unlimited Company", amount_minor=5784,
                        document_date="2026-07-15")
        q = self.pay("Linkedin", 5784, "2026-08-15")
        with db.tx(self.conn):
            self.assertEqual(work.describe(self.conn, q)["readable"],
                             "LinkedIn Ireland Unlimited Company")

    def test_a_rename_onto_an_unlearned_vendors_bank_text_is_refused(self):
        # r2 (Astra S1): "BANK B" has no entry; renaming Vendor A to it made A take B's
        # payments (A's ruling then covered both)
        kb.upsert_counterparty(self.conn, "Vendor A", patterns=["BANK A"])
        self.pay("BANK A", 2100, "2026-08-02")
        self.pay("BANK B", 2200, "2026-08-03")
        with self.assertRaises(db.Refusal):
            kb.upsert_counterparty(self.conn, "Vendor A", new_name="bank b")
        self.assertEqual(kb.counterparty_for(self.conn, "Vendor A")["name"], "Vendor A")
        self.assertIsNone(kb.counterparty_for(self.conn, "BANK B"))

    def test_entries_nobody_renamed_read_as_in_0_11_12(self):
        # r2 (Astra + Terra S2): whatever capitals an entry's name and patterns carry, only a
        # rename makes its name win over the matched invoice's issuer
        for name, pats, text in (("LinkedIn", ["LINKEDIN"], "LINKEDIN"),
                                 ("LINKEDIN", ["Linkedin"], "Linkedin")):
            with db.tx(self.conn):
                self.conn.execute("DELETE FROM counterparties")
            self.kb(name, patterns=pats)
            done = self.pay(text, 5784, "2026-07-15")
            self.matched_to(done, "LinkedIn Ireland Unlimited Company", amount_minor=5784,
                            document_date="2026-07-15")
            q = self.pay(text, 5784, "2026-08-15")
            with db.tx(self.conn):
                self.assertEqual(work.describe(self.conn, q)["readable"],
                                 "LinkedIn Ireland Unlimited Company", name)


class ListVendors(_Q3):
    def test_every_vendor_once_with_its_invoice_name(self):
        done = self.pay("Aws Emea", 1200, "2026-07-03")
        self.matched_to(done, "Amazon Web Services EMEA SARL", amount_minor=1200,
                        document_date="2026-07-03")
        self.pay("Aws Emea", 1300, "2026-08-03")
        self.pay("OPENAI *CHATGPT", 2000, "2026-08-05")
        self.pay("LINKEDIN", 5784, "2026-09-15")
        kb.upsert_counterparty(self.conn, "LINKEDIN", new_name="LinkedIn")
        with db.tx(self.conn):
            out = work.list_vendors(self.conn)
        got = {v["name"]: v for v in out["vendors"]}
        self.assertEqual(sorted(got), ["Aws Emea", "LinkedIn", "OPENAI *CHATGPT"])
        self.assertEqual(got["Aws Emea"]["invoice_name"], "Amazon Web Services EMEA SARL")
        self.assertEqual(got["Aws Emea"]["payments"], 2)
        self.assertFalse(got["Aws Emea"]["named"])
        self.assertIsNone(got["OPENAI *CHATGPT"]["invoice_name"])
        self.assertTrue(got["LinkedIn"]["named"])
        self.assertEqual(got["LinkedIn"]["bank_texts"], ["LINKEDIN"])
        self.assertIsNone(out["next"])

    def test_pages_continue_after_the_last_name(self):
        for i in range(60):
            self.pay(f"Vendor with a rather long bank text number {i:03d}", 100 + i,
                     "2026-08-03")
        seen, after = [], None
        with db.tx(self.conn):
            while True:
                out = work.list_vendors(self.conn, after=after)
                seen += [v["name"] for v in out["vendors"]]
                if out["next"] is None:
                    break
                after = out["next"][0]
        self.assertEqual(len(seen), 60)
        self.assertEqual(len(set(seen)), 60)

    def test_a_long_name_is_listed_whole_and_renames(self):
        # r1 (Astra S2): a clipped name passed back made a second entry
        long = "A vendor whose bank text runs on " + "and on " * 15 + "until it ends"
        self.pay(long, 1300, "2026-08-03")
        self.kb(long)
        with db.tx(self.conn):
            v = work.list_vendors(self.conn)["vendors"][0]
        self.assertEqual(v["name"], long)
        kb.upsert_counterparty(self.conn, v["name"], new_name="Short")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0],
                         1)

    def test_the_tool_renames_and_lists(self):
        import qa_server
        import tools  # noqa: F401
        self.pay("Aws Emea", 1300, "2026-08-03")
        self.kb("Aws Emea")
        from tests.fakebroker import FakeBroker
        with FakeBroker():     # #89: a rename goes through rename_vendor, which posts it
            qa_server.TOOLS["rename_vendor"]["fn"](
                {"vendor": "Aws Emea", "new_name": "Amazon Web Services EMEA SARL"})
        out = qa_server.TOOLS["list_vendors"]["fn"]({})
        self.assertEqual([v["name"] for v in out["vendors"]], ["Amazon Web Services EMEA SARL"])


class Schema17(_Q3):
    def test_16_to_17_adds_named_at_and_keeps_the_entries(self):
        kb.upsert_counterparty(self.conn, "Zapier", patterns=["BCK*ZAPIER"])
        with db.tx(self.conn):
            self.conn.execute("ALTER TABLE counterparties DROP COLUMN named_at")
            self.conn.execute("UPDATE meta SET value='16' WHERE key='schema_version'")
        db.migrate(self.conn)
        r = self.conn.execute("SELECT name, named_at FROM counterparties").fetchall()
        self.assertEqual([tuple(x) for x in r], [("Zapier", None)])
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                         .fetchone()[0], "17")
