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

    def test_a_notice_about_an_earlier_holder_changes_nothing(self):
        """FW-I3 (M18): the notice names a job this store knows, but not the one that
        holds the pass now (B adopted it): no orphan, the drain stays B's."""
        import asks, job
        _, pid = self.live_pass_of(A)
        job.claim(self.conn, B)                                   # B adopts the pass
        out = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertFalse(out["orphaned"])
        self.assertIsNone(out["start_job"])
        row = self.conn.execute("SELECT holder_job, orphaned_by, ended_at FROM passes WHERE"
                                " pass_id=?", (pid,)).fetchone()
        self.assertEqual(tuple(row), (B, None, None))
        self.assertEqual(self.drain(), B)
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "taken")

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

    def test_status_must_be_ok_error_or_cancelled_with_an_id(self):
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

    def test_the_answer_is_bounded_at_the_exact_limit(self):
        """Minor (final review): `more` is measured as `false`, its longer value. A last
        page that fits only while `more` reads `true` is left for the next call."""
        import asks, budget

        def answer():
            return {"orphaned": False, "start_job": None, "texts": [], "speak": None,
                    "continue": None, "line": None, "more": False}

        def pages(n):
            return [{"render_id": "r1", "text": "a"}, {"render_id": "r2", "text": "x" * n}]
        full = answer()
        full["texts"] = pages(0)
        n = budget.RESULT_LIMIT - budget.size(full)     # both pages, `false`: the limit
        out = answer()
        asks._bounded(out, pages(n))
        self.assertEqual((len(out["texts"]), out["more"]), (2, False))
        self.assertEqual(budget.size(out), budget.RESULT_LIMIT)
        out = answer()
        asks._bounded(out, pages(n + 1))                # one character over with `false`
        self.assertEqual((len(out["texts"]), out["more"]), (1, True))
        self.assertLessEqual(budget.size(out), budget.RESULT_LIMIT)

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
        tool removes one; request_work refuses an id that is not filed)."""
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
        d = self.doc()
        self.handover_done([d], gone=[d])
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
                self.assertLessEqual(views.utf16_len(x["text"]), views.BODY_LIMIT)
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
    def test_a_refresh_stop_keeps_its_reason(self):
        """An initial sweep, a second check, then its refresh found no bank tools. One
        stopped result names the reason, even after a later successful probe overwrote
        the one that carried it. (Was: a refresh's failed sync — PLAY T7 F1: a failed sync
        no longer stops a pass, so the missing tools carry the stop here.)"""
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
        texts = [x["text"] for x in asks.job_report(self.conn)["texts"]]
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


class Cancelled(StoreCase):
    """#38: Casa's "Cancelled by user" is the operator's /cancel. job_report(status=
    "cancelled") withdraws every open ask, ends the holder's pass `stopped`, and never
    restarts the job — unlike `error`, whose orphan-and-restart T7's SIGKILL recovery
    needs."""
    LINE = "The accounting check was cancelled — ask again when you want it."
    REASON = "you cancelled the check"

    def live_pass_of(self, job_id):
        return Report.live_pass_of(self, job_id)

    def drain(self):
        return Report.drain(self)

    def work(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT state, outcome FROM work_requests ORDER BY request_id")]

    def assert_withdrawn(self, out, n_work):
        """No restart; the work asks share ONE line, consumed when it is delivered."""
        import asks, views
        self.assertIsNone(out["start_job"])
        self.assertIsNone(out["line"])
        self.assertFalse(out["orphaned"])
        self.assertEqual(self.drain(), "none")
        self.assertEqual([x["text"] for x in out["texts"]], [self.LINE])
        self.assertEqual(self.work(), [("done", "stopped")] * n_work)
        for word in views.FORBIDDEN:
            self.assertNotIn(word, self.LINE)
        later = asks.job_report(self.conn)                        # the turn's no-id call
        self.assertIsNone(later["start_job"])
        self.assertEqual([x["render_id"] for x in later["texts"]],
                         [x["render_id"] for x in out["texts"]])  # the same line, unsent
        views.mark_rendering_delivered(self.conn, out["texts"][0]["render_id"])
        self.assertEqual(self.work(), [("reported", "stopped")] * n_work)
        self.assertEqual(asks.job_report(self.conn)["texts"], [])
        self.assertIsNone(asks.job_report(self.conn)["start_job"])

    def test_a_cancel_while_a_pass_is_live_withdraws_everything(self):
        import asks, json
        _, pid = self.live_pass_of(A)                             # one check, taken
        asks.request_work(self.conn, "check", "cron")             # one queued
        asks.request_package(self.conn, "2026-Q3", "telegram")    # one package, queued
        self.assertEqual(self.work(), [("taken", None), ("queued", None)])
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        row = self.conn.execute("SELECT ended_at, outcome, orphaned_by, report_json FROM"
                                " passes WHERE pass_id=?", (pid,)).fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual((row["outcome"], row["orphaned_by"]), ("stopped", None))
        self.assertEqual(json.loads(row["report_json"])["stopped_reason"], self.REASON)
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)
        pkg = self.conn.execute("SELECT state, reason FROM package_requests").fetchone()
        self.assertEqual(tuple(pkg), ("stopped", self.REASON))
        self.assertIn("you cancelled the check", out["speak"]["text"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM credits").fetchone()[0], 0)
        self.assert_withdrawn(out, 2)

    def test_a_cancel_before_any_pass_withdraws_the_asks(self):
        import asks, job
        job.claim(self.conn, A)                                   # the drain, no pass yet
        asks.request_work(self.conn, "check", "operator")
        asks.request_work(self.conn, "handover", "operator", doc_ids=[self.doc()])
        asks.request_package(self.conn, "2026-Q3", "email")
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "stopped")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0], 0)
        self.assert_withdrawn(out, 2)

    def test_a_cancelled_package_round_closes_its_request(self):
        import asks, db, job, passes
        asks.request_package(self.conn, "2026-Q3", "telegram")
        t = job.claim(self.conn, A)
        with db.tx(self.conn):
            _, pid = passes.start_pass(self.conn, "package", "silent", protocol="job",
                                       token=t)
            self.conn.execute("UPDATE passes SET holder_job=? WHERE pass_id=?", (A, pid))
        self.bind_round_and_take(pid)
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertIsNone(out["start_job"])
        pkg = self.conn.execute("SELECT state, reason, pass_id FROM package_requests"
                                ).fetchone()
        self.assertEqual(tuple(pkg), ("stopped", self.REASON, pid))
        self.assertEqual(self.conn.execute("SELECT outcome FROM passes").fetchone()[0],
                         "stopped")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM credits").fetchone()[0], 0)
        self.assertEqual(out["texts"], [])                        # no work ask: speak says it
        self.assertIn("you cancelled the check", out["speak"]["text"])

    def test_an_error_still_orphans_and_offers_a_restart(self):
        import asks
        self.live_pass_of(A)
        asks.request_work(self.conn, "check", "operator")
        out = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertTrue(out["orphaned"])
        self.assertIsNotNone(out["start_job"])
        self.assertEqual(self.work(), [("taken", None), ("queued", None)])
        self.assertIsNone(self.conn.execute("SELECT ended_at FROM passes").fetchone()[0])

    def test_a_cancel_after_an_error_stops_the_orphaned_pass(self):
        """The holder's error notice came first (the pass orphaned, a restart offered),
        then the operator's cancel: the cancel wins, nothing restarts."""
        import asks
        self.live_pass_of(A)
        asks.job_report(self.conn, job_id=A[:8], status="error")
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertEqual(self.conn.execute("SELECT outcome FROM passes").fetchone()[0],
                         "stopped")
        self.assert_withdrawn(out, 1)

    def test_a_late_cancelled_notice_for_an_old_id_changes_nothing(self):
        import asks, job
        _, pid = self.live_pass_of(A)
        job.claim(self.conn, B)                                   # B adopts the pass
        asks.request_work(self.conn, "check", "operator")
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertEqual(out["texts"], [])
        row = self.conn.execute("SELECT holder_job, orphaned_by, ended_at FROM passes WHERE"
                                " pass_id=?", (pid,)).fetchone()
        self.assertEqual(tuple(row), (B, None, None))
        self.assertEqual(self.drain(), B)
        self.assertEqual(self.work(), [("taken", None), ("queued", None)])
        out = asks.job_report(self.conn, job_id=C[:8], status="cancelled")   # unknown
        self.assertEqual(self.work(), [("taken", None), ("queued", None)])

    def test_a_replayed_cancel_changes_nothing(self):
        import asks
        self.live_pass_of(A)
        first = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        asks.request_work(self.conn, "check", "operator")         # asked again after it
        again = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertEqual(self.work(), [("done", "stopped"), ("queued", None)])
        self.assertIsNotNone(again["start_job"])                  # the new ask's standing retry
        self.assertEqual([x["render_id"] for x in again["texts"]],
                         [x["render_id"] for x in first["texts"]])

    def test_the_job_starts_afresh_after_a_cancel(self):
        """sim_job: A's pass is cancelled mid-sweep; a new ask runs a new job to the end."""
        import asks
        from tests.sim_job import JobDriver
        self.bind()
        drv = JobDriver(self)
        asks.request_work(self.conn, "check", "operator")
        drv.run_until(A, "sweep")
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertIsNone(out["start_job"])
        self.assertEqual([x["text"] for x in out["texts"]], [self.LINE])
        asks.request_work(self.conn, "check", "operator")
        last = drv.run_job(B)[-1]
        self.assertEqual(last["unit"], "complete")
        outcomes = [r[0] for r in self.conn.execute("SELECT outcome FROM passes ORDER BY"
                                                    " generation")]
        self.assertEqual(outcomes, ["stopped", "complete"])


class CancelRevokes(StoreCase):
    """#38 round 7: a cancel revokes the cancelled job (R7-1) — no token it was issued and
    no new claim of its id lands after it — and an `error` notice before the cancel does
    not stop the withdrawal (R7-2)."""
    LINE = Cancelled.LINE
    WORDS = "this job was cancelled — nothing was done"

    def live_pass_of(self, job_id):
        return Report.live_pass_of(self, job_id)

    def drain(self):
        return Report.drain(self)

    def work(self):
        return Cancelled.work(self)

    def test_an_old_job_next_after_a_cancel_is_refused(self):
        """Astra/Terra R7-1: claim A, cancel A, a new ask; A's already-issued
        job_next(pass_token=old) opened a new live pass held by A."""
        import asks, db, job
        t = job.claim(self.conn, A)
        asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        asks.request_work(self.conn, "check", "operator")
        with self.assertRaises(db.Refusal) as e:
            job.next_unit(self.conn, t)
        self.assertEqual(str(e.exception), self.WORDS)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0], 0)
        self.assertEqual(self.work(), [("queued", None)])           # no ask taken
        self.assertIsNotNone(asks.job_report(self.conn)["start_job"])

    def test_a_delayed_write_of_the_cancelled_job_is_refused(self):
        """Astra R7-1: a delayed record_probe(old_token) after the cancel."""
        import asks, db, passes
        t, pid = self.live_pass_of(A)
        asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        with self.assertRaises(db.Refusal) as e:
            passes.record_probe(self.conn, t, "gmail", True)
        self.assertEqual(str(e.exception), self.WORDS)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM probes").fetchone())

    def test_the_live_token_of_a_cancelled_job_is_refused_by_the_pass_fence(self):
        """check_token itself refuses it, even where the marker still names the token
        (the guard is the claim's job id, not the pass's liveness)."""
        import asks, db, passes
        t, _ = self.live_pass_of(A)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO meta(key, value) VALUES (?, 'x')",
                              (passes.cancelled_key(A),))
            with self.assertRaises(db.Refusal) as e:
                passes.check_token(self.conn, t)
        self.assertEqual(str(e.exception), self.WORDS)

    def test_a_fresh_claim_by_the_cancelled_job_is_refused(self):
        import asks, db, job
        job.claim(self.conn, A)
        asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        asks.request_work(self.conn, "check", "operator")
        gens = self.conn.execute("SELECT count(*) FROM claims").fetchone()[0]
        with self.assertRaises(db.Refusal) as e:
            job.claim(self.conn, A)
        self.assertEqual(str(e.exception), self.WORDS)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM claims").fetchone()[0], gens)
        self.assertEqual(self.drain(), "none")
        job.claim(self.conn, B)                                     # another job may
        self.assertEqual(self.drain(), B)

    def test_error_then_cancelled_before_any_pass_withdraws(self):
        """Astra R7-2: job_next paused after its claim committed; `error`, then
        `cancelled`. The cancel returned start_job and left the ask queued."""
        import asks, db, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)                                  # paused after this
        err = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertIsNotNone(err["start_job"])
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertIsNone(out["start_job"])
        self.assertEqual([x["text"] for x in out["texts"]], [self.LINE])
        self.assertEqual(self.work(), [("done", "stopped")])
        with self.assertRaises(db.Refusal):
            job.next_unit(self.conn, t)                              # the paused turn resumes
        self.assertEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0], 0)

    def test_error_then_cancelled_stops_the_orphaned_pass(self):
        import asks, json
        _, pid = self.live_pass_of(A)
        asks.request_work(self.conn, "check", "operator")
        asks.job_report(self.conn, job_id=A[:8], status="error")
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertIsNone(out["start_job"])
        self.assertFalse(out["orphaned"])
        row = self.conn.execute("SELECT outcome, report_json FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual(row["outcome"], "stopped")
        self.assertEqual(json.loads(row["report_json"])["stopped_reason"],
                         "you cancelled the check")
        self.assertEqual(self.work(), [("done", "stopped")] * 2)
        self.assertEqual([x["text"] for x in out["texts"]], [self.LINE])

    def test_a_cancel_while_another_job_holds_the_drain_changes_nothing(self):
        import asks, job, passes
        job.claim(self.conn, A)
        asks.job_report(self.conn, job_id=A[:8], status="error")
        asks.request_work(self.conn, "check", "operator")
        job.claim(self.conn, B)                                       # B drains now
        out = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertEqual(out["texts"], [])
        self.assertEqual(self.drain(), B)
        self.assertEqual(self.work(), [("queued", None)])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM meta WHERE key=?",
                                            (passes.cancelled_key(A),)).fetchone())

    def test_an_error_only_crash_still_restarts(self):
        import asks, job
        _, pid = self.live_pass_of(A)
        out = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertTrue(out["orphaned"])
        self.assertIsNotNone(out["start_job"])
        job.claim(self.conn, B)                                        # the restart adopts
        row = self.conn.execute("SELECT holder_job, ended_at FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual(tuple(row), (B, None))
        self.assertEqual(self.work(), [("taken", None)])


class CancelBeforeClaim(StoreCase):
    """#38 round 8 (Astra R8-1): a cancel that names a job before its first claim — no
    claim matches it — still withdraws while no job holds the drain, and the job's later
    claim is refused, matched by the #17 rule (an equal id, or one starting with the
    recorded ≥ 8-character prefix)."""
    LINE = Cancelled.LINE
    WORDS = CancelRevokes.WORDS

    def drain(self):
        return Report.drain(self)

    def work(self):
        return Cancelled.work(self)

    def test_a_cancel_before_the_first_claim_withdraws_and_refuses_the_job(self):
        import asks, db, job
        for named in (A, A[:8]):                                    # full id, 8 characters
            with self.subTest(named=named):
                self.conn.execute("DELETE FROM meta WHERE key LIKE 'cancelled:%'")
                self.conn.execute("DELETE FROM work_requests")
                self.conn.commit()
                asks.request_work(self.conn, "check", "operator")
                out = asks.job_report(self.conn, job_id=named, status="cancelled")
                self.assertIsNone(out["start_job"])
                self.assertEqual([x["text"] for x in out["texts"]], [self.LINE])
                self.assertEqual(self.work(), [("done", "stopped")])
                with self.assertRaises(db.Refusal) as e:
                    job.claim(self.conn, A)                         # X's job_next
                self.assertEqual(str(e.exception), self.WORDS)
                self.assertEqual(self.conn.execute("SELECT count(*) FROM claims"
                                                   ).fetchone()[0], 0)
                self.assertEqual(self.conn.execute("SELECT count(*) FROM passes"
                                                   ).fetchone()[0], 0)
        t = job.claim(self.conn, B)                                  # a replacement job Y
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "complete")   # no ask for it
        self.assertEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0], 0)

    def test_an_unmatched_cancel_while_a_job_holds_the_drain_changes_nothing(self):
        import asks, job
        job.claim(self.conn, B)
        asks.request_work(self.conn, "check", "operator")
        out = asks.job_report(self.conn, job_id=A, status="cancelled")
        self.assertEqual(out["texts"], [])
        self.assertEqual(self.drain(), B)
        self.assertEqual(self.work(), [("queued", None)])
        job.claim(self.conn, A)                                      # not recorded: allowed

    def test_a_replayed_unmatched_cancel_changes_nothing(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        first = asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        asks.request_work(self.conn, "check", "operator")            # asked again after it
        for named in (A[:8], A):                                     # the same notice, replayed
            again = asks.job_report(self.conn, job_id=named, status="cancelled")
            self.assertIsNotNone(again["start_job"])                 # the new ask stands
        self.assertEqual(self.work(), [("done", "stopped"), ("queued", None)])
        self.assertEqual([x["render_id"] for x in again["texts"]],
                         [x["render_id"] for x in first["texts"]])
        job.claim(self.conn, B)                                      # a new job takes it
        self.assertEqual(self.drain(), B)

    def test_a_matched_cancel_recorded_in_full_is_replayed_by_its_prefix(self):
        import asks, job
        job.claim(self.conn, A)
        asks.job_report(self.conn, job_id=A, status="cancelled")
        asks.request_work(self.conn, "check", "operator")
        asks.job_report(self.conn, job_id=A[:8], status="cancelled")
        self.assertEqual(self.work(), [("queued", None)])

    def test_the_id_is_still_checked(self):
        import asks, db
        with self.assertRaises(db.Refusal):
            asks.job_report(self.conn, job_id="aaaa", status="cancelled")


class CancelActsOnTheStore(StoreCase):
    """#38 round 9 (Astra): a cancel acts on the store's open work, not the cancelled
    id's. A claims a check, error(A) offers recovery, then cancelled(B) arrives before B's
    first claim: A's orphaned pass survived, the ask stayed taken, and C adopted it."""
    LINE = Cancelled.LINE
    REASON = Cancelled.REASON

    def live_pass_of(self, job_id):
        return Report.live_pass_of(self, job_id)

    def drain(self):
        return Report.drain(self)

    def work(self):
        return Cancelled.work(self)

    def assert_nothing_restarts(self, out, pid):
        import asks, db, job, json
        self.assertIsNone(out["start_job"])
        self.assertIsNone(out["line"])
        self.assertFalse(out["orphaned"])
        row = self.conn.execute("SELECT outcome, report_json FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual(row["outcome"], "stopped")
        self.assertEqual(json.loads(row["report_json"])["stopped_reason"], self.REASON)
        self.assertIsNone(asks.job_report(self.conn)["start_job"])
        with self.assertRaises(db.Refusal):
            job.claim(self.conn, B)                                 # B is revoked
        t = job.claim(self.conn, C)                                 # C adopts nothing
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "complete")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM passes").fetchone()[0], 1)

    def test_cancelling_the_unclaimed_recovery_job_stops_the_orphaned_pass(self):
        import asks
        _, pid = self.live_pass_of(A)
        err = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertTrue(err["orphaned"])
        out = asks.job_report(self.conn, job_id=B, status="cancelled")
        self.assertEqual([x["text"] for x in out["texts"]], [self.LINE])
        self.assertEqual(self.work(), [("done", "stopped")])
        self.assert_nothing_restarts(out, pid)

    def test_cancelling_the_unclaimed_recovery_job_closes_a_package_round(self):
        import asks, db, job, passes
        asks.request_package(self.conn, "2026-Q3", "telegram")
        t = job.claim(self.conn, A)
        with db.tx(self.conn):
            _, pid = passes.start_pass(self.conn, "package", "silent", protocol="job",
                                       token=t)
            self.conn.execute("UPDATE passes SET holder_job=? WHERE pass_id=?", (A, pid))
        self.bind_round_and_take(pid)
        asks.job_report(self.conn, job_id=A[:8], status="error")
        out = asks.job_report(self.conn, job_id=B, status="cancelled")
        pkg = self.conn.execute("SELECT state, reason FROM package_requests").fetchone()
        self.assertEqual(tuple(pkg), ("stopped", self.REASON))
        self.assertIn(self.REASON, out["speak"]["text"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM credits").fetchone()[0], 0)
        self.assert_nothing_restarts(out, pid)

    def test_a_package_past_its_check_is_not_withdrawn(self):
        """Operator ruling: snapshot-done / built / staged packages go on."""
        import asks
        self.package_built_unsent()
        asks.request_work(self.conn, "check", "operator")
        asks.job_report(self.conn, job_id=B, status="cancelled")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "built")
        self.assertEqual(self.work(), [("done", "stopped")])

    def test_an_error_only_crash_still_restarts(self):
        import asks, job
        _, pid = self.live_pass_of(A)
        out = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertTrue(out["orphaned"])
        self.assertIsNotNone(out["start_job"])
        job.claim(self.conn, C)
        self.assertEqual(self.conn.execute("SELECT holder_job FROM passes WHERE pass_id=?",
                                           (pid,)).fetchone()[0], C)
        self.assertEqual(self.work(), [("taken", None)])
