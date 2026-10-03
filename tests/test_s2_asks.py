"""S2 Task 6 (spec §6.1–6.2, INV-J9, INV-J12, §6.3): work requests are recorded before
start_job, taken only by the live job pass, and given a disposition in the transaction that
decides it."""
from tests._base import StoreCase

A = "aaaaaaaa-1"


class Requests(StoreCase):
    def test_request_records_then_offers_start_job(self):
        import asks
        out = asks.request_work(self.conn, "check", "operator")
        self.assertEqual(out["start_job"]["job"], "quarterly-accounting:work")
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "queued")

    def test_a_package_ask_opens_a_queued_request_without_a_pass(self):
        import asks
        out = asks.request_package(self.conn, "2026-Q3", "telegram")
        self.assertEqual(out["status"], "asked")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "queued")
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone(), None)
        self.assertEqual(asks.request_package(self.conn, "2026-Q3", "email")["status"],
                         "already")

    def test_taken_only_by_the_live_pass_and_settled_at_its_end(self):
        import asks, db, job, passes
        asks.request_work(self.conn, "check", "cron")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            self.assertEqual(len(asks.take_queued(self.conn, pid)), 1)
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "stopped", {})
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))

    def test_a_cron_check_with_nothing_to_show_is_reported_at_its_pass_end(self):
        """FW-I3 (M7): spec §6.4 — a cron check served complete or interrupted is
        reported in the transaction that settles it; an operator check waits for its
        result to be shown, and a stopped cron check for its stop line."""
        import asks, db, job, passes
        for outcome, want in (("complete", "reported"), ("interrupted", "reported"),
                              ("stopped", "done")):
            with self.subTest(outcome=outcome):
                asks.request_work(self.conn, "check", "cron")
                asks.request_work(self.conn, "check", "operator")
                t = job.claim(self.conn, A)
                pid = self.start_job_pass(t)
                with db.tx(self.conn):
                    asks.take_queued(self.conn, pid)
                    passes._end_pass_tx(self.conn, t, outcome, {})
                got = dict(self.conn.execute("SELECT trigger, state FROM work_requests WHERE"
                                             " pass_id=?", (pid,)).fetchall())
                self.assertEqual(got, {"cron": want, "operator": "done"})

    def test_terminalize_requeues(self):
        import asks, db, job, passes
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            passes._terminalize(self.conn, pid)
        r = self.conn.execute("SELECT state, pass_id FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["pass_id"]), ("queued", None))

    def test_exhausted_adoptions_close_a_check_pass_requests(self):
        import asks, db, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        for j in ("bbbbbbbb-2", "cccccccc-3", "dddddddd-4"):
            job.claim(self.conn, j)
        w = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((w["state"], w["outcome"]), ("done", "stopped"))

    def test_exhausted_adoptions_close_a_package_round_and_leave_other_work_queued(self):
        import asks, job
        asks.request_package(self.conn, "2026-Q3", "telegram")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t, trigger="package")
        self.bind_round_and_take(pid)
        asks.request_work(self.conn, "check", "operator")   # not this pass's to take
        for j in ("bbbbbbbb-2", "cccccccc-3", "dddddddd-4"):
            job.claim(self.conn, j)
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "stopped")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts WHERE kind="
                                           "'package-stopped'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "queued")

    def test_reset_fences_every_claim_and_drops_requests(self):
        import asks, binding, db, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        binding.reset_store(self.conn)
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                job.check_claim(self.conn, t)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT value FROM meta WHERE key='drain'").fetchone())

    def test_a_handover_is_done_only_after_a_later_whole_judgment_naming_its_docs(self):
        import asks, db, job, steps
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        steps.start(self.conn, t, "sweep", {})
        steps.finish(self.conn, t, "sweep", counts={"remaining_in_cycle": 0})
        self.hand_empty_chunk()
        steps.start(self.conn, t, "judge", {})
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})  # before the ask
        d = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            self.assertFalse(asks.handover_covered(self.conn, pid))
        steps.start(self.conn, t, "judge", {})          # restarted after the ask
        with db.tx(self.conn):
            asks.record_verdicts(self.conn, pid, {str(d): "no-payment-yet"})
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})
        with db.tx(self.conn):
            self.assertTrue(asks.handover_covered(self.conn, pid))

    def test_a_verdict_from_an_earlier_judgment_does_not_cover(self):
        import asks, db, job, steps
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        steps.start(self.conn, t, "sweep", {})
        steps.finish(self.conn, t, "sweep", counts={"remaining_in_cycle": 0})
        self.hand_empty_chunk()
        d = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        steps.start(self.conn, t, "judge", {})
        with db.tx(self.conn):
            asks.record_verdicts(self.conn, pid, {str(d): "no-payment-yet"})
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})
        steps.start(self.conn, t, "judge", {})          # a restart (e.g. after a refresh)
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})   # names nothing
        with db.tx(self.conn):
            self.assertFalse(asks.handover_covered(self.conn, pid))

    def test_a_job_package_round_is_handed_over_with_no_token_and_no_lease(self):
        """A job pass's package request is settled with token=None (§6.4): job_report
        claims a buildable one at once, so it holds no lease another turn waits out."""
        import asks, db, job, passes, steps
        asks.request_package(self.conn, "2026-Q3", "telegram")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t, trigger="package")
        self.bind_round_and_take(pid)
        steps.start(self.conn, t, "snapshot", {})
        with db.tx(self.conn):          # this pass's own import
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id)"
                              " VALUES (?, ?, 0, 0)", (pid, db.now()))
        steps.finish(self.conn, t, "snapshot", counts={})
        self.check_round(t)
        gen = self.conn.execute("SELECT value FROM counters WHERE name='pass_generation'"
                                ).fetchone()[0]
        with db.tx(self.conn):
            out = passes._end_pass_tx(self.conn, t, "complete", {})
        r = self.conn.execute("SELECT state, token, lease_at FROM package_requests").fetchone()
        self.assertEqual((r["state"], r["token"], r["lease_at"]), ("snapshot-done", None, None))
        self.assertIsNone(out["package_token"])
        self.assertEqual(self.conn.execute("SELECT value FROM counters WHERE"
                                           " name='pass_generation'").fetchone()[0], gen)

    def _judged_pass(self):
        """A live job check pass whose sweep is done and whose empty Gmail chunk is out:
        a judge step may start. Returns (token, pass_id)."""
        import job, steps
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        steps.start(self.conn, t, "sweep", {})
        steps.finish(self.conn, t, "sweep", counts={"remaining_in_cycle": 0})
        self.hand_empty_chunk()
        return t, pid

    def test_a_judgment_running_when_the_handover_was_asked_does_not_cover_it(self):
        """INV-J12: the judgment must START after the ask — a verdict recorded, after the
        ask, by a judgment already running is not enough."""
        import asks, db, steps
        t, pid = self._judged_pass()
        steps.start(self.conn, t, "judge", {})              # started before the ask
        d = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            asks.record_verdicts(self.conn, pid, {str(d): "no-payment-yet"})
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})
        with db.tx(self.conn):
            self.assertFalse(asks.handover_covered(self.conn, pid))

    def test_verdicts_are_refused_with_no_judge_step_running(self):
        import asks, db
        _, pid = self._judged_pass()
        d = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                asks.take_queued(self.conn, pid)
                asks.record_verdicts(self.conn, pid, {str(d): "matched"})

    def test_a_package_round_takes_no_work_request(self):
        """INV-J9 with §6.1: a package round serves its own request only; a check asked
        before it began stays queued for the next check pass."""
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        asks.request_package(self.conn, "2026-Q3", "telegram")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t, trigger="package")
        self.assertEqual(self.bind_round_and_take(pid), [])
        r = self.conn.execute("SELECT state, pass_id FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["pass_id"]), ("queued", None))

    def _covered_after_judge_finish(self, **finish):
        """A handover asked, then a judgment started after it that records a verdict for
        every document and finishes with `finish`: is the handover covered?"""
        import asks, db, steps
        t, pid = self._judged_pass()
        d = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        steps.start(self.conn, t, "judge", {})              # started after the ask
        with db.tx(self.conn):
            asks.record_verdicts(self.conn, pid, {str(d): "no-payment-yet"})
        steps.finish(self.conn, t, "judge", **finish)
        with db.tx(self.conn):
            return asks.handover_covered(self.conn, pid)

    def test_the_whole_judgment_baseline_covers(self):
        self.assertTrue(self._covered_after_judge_finish(counts={"triage_remaining": 0}))

    def test_a_judgment_with_triage_left_does_not_cover(self):
        """INV-J12 / work.judge_whole: every triage page seen."""
        self.assertFalse(self._covered_after_judge_finish(counts={"triage_remaining": 3}))

    def test_a_stopped_judgment_does_not_cover(self):
        self.assertFalse(self._covered_after_judge_finish(counts={"triage_remaining": 0},
                                                          stopped="the ledger changed"))

    def test_an_out_of_time_judgment_does_not_cover(self):
        """#15/D3: a timed-out judgment covers nothing."""
        self.assertFalse(self._covered_after_judge_finish(counts={"triage_remaining": 0},
                                                          out_of_time=True))

    def test_a_failed_judgment_does_not_cover(self):
        self.assertFalse(self._covered_after_judge_finish(counts={"triage_remaining": 0},
                                                          failed=True))
