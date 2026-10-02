# tests/test_round_chunks.py
"""Issue #17: every Ellen turn of a package round does a bounded amount of work and ends
at a delegation whose notification starts the next one. The Gmail round is handed out in
chunks, each followed by a judgment, inside one pass; and a delegation's end ends the
step it served (design docs/superpowers/specs/2026-09-30-issues-17-18-19-design.md,
Part A)."""
import json
import unittest

from tests import _base  # noqa: F401  (puts server/ on sys.path)
from tests.test_package_rounds import Rounds
import db  # noqa: E402
import kb  # noqa: E402
import steps  # noqa: E402
import work  # noqa: E402

D1 = "3f2a9c1e-0b7d-4e21-9a55-1c2d3e4f5a6b"      # Casa's ids are uuid4 strings
D2 = "7c0de5aa-91b2-4c3d-8e4f-5a6b7c8d9e0f"


class Chunks(Rounds):
    def chunk(self, c, n=None):
        """Ellen's Gmail chunk: the probe, the first n items searched (all by default)."""
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.search(t, c["work"]["triage"][:n])
        return t

    def judged(self, t, **finish):
        """The judgment after a chunk (its delegation async), then the notification's claim."""
        self.start(t, step="judge")
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  **({"triage_remaining": 0} | finish))
        return self.claim()["continue"]

    def judge_row(self):
        return self.conn.execute("SELECT * FROM pass_steps WHERE step='judge'"
                                 " ORDER BY rowid DESC LIMIT 1").fetchone()


class TestTheRoundInChunks(Chunks):
    def test_a_quarter_of_25_items_runs_in_three_chunks_on_one_bank_read(self):
        self.seed(25)
        c = self.snapshot_round(self.ask()["pass_token"])
        sizes = []
        while c["next"] == "gmail-round":
            self.assertEqual(c["work"]["total"], 25 - sum(sizes))
            sizes.append(len(c["work"]["triage"]))
            c = self.judged(self.chunk(c))              # claimed at once: no lease wait
        self.assertEqual(sizes, [3, 6, 6, 6, 4])     # issue #24: CHUNK_FIRST, CHUNK_LATER
        self.assertEqual(c["next"], "end-pass")
        end = self.end(c)
        self.assertEqual(end["next"], "build")
        r = self.request()
        self.assertEqual((r["round"], r["check_json"]), (1, None))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM snapshots s JOIN passes p ON p.pass_id=s.pass_id"
            " WHERE p.trigger='package'").fetchone()[0], 1)

    def test_a_chunk_that_searched_nothing_ends_the_pass(self):
        self.seed(5)
        c = self.snapshot_round(self.ask()["pass_token"])
        c = self.judged(self.chunk(c))
        self.assertEqual(c["next"], "gmail-round")
        c = self.judged(self.chunk(c, n=0))                  # the turn searched nothing
        self.assertEqual(c["next"], "end-pass")
        self.end(c, "interrupted")
        self.assertEqual((self.request()["state"], self.request()["remaining"]), ("queued", 2))

    def test_searching_an_item_already_searched_is_not_progress(self):
        self.seed(5)
        c = self.snapshot_round(self.ask()["pass_token"])
        first = c["work"]["triage"]
        c = self.judged(self.chunk(c))
        self.assertEqual((c["next"], c["work"]["total"]), ("gmail-round", 2))
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        # the first chunk's, again: not the work handed out now (issue #26, A4) — refused,
        # and nothing recorded is progress
        for it in first[:3]:
            out = self.text("record_search", pid=it["pid"], pass_token=t, queries=["q"])
            self.assertIn("is not in the work you were handed", out)
        for it in c["work"]["triage"]:
            self.call("record_search", pid=it["pid"], pass_token=t, incomplete=True)
        c = self.judged(t)
        self.assertEqual(c["next"], "end-pass")

    def test_a_judgment_that_failed_stopped_or_expired_ends_the_pass(self):
        for how in ("failed", "stopped", "expired"):
            with self.subTest(how=how):
                self.doCleanups()
                self.setUp()
                self.seed(12)
                c = self.snapshot_round(self.ask()["pass_token"])
                t = self.chunk(c)
                self.start(t, step="judge")
                if how == "expired":
                    self.clock.advance(steps.STEP_EXPIRY_S)
                else:
                    self.call("record_step", pass_token=t, step="judge", action="finish",
                              **({"failed": True} if how == "failed"
                                 else {"stopped": "the ledger changed"}))
                c = self.claim()["continue"]
                self.assertEqual(c["next"], "end-pass", how)

    def test_gmail_down_or_no_probe_ends_the_pass(self):
        for ok in (False, None):
            with self.subTest(ok=ok):
                self.doCleanups()
                self.setUp()
                self.seed(12)
                c = self.snapshot_round(self.ask()["pass_token"])
                t = c["pass_token"]
                if ok is not None:
                    self.call("record_probe", pass_token=t, kind="gmail", ok=ok)
                self.search(t, c["work"]["triage"])
                self.assertEqual(self.judged(t)["next"], "end-pass")

    def test_the_restarted_judgment_is_the_one_the_check_reads(self):
        self.seed(9)
        c = self.snapshot_round(self.ask()["pass_token"])
        c = self.judged(self.chunk(c))                       # judgment 1: whole
        c = self.judged(self.chunk(c), triage_remaining=3)   # judgment 2: short of pages
        self.assertEqual(c["next"], "end-pass")
        self.assertEqual(json.loads(self.judge_row()["finish_json"]), {"triage_remaining": 3})
        self.end(c, "interrupted")
        self.assertEqual((self.request()["state"], self.request()["remaining"]), ("queued", 1))

    def test_a_judgment_is_not_restarted_while_it_runs_nor_after_it_failed(self):
        self.seed(12)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = self.chunk(c)
        self.start(t, step="judge")
        self.assertEqual(self.text("record_step", pass_token=t, step="judge", action="start"),
                         "refused: the judge step was already started in this pass")
        self.call("record_step", pass_token=t, step="judge", action="finish", failed=True)
        self.assertEqual(self.text("record_step", pass_token=t, step="judge", action="start"),
                         "refused: the judge step was already started in this pass")
        # issue #21: a check restarts its judgment per chunk too (tests/test_check_chunks.py)

    def test_portals_do_not_fill_a_chunk(self):
        # D1 (Astra S2, Terra S2): ten portal payments ahead in pid order
        bf = self.bf
        bf.fetch([bf.row(f"2026-07-{5 + i:02d}", ref=f"P{i}", amount=500 + i,
                         counterparty="Portalco") for i in range(10)]
                 + [bf.row("2026-07-20", ref="A", amount=1000, counterparty="Adobe")])
        for r in self.active():
            self.classify(r["row_id"], "software")
        self.first_pass()
        with db.tx(self.conn):
            kb.upsert_in_tx(self.conn, "Portalco", patterns=["Portalco"], source="portal",
                            document_link="https://example.invalid/invoices",
                            link_note="found in the account page")
        c = self.snapshot_round(self.ask()["pass_token"])
        self.assertEqual([i["counterparty"] for i in c["work"]["triage"]], ["Adobe"])
        c = self.judged(self.chunk(c))
        self.assertEqual(c["next"], "end-pass")
        self.assertEqual(self.end(c)["next"], "build")


class TestADelegationsEndEndsItsStep(Chunks):
    def delegated_snapshot(self):
        self.seed(2)
        t = self.ask()["pass_token"]
        self.start(t, step="snapshot")
        out = self.call("record_step", pass_token=t, step="snapshot", action="delegated",
                        delegation_id=D1)
        self.assertEqual((out["step"], out["bound"]), ("snapshot", True))
        return t

    def running(self):
        return self.claim()

    def test_a_failed_delegation_closes_its_step_and_the_same_claim_continues_it(self):
        # D1 (Astra S1, Terra S1): the failure's notification is the round's one wake
        self.delegated_snapshot()
        self.assertIn("running", self.claim())                     # a plain claim waits
        c = self.call("continue_pass", delegation_id=D1[:8], delegation_status="error")
        c = c["continue"]
        self.assertEqual((c["step"], c["ended"], c["next"]), ("snapshot", "errored", "end-pass"))
        self.call("end_pass", pass_token=c["pass_token"], outcome="failed")
        self.assertEqual(self.request()["state"], "recovery-failed")

    def test_an_answer_without_the_specialists_finish_closes_it_countless(self):
        t = self.delegated_snapshot()
        self.probe_import(t)
        self.sweep(t, quarter="2026-Q3")
        c = self.call("continue_pass", delegation_id=D1, delegation_status="ok")["continue"]
        self.assertEqual((c["ended"], c["finish"], c["next"]), ("finished", {}, "gmail-round"))

    def test_a_notice_that_names_no_running_step_changes_nothing(self):
        self.delegated_snapshot()
        for did in (D2, D2[:8], D1[:8] + "0"):
            out = self.call("continue_pass", delegation_id=did, delegation_status="error")
            self.assertIn("running", out, did)
        self.assertIsNone(self.conn.execute(
            "SELECT finished_at FROM pass_steps WHERE step='snapshot'").fetchone()[0])
        for did, status in ((D1[:7], "error"), (D1, "failed"), (None, "error"), (D1, None)):
            out = self.text("continue_pass", delegation_id=did, delegation_status=status)
            self.assertTrue(out.startswith("refused: delegation_"), (did, status, out))

    def test_a_notice_for_the_first_judgment_leaves_the_restarted_one_running(self):
        self.seed(12)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = self.chunk(c)
        self.start(t, step="judge")
        self.call("record_step", pass_token=t, step="judge", action="delegated",
                  delegation_id=D1)
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0)
        c = self.call("continue_pass", delegation_id=D1, delegation_status="ok")["continue"]
        self.assertEqual(c["next"], "gmail-round")
        t = self.chunk(c)
        self.start(t, step="judge")
        self.call("record_step", pass_token=t, step="judge", action="delegated",
                  delegation_id=D2)
        out = self.call("continue_pass", delegation_id=D1[:8], delegation_status="error")
        self.assertIn("running", out)                              # replayed: judgment 1's
        self.assertIsNone(self.judge_row()["finished_at"])
        c = self.call("continue_pass", delegation_id=D2[:8], delegation_status="error")
        self.assertEqual(c["continue"]["ended"], "errored")

    def test_a_notice_replayed_after_the_specialists_finish_changes_nothing(self):
        # a restart replays a notice: the specialist's own finish (its counts) stands
        self.seed(12)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = self.chunk(c)
        self.start(t, step="judge")
        self.call("record_step", pass_token=t, step="judge", action="delegated",
                  delegation_id=D1)
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0, remaining_in_cycle=0)
        before = tuple(self.judge_row())
        c = self.call("continue_pass", delegation_id=D1[:8], delegation_status="error")
        self.assertEqual(tuple(self.judge_row()), before)
        self.assertEqual((c["continue"]["ended"], c["continue"]["finish"]),
                         ("finished", {"remaining_in_cycle": 0, "triage_remaining": 0}))
        self.call("continue_pass", delegation_id=D1, delegation_status="error")   # again
        self.assertEqual(tuple(self.judge_row()), before)

    def test_an_expired_step_is_left_to_expire(self):
        self.delegated_snapshot()
        self.clock.advance(steps.STEP_EXPIRY_S)
        c = self.call("continue_pass", delegation_id=D1, delegation_status="error")
        self.assertEqual(c["continue"]["ended"], "expired")
        self.assertIsNone(self.conn.execute(
            "SELECT finished_at FROM pass_steps WHERE step='snapshot'").fetchone()[0])

    def test_an_ambiguous_prefix_closes_nothing(self):
        self.seed(12)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = self.chunk(c)
        self.start(t, step="judge")
        twin = D1[:8] + D2[8:]
        self.call("record_step", pass_token=t, step="judge", action="delegated",
                  delegation_id=D1)
        self.call("record_step", pass_token=t, step="judge", action="delegated",
                  delegation_id=twin)                              # re-delegated
        self.assertIn("running", self.call("continue_pass", delegation_id=D1[:8],
                                           delegation_status="error"))
        c = self.call("continue_pass", delegation_id=twin, delegation_status="error")
        self.assertEqual(c["continue"]["ended"], "errored")

    def test_a_restarted_judgment_remembers_the_delegation_it_replaced(self):
        # judgment 2's delegation shares its prefix with judgment 1's: a replayed notice
        # for judgment 1 is ambiguous and closes nothing
        self.seed(12)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = self.chunk(c)
        self.start(t, step="judge")
        self.call("record_step", pass_token=t, step="judge", action="delegated",
                  delegation_id=D1)
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0)
        t = self.chunk(self.claim()["continue"])
        self.start(t, step="judge")
        twin = D1[:8] + D2[8:]
        self.call("record_step", pass_token=t, step="judge", action="delegated",
                  delegation_id=twin)
        self.assertIn("running", self.call("continue_pass", delegation_id=D1[:8],
                                           delegation_status="error"))
        self.assertIsNone(self.judge_row()["finished_at"])

    def test_a_prefix_shared_with_an_older_passs_delegation_closes_nothing(self):
        # C1 (Astra S1): a notice replayed from an earlier pass never closes a newer step
        self.seed(1)
        t = self.begin("operator")
        self.start(t)
        self.call("record_step", pass_token=t, step="sweep", action="delegated",
                  delegation_id=D1)
        self.specialist(t)
        c = self.claim()["continue"]
        self.call("record_probe", pass_token=c["pass_token"], kind="gmail", ok=False)
        self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted")
        twin = D1[:8] + D2[8:]
        t = self.ask()["pass_token"]
        self.start(t, step="snapshot")
        self.call("record_step", pass_token=t, step="snapshot", action="delegated",
                  delegation_id=twin)
        self.assertIn("running", self.call("continue_pass", delegation_id=D1[:8],
                                           delegation_status="ok"))
        self.assertIsNone(self.conn.execute(
            "SELECT finished_at FROM pass_steps WHERE step='snapshot'").fetchone()[0])
        c = self.call("continue_pass", delegation_id=twin, delegation_status="error")
        self.assertEqual(c["continue"]["ended"], "errored")

    def test_binding_after_a_sync_answer_finished_the_step_is_not_refused(self):
        self.seed(1)
        t = self.begin("operator")
        self.start(t)
        self.specialist(t)                                         # finished in the turn
        out = self.call("record_step", pass_token=t, step="sweep", action="delegated",
                        delegation_id=D1)
        self.assertEqual((out["step"], out["bound"], out["finished"]), ("sweep", False, True))

    def test_binding_is_the_live_holders_and_the_running_steps_only(self):
        t = self.delegated_snapshot()
        self.assertTrue(self.text("record_step", pass_token=t, step="judge",
                                  action="delegated", delegation_id=D2)
                        .startswith("refused: the judge step was not started"))
        self.assertTrue(self.text("record_step", pass_token=t + 99, step="snapshot",
                                  action="delegated", delegation_id=D2)
                        .startswith("refused: this pass is no longer the current one"))
        self.assertEqual(self.text("record_step", pass_token=t, step="snapshot",
                                   action="delegated", delegation_id="abc"),
                         "refused: delegation_id is the id delegate_to_agent returned (or "
                         "the one the notification names), as given")
        self.assertEqual(self.text("record_step", pass_token=t, step="snapshot",
                                   action="start", delegation_id=D1),
                         'refused: delegation_id goes with action="delegated"')


if __name__ == "__main__":
    unittest.main()
