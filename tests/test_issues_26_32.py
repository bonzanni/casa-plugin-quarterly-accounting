# tests/test_issues_26_32.py
"""Issues #26–#32, the live check of 2026-10-01 (design
docs/superpowers/specs/2026-10-02-issues-26-32-design.md): the open chunk and more_work
(A1–A7), age-out spaced and re-armed (B), payee-unknown items leave the check's work (C),
the stored report is the server's (E), foreign-currency documents (D), and the package's
file names (F)."""
import datetime as _dt
import json
import unittest

from tests import _base  # noqa: F401  (puts server/ on sys.path)
from tests._base import StoreCase
from tests.test_check_chunks import Check
from tests.test_package_rounds import Rounds
import db  # noqa: E402
import lineage  # noqa: E402
import matches  # noqa: E402
import package  # noqa: E402
import passes  # noqa: E402
import steps  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402

DAY = 24 * 3600


def fits(used):
    """more_work's hand-out at `used` calls (A3, D1): what the caps leave."""
    return max(0, (work.ELLEN_TURNS - work.TURN_TAIL - 1 - work.MORE_MARGIN - used)
               // work.ITEM_COST)


class C(Check):
    def chunk_of(self):
        return json.loads(self.conn.execute("SELECT carry_json FROM pass_steps WHERE"
                                            " step='sweep' ORDER BY rowid DESC LIMIT 1"
                                            ).fetchone()[0])["chunk"]

    def record(self, t, items, **kw):
        for it in items:
            self.call("record_search", pid=it["pid"], pass_token=t, **(kw or {"queries": ["q"]}))


class TestMoreWork(C):
    def test_more_work_fills_the_turn_by_the_caps(self):
        self.seed(20)
        c = self.swept()
        first = {i["pid"] for i in c["work"]["triage"]}
        self.assertEqual(len(first), work.CHUNK_FIRST)
        t = self.chunk(c)
        out = self.call("more_work", pass_token=t, calls_made=30)
        self.assertEqual(len(out["triage"]), fits(30))
        self.assertEqual(out["next"], "gmail-round")
        more = {i["pid"] for i in out["triage"]}
        self.assertFalse(more & first)                      # never the chunk's again
        self.record(t, out["triage"])
        # a count below the floor (30 + this call + one record per item) is raised to it
        out2 = self.call("more_work", pass_token=t, calls_made=5)
        floor = 30 + 1 + len(more)
        self.assertEqual(len(out2["triage"]), fits(floor))
        self.record(t, out2["triage"])
        out3 = self.call("more_work", pass_token=t, calls_made=60)
        self.assertEqual((out3["triage"], out3["next"]), ([], "judge"))
        ch = self.chunk_of()
        handed = first | more | {i["pid"] for i in out2["triage"]}
        self.assertEqual(set(ch["pids"]), handed)
        self.assertTrue(handed <= set(self.sweep_carry()["owed"]))
        # `handed` is the turn's start: the loop goes on while the work falls
        c = self.judged(t)
        self.assertEqual(c["next"], "gmail-round")
        self.assertEqual(c["work"]["total"], 20 - len(handed))
        self.assertFalse({i["pid"] for i in c["work"]["triage"]} & handed)

    def test_the_chunk_and_its_last_more_work_fit_the_turn(self):
        # C1 (Terra S1): the worst-case chunk, the more_work that answers `judge`, and the
        # judge tail stay within Ellen's turn
        for first, size in ((True, work.CHUNK_FIRST), (False, work.CHUNK_LATER)):
            head = work.TURN_HEAD + (work.FILING_HEAD + work.FILINGS_FIRST * work.FILING_COST
                                     if first else 0)
            self.assertLessEqual(head + size * work.ITEM_COST + work.TURN_TAIL,
                                 work.ELLEN_TURNS)
        # and at more_work's own bound, an honest count ends within the margin
        for used in range(0, 80):
            n = fits(used)
            if n:
                self.assertLessEqual(used + 1 + n * work.ITEM_COST + work.TURN_TAIL,
                                     work.ELLEN_TURNS - work.MORE_MARGIN)

    def test_the_tail_is_counted_call_by_call(self):
        # C2 (Terra S2, Astra S2): pinned independently of TURN_TAIL itself — the last
        # more_work, record_step(judge, start), the delegation, record_step(delegated),
        # the closing message
        tail = ["more_work", "record_step start", "delegate_to_agent",
                "record_step delegated", "close"]
        self.assertGreaterEqual(work.TURN_TAIL, len(tail))
        self.seed(20)
        t = self.chunk(self.swept())
        # 80 - 5 (tail) - 1 (this call) - 8 (margin) - 34 = 32: two items, never three
        self.assertEqual(len(self.call("more_work", pass_token=t, calls_made=34)["triage"]), 2)

    def test_the_first_floor_counts_the_chunk(self):
        self.seed(20)
        c = self.swept()
        t = self.chunk(c)
        out = self.call("more_work", pass_token=t, calls_made=0)
        self.assertEqual(len(out["triage"]), fits(3 + work.CHUNK_FIRST))

    def test_more_work_is_refused_until_the_chunk_is_recorded_and_after_the_judgment(self):
        self.seed(10)
        c = self.swept()
        t = c["pass_token"]
        out = self.text("more_work", pass_token=t, calls_made=10)
        self.assertIn("make the Gmail probe first", out)
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.record(t, c["work"]["triage"][:1])
        out = self.text("more_work", pass_token=t, calls_made=10)
        self.assertTrue(out.startswith("refused: record every item you were handed first"), out)
        self.record(t, c["work"]["triage"][1:])
        self.call("more_work", pass_token=t, calls_made=10)
        self.start(t, step="judge")
        out = self.text("more_work", pass_token=t, calls_made=10)
        self.assertIn("no Gmail chunk is open", out)

    def test_more_work_is_refused_when_gmail_is_down(self):
        self.seed(10)
        c = self.swept()
        t = self.chunk(c, n=0, probe=False)
        self.assertIn("the Gmail probe failed", self.text("more_work", pass_token=t,
                                                          calls_made=10))


class TestARecordBelongsToTheChunk(C):
    def test_an_effort_record_outside_the_chunk_is_refused_and_writes_nothing(self):
        self.seed(10)
        c = self.swept()
        t = c["pass_token"]
        handed = {i["pid"] for i in c["work"]["triage"]}
        other = next(d["pid"] for d in work.triage(self.conn) if d["pid"] not in handed)
        before = lineage.projection(self.conn, other)
        for kw in ({"queries": ["q"]}, {"found_candidate": True}, {"exhausted": True}):
            out = self.text("record_search", pid=other, pass_token=t, **kw)
            self.assertIn(f"payment #{other} is not in the work you were handed", out)
        after = lineage.projection(self.conn, other)
        self.assertEqual((after["search_json"], after["passes_without_candidate"]),
                         (before["search_json"], before["passes_without_candidate"]))

    def test_a_call_without_effort_is_accepted_anywhere(self):
        self.seed(10)
        c = self.swept()
        t = c["pass_token"]
        handed = {i["pid"] for i in c["work"]["triage"]}
        other = next(d["pid"] for d in work.triage(self.conn) if d["pid"] not in handed)
        self.call("record_search", pid=other, pass_token=t, identity_unknown=True)
        self.call("record_search", pid=other, pass_token=t, incomplete=True)
        self.call("record_search", pid=other, revive=True)
        self.assertNotIn(other, self.chunk_of()["recorded"])

    def test_after_the_judgment_starts_nothing_is_recorded_with_effort(self):
        self.seed(10)
        c = self.swept()
        t = self.chunk(c)
        self.start(t, step="judge")
        out = self.text("record_search", pid=c["work"]["triage"][0]["pid"], pass_token=t,
                        queries=["late"])
        self.assertIn("is not in the work you were handed", out)


class TestHandOutOrder(StoreCase):
    def d(self, pid, tier, seq=None):
        return {"pid": pid, "expectation": {"tier": tier},
                "search": {"searched_seq": seq} if seq else {}}

    def test_required_then_never_searched_then_longest_unsearched(self):
        items = [self.d(1, "optional"), self.d(2, "required", 50), self.d(3, "required"),
                 self.d(4, "required", 10), self.d(5, "optional", 5)]
        self.assertEqual([d["pid"] for d in sorted(items, key=work.hand_order)],
                         [3, 4, 2, 1, 5])


class TestTheCheckEndsOnlyWhenItsChunkIsJudged(C):
    def test_a_handed_chunk_is_owed_its_search_then_its_judgment(self):
        self.seed(5)
        c = self.swept()
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        for outcome in ("interrupted", "complete"):
            out = self.text("end_pass", pass_token=t, outcome=outcome)
            self.assertTrue(out.startswith("refused: not ended: 3 payments handed out"), out)
        self.record(t, c["work"]["triage"])
        out = self.text("end_pass", pass_token=t, outcome="interrupted")
        self.assertTrue(out.startswith("refused: not ended: this Gmail chunk is recorded but "
                                       "not judged"), out)
        self.assertTrue(passes._marker(self.conn)["live"])       # nothing ended
        c = self.judged(t)
        # the next chunk is owed in turn; worked and judged, the check ends
        self.assertEqual(c["next"], "gmail-round")
        c = self.judged(self.chunk(c))
        self.assertEqual(self.call("end_pass", pass_token=c["pass_token"],
                                   outcome="complete")["outcome"], "complete")

    def test_a_reclaim_keeps_the_judgment_the_recorded_chunk_owes(self):
        # D2 (Astra S1): the turn recorded its chunk and died before the judgment; the
        # re-claim's hand-out is empty, and the judgment is still owed
        self.seed(3)
        c = self.swept()
        t = self.chunk(c)
        recorded = {i["pid"] for i in c["work"]["triage"]}
        self.clock.advance(steps.LEASE_S)
        c = self.claim()["continue"]
        self.assertEqual((c["next"], c["work"]["total"]), ("gmail-round", 0))
        self.assertEqual(set(self.chunk_of()["recorded"]), recorded)
        for outcome in ("complete", "interrupted"):
            out = self.text("end_pass", pass_token=c["pass_token"], outcome=outcome)
            self.assertTrue(out.startswith("refused: not ended: this Gmail chunk is recorded "
                                           "but not judged"), out)
        c = self.judged(c["pass_token"])
        self.assertEqual(self.call("end_pass", pass_token=c["pass_token"],
                                   outcome="complete")["outcome"], "complete")

    def test_gmail_down_ends_it(self):
        self.seed(5)
        c = self.swept()
        self.call("record_probe", pass_token=c["pass_token"], kind="gmail", ok=False)
        self.assertEqual(self.call("end_pass", pass_token=c["pass_token"],
                                   outcome="interrupted")["outcome"], "interrupted")

    def test_stopped_and_failed_are_not_refused(self):
        self.seed(5)
        c = self.swept()
        self.assertEqual(self.call("end_pass", pass_token=c["pass_token"],
                                   outcome="stopped")["outcome"], "stopped")

    def test_a_round_never_handed_out_is_owed_its_continuation(self):
        # D2: ending from the step's own token, before continue_pass
        self.seed(5)
        t = self.begin()
        self.start(t)
        self.specialist(t)
        for outcome in ("interrupted", "complete"):
            out = self.text("end_pass", pass_token=t, outcome=outcome)
            self.assertTrue(out.startswith("refused: not ended: this pass's Gmail round is "
                                           "due"), out)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "gmail-round")

    def test_a_judgment_never_stands_in_for_the_gmail_round(self):
        # C1 (Astra S1): a judge started from the sweep's token before the continuation
        self.seed(5)
        t = self.begin()
        self.start(t)
        self.specialist(t)
        out = self.text("record_step", pass_token=t, step="judge", action="start")
        self.assertTrue(out.startswith("refused: the Gmail round comes first"), out)
        self.assertEqual(self.claim()["continue"]["next"], "gmail-round")

    def test_a_judgment_never_starts_while_the_sweep_runs(self):
        # C2 (Astra S1): started before the sweep finished, it stood in for the round
        self.seed(5)
        t = self.begin()
        self.start(t)
        self.specialist(t, finish=False)
        out = self.text("record_step", pass_token=t, step="judge", action="start")
        self.assertTrue(out.startswith("refused: the Gmail round comes first"), out)
        self.call("record_step", pass_token=t, step="sweep", action="finish",
                  remaining_in_cycle=0, triage_remaining=0)
        self.assertEqual(self.claim()["continue"]["next"], "gmail-round")

    def test_a_judgment_follows_a_sweep_that_hands_out_no_round(self):
        # a stopped sweep's pass may still judge (step 5's "anything filed")
        self.seed(5)
        t = self.begin()
        self.start(t)
        self.call("record_step", pass_token=t, step="sweep", action="finish",
                  stopped="the ledger changed")
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
        self.start(c["pass_token"], step="judge")

    def test_a_pass_is_not_ended_while_its_first_step_runs(self):
        # C3 (Astra S1): imported and swept, the step unfinished
        self.seed(5)
        t = self.begin()
        self.start(t)
        self.specialist(t, finish=False)
        for outcome in ("complete", "interrupted"):
            out = self.text("end_pass", pass_token=t, outcome=outcome)
            self.assertTrue(out.startswith("refused: not ended: the pass's step is still "
                                           "running"), out)
        self.assertEqual(self.call("end_pass", pass_token=t, outcome="stopped")["outcome"],
                         "stopped")

    def test_a_round_due_after_the_judgment_started_is_handed_out_by_its_continuation(self):
        # C3 (Astra S1): the sweep expired before its import; the judgment that followed
        # saw the import land; its continuation hands out the round instead of looping
        self.seed(5)
        t = self.begin()
        self.start(t)
        self.clock.advance(steps.STEP_EXPIRY_S)
        c = self.claim()["continue"]
        self.assertEqual((c["ended"], c["next"]), ("expired", "end-pass"))
        t = c["pass_token"]
        self.start(t, step="judge")
        self.probe_import(t)
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "gmail-round")
        self.assertEqual(c["work"]["total"], 5)
        c = self.judged(self.chunk(c))
        self.assertEqual(c["next"], "gmail-round")

    def test_a_failed_or_expired_judgment_hands_out_a_round_it_can_judge(self):
        # C4 (Astra S1): the round owed after the judgment is judged by a restarted one
        for how in ("failed", "expired"):
            with self.subTest(how=how):
                self.doCleanups()
                self.setUp()
                self.seed(3)
                t = self.begin()
                self.start(t)
                self.clock.advance(steps.STEP_EXPIRY_S)
                t = self.claim()["continue"]["pass_token"]
                self.start(t, step="judge")
                self.probe_import(t)
                if how == "failed":
                    self.call("record_step", pass_token=t, step="judge", action="finish",
                              failed=True)
                else:
                    self.clock.advance(steps.STEP_EXPIRY_S)
                c = self.claim()["continue"]
                self.assertEqual(c["next"], "gmail-round")
                t = self.chunk(c)
                c = self.judged(t)                       # the judgment restarts
                self.assertEqual(c["next"], "end-pass")
                self.assertEqual(self.call("end_pass", pass_token=c["pass_token"],
                                           outcome="complete")["outcome"], "complete")

    def test_a_stopped_judgment_hands_out_nothing(self):
        self.seed(3)
        t = self.begin()
        self.start(t)
        self.clock.advance(steps.STEP_EXPIRY_S)
        t = self.claim()["continue"]["pass_token"]
        self.start(t, step="judge")
        self.probe_import(t)
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  stopped="the ledger changed", triage_remaining=0)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
        self.assertEqual(self.call("end_pass", pass_token=c["pass_token"],
                                   outcome="stopped")["outcome"], "stopped")

    def test_complete_needs_every_owed_search(self):
        # D1 (Astra S1): a chunk closed by a judgment with nothing searched
        self.seed(5)
        c = self.swept()
        t = self.chunk(c)
        c = self.judged(t)
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.record(t, c["work"]["triage"], incomplete=True)    # not reached
        c = self.judged(t)
        self.assertEqual(c["next"], "end-pass")
        out = self.text("end_pass", pass_token=c["pass_token"], outcome="complete")
        self.assertTrue(out.startswith("refused: not complete: 2 payments this check owes"),
                        out)
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted")
        self.assertEqual(end["report"]["not_searched"], 2)


class TestTheStoredReportIsTheServers(C):
    def test_the_callers_counts_are_never_stored(self):
        self.seed(3)
        c = self.judged(self.chunk(self.swept()))
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="complete",
                        report={"checked": 999, "total": 999, "not_searched": 0, "x": 1})
        rep = json.loads(self.conn.execute("SELECT report_json FROM passes WHERE pass_id=?",
                                           (end["ended"],)).fetchone()[0])
        self.assertEqual((rep["checked"], rep["total"], rep["not_searched"], rep["x"]),
                         (3, 3, 0, 1))
        self.assertEqual(end["report"]["checked"], 3)

    def test_a_pass_with_no_check_origin_stores_no_counts(self):
        self.seed(3)
        t = self.begin()
        self.start(t)
        self.call("record_step", pass_token=t, step="sweep", action="finish",
                  stopped="the ledger changed")
        c = self.claim()["continue"]
        end = self.call("end_pass", pass_token=c["pass_token"], outcome="stopped",
                        report={"checked": 7, "total": 9})
        rep = json.loads(self.conn.execute("SELECT report_json FROM passes WHERE pass_id=?",
                                           (end["ended"],)).fetchone()[0])
        self.assertNotIn("checked", rep)
        self.assertNotIn("total", rep)

    def test_a_reclaimed_check_stores_the_servers_counts(self):
        self.seed(5)
        c = self.swept()
        t = self.chunk(c)
        pass_id = c["pass_id"]
        self.clock.advance(passes.STALE_AFTER_S + 60)
        self.assertEqual(self.call("begin_pass", trigger="operator")["status"], "started")
        rep = json.loads(self.conn.execute("SELECT report_json FROM passes WHERE pass_id=?",
                                           (pass_id,)).fetchone()[0])
        self.assertEqual((rep["reclaimed"], rep["checked"], rep["total"]), (True, 3, 5))
        self.assertIsNotNone(t)


class TestAgeOutIsSpaced(C):
    def counted(self, pid):
        return lineage.projection(self.conn, pid)["passes_without_candidate"]

    def test_back_to_back_checks_count_once_and_a_week_later_counts_again(self):
        self.seed(1)
        pid = work.triage(self.conn)[0]["pid"]
        start = self.counted(pid)               # the seeding pass's search counted
        for _ in range(2):                       # two checks within the hour
            c = self.judged(self.chunk(self.swept()))
            self.call("end_pass", pass_token=c["pass_token"], outcome="complete")
            self.clock.advance(600)
        self.assertEqual(self.counted(pid), start)
        self.clock.advance(work.AGE_OUT_SPACING_S)
        c = self.judged(self.chunk(self.swept()))
        self.assertEqual(self.counted(pid), start + 1)


class TestAgeOutUnits(StoreCase):
    def setUp(self):
        super().setUp()
        from unittest import mock
        self.now = _dt.datetime(2026, 9, 20, 8, 0, tzinfo=_dt.timezone.utc)
        p = mock.patch.object(db, "_clock", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        self.bind()
        self.token = self.pass_()
        self.row(1)
        self.pid = self.lineage_for(1)
        self.classify(self.pid, {"software"})
        self.settle(self.pid)
        self.handed(self.pid)

    def ago(self, seconds):
        return (self.now - _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def set(self, state, streak, search):
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state=?, passes_without_candidate=?,"
                              " search_json=? WHERE pid=?",
                              (state, streak, json.dumps(search), self.pid))

    def test_a_record_from_before_0_7_counts_as_of_its_last_search(self):
        self.set("active", 2, {"last_counted_pass": "old", "last_searched_at": self.ago(DAY)})
        out = work.record_search(self.conn, pid=self.pid, token=self.token, queries=["q"])
        self.assertEqual((out["search_state"], out["passes_without_candidate"]), ("active", 2))

    def test_an_aged_out_payment_is_searched_again_after_28_days_and_not_before(self):
        self.set("aged-out", 3, {"last_counted_at": self.ago(work.AGE_OUT_REARM_S - 60)})
        self.assertEqual(work.triage(self.conn), [])
        self.set("aged-out", 3, {"last_counted_at": self.ago(work.AGE_OUT_REARM_S)})
        self.assertEqual([d["pid"] for d in work.triage(self.conn)], [self.pid])
        # a fruitless re-search keeps it aged-out and moves its 28 days
        out = work.record_search(self.conn, pid=self.pid, token=self.token, queries=["q"])
        self.assertEqual(out["search_state"], "aged-out")
        self.assertEqual(work.triage(self.conn), [])
        # a candidate makes it active again
        self.now += _dt.timedelta(seconds=work.AGE_OUT_REARM_S)
        self.token = self.pass_()
        self.handed(self.pid)
        out = work.record_search(self.conn, pid=self.pid, token=self.token,
                                 found_candidate=True)
        self.assertEqual((out["search_state"], out["passes_without_candidate"]), ("active", 0))

    def test_accepted_missing_stays_out(self):
        self.set("accepted-missing", 0, {"last_counted_at": self.ago(work.AGE_OUT_REARM_S * 2)})
        self.assertEqual(work.triage(self.conn), [])


class TestPayeeUnknown(C):
    def test_an_unknown_payee_is_not_handed_out_again_in_the_check(self):
        self.seed(8)
        c = self.swept()
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        unknown = c["work"]["triage"][0]["pid"]
        self.call("record_search", pid=unknown, pass_token=t, identity_unknown=True)
        self.record(t, c["work"]["triage"][1:])
        seen = set()
        while True:
            c = self.judged(t)
            if c["next"] != "gmail-round":
                break
            pids = {i["pid"] for i in c["work"]["triage"]}
            self.assertNotIn(unknown, pids)
            seen |= pids
            t = self.chunk(c)
        # it counts as checked: the check asked who the payee is
        self.assertEqual(c["report"], {"checked": 8, "total": 8, "not_searched": 0})
        self.call("end_pass", pass_token=c["pass_token"], outcome="complete")
        # the next check hands it out again
        c = self.swept()
        self.assertIn(unknown, {i["pid"] for i in c["work"]["triage"]})

    def test_changed_facts_hand_it_out_again(self):
        d = {"search": {"identity_seq": 10, "identity_fp": "old"},
             "row_snapshot": {"amount_minor": 1}}
        self.assertFalse(work.handled_since(d, 5))
        d["search"]["identity_fp"] = db.canonical(d["row_snapshot"])
        self.assertTrue(work.handled_since(d, 5))
        self.assertFalse(work.handled_since(d, 10))


class TestPackageRounds(Rounds):
    def test_identity_only_items_never_hold_the_round(self):
        # D1 (Astra S2): six payee-unknown items and one searchable one
        self.seed(7)
        c = self.snapshot_round(self.ask()["pass_token"])
        unknown, searched = set(), set()
        while True:
            t = c["pass_token"]
            self.call("record_probe", pass_token=t, kind="gmail", ok=True)
            for it in c["work"]["triage"]:
                self.assertNotIn(it["pid"], unknown | searched)
                if len(unknown) < 6:
                    self.call("record_search", pid=it["pid"], pass_token=t,
                              identity_unknown=True)
                    unknown.add(it["pid"])
                else:
                    self.call("record_search", pid=it["pid"], pass_token=t, queries=["q"])
                    searched.add(it["pid"])
            self.start(t, step="judge")
            self.call("record_step", pass_token=t, step="judge", action="finish",
                      triage_remaining=0)
            c = self.claim()["continue"]
            if c["next"] != "gmail-round":
                break
        self.assertEqual((len(unknown), len(searched)), (6, 1))
        self.assertEqual(self.end(c)["next"], "build")

    def test_a_package_round_fills_its_turn(self):
        self.seed(12)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.search(t, c["work"]["triage"])
        out = self.call("more_work", pass_token=t, calls_made=20)
        self.assertEqual(len(out["triage"]), fits(20))
        self.assertFalse({i["pid"] for i in out["triage"]}
                         & {i["pid"] for i in c["work"]["triage"]})

    def test_a_failed_judgment_in_a_round_hands_out_a_chunk_it_can_judge(self):
        # C4 (Astra S1), the package round's case
        self.seed(3)
        t = self.ask()["pass_token"]
        self.start(t, step="snapshot")
        self.clock.advance(steps.STEP_EXPIRY_S)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
        t = c["pass_token"]
        self.start(t, step="judge")
        self.probe_import(t)
        self.call("record_step", pass_token=t, step="judge", action="finish", failed=True)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "gmail-round")
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.search(t, c["work"]["triage"])
        c = self.judge(t)
        self.assertNotIn("refused", json.dumps(self.end(c, "interrupted")))

    def test_a_package_round_owes_its_handed_chunk(self):
        self.seed(3)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        out = self.text("end_pass", pass_token=t, outcome="interrupted")
        self.assertTrue(out.startswith("refused: not ended: 3 payments handed out"), out)


class TestForeignCurrency(Check):
    def test_a_usd_invoice_in_the_window_makes_the_payment_judge_due(self):
        self.seed(1)                                    # EUR 10.00, 2026-07-05
        self.assertEqual(work.judge_due(self.conn), 0)
        self.file(amount_minor=1160, currency="USD", document_date="2026-08-30")
        self.assertEqual(work.judge_due(self.conn), 0)   # outside the window
        self.file(amount_minor=1161, currency="USD", document_date="2026-07-08")
        self.assertEqual(work.judge_due(self.conn), 1)

    def test_the_view_and_the_package_show_both_amounts(self):
        doc = {"doc_id": 1, "kind": "invoice", "number": None, "date": "2026-07-08",
               "amount_minor": 1100, "currency": "USD", "recipient": None, "date_read": True}
        d = {"pid": 1, "status": "proposed", "reasons": [], "candidates": [],
             "currency": "EUR", "amount_minor": 948, "date": "2026-07-05",
             "expectation": {"kind": "invoice", "tier": "required"},
             "current": {"match_id": 1, "document": doc, "labels": ["clean"],
                         "runners_up": [], "author": "auto", "state": "proposed"}}
        lines = views.evidence(d)
        self.assertIn("The invoice is in USD 11.00; the payment is EUR 9.48.", lines)
        self.assertIn("Not sure — say if it's wrong.", lines)
        # C1 (Astra S2): competing candidates name their own amounts too
        cand = {"match_id": 2, "document": {**doc, "doc_id": 2, "number": "B",
                                            "amount_minor": 1200}}
        d2 = {**d, "status": "open", "current": None, "candidates": [
            {"match_id": 1, "document": {**doc, "number": "A"}}, cand]}
        line = [x for x in views.evidence(d2) if x.startswith("Could be:")][0]
        self.assertIn("in USD 11.00", line)
        self.assertIn("in USD 12.00", line)
        self.assertEqual(package._other_currency({"currency": "USD", "amount_minor": 1100},
                                                 {"currency": "EUR"}),
                         ["document in USD 11.00"])
        self.assertEqual(package._other_currency({"currency": "EUR", "amount_minor": 1100},
                                                 {"currency": "EUR"}), [])


class TestAmounts(StoreCase):
    def test_a_document_with_no_amount_is_named_without_one(self):
        doc = {"document_date": "2026-04-27", "issuer": "Airline", "amount_minor": None,
               "ext": "pdf", "sha256": "a" * 64}
        self.assertEqual(package.doc_filename(doc, set(), "2026-04-27"),
                         "2026-04-27_Airline.pdf")
        doc["amount_minor"] = 0
        self.assertEqual(package.doc_filename(doc, set(), "2026-04-27"),
                         "2026-04-27_Airline_0.00.pdf")

    def test_a_machine_pairing_needs_the_documents_amount(self):
        self.bind()
        token = self.pass_()
        self.row(1)
        pid = self.lineage_for(1)
        self.classify(pid, {"software"})
        self.settle(pid)
        doc = self.doc(amount_minor=None)
        for fn, extra in ((matches.record_match, {"author": "auto"}),
                          (matches.propose_match, {})):
            with self.assertRaises(db.Refusal) as cm:
                fn(self.conn, pid=pid, doc_id=doc, expected_revision=self.rev(pid),
                   row_snapshot=self.snapshot(pid), token=token, **extra)
            self.assertIn("amount was never read", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
