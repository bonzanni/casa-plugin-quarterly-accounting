# tests/test_s7_readings.py
"""S7 §8: typed words are read, never applied. A reading with writes is posted as a
proposal with Apply / Cancel; Apply commits exactly what it listed, all-or-nothing, and
only while every revision it read still holds."""
import json
from tests._base import StoreCase
from tests.fakebroker import FakeBroker


class Readings(StoreCase):
    def setUp(self):
        """Ruling F3: the sheet fixture's data sits in 2026-Q3, as do the views built here."""
        super().setUp()
        import datetime as dt
        cm = self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc))
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def propose(self, ops, quoted=None):
        """#121: the desk's operations (tests._base.as_ops shorthand)."""
        import posting
        from tests._base import as_ops
        with FakeBroker() as b:
            out = posting.propose_reading(self.conn, as_ops(ops), quoted)
        return out, (b.proposal() if b.deposits else None)

    def tap(self, prop, label):
        import tools, qa_server  # noqa: F401
        call = next(x for x in prop["buttons"] if x["label"] == label)["call"]
        return qa_server.TOOLS[call["tool"]]["fn"](call["arguments"])

    def operator_rows(self):
        return self.conn.execute("SELECT count(*) FROM log WHERE author='operator'").fetchone()[0]

    def test_a_typed_verdict_commits_nothing_until_apply(self):
        fx = self.sheet_fixture()
        out, prop = self.propose([("reject", fx["pid"])])
        self.assertRegex(out["reading"], r"^casa-cap-")
        self.assertTrue(prop["text"].lstrip("*").startswith("I read this as:"))
        self.assertIn("Remove the match for", prop["text"])
        self.assertEqual([b["label"] for b in prop["buttons"]], ["Apply", "Cancel"])
        self.assertEqual(prop["revision"], "reading")
        self.assertEqual(self.operator_rows(), 0)
        rec = self.tap(prop, "Apply")
        self.assertIn("Removed the match for", rec["receipt"])
        self.assertEqual(self.operator_rows(), 1)

    def test_cancel_applies_nothing_and_spends_the_key(self):
        import taps
        fx = self.sheet_fixture()
        _, prop = self.propose([("reject", fx["pid"])])
        self.assertIn("nothing was applied", self.tap(prop, "Cancel")["receipt"])
        # Ruling F4: a cancelled reading answers a later tap with the code's own words
        self.assertIn(taps.DONE["cancelled"], self.tap(prop, "Apply")["receipt"])
        self.assertEqual(self.operator_rows(), 0)

    def test_apply_after_the_payment_changed_applies_nothing(self):
        """Review Focus 2."""
        fx = self.sheet_fixture(guesses=2)
        _, prop = self.propose([("confirm", p) for p in fx["pids"]])
        self.rejudge(fx["pids"][0])
        rec = self.tap(prop, "Apply")
        self.assertEqual(rec["receipt"], __import__("taps").CHANGED)
        self.assertEqual(self.operator_rows(), 0)
        st = self.conn.execute("SELECT state FROM readings").fetchone()[0]
        self.assertEqual(st, "stale")

    def test_an_unchanged_multi_step_reading_applies(self):
        """Plan round 2, Astra S2: two steps where the second reads what the first
        wrote; nothing changes between reading and Apply — it applies."""
        fx = self.sheet_fixture()
        _, prop = self.propose([("confirm", fx["pid"]), ("look_again", fx["pid"])])
        rec = self.tap(prop, "Apply")
        self.assertIn("Confirmed", rec["receipt"])
        self.assertEqual(self.operator_rows(), 1)

    def test_a_newer_reading_makes_the_older_stale(self):
        fx = self.sheet_fixture()
        _, old = self.propose([("reject", fx["pid"])])
        _, new = self.propose([("confirm", fx["pid"])])
        self.assertIn("no longer applies", self.tap(old, "Apply")["receipt"])
        self.assertIn("Confirmed", self.tap(new, "Apply")["receipt"])

    def test_no_write_no_deposit(self):
        # #121: ops — a reading whose only outcome is a note (a confirm of a payment already
        # confirmed) posts nothing; the note is said
        fx = self.sheet_fixture()
        _, prop = self.propose([("confirm", fx["pid"])])
        self.tap(prop, "Apply")
        out, prop = self.propose([("confirm", fx["pid"])])
        self.assertIsNone(prop)
        self.assertIsNone(out["reading"])
        self.assertIn("was already fine.", out["say"])

    def test_quoted_binds_to_that_rendering(self):
        """Two sheets delivered; the operator swipe-replied "all good" on the OLDER one.
        The reading confirms the older sheet's guesses, not the newer one's."""
        import views
        import datetime as dt
        a = self.sheet_fixture(guesses=1)
        # #53: the tag is the composition second; the two sheets are composed seconds apart
        with self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, 1, tzinfo=dt.timezone.utc)):
            older = views.build_review(self.conn, view="check")
        views.mark_rendering_delivered(self.conn, older["render_id"])
        b = self.add_guess()                          # a second guessed pairing, a new sheet
        with self.patch_clock(dt.datetime(2026, 9, 15, 12, 0, 2, tzinfo=dt.timezone.utc)):
            newer = views.build_review(self.conn, view="check")
        views.mark_rendering_delivered(self.conn, newer["render_id"])
        quoted = "📊 Finance\n" + views.displayed(older["text"])[:300]
        # #121: ops — the newer sheet's payment is not on the quoted post: refused to the desk
        with self.assertRaisesRegex(__import__("db").Refusal, "is not on the post"):
            self.propose([("confirm", a["pid"]), ("confirm", b[1])], quoted=quoted)
        out, prop = self.propose([("confirm", a["pid"])], quoted=quoted)
        self.assertIsNotNone(prop, out)
        self.assertEqual(prop["text"].count("Confirm "), 1)

    def test_the_proposal_never_carries_the_key_in_the_result(self):
        fx = self.sheet_fixture()
        out, prop = self.propose([("reject", fx["pid"])])
        key = prop["buttons"][0]["call"]["arguments"]["key"]
        self.assertNotIn(key, json.dumps(out))

    def test_apply_reading_is_refused_without_the_key(self):
        import tools, qa_server  # noqa: F401
        fx = self.sheet_fixture()
        out, _ = self.propose([("reject", fx["pid"])])
        rec = qa_server.TOOLS["apply_reading"]["fn"]({"reading_id": out["reading_id"],
                                                     "key": "0" * 32})
        self.assertIn("no longer applies", rec["receipt"])
        self.assertEqual(self.operator_rows(), 0)

    def test_a_quote_that_matches_no_rendering_refuses(self):
        """§8 (binding R3): a quote that matches no rendering refuses — never the latest
        delivered one; a quote of a longer post binds the rendering its text begins with."""
        import views
        fx = self.sheet_fixture(guesses=2)
        out, prop = self.propose([("confirm", p) for p in fx["pids"]],
                                 quoted="📊 Finance\nsomething never posted")
        self.assertIsNone(prop)
        self.assertIn(views.UNMATCHED, out["say"])
        self.assertEqual(out["show_view"], {"view": "status"})
        r = self.conn.execute("SELECT * FROM renders WHERE render_id=?",
                              (fx["render_id"],)).fetchone()
        long = "📊 Finance\n" + views.displayed(r["text"]) + "\n" + "x " * 300
        self.assertEqual(views.bound_rendering(self.conn, long)["render_id"], fx["render_id"])

    def test_a_quote_of_a_joined_post_binds_to_its_first_rendering(self):
        """Task 6 ruling (§8's literal rule, §5): a post joins up to POST_MAX renderings; a
        quote of it binds to the first, even a short one (its whole text is a prefix of the
        quote: binding R1) — not to the later ones delivered with it."""
        import db, job, views
        texts = ["The bank connection stopped — new payments aren't coming in.",
                 "a" * 900, "b" * 900]
        rids = []
        with db.tx(self.conn):
            for kind, text in zip(("alert", "status", "check"), texts):
                rid = f"r{db.next_seq(self.conn)}"
                self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                                  " text, membership_json) VALUES (?,?, '{}', ?, ?, '[]')",
                                  (rid, kind, db.now(), text))
                rids.append(rid)
        self.assertEqual(len(rids), job.POST_MAX)
        self.assertLess(len(views._bnorm(texts[0])), 200)
        for rid in rids:                                  # one post: marked in order
            views.mark_rendering_delivered(self.conn, rid)
        quoted = "📊 Finance\n" + "\n\n".join(texts)
        self.assertEqual(views.bound_rendering(self.conn, quoted)["render_id"], rids[0])
        self.assertEqual(db.last_delivered(self.conn)["render_id"], rids[-1])

    def test_a_refused_deposit_leaves_no_reading_to_apply(self):
        """§7.5: a key Casa never took can never be spent — the reading is stale, and the
        tool answers the no-post shape."""
        import tools, qa_server  # noqa: F401
        fx = self.sheet_fixture()
        with FakeBroker() as b:
            b.refuse = "proposal_invalid"
            out = qa_server.TOOLS["propose_reading"]["fn"](
                {"ops": [{"op": "reject", "pid": fx["pid"]}]})
        self.assertIsNone(out["reading"])
        self.assertIn("proposal_invalid", out["refused"])
        self.assertEqual(self.conn.execute("SELECT state FROM readings").fetchone()[0], "stale")
        self.assertEqual(self.operator_rows(), 0)
