# tests/test_s7_accounts.py
"""S7 §11/§7.6: the account question is a proposal; at most five choices a page, each
button carries only (choice, key); the account id and label are frozen under the key;
hostile labels reach only the body."""
from tests._base import StoreCase
from tests.fakebroker import FakeBroker, arguments_ok

LABELS = ["Zakelijk <B.V.>", "www.bank.example", "x\x01y", "L" * 4050, "Ops", "Tax", "Payroll"]


class Accounts(StoreCase):
    def setUp(self):
        super().setUp()
        self.accounts_probe([{"account_id": f"acc<{i}>", "category": "company", "label": l}
                             for i, l in enumerate(LABELS)])

    def propose(self, after=0):
        import posting
        with FakeBroker() as b:
            out = posting.propose_account(self.conn, after=after)
        return out, b.proposal()

    def test_five_a_page_then_more(self):
        out, p = self.propose()
        self.assertEqual([b["label"] for b in p["buttons"]],
                         [f"Account {i}" for i in range(1, 6)] + ["More"])
        self.assertTrue(out["more"])
        for b in p["buttons"]:
            self.assertIsNone(arguments_ok(b["call"]["arguments"]))
            self.assertEqual(set(b["call"]["arguments"]) - {"choice", "key", "after"}, set())
        self.assertLessEqual(len(p["text"]), 4000)
        self.assertNotIn("\x01", p["text"])
        _, p2 = self.propose(after=1)
        self.assertEqual([b["label"] for b in p2["buttons"]], ["Account 1", "Account 2"])

    def test_a_tap_binds_the_frozen_account(self):
        import tools, qa_server  # noqa: F401
        _, p = self.propose()
        call = p["buttons"][1]["call"]
        rec = qa_server.TOOLS["bind_account"]["fn"](call["arguments"])
        self.assertIn("www", rec["receipt"])      # escaped in the body, shown as text
        self.assertEqual(self.conn.execute("SELECT account_id FROM binding").fetchone()[0],
                         "acc<1>")
        again = qa_server.TOOLS["bind_account"]["fn"](p["buttons"][2]["call"]["arguments"])
        self.assertIn("no longer applies", again["receipt"])

    def test_bind_account_without_the_key_binds_nothing(self):
        import tools, qa_server  # noqa: F401
        rec = qa_server.TOOLS["bind_account"]["fn"]({"choice": 1, "key": "0" * 32})
        self.assertIn("no longer applies", rec["receipt"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM binding").fetchone())
