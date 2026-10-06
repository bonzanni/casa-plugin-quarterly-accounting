# tests/test_s7_broker.py
"""S7 §2/§3: a posting tool deposits its slot value with Casa's broker during the call and
returns the reference; a refusal or a refused deposit is the no-post shape (INV-PLUG-028),
never `refused: …` text Casa would withhold; a keyed handler's refusal is a receipt."""
import json
from tests._base import StoreCase
from tests.fakebroker import FakeBroker, arguments_ok


class Broker(StoreCase):
    def test_deposit_sends_only_the_given_members_and_returns_the_reference(self):
        import casa_broker
        with FakeBroker() as b:
            ref = casa_broker.deposit("view", '{"text":"x"}')
        self.assertRegex(ref, r"^casa-cap-[0-9a-f]{32}$")
        self.assertEqual(set(b.deposits[0]), {"client", "slot", "value"})
        with FakeBroker() as b:
            casa_broker.deposit("package", "/o/qa-1.zip", caption="c", kind="zip",
                                filename="books-2026-Q3.zip")
        self.assertEqual(b.deposits[0]["filename"], "books-2026-Q3.zip")
        self.assertEqual(b.deposits[0]["kind"], "zip")

    def test_a_refused_deposit_raises_casas_code_only(self):
        import casa_broker
        with FakeBroker() as b:
            b.refuse = "bad_proposal"
            with self.assertRaises(casa_broker.DepositFailed) as cm:
                casa_broker.deposit("view", "x")
        self.assertEqual(cm.exception.code, "bad_proposal")

    def test_no_broker_is_a_refusal_code(self):
        import casa_broker
        with self.assertRaises(casa_broker.DepositFailed) as cm:
            casa_broker.deposit("view", "x")
        self.assertEqual(cm.exception.code, "broker_env_missing")

    def test_capability_turns_refusals_into_the_no_post_shape(self):
        import casa_broker, db, tools

        @tools.capability("view")
        def refuses(args):
            raise db.Refusal("view is one of status, missing")

        @tools.capability("view")
        def fails(args):
            raise casa_broker.DepositFailed("bad_proposal")
        self.assertEqual(refuses({}), {"view": None, "refused": "view is one of status, missing"})
        out = fails({})
        self.assertIsNone(out["view"])
        self.assertIn("could not be posted", out["refused"])

    def test_keyed_turns_refusals_into_a_receipt(self):
        import db, tools

        @tools.keyed
        def refuses(args):
            raise db.Refusal("That button no longer applies.")
        self.assertEqual(refuses({}), {"receipt": "That button no longer applies."})

    def test_the_argument_grammar_copy(self):
        self.assertIsNone(arguments_ok({"render_id": "r12", "pid": 4, "after": [9]}))
        self.assertEqual(arguments_ok({"label": "a<b"}), "angle_bracket")
        self.assertEqual(arguments_ok({"x": 1.5}), "float")
        self.assertEqual(arguments_ok({"_k": 1}), "key")
