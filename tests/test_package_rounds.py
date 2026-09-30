# tests/test_package_rounds.py
"""Issue #15: a package request runs its quarter's check — the bank, the Gmail round and
judging for that quarter — in rounds, and the zip arrives when the check is done (design
docs/superpowers/specs/2026-09-30-issues-14-15-design.md, Part B)."""
import csv
import io
import json
import unittest
import zipfile

from tests import _base  # noqa: F401  (puts server/ on sys.path)
from tests import sim
from tests.test_package_requests import Requests
import db  # noqa: E402
import work  # noqa: E402


class Rounds(Requests):
    def ask(self, quarter="2026-Q3", channel="telegram"):
        return self.call("begin_pass", trigger="package", quarter=quarter, channel=channel)

    def snapshot_round(self, token):
        """The specialist's snapshot step for a round; returns the continuation."""
        self.start(token, step="snapshot")
        self.probe_import(token)
        self.sweep(token, quarter="2026-Q3")
        self.call("record_step", pass_token=token, step="snapshot", action="finish",
                  remaining_in_cycle=0)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "gmail-round", c)
        return c

    def judge(self, token, match=False, **finish):
        self.start(token, step="judge")
        if match:
            sim.triage(self.conn, self.bf, token)
        self.call("record_step", pass_token=token, step="judge", action="finish",
                  **({"triage_remaining": 0} | finish))
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass", c)
        return c

    def search(self, token, items):
        for it in items:
            self.call("record_search", pid=it["pid"], pass_token=token, queries=["q"])

    def end(self, c, outcome="complete"):
        return self.call("end_pass", pass_token=c["pass_token"], outcome=outcome)

    def rows_of(self, pkg):
        z = zipfile.ZipFile(pkg["path"])
        return list(csv.DictReader(io.StringIO(z.read("ledger.csv").decode())))


class TestTheCheckRuns(Rounds):
    def test_an_invoice_emailed_since_the_last_check_ships_matched(self):
        # the issue's case: the weekly check left the payment missing; the Gmail round of
        # the package request finds its invoice, the judge step matches it, the zip ships it
        self.seed(2)
        t = self.ask()["pass_token"]
        c = self.snapshot_round(t)
        self.assertEqual(len(c["work"]["triage"]), 2)
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.file(amount_minor=1000, document_date="2026-07-05")       # found in Gmail
        self.search(t, c["work"]["triage"])
        c = self.judge(t, match=True)
        end = self.end(c)
        self.assertEqual(end["next"], "build")
        pkg = self.call("build_quarterly_package", quarter="2026-Q3",
                        package_token=end["package_token"])
        self.assertEqual(sorted(r["status"] for r in self.rows_of(pkg)), ["MATCHED", "MISSING"])
        self.assertNotIn("couldn't", pkg["caption"])

    def test_an_unsearched_item_takes_another_round_and_only_it_is_listed(self):
        self.seed(2)
        t = self.ask()["pass_token"]
        c = self.snapshot_round(t)
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.search(t, c["work"]["triage"][:1])                         # time ran out
        end = self.end(self.judge(t), "interrupted")
        self.assertIsNone(end["next"])
        self.assertNotIn("package_token", end)
        self.assertTrue(end["more"])
        r = self.request()
        self.assertEqual((r["state"], r["round"], r["remaining"]), ("queued", 1, 1))
        c = self.claim()["continue"]
        self.assertEqual((c["next"], c["reply"], c["request"]["round"]), ("snapshot", "silent", 2))
        c = self.snapshot_round(c["pass_token"])
        self.assertEqual(len(c["work"]["triage"]), 1)
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.search(t, c["work"]["triage"])
        end = self.end(self.judge(t))
        self.assertEqual(end["next"], "build")
        self.assertEqual(self.request()["round"], 2)

    def test_a_round_that_gets_no_further_ships_and_says_so(self):
        self.seed(2)
        t = self.ask()["pass_token"]
        for rnd in (1, 2):
            c = self.snapshot_round(t)
            t = c["pass_token"]
            self.call("record_probe", pass_token=t, kind="gmail", ok=True)
            end = self.end(self.judge(t), "interrupted")                # nothing searched
            if rnd == 1:
                self.assertEqual(self.request()["state"], "queued")
                t = self.claim()["continue"]["pass_token"]
        self.assertEqual(end["next"], "build")
        self.assertEqual(json.loads(self.request()["check_json"]), {"unfinished": 2})
        pkg = self.call("build_quarterly_package", quarter="2026-Q3",
                        package_token=end["package_token"])
        self.assertIn("The check couldn't get through 2 payments — say \"rebuild it\"",
                      pkg["caption"])

    def test_gmail_down_ships_and_says_the_search_could_not_run(self):
        self.seed(2)
        t = self.ask()["pass_token"]
        c = self.snapshot_round(t)
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=False, detail="token revoked")
        end = self.end(self.judge(t), "interrupted")
        self.assertEqual(end["next"], "build")
        pkg = self.call("build_quarterly_package", quarter="2026-Q3",
                        package_token=end["package_token"])
        self.assertIn("The email search couldn't run", pkg["caption"])

    def test_a_judgment_out_of_time_or_short_of_its_pages_covers_nothing(self):
        # design D1 (Astra) and D3 (Astra S2): only a whole judge step covers a payment
        for finish in ({"out_of_time": True}, {"triage_remaining": 1}, {"failed": True}):
            with self.subTest(finish=finish):
                self.setUp_again()
                t = self.ask()["pass_token"]
                c = self.snapshot_round(t)
                t = c["pass_token"]
                self.call("record_probe", pass_token=t, kind="gmail", ok=True)
                self.file(amount_minor=1000, document_date="2026-07-05")
                self.search(t, c["work"]["triage"])
                self.start(t, step="judge")
                self.call("record_step", pass_token=t, step="judge", action="finish",
                          **({"triage_remaining": 0} | finish))
                c = self.claim()["continue"]
                end = self.end(c, "interrupted")
                self.assertIsNone(end["next"])
                r = self.request()
                self.assertEqual((r["state"], r["remaining"]), ("queued", 2))  # unjudged+round

    def setUp_again(self):
        """A fresh store for each subtest."""
        self.doCleanups()
        self.setUp()
        self.seed(1)

    def test_a_round_with_nothing_to_search_still_needs_its_gmail_round(self):
        # design D3 (Astra S1): an empty quarter, or a reclaimed round, is not done
        # without the Gmail round's probe and a whole judge step
        self.seed(0)
        t = self.ask()["pass_token"]
        c = self.snapshot_round(t)
        self.assertEqual(c["work"]["triage"], [])
        end = self.end(c)                                     # no Gmail round, no judge
        self.assertIsNone(end["next"])
        self.assertEqual((self.request()["state"], self.request()["remaining"]), ("queued", 1))
        c = self.snapshot_round(self.claim()["continue"]["pass_token"])
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.assertEqual(self.end(self.judge(t))["next"], "build")


class TestSearchesCountForTheRequest(Rounds):
    def test_a_search_made_before_the_ask_does_not_count(self):
        self.seed(1)
        pid = self.conn.execute("SELECT pid FROM projections").fetchone()[0]
        t = self.begin("operator")
        self.call("record_search", pid=pid, pass_token=t, queries=["before"])
        self.call("end_pass", pass_token=t, outcome="interrupted")
        t = self.ask()["pass_token"]
        c = self.snapshot_round(t)
        self.assertEqual([i["pid"] for i in c["work"]["triage"]], [pid])

    def test_a_search_in_the_asks_own_second_does_not_count(self):
        # design D1 (Terra S1): ordered by the store sequence, not the clock
        self.seed(1)
        pid = self.conn.execute("SELECT pid FROM projections").fetchone()[0]
        req = {"created_seq": 10, "quarter": "2026-Q3"}
        d = work.describe(self.conn, pid)
        d["search"] = {"searched_seq": 10, "facts_fp": db.canonical(d["row_snapshot"])}
        self.assertFalse(work.searched_for(d, req))
        d["search"]["searched_seq"] = 11
        self.assertTrue(work.searched_for(d, req))

    def test_a_payment_whose_facts_changed_is_searched_again(self):
        # design D3 (Terra S1)
        self.seed(1)
        pid = self.conn.execute("SELECT pid FROM projections").fetchone()[0]
        d = work.describe(self.conn, pid)
        d["search"] = {"searched_seq": 11, "facts_fp": db.canonical(
            {**d["row_snapshot"], "amount_minor": 999})}
        self.assertFalse(work.searched_for(d, {"created_seq": 10}))


class TestAsking(Rounds):
    def test_asked_while_a_check_runs_it_queues_and_follows_that_check(self):
        self.seed(1)
        t = self.begin("operator")
        out = self.ask()
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["text"], "A check is running — the Q3 2026 package follows when "
                                      "it ends.")
        self.assertEqual(self.request()["state"], "queued")
        self.assertIsNone(self.claim()["continue"])             # the check is live
        end = self.call("end_pass", pass_token=t, outcome="interrupted")
        self.assertTrue(end["more"])
        c = self.claim()["continue"]
        self.assertEqual((c["next"], c["trigger"], c["request"]["quarter"]),
                         ("snapshot", "package", "2026-Q3"))

    def test_asked_again_while_it_is_on_its_way(self):
        self.seed(1)
        self.ask()
        again = self.ask(channel="email")
        self.assertEqual(again["status"], "already")
        self.assertEqual(again["text"], "The Q3 2026 package is already on its way — it "
                                        "follows when the check is done.")
        rows = self.conn.execute("SELECT state, channel FROM package_requests").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("snapshot", "email")])

    def test_the_snapshot_step_no_longer_names_the_quarter(self):
        self.seed(1)
        t = self.ask()["pass_token"]
        self.assertEqual(self.text("record_step", pass_token=t, step="snapshot",
                                   action="start", quarter="2026-Q3"),
                         "refused: a package's quarter goes with begin_pass")
        self.assertEqual(self.text("begin_pass", trigger="package"),
                         "refused: a package is asked for with its quarter")


class TestTheCheckIsBoundToItsImport(Rounds):
    def test_an_import_after_the_check_sends_the_request_back_to_it(self):
        # design D2 (Astra S1): a cron import between the check and the build added
        # payments nobody searched for
        self.seed(1)
        t = self.ask()["pass_token"]
        c = self.snapshot_round(t)
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.search(t, c["work"]["triage"])
        end = self.end(self.judge(t))
        sim.run_pass(self.conn, self.bf)                          # a cron import lands
        text = self.text("build_quarterly_package", quarter="2026-Q3",
                         package_token=end["package_token"])
        self.assertEqual(text, "refused: the bank was re-read since the check — the check "
                               "runs again, and the package follows it; call continue_pass")
        r = self.request()
        self.assertEqual((r["state"], r["token"], r["round"]), ("queued", None, 1))
        self.assertEqual(self.claim()["continue"]["next"], "snapshot")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM packages").fetchone()[0], 0)


class TestSendTheLastBuild(Rounds):
    def test_the_previous_build_is_sent_unchanged_and_never_revoked(self):
        self.seed(1, documents=1)
        p, pkg, d = self.staged()
        self.call("record_delivery", delivery_id=d["delivery_id"], outcome="delivered",
                  package_token=p)
        out = self.call("apply_reply", text="send me the last package you built for Q3")
        self.assertEqual(out["instructions"], ["send last 2026-Q3"])
        again = self.call("stage_for_delivery", channel="telegram", last_built=True,
                          quarter="2026-Q3")
        self.assertEqual(again["filename"], pkg["filename"])
        self.assertTrue(again["caption"].startswith("Built on "), again["caption"])
        self.assertTrue(again["caption"].endswith(pkg["caption"]))
        sim.run_pass(self.conn, self.bf)                          # a newer import lands
        row = self.conn.execute("SELECT status, revoked_at, as_built FROM deliveries WHERE"
                                " delivery_id=?", (again["delivery_id"],)).fetchone()
        self.assertEqual(tuple(row), ("staged", None, 1))
        self.assertEqual(self.call("record_delivery", delivery_id=again["delivery_id"],
                                   outcome="delivered")["status"], "delivered")

    def test_nothing_built_is_said_in_words(self):
        self.seed(1)
        self.assertEqual(self.text("stage_for_delivery", channel="telegram", last_built=True,
                                   quarter="2026-Q2"),
                         "refused: I haven't built a Q2 2026 package yet — ask for it and "
                         "I'll build it")


if __name__ == "__main__":
    unittest.main()
