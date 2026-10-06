# tests/test_wave_f.py
"""Fix wave F (the final whole-branch review's findings), at the tool layer:
every call goes through qa_server.handle, as Casa makes it."""
import datetime as dt
import json
import unittest
from unittest import mock

from tests._base import StoreCase  # noqa: F401
from tests.test_tools import ToolCase, _json, _text, _tool
import db  # noqa: E402
import views  # noqa: E402

AUTUMN = dt.datetime(2026, 10, 5, 9, 0, tzinfo=dt.timezone.utc)


class TestQuarterWords(ToolCase):
    """(3) "give me Q3": the model passes the operator's words; a quarter tool
    takes "Q3", "Q3 2026" and "2026-Q3", and anything else is a refusal in
    words, never an error."""
    def setUp(self):
        super().setUp()
        clock = mock.patch.object(db, "_clock", lambda: AUTUMN)
        clock.start()
        self.addCleanup(clock.stop)
        self.bind()
        self.pass_()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)

    def test_a_view_of_q3(self):
        r = _json("build_review", view="quarter", quarter="Q3")
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                             (r["render_id"],)).fetchone()[0])
        self.assertEqual(scope["quarter"], "2026-Q3")

    def test_the_state_of_q3(self):
        out = _json("list_quarter_state", quarter="Q3")
        self.assertEqual((out["quarter"], [d["pid"] for d in out["items"]]), ("2026-Q3", [self.pid]))

    def test_anything_else_is_a_refusal_in_words_never_an_error(self):
        calls = [("build_review", {"view": "quarter", "quarter": "the third quarter"}),
                 ("list_quarter_state", {"quarter": "Q5"}),
                 ("build_review", {"view": "quarter", "quarter": 3})]
        for name, args in calls:
            res = _tool(name, **args)
            self.assertNotIn("isError", res, (name, args))
            text = res["content"][0]["text"]
            self.assertTrue(text.startswith("refused: "), (name, args, text))
            self.assertIn("2026-Q3", text, (name, args, text))      # it shows the format

    def test_the_schemas_show_the_format(self):
        import qa_server
        for name, arg in (("build_review", "quarter"), ("get_package", "quarter"),
                          ("list_quarter_state", "quarter")):
            desc = qa_server.TOOLS[name]["schema"]["properties"][arg].get("description", "")
            self.assertIn("YYYY-Qn, e.g. 2026-Q3 (Qn and Qn YYYY accepted)", desc, name)


class TestTriageListing(ToolCase):
    """(2b) list_quarter_state(triage=true) no longer hands every open payment of
    every quarter to the specialist: only fresh ones by default, at most `limit`."""
    def setUp(self):
        super().setUp()
        self.bind()
        self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(imported_at, rows, max_row_id)"
                              " VALUES (?, 0, 0)", (db.now(),))
        self.pids = []
        for rid in (1, 2, 3):
            self.row(rid, booking_date=f"2026-07-0{rid}", value_date=f"2026-07-0{rid}")
            pid = self.lineage_for(rid)
            self.classify(pid, {"software"})
            self.pids.append(pid)
        with db.tx(self.conn):                  # a newer import: every read is stale again
            self.conn.execute("INSERT INTO snapshots(imported_at, rows, max_row_id)"
                              " VALUES (?, 0, 0)", (db.now(),))
        for pid in self.pids[:2]:               # the sweep re-read two of them
            self.classify(pid, {"software"})
        for pid in self.pids:
            self.settle(pid)

    def test_fresh_only_and_capped_by_default(self):
        out = _json("list_quarter_state", triage=True)
        self.assertEqual([d["pid"] for d in out["triage"]], self.pids[:2])
        self.assertEqual((out["not_fresh"], out["truncated"], out["remaining"]), (1, False, 0))
        out = _json("list_quarter_state", triage=True, limit=1)
        self.assertEqual([d["pid"] for d in out["triage"]], self.pids[:1])
        self.assertEqual((out["total"], out["truncated"], out["remaining"]), (2, True, 1))

    def test_fresh_only_false_lists_the_unread_too(self):
        out = _json("list_quarter_state", triage=True, fresh_only=False)
        self.assertEqual([d["pid"] for d in out["triage"]], self.pids)
        self.assertEqual(out["not_fresh"], 0)

    def test_the_schema_offers_both(self):
        import qa_server
        schema = qa_server.TOOLS["list_quarter_state"]["schema"]["properties"]
        self.assertIn("fresh_only", schema)
        self.assertIn("limit", schema)


class TestSendItAgainAfterATimeout(ToolCase):
    """(4) "send it again" right after a timed-out send: record_delivery(uncertain)
    returns the rendering that offers exactly that package; once it is sent and
    marked delivered, "send it again" stages exactly that file."""
    def setUp(self):
        super().setUp()
        import package
        self.bind()
        self.pass_()
        self.row(1)
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        self.other = package.build_quarterly_package(self.conn, "2026-Q3")
        self.pkg = package.build_quarterly_package(self.conn, "2026-Q3")

    def test_timeout_offer_mark_delivered_then_send_it_again(self):
        import os
        import pathlib
        staged = _json("stage_for_delivery", channel="telegram", package_id=self.pkg["package_id"])
        out = _json("record_delivery", delivery_id=staged["delivery_id"], outcome="uncertain")
        speak = out["speak"]
        self.assertEqual(speak["text"], f"{views.field(self.pkg['filename'])} may not have arrived —\n"
                                        'say "send it again".')
        for f in os.listdir(self.outbox):          # Casa consumed the outbox copy on send
            os.unlink(self.outbox / f)
        _json("mark_rendering_delivered", render_id=speak["render_id"])
        # S7 §6.3/§8: "send it again" is a direct — propose_reading returns it, posts nothing
        self.assertIn("resend", _json("propose_reading", text="send it again")["instructions"])
        again = _json("stage_for_delivery", channel="telegram", resend=True)
        self.assertEqual(again["filename"], self.pkg["filename"])
        self.assertNotEqual(again["path"], staged["path"])          # a path of its own
        self.assertEqual(pathlib.Path(again["path"]).read_bytes(),
                         pathlib.Path(self.pkg["path"]).read_bytes())

    def test_a_delivered_send_offers_nothing_and_a_failed_one_offers_it_again(self):
        # issue #2: a send that did not go out is a package notice, offered like a timeout
        import os
        # two packages: once a package arrived, a failed extra copy of it offers nothing
        # (TestAPackageThatArrivedIsNeverOfferedAgain), so the failure is another package's
        for outcome, pkg in (("delivered", self.other), ("failed", self.pkg)):
            staged = _json("stage_for_delivery", channel="telegram",
                           package_id=pkg["package_id"])
            if outcome == "delivered":              # r3 #2: posted first
                from tests.fakebroker import FakeBroker
                with FakeBroker():
                    _json("post_package", delivery_id=staged["delivery_id"])
            out = _json("record_delivery", delivery_id=staged["delivery_id"], outcome=outcome)
            for f in os.listdir(self.outbox):
                os.unlink(self.outbox / f)
            if outcome == "delivered":
                self.assertIsNone(out.get("speak"), outcome)
                continue
            self.assertEqual(out["speak"]["text"], "The Q3 2026 package didn't go out. Say "
                                                   '"send it again" and I\'ll\nsend it.')
            scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                                 " render_id=?",
                                                 (out["speak"]["render_id"],)).fetchone()[0])
            self.assertEqual(scope["offers"], [self.pkg["package_id"]])


if __name__ == "__main__":
    unittest.main()
