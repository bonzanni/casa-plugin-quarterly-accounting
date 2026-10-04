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
        post = next(u for u in units if u["unit"] == "post")
        text = self.conn.execute("SELECT text FROM renders WHERE render_id=?",
                                 (post["render_ids"][0],)).fetchone()[0]
        self.assertTrue(text.startswith("The accounting check stopped"))

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
        self.assertTrue(all(n <= job.OFFER_MAX for n in handed.values()), handed)
        self.assertEqual(units[-1]["unit"], "complete")
        status = [u for u in units if u["unit"] in ("view", "post")
                  and (u.get("render_id") or u["render_ids"][0]) in handed]
        self.assertEqual([u["unit"] for u in status[-2:]], ["view", "post"])

    def test_an_earlier_runs_unposted_result_is_owed_by_the_next_run(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drive(A, deliver=False)
        asks.request_work(self.conn, "check", "operator")
        units = self.drive(B, deliver=True)
        posted = [u for u in units if u["unit"] in ("view", "post")]
        self.assertGreaterEqual(len(posted), 2)      # A's sheet and B's

    def test_a_legacy_status_rendering_too_long_for_a_proposal_is_posted_plain(self):
        import asks, db, job
        r = asks.request_work(self.conn, "check", "operator")["request_id"]
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                              " text, membership_json) VALUES ('r950','status','{}','x',?,'[]')",
                              ("a" * 4050,))
            self.conn.execute("UPDATE work_requests SET state='done', outcome='complete',"
                              " render_ids_json='[\"r950\"]' WHERE request_id=?", (r,))
        tok = job.claim(self.conn, A)
        u = job.next_unit(self.conn, tok)
        self.assertEqual((u["unit"], u["render_ids"]), ("post", ["r950"]))

    def test_the_budget_spent_with_asks_waiting_posts_the_left_line(self):
        import asks, job
        quarters = ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1"]   # five distinct
        self.assertGreater(len(quarters), job.MAX_PASSES_PER_JOB)
        for q in quarters:
            asks.request_package(self.conn, q)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                           " state='queued'").fetchone()[0], 5)
        units = self.drive(A, deliver=True)
        texts = [self.conn.execute("SELECT text FROM renders WHERE render_id=?", (rid,))
                 .fetchone()[0] for u in units if u["unit"] == "post" for rid in u["render_ids"]]
        self.assertIn(job.LEFT_WAITING, texts)
        self.assertGreaterEqual(self.conn.execute(
            "SELECT count(*) FROM package_requests WHERE state='queued'").fetchone()[0], 1)
        left = self.conn.execute("SELECT delivered_at FROM renders WHERE kind='job-left'"
                                 ).fetchone()
        self.assertIsNotNone(left[0])

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

    def test_forty_withheld_alerts_are_all_offered_and_the_run_completes(self):
        """Plan round 1, Astra S1: more alerts than one rendering holds, nothing ever
        delivered — every occurrence is handed out (at most twice), then complete."""
        import alerts, db, job
        with db.tx(self.conn):
            for i in range(40):
                alerts.raise_package(self.conn, "package-stopped", f"t:{i}",
                                     quarter="2026-Q3", reason="x" * 250, pass_id="")
        units = self.drive(A, deliver=False)
        self.assertEqual(units[-1]["unit"], "complete")
        offered = {r[0] for r in self.conn.execute(
            "SELECT a.alert_id FROM alerts a JOIN post_offers o ON o.render_id=a.render_id")}
        self.assertEqual(len(offered), 40)

    def test_job_status_is_not_done_while_a_post_is_owed(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drive(A, deliver=True, stop_before="view")
        self.assertFalse(job.status(self.conn, A)["done"])

    def test_a_unit_the_budget_swaps_for_end_batch_is_not_counted_as_offered(self):
        """Plan round 3, Astra S2."""
        import asks, db, job
        asks.request_work(self.conn, "check", "operator")
        units = self.drive(A, deliver=False, spend_before_posts=job.TURNS_PER_BATCH)
        self.assertEqual(units[-1]["unit"], "complete")
        self.assertTrue(any(u["unit"] == "view" for u in units))     # still handed out later
        first = next(i for i, u in enumerate(units) if u["unit"] in ("post", "view"))
        self.assertEqual(units[first - 1]["unit"], "end-batch")     # the swapped hand-out
        sheet = next(u["render_id"] for u in units if u["unit"] == "view")
        self.assertEqual(job.offers(self.conn, sheet, A), job.OFFER_MAX)

    def test_a_package_stopped_notice_raised_at_a_claim_is_posted_next(self):
        """Task 9 carry (§10): the claim closes a left-behind run's package ask with its
        package-stopped notice; the next `post` carries it."""
        import asks, job
        asks.request_package(self.conn, "2026-Q3")
        job.claim(self.conn, A)                      # A's run never completes
        tok = job.claim(self.conn, B)                # B leaves A's ask behind (§10)
        u = job.next_unit(self.conn, tok)
        self.assertEqual(u["unit"], "post")
        text = self.render_text(u["render_ids"][0])
        self.assertIn("I couldn't build the Q3 2026 package", text)

    def test_a_package_uncertain_notice_raised_at_a_claim_is_posted_next(self):
        """§6.1: the claim recovers a stalled staged send as uncertain; its notice is the
        next `post`."""
        import job
        self.stage_stalled_package()
        tok = job.claim(self.conn, A)
        u = job.next_unit(self.conn, tok)
        self.assertEqual(u["unit"], "post")
        kinds = {r[0] for r in self.conn.execute(
            "SELECT kind FROM alerts WHERE render_id=?", (u["render_ids"][0],))}
        self.assertIn("package-uncertain", kinds)

    def test_a_post_joins_at_most_post_max_renderings_within_post_chars(self):
        import asks, db, job
        r = asks.request_work(self.conn, "handover", "operator", [self.doc()])["request_id"]
        pages = [self.insert_render("handover", "h" * 3900) for _ in range(4)]
        with db.tx(self.conn):
            self.conn.execute("UPDATE work_requests SET state='done', outcome='complete',"
                              " render_ids_json=? WHERE request_id=?",
                              (json.dumps(pages), r))
        tok = job.claim(self.conn, A)
        u = job.next_unit(self.conn, tok)
        self.assertEqual(u, {**u, "unit": "post", "render_ids": pages[:job.POST_MAX]})
        size = sum(len(self.render_text(x)) for x in u["render_ids"]) + 2 * (job.POST_MAX - 1)
        self.assertLessEqual(size, job.POST_CHARS)
        u = job.next_unit(self.conn, tok)                       # nothing delivered yet
        self.assertEqual(u["render_ids"], pages[:job.POST_MAX])  # its second hand-out
        u = job.next_unit(self.conn, tok)
        self.assertEqual(u["render_ids"], pages[job.POST_MAX:])  # the rest

    def test_short_pages_are_still_posted_at_most_post_max_at_a_time(self):
        import asks, db, job
        r = asks.request_work(self.conn, "handover", "operator", [self.doc()])["request_id"]
        pages = [self.insert_render("handover", f"line {i}") for i in range(5)]
        with db.tx(self.conn):
            self.conn.execute("UPDATE work_requests SET state='done', outcome='complete',"
                              " render_ids_json=? WHERE request_id=?",
                              (json.dumps(pages), r))
        u = job.next_unit(self.conn, job.claim(self.conn, A))
        self.assertEqual(u["render_ids"], pages[:job.POST_MAX])

    def test_a_pre_s7_alert_rendering_over_the_body_budget_is_refitted(self):
        """Task 3 carry: a parked alert rendering is reused only within views.BODY_LIMIT, so
        what a post hands to a deposit fits; one stored up to Telegram's limit before S7 is
        composed again."""
        import alerts, db, views
        with db.tx(self.conn):
            alerts.raise_package(self.conn, "package-stopped", "t:1", quarter="2026-Q3",
                                 reason="x", pass_id="")
            first = alerts.pending_in_tx(self.conn)["render_id"]
            self.assertEqual(alerts.pending_in_tx(self.conn)["render_id"], first)   # reused
            self.conn.execute("UPDATE renders SET text=? WHERE render_id=?",
                              ("a" * (views.BODY_LIMIT + 1), first))
            again = alerts.pending_in_tx(self.conn)
        self.assertNotEqual(again["render_id"], first)
        self.assertLessEqual(views.utf16_len(again["text"]), views.BODY_LIMIT)

    def test_the_accounts_question_is_handed_out_once_a_run(self):
        """§11: an unbound store whose bank lists two company accounts."""
        import db, job
        with db.tx(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO probes(kind, ok, data_json, observed_at)"
                              " VALUES ('bank_accounts', 1, ?, ?)", (json.dumps({"accounts": [
                                  {"account_id": "acc-1", "category": "company"},
                                  {"account_id": "acc-2", "category": "company"}]}),
                                  db.now()))
            self.assertTrue(job._accounts_owed(self.conn, A))
            job._offer(self.conn, "accounts", A)
            self.assertFalse(job._accounts_owed(self.conn, A))
            self.assertTrue(job._accounts_owed(self.conn, B))

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
