# tests/test_check_chunks.py
"""Issue #21: the weekly check and "go and check now" keep the property package rounds got
in 0.5.0 — every Ellen turn does a bounded amount of work and ends at a delegation whose
notification starts the next one. The check's Gmail round comes in chunks, each followed
by a judgment, inside one pass; its report counts the searches the pass owes, by pid
(design docs/superpowers/specs/2026-09-30-issues-21-22-design.md, Part A)."""
import json
import unittest

from tests import _base  # noqa: F401  (puts server/ on sys.path)
from tests.test_continuation import Flow
import db  # noqa: E402
import kb  # noqa: E402
import steps  # noqa: E402
import work  # noqa: E402


class Check(Flow):
    def swept(self, trigger="operator"):
        """A check's sweep step, finished; returns its continuation (the first chunk)."""
        t = self.begin(trigger)
        self.start(t)
        self.specialist(t)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "gmail-round", c)
        return c

    def chunk(self, c, n=None, probe=True):
        """Ellen's Gmail chunk: the probe, the first n items searched (all by default)."""
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=probe)
        for it in c["work"]["triage"][:n]:
            self.call("record_search", pid=it["pid"], pass_token=t, queries=["q"])
        return t

    def judged(self, t, **finish):
        """The judgment after a chunk (its delegation async), then the notification's claim."""
        self.start(t, step="judge", report={"checked": 0, "total": 0, "not_searched": 0})
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  **({"triage_remaining": 0} | finish))
        return self.claim()["continue"]

    def sweep_carry(self):
        return json.loads(self.conn.execute("SELECT carry_json FROM pass_steps WHERE"
                                            " step='sweep'").fetchone()[0])


class TestTheCheckInChunks(Check):
    def test_25_items_run_in_three_chunks_on_one_bank_read(self):
        self.seed(25)
        c = self.swept()
        sizes, seen = [], set()
        while c["next"] == "gmail-round":
            self.assertEqual(c["work"]["total"], 25 - sum(sizes))
            pids = {i["pid"] for i in c["work"]["triage"]}
            self.assertFalse(pids & seen)                   # never handed out twice
            seen |= pids
            sizes.append(len(pids))
            c = self.judged(self.chunk(c))                  # claimed at once: no lease wait
        self.assertEqual(sizes, [10, 10, 5])
        self.assertEqual((c["step"], c["next"]), ("judge", "end-pass"))
        self.assertEqual(c["report"], {"checked": 25, "total": 25, "not_searched": 0})
        out = self.call("end_pass", pass_token=c["pass_token"], outcome="complete",
                        report=c["report"])
        self.assertEqual(out["outcome"], "complete")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM snapshots s JOIN passes p ON p.pass_id=s.pass_id"
            " WHERE p.trigger='operator'").fetchone()[0], 1)

    def test_the_cron_check_is_chunked_too(self):
        self.seed(12)
        c = self.swept("cron")
        self.assertEqual((len(c["work"]["triage"]), c["work"]["total"]), (10, 12))
        c = self.judged(self.chunk(c))
        self.assertEqual((c["next"], len(c["work"]["triage"])), ("gmail-round", 2))

    def test_the_report_rises_chunk_by_chunk(self):
        self.seed(12)
        c = self.judged(self.chunk(self.swept()))
        self.assertEqual(c["report"], {"checked": 10, "total": 12, "not_searched": 2})
        c = self.judged(self.chunk(c))
        self.assertEqual(c["report"], {"checked": 12, "total": 12, "not_searched": 0})

    def test_a_chunk_that_searched_nothing_ends_the_loop_and_the_check_is_interrupted(self):
        self.seed(12)
        c = self.judged(self.chunk(self.swept()))
        c = self.judged(self.chunk(c, n=0))
        self.assertEqual(c["next"], "end-pass")
        self.assertEqual(c["report"]["not_searched"], 2)

    def test_an_errored_expired_or_stopped_judgment_ends_the_loop(self):
        for how in ("failed", "expired", "stopped"):
            with self.subTest(how):
                self.doCleanups()
                self.setUp()
                self.seed(12)
                t = self.chunk(self.swept())
                self.start(t, step="judge")
                if how == "failed":
                    self.call("record_step", pass_token=t, step="judge", action="finish",
                              failed=True)
                elif how == "stopped":
                    self.call("record_step", pass_token=t, step="judge", action="finish",
                              stopped="the ledger changed", triage_remaining=0)
                else:
                    self.clock.advance(steps.STEP_EXPIRY_S)
                c = self.claim()["continue"]
                self.assertEqual(c["next"], "end-pass")
                self.assertEqual(c["report"]["not_searched"], 2)

    def test_gmail_down_ends_the_loop_with_everything_unsearched(self):
        self.seed(12)
        c = self.swept()
        t = self.chunk(c, n=0, probe=False)
        c = self.judged(t)
        self.assertEqual(c["next"], "end-pass")
        self.assertEqual(c["report"], {"checked": 0, "total": 12, "not_searched": 12})

    def test_portals_are_never_handed_out_nor_owed(self):
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
        c = self.swept()
        self.assertEqual([i["counterparty"] for i in c["work"]["triage"]], ["Adobe"])
        c = self.judged(self.chunk(c))
        self.assertEqual((c["next"], c["report"]),
                         ("end-pass", {"checked": 1, "total": 1, "not_searched": 0}))

    def test_a_reclaimed_sweep_continuation_keeps_the_checks_origin(self):
        self.seed(12)
        c1 = self.swept()
        origin = self.sweep_carry()["since_seq"]
        self.chunk(c1, n=3)                                 # searched, then the turn died
        self.clock.advance(steps.LEASE_S)
        c2 = self.claim()["continue"]
        self.assertEqual((c2["step"], c2["next"]), ("sweep", "gmail-round"))
        self.assertEqual(self.sweep_carry()["since_seq"], origin)
        self.assertEqual(c2["work"]["total"], 9)            # the three stay searched


class TestTheReportCountsWhatThePassOwes(Check):
    def test_an_item_paired_without_a_search_is_still_not_searched(self):
        # D1 (Astra S1, Terra S1): leaving triage is not a search
        self.seed(11)
        c = self.swept()
        t = self.chunk(c)                                    # the first ten
        doc = self.file(amount_minor=1010, document_date="2026-07-15")   # the 11th's invoice
        self.start(t, step="judge")
        item = [i for i in self.call("list_quarter_state", triage=True, pass_token=t)["triage"]
                if i["amount_minor"] == 1010][0]
        self.call("record_match", pid=item["pid"], doc_id=doc, author="auto",
                  expected_revision=item["revision"], row_digest=item["row_digest"],
                  document_date="2026-07-15", pass_token=t)
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")                # nothing left to search
        self.assertEqual(c["report"], {"checked": 10, "total": 11, "not_searched": 1})

    def test_a_revived_item_is_counted_once_in_both(self):
        # D1 (Astra S2, Terra S2): one population for every count
        self.seed(2)
        second = sorted(work.triage(self.conn), key=lambda d: d["pid"])[1]["pid"]
        with db.tx(self.conn):
            self.conn.execute("UPDATE projections SET search_state='aged-out' WHERE pid=?",
                              (second,))
        c = self.swept()
        self.assertEqual(c["work"]["total"], 1)
        t = self.chunk(c)
        self.call("record_search", pid=second, pass_token=t, revive=True)   # "have another look"
        c = self.judged(t)
        self.assertEqual(c["next"], "end-pass")                # 1 left is not fewer than 1
        self.assertEqual(c["report"], {"checked": 1, "total": 2, "not_searched": 1})

    def test_a_not_fresh_item_is_owed_and_counted_once(self):
        self.seed(2)
        c = self.swept()
        stale = c["work"]["triage"][1]["pid"]
        with db.tx(self.conn):          # the export no longer carried it (read before import)
            self.conn.execute("UPDATE projections SET read_snapshot=NULL,"
                              " class_observed_snapshot=0 WHERE pid=?", (stale,))
        self.assertFalse(work.describe(self.conn, stale)["fresh"])
        c = self.judged(self.chunk(c, n=1))
        self.assertEqual(c["report"], {"checked": 1, "total": 2, "not_searched": 1})


class TestTheLatestJudgmentCovers(Check):
    def test_a_payment_due_after_the_last_judgment_started_is_not_complete(self):
        self.seed(12)
        c = self.judged(self.chunk(self.swept()))
        t = self.chunk(c)
        self.start(t, step="judge")
        self.file(amount_minor=1003, document_date="2026-07-08")  # fits a payment, filed late
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
        out = self.text("end_pass", pass_token=c["pass_token"], outcome="complete",
                        report=c["report"])
        self.assertTrue(out.startswith("refused: not ended: 1 payment with a filed document "
                                       "that may fit was not judged in this pass. End it "
                                       "interrupted"), out)

    def test_a_payment_due_before_the_first_judgment_is_covered_by_the_last(self):
        self.seed(12)
        self.file(amount_minor=1003, document_date="2026-07-08")  # due all along, declined
        c = self.judged(self.chunk(self.swept()))
        c = self.judged(self.chunk(c))
        out = self.call("end_pass", pass_token=c["pass_token"], outcome="complete",
                        report=c["report"])
        self.assertEqual(out["outcome"], "complete")

    def test_a_judgment_is_not_restarted_while_it_runs_or_after_it_failed(self):
        self.seed(12)
        t = self.chunk(self.swept())
        self.start(t, step="judge")
        self.assertEqual(self.text("record_step", pass_token=t, step="judge", action="start"),
                         "refused: the judge step was already started in this pass")
        self.call("record_step", pass_token=t, step="judge", action="finish", failed=True)
        self.assertEqual(self.text("record_step", pass_token=t, step="judge", action="start"),
                         "refused: the judge step was already started in this pass")


if __name__ == "__main__":
    unittest.main()
