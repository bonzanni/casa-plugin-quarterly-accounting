# tests/test_s2_report.py
"""S2 Task 10 (spec §6.3–§6.4, INV-J5, INV-J7): job_report — the orphan handoff by job id,
the standing retry, results shown at least once, and the send recovery — in ONE
transaction under the custody lock."""
import json
from unittest import mock

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

    def drain(self):
        r = self.conn.execute("SELECT value FROM meta WHERE key='drain'").fetchone()
        return r[0] if r else None

    def test_the_holders_death_orphans_the_pass_and_offers_a_restart(self):
        import asks
        self.live_pass_of(A)
        out = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertTrue(out["orphaned"])
        self.assertIsNotNone(out["start_job"])
        self.assertEqual(out["line"], "The accounting check stopped before it finished — I'm "
                                      "starting it again.")
        self.assertEqual(self.conn.execute("SELECT orphaned_by FROM passes").fetchone()[0], A)
        self.assertEqual(self.drain(), "none")
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "taken")

    def test_a_late_notice_about_another_job_changes_nothing(self):
        import asks
        self.live_pass_of(A)
        asks.job_report(self.conn, job_id=B[:8], status="error")
        self.assertIsNone(self.conn.execute("SELECT orphaned_by FROM passes").fetchone()[0])
        self.assertEqual(self.drain(), A)

    def test_the_retry_stands_on_every_report_until_a_job_claims(self):
        import asks, job
        self.live_pass_of(A)
        asks.job_report(self.conn, job_id=A[:8], status="error")
        for _ in range(3):                                        # refused starts
            self.assertIsNotNone(asks.job_report(self.conn)["start_job"])
        job.claim(self.conn, B)
        self.assertIsNone(asks.job_report(self.conn)["start_job"])

    def test_a_pass_stopped_on_adoption_never_offers_a_restart(self):
        """Carry from the Task 4 review: the pass stopped on its third adoption keeps its
        holder_job and orphaned_by, but it is ended — never a standing retry."""
        import asks, job
        _, pid = self.live_pass_of(A)
        asks.job_report(self.conn, job_id=A[:8], status="error")
        for prev, nxt in ((A, B), (B, C)):
            job.claim(self.conn, nxt)                             # adoptions 1 and 2
            asks.job_report(self.conn, job_id=nxt[:8], status="error")
        job.claim(self.conn, D)                                   # the third: stopped
        row = self.conn.execute("SELECT ended_at, outcome, orphaned_by FROM passes WHERE"
                                " pass_id=?", (pid,)).fetchone()
        self.assertEqual(row["outcome"], "stopped")
        self.assertIsNotNone(row["orphaned_by"])                  # kept on the ended pass
        out = asks.job_report(self.conn, job_id=D[:8], status="ok")
        self.assertIsNone(out["start_job"])
        self.assertFalse(out["orphaned"])
        self.assertEqual(self.drain(), "none")
        self.assertIsNone(asks.job_report(self.conn)["start_job"])
        self.assertEqual([x["text"] for x in out["texts"]],
                         ["The accounting check kept stopping — ask again when you want me "
                          "to retry."])

    def test_the_drains_job_ending_with_nothing_live_offers_a_queued_request(self):
        import asks, job
        job.claim(self.conn, A)                                    # the drain, no pass yet
        asks.request_work(self.conn, "check", "operator")
        self.assertIsNone(asks.job_report(self.conn)["start_job"])  # the drain will take it
        out = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertEqual(self.drain(), "none")
        self.assertIsNotNone(out["start_job"])
        self.assertFalse(out["orphaned"])

    def test_status_must_be_ok_or_error_with_an_id(self):
        import asks, db
        with self.assertRaises(db.Refusal):
            asks.job_report(self.conn, job_id=A[:8], status="done")
        with self.assertRaises(db.Refusal):
            asks.job_report(self.conn, job_id="aaaa", status="ok")

    def test_a_result_is_offered_until_its_rendering_is_marked_delivered(self):
        import asks, db, passes, views
        t, pid = self.live_pass_of(A)
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "complete", {})
        first = asks.job_report(self.conn)["texts"]
        again = asks.job_report(self.conn)["texts"]               # the send was lost
        self.assertEqual([x["render_id"] for x in first], [x["render_id"] for x in again])
        views.mark_rendering_delivered(self.conn, first[0]["render_id"])
        self.assertEqual(asks.job_report(self.conn)["texts"], [])
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "reported")

    def test_two_checks_served_by_one_pass_share_one_result(self):
        import asks, db, passes, views
        t, pid = self.live_pass_of(A)
        asks.request_work(self.conn, "check", "operator")
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            passes._end_pass_tx(self.conn, t, "complete", {})
        texts = asks.job_report(self.conn)["texts"]
        self.assertEqual(len(texts), 1)
        views.mark_rendering_delivered(self.conn, texts[0]["render_id"])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT state FROM work_requests")],
                         ["reported", "reported"])

    def test_checks_of_two_passes_reported_together_share_one_status_view(self):
        """Both results are composed in one call, from one state: one view, not two
        identical ones back to back."""
        import asks, db, passes, views
        for _ in range(2):
            t, pid = self.live_pass_of(A)
            with db.tx(self.conn):
                passes._end_pass_tx(self.conn, t, "complete", {})
        texts = asks.job_report(self.conn)["texts"]
        self.assertEqual(len(texts), 1)
        views.mark_rendering_delivered(self.conn, texts[0]["render_id"])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT state FROM work_requests")],
                         ["reported", "reported"])

    def test_buildable_and_built_packages_are_recovered(self):
        import asks
        self.package_built_unsent()          # existing package helpers, request state 'built'
        out = asks.job_report(self.conn)
        self.assertEqual(out["continue"]["next"], "stage")

    def test_report_is_one_transaction_under_the_custody_lock(self):
        import asks, db, passes
        t, pid = self.live_pass_of(A)
        asks.request_work(self.conn, "check", "operator")         # a second check, queued
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "complete", {})
        renders = self.conn.execute("SELECT count(*) FROM renders").fetchone()[0]
        before = (self.drain(), self.conn.execute("SELECT orphaned_by FROM passes"
                                                  ).fetchone()[0])
        real = db.custody_lock
        with real():                                              # another holder
            with mock.patch.object(db, "custody_lock", lambda *a, **k: real(0.2)):
                with self.assertRaises(db.Busy):
                    asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM renders").fetchone()[0],
                         renders)
        self.assertEqual((self.drain(), self.conn.execute("SELECT orphaned_by FROM passes"
                                                          ).fetchone()[0]), before)
        self.assertEqual(self.conn.execute("SELECT render_ids_json FROM work_requests WHERE"
                                           " state='done'").fetchone()[0], "[]")

    def test_the_answer_is_deliverable_text_by_text(self):
        import tools, views
        out = {"texts": [{"render_id": "r1", "text": "x" * (views.TELEGRAM_LIMIT + 1)}]}
        with self.assertRaises(tools.Undeliverable):
            tools._deliverable("job_report", out)
        tools._deliverable("job_report", {"texts": [{"render_id": "r1", "text": "ok"}]})


class Handover(StoreCase):
    def handover_done(self, doc_ids, verdict="no-payment-yet", outcome="complete"):
        import asks, db, job, passes
        asks.request_work(self.conn, "handover", "operator", doc_ids)
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            self.conn.execute("UPDATE work_requests SET verdicts_json=? WHERE kind='handover'",
                              (json.dumps({str(d): {"verdict": verdict, "judge": 1}
                                           for d in doc_ids}),))
            passes._end_pass_tx(self.conn, t, outcome, {})

    def test_one_document_gets_its_case_line(self):
        import asks
        d = self.doc(amount_minor=1210)
        self.handover_done([d])
        texts = asks.job_report(self.conn)["texts"]
        self.assertEqual([x["text"] for x in texts],
                         ["Filed. No payment matches EUR 12.10 yet — the charge may not have "
                          "posted. It'll match when it appears."])

    def test_an_unknown_document_says_so(self):
        import asks
        self.handover_done([987654])
        texts = asks.job_report(self.conn)["texts"]
        self.assertEqual([x["text"] for x in texts],
                         ["I can't find that document in what I've filed — please send the "
                          "file once more."])

    def test_a_handover_of_200_documents_is_paged_whole(self):
        """Every page within Telegram's limit, every answer within the agent's (issue #3,
        budget.RESULT_LIMIT): the pages come across calls, the request reported only
        after the last one is delivered."""
        import asks, budget, views
        docs = [self.doc() for _ in range(200)]
        self.handover_done(docs)
        asks.request_work(self.conn, "check", "operator")       # its status view too
        t, pid = Report.live_pass_of(self, B)
        import db, passes
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "complete", {})
        lines, calls, offered = [], 0, []
        while True:
            out = asks.job_report(self.conn)
            calls += 1
            self.assertLessEqual(budget.size(out), budget.RESULT_LIMIT)
            if not out["texts"]:
                self.assertFalse(out["more"])
                break
            for x in out["texts"]:
                self.assertLessEqual(views.utf16_len(x["text"]), views.TELEGRAM_LIMIT)
                lines += x["text"].split("\n")
                offered.append(x["render_id"])
            state = self.conn.execute("SELECT state FROM work_requests WHERE"
                                      " kind='handover'").fetchone()[0]
            if out["more"]:
                self.assertEqual(state, "done")
            for x in out["texts"]:
                views.mark_rendering_delivered(self.conn, x["render_id"])
            self.assertLess(calls, 50)
        self.assertGreater(calls, 2)                               # it took several answers
        self.assertEqual(len(offered), len(set(offered)))         # none offered twice
        self.assertEqual(sum("No payment matches" in ln for ln in lines), 200)
        self.assertFalse(any(ln.endswith(views.CLIP_MARK) for ln in lines))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT state FROM work_requests")],
                         ["reported", "reported", "reported"])

    def test_pages_left_out_of_an_answer_are_offered_next(self):
        import asks, budget
        docs = [self.doc() for _ in range(200)]
        self.handover_done(docs)
        first = asks.job_report(self.conn)
        self.assertTrue(first["more"])
        self.assertLessEqual(budget.size(first), budget.RESULT_LIMIT)
        again = asks.job_report(self.conn)                        # nothing delivered yet
        self.assertEqual(first["texts"], again["texts"])
        total = len(self.conn.execute("SELECT render_ids_json FROM work_requests"
                                      ).fetchone()[0].split(","))
        self.assertLess(len(first["texts"]), total)

    def test_an_answer_with_everything_says_no_more(self):
        import asks
        d = self.doc()
        self.handover_done([d])
        out = asks.job_report(self.conn)
        self.assertEqual((len(out["texts"]), out["more"]), (1, False))


class StoppedResult(StoreCase):
    def test_a_refresh_sync_failure_keeps_its_reason(self):
        """An initial sweep, a second check, then its refresh's sync failed. One stopped
        result names the reason, even after a later successful sync overwrote the probe."""
        import asks
        from tests.sim_job import JobDriver
        self.bind()
        drv = JobDriver(self)
        asks.request_work(self.conn, "check", "operator")
        drv.run_until(A, "gmail-probe")                          # the initial sweep is done
        asks.request_work(self.conn, "check", "operator")        # a second check: a refresh
        drv.fail_next_sync("bank unreachable")
        drv.next_until(drv.token, "complete")                    # the refresh stops the pass
        asks.request_work(self.conn, "check", "operator")
        drv.run_job(A)                                           # a later sync succeeds
        sync = self.conn.execute("SELECT ok FROM probes WHERE kind='bank_sync'").fetchone()
        self.assertEqual(sync[0], 1)
        texts = [x["text"] for x in asks.job_report(self.conn)["texts"]]
        stops = [x for x in texts if x.startswith("The accounting check stopped")]
        self.assertEqual(stops, ["The accounting check stopped: the bank sync failed: "
                                 "bank unreachable."])

    def test_a_stop_without_a_reason_says_only_that(self):
        import asks, db, passes
        asks.request_work(self.conn, "check", "cron")
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t, trigger="cron")
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            passes._end_pass_tx(self.conn, t, "stopped", {})
        self.assertEqual([x["text"] for x in asks.job_report(self.conn)["texts"]],
                         ["The accounting check stopped."])

    def test_a_cron_check_that_completed_shows_nothing(self):
        import asks, db, job, passes
        asks.request_work(self.conn, "check", "cron")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t, trigger="cron")
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            passes._end_pass_tx(self.conn, t, "complete", {})
        self.assertEqual(asks.job_report(self.conn)["texts"], [])
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "reported")
