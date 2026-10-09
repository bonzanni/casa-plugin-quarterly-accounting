# tests/test_s7_posts.py
"""S7 §5/§4.2: the job posts its own results between passes and before complete; a
rendering is handed out at most twice a run; a status sheet goes as a view (buttons)
first, as a plain post second; a run that cannot post still completes."""
import json

from tests._base import StoreCase

A, B = "aaaaaaaa-1", "bbbbbbbb-2"


class Posts(StoreCase):
    def test_an_operator_check_ends_with_its_status_view_then_complete(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        units = self.drive(A, deliver=True)          # the simulator marks each receipt
        kinds = [u["unit"] for u in units]
        self.assertIn("view", kinds)
        self.assertEqual(kinds[-1], "complete")
        self.assertLess(kinds.index("view"), kinds.index("complete"))

    def test_a_stop_line_is_posted_before_complete(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        units = self.drive(A, deliver=True, bank_tools=False)   # the pass stops
        kinds = [u["unit"] for u in units]
        # its one message (#93: nothing to tap: plain), then complete
        self.assertEqual(kinds, ["probes", "post", "complete"])
        text = self.render_text(units[1]["render_ids"][0])
        self.assertIn("Accounting check stopped: bank\\-feed's tools are not available to"
                      " the finance specialist.", " ".join(text.split()))   # escaped field

    def test_a_post_withheld_twice_is_not_handed_out_again_and_the_run_completes(self):
        """Review Focus 4: the channel is broken — nothing is ever marked delivered."""
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        units = self.drive(A, deliver=False)
        handed = {}
        for u in units:
            for rid in u.get("render_ids") or ([u["render_id"]] if u.get("render_id") else []):
                handed[rid] = handed.get(rid, 0) + 1
        self.assertTrue(handed)
        import loop
        self.assertEqual(list(handed.values()), [loop.OFFER_MAX])   # the run's one message
        self.assertEqual(units[-1]["unit"], "complete")

    def test_post_results_joins_and_skips_the_delivered(self):
        import posting, views
        from tests.fakebroker import FakeBroker
        a = self.insert_render("alert", "one")
        b = self.insert_render("job-stop", "two")
        views.mark_rendering_delivered(self.conn, a)
        with FakeBroker() as fb:
            out = posting.post_results(self.conn, [a, b])
        self.assertEqual(out["render_ids"], [b])
        self.assertEqual(fb.deposits[0]["value"], "two")
        with FakeBroker() as fb:
            out = posting.post_results(self.conn, [a])
        self.assertEqual(out, {"results": None, "render_ids": []})
        self.assertEqual(fb.deposits, [])

    def test_forty_alerts_are_said_once_each_over_runs_each_message_fitting(self):
        """Plan round 1, Astra S1, simple loop Task 10: more alerts than one message holds —
        each run's message carries the occurrences that fit (scope['alerts'] binds exactly
        them), and the following runs say the rest, each once."""
        import alerts, db, views
        with db.tx(self.conn):
            for i in range(40):
                alerts.raise_package(self.conn, "package-not-sent", f"t:{i}",
                                     quarter="2026-Q3", reason="x" * 250, pass_id="")
        said = []
        for n in range(12):
            units = self.drive(f"aaaaaaaa-{n:02x}", deliver=True)
            self.assertEqual(units[-1]["unit"], "complete")
            (msg,) = [u for u in units if u["unit"] in ("view", "post")]
            rid = msg.get("render_id") or msg["render_ids"][0]
            self.assertLessEqual(views.utf16_len(self.render_text(rid)), views.BODY_LIMIT)
            said += [r[0] for r in self.conn.execute("SELECT alert_id FROM alerts WHERE"
                                                     " render_id=?", (rid,))]
            if not self.conn.execute("SELECT 1 FROM alerts WHERE sent_at IS NULL").fetchone():
                break
        self.assertEqual(sorted(said), list(range(1, 41)))
        self.assertGreater(n, 0)

    def test_job_status_is_not_done_while_a_post_is_owed(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drive(A, deliver=True, stop_before="view")
        self.assertFalse(job.status(self.conn, A)["done"])

    def test_a_package_uncertain_notice_raised_at_a_claim_is_in_the_runs_message(self):
        """§6.1: the claim recovers a stalled staged send as uncertain; its notice is in the
        run's one message (Task 10: alert lines join it, bound by scope['alerts'])."""
        import json
        self.staged_package(lapsed=True)
        units = self.drive(A, deliver=True)
        (msg,) = [u for u in units if u["unit"] in ("view", "post")]
        rid = msg.get("render_id") or msg["render_ids"][0]
        scope = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                             " render_id=?", (rid,)).fetchone()[0])
        kinds = {r[0] for r in self.conn.execute(
            "SELECT kind FROM alerts WHERE alert_id IN (%s)" % ",".join(
                "?" * len(scope["alerts"])), scope["alerts"])}
        self.assertIn("package-uncertain", kinds)

    def test_a_pre_s7_alert_rendering_over_the_body_budget_is_refitted(self):
        """Task 3 carry: a parked alert rendering is reused only within views.BODY_LIMIT, so
        what a post hands to a deposit fits; one stored up to Telegram's limit before S7 is
        composed again."""
        import alerts, db, views
        with db.tx(self.conn):
            alerts.raise_package(self.conn, "package-not-sent", "t:1", quarter="2026-Q3",
                                 reason="x", pass_id="")
            first = alerts.pending_in_tx(self.conn)["render_id"]
            self.assertEqual(alerts.pending_in_tx(self.conn)["render_id"], first)   # reused
            self.conn.execute("UPDATE renders SET text=? WHERE render_id=?",
                              ("a" * (views.BODY_LIMIT + 1), first))
            again = alerts.pending_in_tx(self.conn)
        self.assertNotEqual(again["render_id"], first)
        self.assertLessEqual(views.utf16_len(again["text"]), views.BODY_LIMIT)

    def test_post_results_refuses_a_malformed_list_and_an_unknown_id(self):
        import db, job, posting
        for bad in ([], "r1", [1], ["r1"] * (job.POST_MAX + 1)):
            with self.assertRaises(db.Refusal):
                posting.post_results(self.conn, bad)
        with self.assertRaises(db.Refusal):
            posting.post_results(self.conn, ["r99999"])

    def test_the_tools_post_and_mark_by_render_ids(self):
        import qa_server, tools  # noqa: F401
        from tests.fakebroker import FakeBroker
        a = self.insert_render("job-stop", "one")
        b = self.insert_render("handover", "two")
        with FakeBroker() as fb:
            out = qa_server.TOOLS["post_results"]["fn"]({"render_ids": [a, b]})
        self.assertRegex(out["results"], r"^casa-cap-")
        self.assertEqual(fb.deposits[0]["value"], "one\n\ntwo")
        marked = qa_server.TOOLS["mark_rendering_delivered"]["fn"]({"render_ids": out["render_ids"]})
        self.assertEqual([m["render_id"] for m in marked["marked"]], [a, b])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders WHERE delivered_at IS"
                                           " NULL AND render_id IN (?,?)", (a, b)).fetchone()[0], 0)
        refused = qa_server.TOOLS["post_results"]["fn"]({"render_ids": []})
        self.assertIsNone(refused["results"])

    def test_every_rendering_marked_delivered_binds_its_items(self):
        """§9 retires renders.binding: a rendering stamped non-binding before S7 still
        becomes its payments' shown revision once delivered."""
        import db, views
        fx = self.sheet_fixture()
        with db.tx(self.conn):
            self.conn.execute("UPDATE renders SET binding=0 WHERE render_id=?",
                              (fx["render_id"],))
            self.conn.execute("DELETE FROM shown")
            self.conn.execute("UPDATE renders SET delivered_at=NULL WHERE render_id=?",
                              (fx["render_id"],))
        views.mark_rendering_delivered(self.conn, fx["render_id"])
        self.assertEqual({r[0] for r in self.conn.execute("SELECT render_id FROM shown")},
                         {fx["render_id"]})
