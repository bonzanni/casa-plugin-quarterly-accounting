# tests/test_s2_report.py
"""S2 Task 10 (spec §6.3–§6.4, INV-J7): results shown at least once — what S7 keeps of
job_report's tests. S7 §9 deleted job_report, the orphan handoff, the standing retry and
the cancel records; the stalled-send recovery moved to the claim (test_s7_claim). The
result cases are ported onto the job's own `post` and `view` units (S7 §5); the package
recovery waits for the build/deliver units (Task 11)."""
import json
import unittest

from tests._base import StoreCase

A, B, C, D = "aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3", "dddddddd-4"


class Report(StoreCase):
    def live_pass_of(self, job_id):
        import asks, db, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, job_id)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        return t, pid

    def test_a_result_is_offered_until_its_rendering_is_marked_delivered(self):
        """S7 §5: the status sheet is handed out as a `view`, then (the send was lost) as a
        `post` of the same rendering; once marked, the request is reported and the run
        completes."""
        import db, job, passes, views
        t, pid = self.live_pass_of(A)
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "complete", {})
        first = job.next_unit(self.conn, t)
        again = job.next_unit(self.conn, t)                      # the send was lost
        self.assertEqual((first["unit"], again["unit"]), ("view", "post"))
        self.assertEqual(again["render_ids"], [first["render_id"]])
        views.mark_rendering_delivered(self.conn, first["render_id"])
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "complete")
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "reported")

    def test_two_checks_served_by_one_pass_share_one_result(self):
        import asks, db, job, passes, views
        t, pid = self.live_pass_of(A)
        asks.request_work(self.conn, "check", "operator")
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            passes._end_pass_tx(self.conn, t, "complete", {})
        u = job.next_unit(self.conn, t)
        self.assertEqual(u["unit"], "view")
        views.mark_rendering_delivered(self.conn, u["render_id"])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT state FROM work_requests")],
                         ["reported", "reported"])
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "complete")

    def test_checks_of_two_passes_reported_together_share_one_status_view(self):
        """Both results are composed in one hand-out, from one state: one view, not two
        identical ones back to back."""
        import db, job, passes, views
        for _ in range(2):
            t, pid = self.live_pass_of(A)
            with db.tx(self.conn):
                passes._end_pass_tx(self.conn, t, "complete", {})
        u = job.next_unit(self.conn, t)
        self.assertEqual(u["unit"], "view")
        ids = {r[0] for r in self.conn.execute("SELECT render_ids_json FROM work_requests")}
        self.assertEqual(ids, {f'["{u["render_id"]}"]'})
        views.mark_rendering_delivered(self.conn, u["render_id"])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT state FROM work_requests")],
                         ["reported", "reported"])

    @unittest.skip("S7: re-enabled in Task 11")
    def test_buildable_and_built_packages_are_recovered(self):
        import asks
        self.package_built_unsent()          # existing package helpers, request state 'built'
        out = asks.job_report(self.conn)
        self.assertEqual(out["continue"]["next"], "stage")

    def test_the_answer_is_deliverable_text_by_text(self):
        import tools, views
        out = {"texts": [{"render_id": "r1", "text": "x" * (views.TELEGRAM_LIMIT + 1)}]}
        with self.assertRaises(tools.Undeliverable):
            tools._deliverable("job_report", out)
        tools._deliverable("job_report", {"texts": [{"render_id": "r1", "text": "ok"}]})


class Handover(StoreCase):
    def handover_done(self, doc_ids, verdict="no-payment-yet", outcome="complete",
                      gone=()):
        """`gone`: documents removed from the store after the handover named them (no
        tool removes one; request_work refuses an id that is not filed). Returns the
        claim token, its pass ended."""
        import asks, db, job, passes
        asks.request_work(self.conn, "handover", "operator", doc_ids)
        with db.tx(self.conn):
            for d in gone:
                self.conn.execute("DELETE FROM documents WHERE doc_id=?", (d,))
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            self.conn.execute("UPDATE work_requests SET verdicts_json=? WHERE kind='handover'",
                              (json.dumps({str(d): {"verdict": verdict, "judge": 1}
                                           for d in doc_ids}),))
            passes._end_pass_tx(self.conn, t, outcome, {})
        return t

    def posted(self, u):
        return [self.render_text(r) for r in u["render_ids"]]

    def test_one_document_gets_its_case_line(self):
        import job
        d = self.doc(amount_minor=1210)
        t = self.handover_done([d])
        u = job.next_unit(self.conn, t)
        self.assertEqual(u["unit"], "post")
        self.assertEqual(self.posted(u),
                         ["Filed. No payment matches EUR 12.10 yet — the charge may not have "
                          "posted. It'll match when it appears."])

    def test_an_unknown_document_says_so(self):
        import job
        d = self.doc()
        t = self.handover_done([d], gone=[d])
        self.assertEqual(self.posted(job.next_unit(self.conn, t)),
                         ["I can't find that document in what I've filed — please send the "
                          "file once more."])

    def test_a_handover_of_200_documents_is_paged_whole(self):
        """Every page within the deposit body's budget (views.BODY_LIMIT), every post at
        most job.POST_MAX pages within job.POST_CHARS (S7 §5): the pages come across
        posts, the request reported only after the last one is delivered."""
        import job, views
        docs = [self.doc() for _ in range(200)]
        self.handover_done(docs)
        import asks
        asks.request_work(self.conn, "check", "operator")       # its status view too
        t, pid = Report.live_pass_of(self, B)
        import db, passes
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "complete", {})
        lines, posts, offered = [], 0, []
        for _ in range(50):
            u = job.next_unit(self.conn, t)
            if u["unit"] == "end-batch":
                t = job.claim(self.conn, B)
                continue
            if u["unit"] == "complete":
                break
            if u["unit"] == "view":
                views.mark_rendering_delivered(self.conn, u["render_id"])
                continue
            self.assertEqual(u["unit"], "post")
            posts += 1
            self.assertLessEqual(len(u["render_ids"]), job.POST_MAX)
            texts = self.posted(u)
            self.assertLessEqual(sum(len(x) for x in texts) + 2 * (len(texts) - 1),
                                 job.POST_CHARS)
            for rid, x in zip(u["render_ids"], texts):
                self.assertLessEqual(views.utf16_len(x), views.BODY_LIMIT)
                lines += x.split("\n")
                offered.append(rid)
            state = self.conn.execute("SELECT state FROM work_requests WHERE"
                                      " kind='handover'").fetchone()[0]
            self.assertEqual(state, "done")
            for rid in u["render_ids"]:
                views.mark_rendering_delivered(self.conn, rid)
        else:
            self.fail("the run never completed")
        self.assertGreater(posts, 1)                               # it took several posts
        self.assertEqual(len(offered), len(set(offered)))         # none offered twice
        self.assertEqual(sum("No payment matches" in ln for ln in lines), 200)
        self.assertFalse(any(ln.endswith(views.CLIP_MARK) for ln in lines))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT state FROM work_requests")],
                         ["reported", "reported", "reported"])

    def test_pages_left_out_of_a_post_are_offered_next(self):
        import job
        docs = [self.doc() for _ in range(200)]
        t = self.handover_done(docs)
        first = job.next_unit(self.conn, t)
        total = json.loads(self.conn.execute("SELECT render_ids_json FROM work_requests"
                                             ).fetchone()[0])
        self.assertEqual(first["render_ids"], total[:job.POST_MAX])
        self.assertLess(len(first["render_ids"]), len(total))
        again = job.next_unit(self.conn, t)                        # nothing delivered yet
        self.assertEqual(first["render_ids"], again["render_ids"])
        rest = job.next_unit(self.conn, t)                         # offered twice: the rest
        self.assertEqual(rest["render_ids"], total[job.POST_MAX:2 * job.POST_MAX])

    def test_a_post_with_everything_leaves_nothing_owed(self):
        import job, views
        d = self.doc()
        t = self.handover_done([d])
        u = job.next_unit(self.conn, t)
        self.assertEqual((u["unit"], len(u["render_ids"])), ("post", 1))
        views.mark_rendering_delivered(self.conn, u["render_ids"][0])
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "complete")


class StoppedResult(StoreCase):
    def test_a_refresh_stop_keeps_its_reason(self):
        """An initial sweep, a second check, then its refresh found no bank tools. One
        stopped result names the reason, posted by the job (S7 §5), and a later successful
        probe overwriting the one that carried it changes nothing. (Was: a refresh's failed
        sync — PLAY T7 F1: a failed sync no longer stops a pass, so the missing tools carry
        the stop here.)"""
        import asks
        from tests.sim_job import JobDriver
        self.bind()
        drv = JobDriver(self)
        asks.request_work(self.conn, "check", "operator")
        drv.run_until(A, "gmail-probe")                          # the initial sweep is done
        asks.request_work(self.conn, "check", "operator")        # a second check: a refresh
        drv.no_bank_tools()
        drv.next_until(drv.token, "complete")                    # the refresh stops the pass
        drv._no_tools = False
        asks.request_work(self.conn, "check", "operator")
        drv.run_job(A)                                           # a later read succeeds
        tools = self.conn.execute("SELECT ok FROM probes WHERE kind='bank_tools'").fetchone()
        self.assertEqual(tools[0], 1)
        texts = [self.render_text(r) for u in drv.units if u["unit"] == "post"
                 for r in u["render_ids"]]
        stops = [x for x in texts if x.startswith("The accounting check stopped")]
        self.assertEqual(stops, ["The accounting check stopped: bank\\-feed's tools are not "
                                 "available to the finance specialist."])

    def test_a_stop_without_a_reason_says_only_that(self):
        import asks, db, passes
        asks.request_work(self.conn, "check", "cron")
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t, trigger="cron")
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            passes._end_pass_tx(self.conn, t, "stopped", {})
        u = job.next_unit(self.conn, t)
        self.assertEqual([self.render_text(r) for r in u["render_ids"]],
                         ["The accounting check stopped."])

    def test_a_cron_check_that_completed_shows_nothing(self):
        import asks, db, job, passes
        asks.request_work(self.conn, "check", "cron")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t, trigger="cron")
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            passes._end_pass_tx(self.conn, t, "complete", {})
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "complete")
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "reported")
