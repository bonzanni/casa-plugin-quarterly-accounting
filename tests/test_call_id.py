"""A tool call's id, read from the request's metadata, is kept for that call only, and every
deposit made during the call carries it as `tool_use_id`. Without an id the deposit is the
request it always was."""
from tests._base import StoreCase
from tests.fakebroker import FakeBroker

META_KEY = "claudecode/toolUseId"


class CallId(StoreCase):
    def call(self, fn, meta=None):
        """Handle one tools/call of a throwaway tool `fn`; return the response."""
        import qa_server
        name = "test_call_id_probe"
        qa_server.TOOLS[name] = {"description": "", "schema": {}, "fn": fn}
        self.addCleanup(qa_server.TOOLS.pop, name, None)
        params = {"name": name, "arguments": {}}
        if meta is not None:
            params["_meta"] = meta
        return qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": params})

    def test_the_server_keeps_the_calls_id_for_the_call_only(self):
        import casa_broker
        seen = []
        resp = self.call(lambda args: seen.append(casa_broker.CALL_ID) or {"ok": True},
                         {META_KEY: "toolu_x"})
        self.assertNotIn("isError", resp["result"])
        self.assertEqual(seen, ["toolu_x"])
        self.assertIsNone(casa_broker.CALL_ID)

        def raises(args):
            seen.append(casa_broker.CALL_ID)
            raise RuntimeError("boom")
        resp = self.call(raises, {META_KEY: "toolu_y"})
        self.assertTrue(resp["result"]["isError"])
        self.assertEqual(seen, ["toolu_x", "toolu_y"])
        self.assertIsNone(casa_broker.CALL_ID)

    def test_a_bad_id_is_ignored(self):
        import casa_broker
        for meta in ({META_KEY: 42}, {META_KEY: ""}, {META_KEY: "t" * 200},
                     {}, None, "not an object"):
            seen = []
            resp = self.call(lambda args: seen.append(casa_broker.CALL_ID) or {"ok": True},
                             meta)
            self.assertNotIn("isError", resp["result"], meta)
            self.assertEqual(seen, [None], meta)
            self.assertIsNone(casa_broker.CALL_ID)

    def test_a_deposit_carries_the_id(self):
        import casa_broker
        with FakeBroker() as b:
            self.call(lambda args: {"view": casa_broker.deposit("view", "x")},
                      {META_KEY: "toolu_x"})
        self.assertEqual(len(b.deposits), 1)
        self.assertEqual(b.deposits[0],
                         {"client": "client-1", "slot": "view", "value": "x",
                          "tool_use_id": "toolu_x"})

    def test_a_deposit_without_an_id_is_todays_request(self):
        import casa_broker
        with FakeBroker() as b:
            self.call(lambda args: {"view": casa_broker.deposit("view", "x")})
            casa_broker.deposit("view", "y")
        self.assertEqual(b.deposits, [{"client": "client-1", "slot": "view", "value": "x"},
                                      {"client": "client-1", "slot": "view", "value": "y"}])
