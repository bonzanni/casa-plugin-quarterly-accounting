# tests/test_continuation.py
"""A pass that outlives its delegation (issue #2). The pass's progress is in the
store (pass_steps); continue_pass is the one claim, and every claim rotates the
token the stale-pass fence already checks. Driven through qa_server.handle
against the real bank-feed (tests/bankfeed.py); the clock is db._clock, advanced
by hand."""
import datetime as _dt
import hashlib
import json
import multiprocessing
import unittest
from unittest import mock

from tests import _procs, sim
from tests.test_e2e import ToolFlow
import db  # noqa: E402
import passes  # noqa: E402
import steps  # noqa: E402

START = _dt.datetime(2026, 9, 28, 9, 0, tzinfo=_dt.timezone.utc)
STALE = "refused: this pass is no longer the current one"


class Clock:
    def __init__(self, start=START):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += _dt.timedelta(seconds=seconds)


class Flow(ToolFlow):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        p = mock.patch.object(db, "_clock", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def text(self, name, **args):
        import qa_server
        out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": args}})
        return out["result"]["content"][0]["text"]

    def seed(self, n=3, documents=0):
        """n Q3 payments that want an invoice; the first `documents` of them get one."""
        bf = self.bf
        bf.fetch([bf.row(f"2026-07-{5 + i:02d}", ref=f"R{i}", amount=1000 + i,
                         counterparty="Adobe") for i in range(n)])
        for r in self.active():
            self.classify(r["row_id"], "software")
        self.first_pass()
        for i in range(documents):
            self.file(amount_minor=1000 + i, document_date=f"2026-07-{5 + i:02d}")

    def probe_import(self, token):
        bf = self.bf
        accounts = [{"account_id": r["account_id"], "category": r["category"], "label": r["name"]}
                    for r in bf.conn.execute("SELECT account_id, category, name FROM accounts")]
        self.call("record_probe", pass_token=token, kind="bank_tools", ok=True)
        self.call("record_probe", pass_token=token, kind="bank_accounts", ok=True,
                  data={"accounts": accounts})
        self.call("record_probe", pass_token=token, kind="bank_sync", ok=True)
        self.call("record_probe", pass_token=token, kind="ledger", ok=True,
                  data=sim.ledger_state(bf.listing()))
        return self.call("import_ledger_export", path=bf.export(), pass_token=token,
                         ledger_instance=bf.last_export_instance)

    def specialist(self, token, step="sweep", finish=True, budget=None):
        """The specialist's pass (probes, import, sweep, triage listing), then its finish."""
        self.probe_import(token)
        remaining = self.sweep(token, budget=budget)
        tri = self.call("list_quarter_state", triage=True, pass_token=token)
        if finish:
            out = self.call("record_step", pass_token=token, step=step, action="finish",
                            remaining_in_cycle=remaining, triage_remaining=tri["remaining"])
            self.assertTrue(out["finished"], out)
        return remaining

    def begin(self, trigger="operator", **kw):
        out = self.call("begin_pass", trigger=trigger, **kw)
        self.assertEqual(out["status"], "started", out)
        return out["pass_token"]

    def start(self, token, step="sweep", **carry):
        return self.call("record_step", pass_token=token, step=step, action="start", **carry)

    def claim(self):
        return self.call("continue_pass")

    def digest(self):
        h = hashlib.sha256()
        for line in self.conn.iterdump():
            h.update(line.encode())
        return h.hexdigest()


class TestPendingThenTheNotice(Flow):
    def test_the_notice_continues_the_pass_under_a_new_token(self):
        # (1) the delegation degraded to pending; its outcome arrives on a later turn
        self.seed(3)
        t1 = self.begin()
        st = self.start(t1)
        self.assertEqual(st["step"], "sweep")
        self.assertEqual(st["started_at"], "2026-09-28T09:00:00Z")
        self.assertEqual((st["sweep_stop_at"], st["return_by"]),
                         ("2026-09-28T09:07:30Z", "2026-09-28T09:08:30Z"))
        self.specialist(t1)
        c = self.claim()["continue"]
        t2 = c["pass_token"]
        self.assertNotEqual(t2, t1)
        self.assertEqual((c["step"], c["next"], c["ended"], c["reply"], c["trigger"]),
                         ("sweep", "gmail-round", "finished", "telegram", "operator"))
        self.assertTrue(c["imported"] and c["can_run"])
        self.assertEqual(c["finish"]["remaining_in_cycle"], 0)
        # issue #3: the same items, in the same order, in the Gmail round's shape
        listed = self.call("list_quarter_state", triage=True)
        self.assertEqual([d["pid"] for d in c["work"]["triage"]],
                         [d["pid"] for d in listed["triage"]])
        self.assertEqual({k: c["work"][k] for k in ("total", "truncated", "remaining", "not_fresh")},
                         {k: listed[k] for k in ("total", "truncated", "remaining", "not_fresh")})
        self.assertEqual(set(c["work"]["triage"][0]),
                         {"pid", "date", "amount_minor", "currency", "direction", "pending",
                          "counterparty", "expectation", "search_hint", "window_days",
                          "portal", "fresh"})
        self.assertEqual(len(c["work"]["triage"]), 3)
        pid = c["work"]["triage"][0]["pid"]
        # the specialist's token, and every earlier holder's, is refused from now on
        self.assertTrue(self.text("record_search", pid=pid, pass_token=t1,
                                  queries=["adobe"]).startswith(STALE))
        self.assertTrue(self.text("end_pass", pass_token=t1,
                                  outcome="complete").startswith(STALE))
        # the Gmail round and the end succeed with the new one
        self.call("record_probe", pass_token=t2, kind="gmail", ok=True)
        for item in c["work"]["triage"]:
            self.call("record_search", pid=item["pid"], pass_token=t2, queries=["adobe"])
        end = self.call("end_pass", pass_token=t2, outcome="complete",
                        report={"checked": 3, "total": 3, "not_searched": 0})
        self.assertEqual(end["outcome"], "complete")
        self.assertEqual(self.claim(), {"continue": None, "speak": None})


class TestTheCeiling(Flow):
    def test_pages_shrink_then_time_up_and_the_clock_says_wrap_up(self):
        # (2) no row is listed after SWEEP_STOP_S; wrap_up comes at RETURN_BY_S
        self.seed(8)
        t1 = self.begin()
        self.start(t1)
        self.probe_import(t1)
        first = self.call("list_projections", pass_token=t1, limit=3)
        self.assertEqual(len(first["projections"]), 3)
        self.assertEqual(first["clock"], {"elapsed_s": 0, "time_left_s": 510, "wrap_up": False})
        self.assertFalse(first["time_up"])
        self.clock.advance(430)                    # 20 s left: two rows' worth
        page = self.call("list_projections", pass_token=t1)
        self.assertEqual(len(page["projections"]), 2)
        self.assertEqual(page["remaining_in_cycle"], 6)
        self.clock.advance(20)                     # SWEEP_STOP_S
        cursor = tuple(self.conn.execute("SELECT * FROM cursor").fetchone())
        up = self.call("list_projections", pass_token=t1)
        self.assertEqual((up["projections"], up["time_up"]), ([], True))
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM cursor").fetchone()), cursor)
        self.clock.advance(59)
        self.assertFalse(self.call("list_quarter_state", triage=True,
                                   pass_token=t1)["clock"]["wrap_up"])
        self.clock.advance(1)                      # RETURN_BY_S
        self.assertEqual(self.call("list_quarter_state", triage=True, pass_token=t1)["clock"],
                         {"elapsed_s": 510, "time_left_s": 0, "wrap_up": True})

    def test_a_step_cut_at_the_ceiling_is_claimed_at_expiry(self):
        self.seed(4)
        t1 = self.begin()
        self.start(t1)
        self.probe_import(t1)
        self.sweep(t1, budget=2)                   # cut: no finish ever comes
        self.clock.advance(599)
        r = self.claim()
        self.assertIsNone(r["continue"])
        self.assertEqual((r["running"]["step"], r["running"]["due_in_s"]), ("sweep", 1))
        self.clock.advance(1)
        c = self.claim()["continue"]
        self.assertEqual((c["ended"], c["next"], c["imported"]),
                         ("expired", "gmail-round", True))
        self.assertGreater(c["throughput"]["swept_this_pass"], 0)
        self.assertGreater(c["throughput"]["remaining_in_cycle"], 0)
        # the cut specialist's late writes are refused
        pid = self.conn.execute("SELECT min(pid) FROM projections").fetchone()[0]
        snap = self.conn.execute("SELECT max(snapshot_id) FROM snapshots").fetchone()[0]
        self.assertTrue(self.text("record_observation", pid=pid, pass_token=t1,
                                  snapshot_id=snap, not_found=True).startswith(STALE))
        self.assertTrue(self.text("record_step", pass_token=t1, step="sweep",
                                  action="finish").startswith(STALE))
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted")
        report = json.loads(self.conn.execute("SELECT report_json FROM passes WHERE pass_id=?",
                                              (end["ended"],)).fetchone()[0])
        self.assertGreater(report["swept_this_pass"], 0)


class TestInlineErrors(Flow):
    def test_an_error_before_the_import_ends_the_pass_failed(self):
        # (3) the delegation errored in Ellen's own turn: she finishes the step, failed
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.call("record_step", pass_token=t1, step="sweep", action="finish", failed=True)
        c = self.claim()["continue"]
        self.assertEqual((c["ended"], c["next"], c["imported"]), ("errored", "end-pass", False))
        self.call("end_pass", pass_token=c["pass_token"], outcome="failed")

    def test_an_error_after_the_import_still_runs_the_gmail_round(self):
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.probe_import(t1)
        self.call("record_step", pass_token=t1, step="sweep", action="finish", failed=True)
        c = self.claim()["continue"]
        self.assertEqual((c["ended"], c["next"], c["imported"]), ("errored", "gmail-round", True))

    def test_a_stopped_sweep_goes_to_the_end(self):
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.probe_import(t1)
        self.call("record_step", pass_token=t1, step="sweep", action="finish",
                  stopped="the ledger was restored since this pass began " + "x" * 400)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
        self.assertLessEqual(len(c["finish"]["stopped"]), 300)

    def test_a_sweep_out_of_time_is_not_a_stop(self):
        # issue #10: the specialist said `stopped` for the clock running out; the pass
        # still reaches its Gmail round, and the words are kept as said
        self.seed(3)
        t1 = self.begin()
        self.start(t1)
        self.probe_import(t1)
        self.clock.advance(steps.SWEEP_STOP_S)
        self.assertTrue(self.call("list_projections", pass_token=t1)["time_up"])
        out = self.call("record_step", pass_token=t1, step="sweep", action="finish",
                        remaining_in_cycle=2, triage_remaining=3,
                        stopped="time_up: wall-clock budget for this pass exhausted")
        self.assertIn("out of time, not stopped", out["recorded_as"])
        c = self.claim()["continue"]
        self.assertEqual((c["ended"], c["next"]), ("finished", "gmail-round"))
        self.assertEqual(c["finish"], {"remaining_in_cycle": 2, "triage_remaining": 3,
                                       "time_up": True, "said": "time_up: wall-clock budget "
                                       "for this pass exhausted"})
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted")
        self.assertEqual(end["outcome"], "interrupted")

    def test_a_stop_before_the_time_is_up_still_stops(self):
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.probe_import(t1)
        self.clock.advance(steps.SWEEP_STOP_S - 1)
        out = self.call("record_step", pass_token=t1, step="sweep", action="finish",
                        stopped="the ledger was restored since this pass began")
        self.assertNotIn("recorded_as", out)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
        self.assertEqual(c["finish"], {"stopped": "the ledger was restored since this pass began"})

    def test_a_stop_without_an_import_keeps_its_reason_whatever_the_time(self):
        # before the import the pass ends anyway, and the refusal is what is told
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.clock.advance(steps.SWEEP_STOP_S + 30)
        self.call("record_step", pass_token=t1, step="sweep", action="finish",
                  stopped="the import was refused: another session holds the documents lock")
        c = self.claim()["continue"]
        self.assertEqual((c["next"], c["imported"]), ("end-pass", False))
        self.assertIn("documents lock", c["finish"]["stopped"])
        self.assertNotIn("time_up", c["finish"])


class TestNoticesThatCarryNothing(Flow):
    def test_a_foreign_notice_during_a_running_step_changes_nothing(self):
        # (4)
        self.assertEqual(self.claim(), {"continue": None, "speak": None})
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        before = self.digest()
        r = self.claim()
        self.assertIsNone(r["continue"])
        self.assertEqual(r["running"]["step"], "sweep")
        self.assertNotIn("speak", r)
        self.assertEqual(self.digest(), before)

    def test_a_replay_after_a_restart_finds_nothing_running_or_held(self):
        # (5)
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.specialist(t1)
        c = self.claim()["continue"]
        self.assertEqual(self.claim(), {"continue": None, "held": True})   # the fresh lease
        t2 = c["pass_token"]
        self.start(t2, step="judge", report={"checked": 1, "total": 1, "not_searched": 0})
        self.assertEqual(self.claim()["running"]["step"], "judge")          # step 5 running
        self.call("record_step", pass_token=t2, step="judge", action="finish")
        c3 = self.claim()["continue"]
        self.assertEqual((c3["step"], c3["next"]), ("judge", "end-pass"))
        self.assertEqual(c3["report"], {"checked": 1, "total": 1, "not_searched": 0})
        self.call("end_pass", pass_token=c3["pass_token"], outcome="complete")
        self.assertIsNone(self.claim()["continue"])

    def test_a_restart_between_launch_and_the_first_write_is_claimed_at_expiry(self):
        # (6) the step started, the delegation never wrote
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.clock.advance(steps.STEP_EXPIRY_S)
        c = self.claim()["continue"]
        self.assertEqual((c["ended"], c["next"], c["imported"]), ("expired", "end-pass", False))

    def test_a_pass_that_never_delegated_is_claimed_at_expiry(self):
        self.seed(1)
        self.begin()
        self.assertEqual(self.claim()["running"]["step"], None)
        self.clock.advance(steps.STEP_EXPIRY_S)
        c = self.claim()["continue"]
        self.assertEqual((c["step"], c["next"], c["outcome"]), (None, "end-pass", "failed"))
        self.assertEqual(self.claim(), {"continue": None, "held": True})
        self.call("end_pass", pass_token=c["pass_token"], outcome="failed")


class TestTheClaimIsExclusive(Flow):
    def test_two_sessions_race_the_claim_and_exactly_one_wins(self):
        # (7) two processes, released together, on one due step
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.call("record_step", pass_token=t1, step="sweep", action="finish", failed=True)
        ctx = multiprocessing.get_context("spawn")
        barrier, out = ctx.Barrier(2), ctx.Queue()
        path = str(db.data_dir() / db.DB_NAME)
        procs = [ctx.Process(target=_procs.continue_pass, args=(path, barrier, out))
                 for _ in range(2)]
        for p in procs:
            p.start()
        got = [out.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(60)
        tokens = [g["continue"]["pass_token"] for g in got if g.get("continue")]
        self.assertEqual(len(tokens), 1, got)
        self.assertIn({"continue": None, "held": True}, got)
        self.assertEqual(self.conn.execute("SELECT generation FROM pass_marker").fetchone()[0],
                         tokens[0])

    def test_a_superseded_claimant_is_refused_everywhere(self):
        # (8) A claims and goes quiet; B claims after the lease lapses
        self.seed(2)
        t1 = self.begin()
        self.start(t1)
        self.specialist(t1)
        a = self.claim()["continue"]["pass_token"]
        self.clock.advance(steps.LEASE_S)
        b = self.claim()["continue"]["pass_token"]
        self.assertNotIn(b, (t1, a))
        pid = self.conn.execute("SELECT min(pid) FROM projections").fetchone()[0]
        self.assertTrue(self.text("end_pass", pass_token=a, outcome="complete").startswith(STALE))
        self.assertTrue(self.text("record_search", pid=pid, pass_token=a,
                                  queries=["q"]).startswith(STALE))
        self.assertTrue(self.text("record_step", pass_token=a, step="judge",
                                  action="start").startswith(STALE))
        # B starts the judge step; A wakes: still refused, and nothing is due
        self.start(b, step="judge")
        self.assertTrue(self.text("record_probe", pass_token=a, kind="gmail",
                                  ok=True).startswith(STALE))
        self.assertEqual(self.claim()["running"]["step"], "judge")
        self.call("record_step", pass_token=b, step="judge", action="finish")
        c = self.claim()["continue"]
        self.call("end_pass", pass_token=c["pass_token"], outcome="complete")

    def test_a_lease_its_holder_keeps_refreshing_is_not_reclaimed(self):
        # (17) held at 599 s with no write, reclaimable at 600 s; a write refreshes it
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.specialist(t1)
        a = self.claim()["continue"]["pass_token"]
        self.clock.advance(599)
        self.assertEqual(self.claim(), {"continue": None, "held": True})
        self.clock.advance(1)
        self.assertIsNotNone(self.claim()["continue"])
        # again, with a write at 590 s
        b = self.conn.execute("SELECT generation FROM pass_marker").fetchone()[0]
        self.assertNotEqual(a, b)
        self.clock.advance(590)
        self.call("record_probe", pass_token=b, kind="gmail", ok=True)
        self.clock.advance(310)
        self.assertEqual(self.claim(), {"continue": None, "held": True})


class TestBusyAndReclaim(Flow):
    def test_busy_says_a_check_is_running_and_names_no_machinery(self):
        # (15)
        import views
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.clock.advance(7 * 60)
        busy = self.call("begin_pass", trigger="operator")
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(busy["text"], "A check is running — started 7 minutes ago.\n"
                                       "Ask again in a few minutes.")
        self.assertLessEqual(views.utf16_len(busy["text"]), views.TELEGRAM_LIMIT)
        for word in views.FORBIDDEN:
            self.assertNotIn(word, busy["text"].lower())

    def test_a_reclaimed_pass_is_ended_interrupted(self):
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.clock.advance(passes.STALE_AFTER_S)
        out = self.call("begin_pass", trigger="operator")
        self.assertTrue(out["reclaimed"])
        self.assertIsNone(out["recovered"])
        old = self.conn.execute("SELECT * FROM passes WHERE generation=?", (t1,)).fetchone()
        self.assertEqual((old["outcome"], old["ended_at"]), ("interrupted", "2026-09-28T12:00:00Z"))
        self.assertTrue(json.loads(old["report_json"])["reclaimed"])
        self.assertEqual(self.call("check_setup")["last_pass"]["pass_id"], old["pass_id"])
        self.assertTrue(self.text("record_step", pass_token=t1, step="sweep",
                                  action="finish").startswith(STALE))


class TestContractAndSurface(Flow):
    def test_the_cron_reports_silently(self):
        # (18)
        self.seed(1)
        t1 = self.begin("cron")
        self.start(t1)
        self.call("record_step", pass_token=t1, step="sweep", action="finish", failed=True)
        self.assertEqual(self.claim()["continue"]["reply"], "silent")

    def test_record_step_refusals(self):
        self.seed(1)
        self.assertTrue(self.text("record_step", step="sweep",
                                  action="start").startswith("refused: missing argument"))
        t1 = self.begin()
        for args, words in (
                ({"step": "snapshot", "action": "start", "quarter": "2026-Q3",
                  "channel": "telegram"}, "refused: a snapshot step belongs to"),
                ({"step": "handover", "action": "start", "doc_ids": [1]},
                 "refused: a handover step belongs to"),
                ({"step": "sweep", "action": "finish"}, "refused: the sweep step was not started"),
                ({"step": "sweep", "action": "start", "remaining_in_cycle": 1},
                 "refused: remaining_in_cycle goes with action=\"finish\""),
                ({"step": "sweep", "action": "start", "doc_ids": [1]},
                 "refused: doc_ids go with a handover start"),
                ({"step": "sweep", "action": "go"}, "refused: action is"),
                ({"step": "audit", "action": "start"}, "refused: step is")):
            self.assertTrue(self.text("record_step", pass_token=t1, **args).startswith(words),
                            (args, self.text("record_step", pass_token=t1, **args)))
        self.start(t1)
        self.assertTrue(self.text("record_step", pass_token=t1, step="sweep",
                                  action="start").startswith("refused: the sweep step was "
                                                             "already started"))
        self.assertTrue(self.text("record_step", pass_token=t1, step="sweep", action="finish",
                                  quarter="2026-Q3").startswith("refused: quarter goes with"))
        self.call("record_step", pass_token=t1, step="sweep", action="finish")
        again = self.call("record_step", pass_token=t1, step="sweep", action="finish",
                          failed=True)
        self.assertTrue(again["already"])
        self.assertEqual(self.claim()["continue"]["ended"], "finished")
        self.assertTrue(self.text("record_step", pass_token=t1 + 999, step="judge",
                                  action="start").startswith(STALE))

    def test_the_clock_is_absent_without_a_running_step(self):
        self.seed(1)
        t1 = self.begin()
        self.probe_import(t1)
        page = self.call("list_projections", pass_token=t1)
        self.assertNotIn("clock", page)
        self.assertFalse(page["time_up"])
        self.assertEqual(len(page["projections"]), 1)
        self.assertNotIn("clock", self.call("list_quarter_state", triage=True))
        self.start(t1)
        self.assertIn("clock", self.call("list_quarter_state", triage=True, pass_token=t1))
        self.call("record_step", pass_token=t1, step="sweep", action="finish")
        self.assertNotIn("clock", self.call("list_quarter_state", triage=True, pass_token=t1))

    def test_the_work_list_names_the_search_hint_and_window(self):
        self.seed(1)
        import qa_server
        qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "upsert_counterparty",
            "arguments": {"name": "Adobe", "patterns": ["Adobe"], "window_days": 14,
                          "search_hint": "from:adobe.com subject:invoice"}}})
        t1 = self.begin()
        self.start(t1)
        self.specialist(t1)
        item = self.claim()["continue"]["work"]["triage"][0]
        self.assertEqual((item["search_hint"], item["window_days"]),
                         ("from:adobe.com subject:invoice", 14))


class TestReviewC1(Flow):
    """Code review round C1 on the continuation."""
    def test_a_finished_sweep_is_continued_after_a_long_restart(self):
        # A2: a finished step older than the reclaim threshold is still continued, and
        # begin_pass does not reclaim a pass whose claim holds a fresh lease
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.specialist(t1)
        self.clock.advance(passes.STALE_AFTER_S + 60)
        c = self.claim()["continue"]
        self.assertEqual((c["step"], c["next"]), ("sweep", "gmail-round"))
        busy = self.call("begin_pass", trigger="operator")
        self.assertEqual(busy["status"], "busy")
        self.call("record_probe", pass_token=c["pass_token"], kind="gmail", ok=True)
        self.assertEqual(self.call("end_pass", pass_token=c["pass_token"],
                                   outcome="complete")["outcome"], "complete")

    def test_an_inline_answer_without_a_finish_is_closed_by_ellens_finish(self):
        # I2: the specialist returned (even with its prefix) but recorded no finish: Ellen
        # always finishes the step herself, so the pass is due at once, never `running`
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.probe_import(t1)
        self.assertEqual(self.claim()["running"]["step"], "sweep")
        out = self.call("record_step", pass_token=t1, step="sweep", action="finish")
        self.assertFalse(out["already"])
        self.assertEqual(self.claim()["continue"]["ended"], "finished")

    def test_ellens_finish_after_the_specialists_is_a_no_op(self):
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.specialist(t1)
        out = self.call("record_step", pass_token=t1, step="sweep", action="finish")
        self.assertTrue(out["already"])
        c = self.claim()["continue"]
        self.assertEqual(c["finish"]["remaining_in_cycle"], 0)

    def test_the_clock_answers_a_token_passed_as_text(self):
        # M4: _int's rule — an integer, or its digits as text — for the clock as well
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.assertIn("clock", self.call("list_quarter_state", triage=True,
                                         pass_token=str(t1)))

    def test_rotate_outside_a_transaction_is_an_error_not_an_assert(self):
        with self.assertRaises(RuntimeError):
            passes.rotate(self.conn)

    def test_the_sim_finishes_an_earlier_continuation_then_runs_its_own_pass(self):
        # I6: a continuation found first is done, then the requested flow still runs
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.clock.advance(steps.STEP_EXPIRY_S)
        out = sim.run_pass(self.conn, self.bf)
        earlier = self.conn.execute("SELECT outcome FROM passes WHERE generation=?",
                                    (t1,)).fetchone()[0]
        self.assertEqual(earlier, "failed")
        self.assertEqual(out["end"]["outcome"], "complete")

    def test_busy_names_hours_for_an_old_pass(self):
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.specialist(t1)
        self.clock.advance(passes.STALE_AFTER_S + 60)
        self.claim()                                   # a held, old pass
        busy = self.call("begin_pass", trigger="operator")
        self.assertEqual(busy["text"], "A check is running — started 3 hours ago.\n"
                                       "Ask again in a few minutes.")


class TestReviewC4(Flow):
    """Review C4 (H1): a write made outside the transaction that checked the token
    re-validates it: a superseded caller's poison() writes nothing."""
    def test_a_superseded_import_cannot_poison_the_continued_pass(self):
        self.seed(1)
        t1 = self.begin()
        self.start(t1)
        self.probe_import(t1)
        self.call("record_step", pass_token=t1, step="sweep", action="finish")
        ctx = multiprocessing.get_context("spawn")
        rolled_back, resume, out = ctx.Event(), ctx.Event(), ctx.Queue()
        proc = ctx.Process(target=_procs.import_poison_paused,
                           args=(self.bf.export(), t1, "b" * 32, rolled_back, resume, out))
        proc.start()
        self.addCleanup(proc.join, 30)
        self.addCleanup(resume.set)
        self.assertTrue(rolled_back.wait(60), "the import never reached poison")
        t2 = self.claim()["continue"]["pass_token"]              # B continues the pass
        resume.set()
        proc.join(60)
        got = out.get(timeout=10)
        self.assertEqual(got[0], "error", got)
        self.assertIn("nothing was imported", got[1])            # A still refused
        cur = self.conn.execute("SELECT snapshot_id, gate_json FROM passes WHERE"
                                " generation=?", (t1,)).fetchone()
        self.assertIsNotNone(cur["snapshot_id"])                 # not poisoned
        self.assertNotIn("another bank ledger", cur["gate_json"] or "")
        page = self.call("list_projections", pass_token=t2)      # B's sweep proceeds
        self.assertIn("projections", page)
        self.assertTrue(page["bank_writes"]["allowed"])


if __name__ == "__main__":
    unittest.main()
