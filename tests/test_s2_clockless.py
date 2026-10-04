# tests/test_s2_clockless.py
import datetime as _dt
from tests._base import StoreCase


class Clockless(StoreCase):
    def job_pass(self):
        import db, passes
        with db.tx(self.conn):
            tok = passes.rotate(self.conn)
            _, pid = passes.start_pass(self.conn, "operator", "telegram", protocol="job",
                                       token=tok)
        return tok, pid

    def test_job_pass_keeps_the_claim_token_and_records_protocol(self):
        tok, pid = self.job_pass()
        m = self.conn.execute("SELECT generation, live FROM pass_marker").fetchone()
        self.assertEqual((m["generation"], m["live"]), (tok, 1))
        self.assertEqual(self.conn.execute("SELECT protocol FROM passes WHERE pass_id=?",
                                           (pid,)).fetchone()[0], "job")

    def test_no_clock_no_expiry_no_time_up_in_a_job_pass(self):
        import db, steps
        tok, pid = self.job_pass()
        steps.start(self.conn, tok, "sweep", {})
        later = db._clock() + _dt.timedelta(seconds=steps.STEP_EXPIRY_S + 5)
        with self.patch_clock(later):
            self.assertIsNone(steps.clock(self.conn, tok))
            self.assertIsNone(steps.sweep_allowance(self.conn))
            row = steps.latest(self.conn, pid)
            self.assertIsNone(steps._ended(row))      # a job step never expires by age

    def test_delegation_pass_still_expires(self):
        import db, steps
        tok = self.pass_("cron")
        steps.start(self.conn, tok, "sweep", {})
        later = db._clock() + _dt.timedelta(seconds=steps.STEP_EXPIRY_S + 5)
        with self.patch_clock(later):
            pid = self.conn.execute("SELECT pass_id FROM pass_marker").fetchone()[0]
            self.assertEqual(steps._ended(steps.latest(self.conn, pid)), "expired")


class InTxCores(StoreCase):
    """The in-tx cores the job cursor runs inside its one transaction (S2 Task 3)."""

    def job_pass(self):
        import db, passes
        with db.tx(self.conn):
            tok = passes.rotate(self.conn)
            _, pid = passes.start_pass(self.conn, "operator", "telegram", protocol="job",
                                       token=tok)
        return tok, pid

    def test_a_job_pass_id_is_its_token_and_a_sequence(self):
        tok, pid = self.job_pass()
        self.assertRegex(pid, rf"^j{tok}\.\d+$")

    def test_a_job_pass_needs_its_claims_token(self):
        import db, passes
        with db.tx(self.conn):
            with self.assertRaises(RuntimeError):
                passes.start_pass(self.conn, "operator", "telegram", protocol="job")

    def test_protocol_of(self):
        import passes
        tok, pid = self.job_pass()
        self.assertEqual(passes.protocol_of(self.conn, pid), "job")
        self.assertEqual(passes.protocol_of(self.conn, "nope"), "delegation")
        t = self.pass_("cron")
        p = self.conn.execute("SELECT pass_id FROM pass_marker").fetchone()[0]
        self.assertEqual(passes.protocol_of(self.conn, p), "delegation")
        self.assertIsNotNone(t)

    def test_a_step_row_records_its_protocol_sequence_and_generation(self):
        import steps
        tok, pid = self.job_pass()
        steps.start(self.conn, tok, "sweep", {})
        r = steps.latest(self.conn, pid)
        self.assertEqual((r["protocol"], r["started_gen"]), ("job", tok))
        self.assertIsNotNone(r["started_seq"])
        t = self.pass_("cron")
        steps.start(self.conn, t, "sweep", {})
        p = self.conn.execute("SELECT pass_id FROM pass_marker").fetchone()[0]
        r = steps.latest(self.conn, p)
        self.assertEqual((r["protocol"], r["started_gen"]), ("delegation", t))
        self.assertIsNotNone(r["started_seq"])

    def test_the_cores_assert_an_open_transaction(self):
        import passes, steps
        tok, _ = self.job_pass()
        with self.assertRaises(AssertionError):
            steps._start_tx(self.conn, tok, "sweep", {})
        with self.assertRaises(AssertionError):
            steps._finish_tx(self.conn, tok, "sweep", counts={})
        with self.assertRaises(AssertionError):
            passes._end_pass_tx(self.conn, tok, "complete", {})

    def test_start_finish_and_end_run_inside_one_transaction(self):
        import db, passes, steps
        tok, pid = self.job_pass()
        with db.tx(self.conn):
            started = steps._start_tx(self.conn, tok, "sweep", {})
            res, refused = steps._finish_tx(self.conn, tok, "sweep",
                                            counts={"remaining_in_cycle": 0})
            ended = passes._end_pass_tx(self.conn, tok, "complete", {})
        self.assertEqual(started["step"], "sweep")
        self.assertEqual((res, refused), ({"step": "sweep", "finished": True,
                                           "already": False}, None))
        self.assertEqual((ended["ended"], ended["outcome"]), (pid, "complete"))
        self.assertNotIn("speak", ended)
        row = self.conn.execute("SELECT outcome, ended_at FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual(row["outcome"], "complete")
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)

    def test_judgment_gap_counts_what_judgment_owed_refuses_for(self):
        import db, passes, work
        from unittest import mock
        tok, pid = self.job_pass()
        due = {7: "s7", 8: "s8"}
        with mock.patch.object(work, "judge_due_state", lambda conn, q=None: dict(due)):
            self.assertEqual(passes.judgment_gap(self.conn, pid), 0)      # nothing swept
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO pass_steps(pass_id, step, started_at,"
                                  " finished_at) VALUES (?, 'sweep', ?, ?)",
                                  (pid, db.now(), db.now()))
            self.assertEqual(passes.judgment_gap(self.conn, pid), 2)      # no judge step
            with self.assertRaisesRegex(db.Refusal, "2 payments .* Start the judge step"):
                passes._judgment_owed(self.conn, pid)
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO pass_steps(pass_id, step, started_at,"
                                  " finished_at, carry_json) VALUES (?, 'judge', ?, ?, ?)",
                                  (pid, db.now(), db.now(),
                                   db.canonical({"due_at_start": {"7": "s7", "8": "old"}})))
            self.assertEqual(passes.judgment_gap(self.conn, pid), 1)      # 8 changed since
            with self.assertRaisesRegex(db.Refusal, "1 payment .* End it interrupted"):
                passes._judgment_owed(self.conn, pid)
            due.pop(8)
            self.assertEqual(passes.judgment_gap(self.conn, pid), 0)
            passes._judgment_owed(self.conn, pid)                          # no refusal

    def test_patch_clock_restores_the_clock(self):
        import db
        real = db._clock
        at = _dt.datetime(2030, 1, 1, tzinfo=_dt.timezone.utc)
        with self.patch_clock(at):
            self.assertEqual(db._clock(), at)
        self.assertIs(db._clock, real)
