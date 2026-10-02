# tests/test_judgment_and_dates.py
"""Issue #18: a judge step's finish says how far triage got, and a package round is not
ended without a judgment. Issue #19: a machine pairing states the date printed on the
document, and the package names the file by it (design
docs/superpowers/specs/2026-09-30-issues-17-18-19-design.md, Parts B and C)."""
import json
import unittest
import zipfile

from tests import _base  # noqa: F401  (puts server/ on sys.path)
from tests.test_package_rounds import Rounds
import db  # noqa: E402

NOT_JUDGED = "refused: not ended: this package round has not judged its quarter"
# issue #28 (A6): a round whose Gmail chunk was worked is asked for its judgment first
CHUNK_NOT_JUDGED = "refused: not ended: this Gmail chunk is recorded but not judged"


class TestJudgeFinish(Rounds):
    def gmail_round(self, n=1):
        self.seed(n)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.search(t, c["work"]["triage"])
        return t

    def judge_row(self):
        return self.conn.execute("SELECT * FROM pass_steps WHERE step='judge'"
                                 " ORDER BY rowid DESC LIMIT 1").fetchone()

    def test_a_finish_with_counts_but_no_triage_remaining_is_refused_and_leaves_it_open(self):
        # the issue's call: record_step(judge, finish, remaining_in_cycle=0)
        t = self.gmail_round()
        self.start(t, step="judge")
        out = self.text("record_step", pass_token=t, step="judge", action="finish",
                        remaining_in_cycle=0)
        self.assertTrue(out.startswith("refused: a judge step's finish carries "
                                       "triage_remaining"), out)
        self.assertIsNone(self.judge_row()["finished_at"])
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  remaining_in_cycle=0, triage_remaining=0)
        c = self.claim()["continue"]
        self.assertEqual(self.end(c)["next"], "build")         # the round was whole

    def test_a_countless_failed_or_stopped_finish_is_accepted_and_is_not_whole(self):
        for how in ({}, {"failed": True}, {"stopped": "the ledger changed"}):
            with self.subTest(how=how):
                self.doCleanups()
                self.setUp()
                t = self.gmail_round()
                self.start(t, step="judge")
                out = self.call("record_step", pass_token=t, step="judge", action="finish",
                                **how)
                self.assertTrue(out["finished"], out)
                self.assertEqual(self.judge_row()["finished_by"],
                                 "specialist" if "stopped" in how else "resident")
                c = self.claim()["continue"]
                self.end(c, "interrupted")
                self.assertEqual(self.request()["state"], "queued")    # not whole: again

    def test_the_sweep_and_a_finish_of_a_finished_step_are_unchanged(self):
        self.seed(1)
        t = self.begin("operator")
        self.start(t)
        self.specialist(t, finish=False)
        out = self.call("record_step", pass_token=t, step="sweep", action="finish",
                        remaining_in_cycle=0)                   # no triage_remaining
        self.assertTrue(out["finished"])
        t = self.gmail_round()
        self.start(t, step="judge")
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0)
        out = self.call("record_step", pass_token=t, step="judge", action="finish",
                        remaining_in_cycle=0)
        self.assertEqual(out, {"step": "judge", "finished": True, "already": True})


class TestARoundIsJudged(Rounds):
    def test_a_round_that_read_the_bank_is_not_ended_without_its_judge_step(self):
        self.seed(1)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        self.search(t, c["work"]["triage"])
        for outcome in ("complete", "interrupted"):
            self.assertTrue(self.text("end_pass", pass_token=t, outcome=outcome)
                            .startswith(CHUNK_NOT_JUDGED), outcome)
        self.assertEqual(self.request()["state"], "snapshot")        # nothing changed
        self.start(t, step="judge")
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0)
        c = self.claim()["continue"]
        self.assertEqual(self.end(c)["next"], "build")

    def test_a_stopped_failed_or_unimported_round_ends_as_before(self):
        self.seed(1)
        c = self.snapshot_round(self.ask()["pass_token"])
        out = self.call("end_pass", pass_token=c["pass_token"], outcome="stopped")
        self.assertEqual(self.request()["state"], "stopped", out)
        t = self.ask()["pass_token"]
        self.start(t, step="snapshot")
        self.call("record_step", pass_token=t, step="snapshot", action="finish", failed=True)
        c = self.claim()["continue"]
        self.assertEqual(c["next"], "end-pass")
        for outcome in ("interrupted", "failed"):
            out = self.text("end_pass", pass_token=c["pass_token"], outcome=outcome)
            if not out.startswith("refused"):
                break
        self.assertFalse(out.startswith("refused"), out)
        self.assertEqual(self.request()["state"], "recovery-failed")

    def test_a_snapshot_that_stopped_after_its_import_ends_as_before(self):
        self.seed(1)
        t = self.ask()["pass_token"]
        self.start(t, step="snapshot")
        self.probe_import(t)
        self.call("record_step", pass_token=t, step="snapshot", action="finish",
                  stopped="the ledger was restored")
        c = self.claim()["continue"]
        out = self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted")
        self.assertEqual(self.request()["state"], "stopped", out)

    def test_a_cron_pass_is_not_asked_for_a_judge_step(self):
        # with Gmail down (issue #28: a handed, searchable chunk is owed its judgment, in
        # a check as in a package round) a check ends without one
        self.seed(1)
        t = self.begin("operator")
        self.start(t)
        self.specialist(t)
        c = self.claim()["continue"]
        self.call("record_probe", pass_token=c["pass_token"], kind="gmail", ok=False)
        out = self.call("end_pass", pass_token=c["pass_token"], outcome="interrupted")
        self.assertEqual(out["outcome"], "interrupted")

    def test_a_round_without_a_chunk_still_asks_for_its_judge_step(self):
        # _round_judged keeps its own refusal where no chunk was handed out (Gmail down)
        self.seed(1)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=False)
        self.assertTrue(self.text("end_pass", pass_token=t, outcome="interrupted")
                        .startswith(NOT_JUDGED))


class TestTheDateReadOnTheDocument(Rounds):
    def judged(self, **match):
        """A Q3 payment of 2026-07-05 and its invoice, filed with the email's date; the
        judge step pairs them through the tool layer."""
        self.seed(1)
        c = self.snapshot_round(self.ask()["pass_token"])
        t = c["pass_token"]
        self.call("record_probe", pass_token=t, kind="gmail", ok=True)
        doc = self.file(amount_minor=1000, document_date="2026-07-20")   # the email's date
        self.search(t, c["work"]["triage"])
        self.start(t, step="judge")
        item = self.call("list_quarter_state", triage=True, quarter="2026-Q3",
                         pass_token=t)["triage"][0]
        args = dict(pid=item["pid"], doc_id=doc, expected_revision=item["revision"],
                    row_digest=item["row_digest"], pass_token=t, **match)
        return t, doc, item, args

    def date_of(self, doc):
        return self.conn.execute("SELECT document_date FROM documents WHERE doc_id=?",
                                 (doc,)).fetchone()[0]

    def test_a_machine_pairing_without_the_date_is_refused_and_writes_nothing(self):
        t, doc, item, args = self.judged()
        for tool, extra in (("record_match", {"author": "auto"}), ("propose_match", {})):
            out = self.text(tool, **args, **extra)
            self.assertTrue(out.startswith("refused: pass document_date: the date printed on "
                                           "the document"), (tool, out))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
        self.assertEqual(self.date_of(doc), "2026-07-20")
        out = self.text("record_match", **args, author="auto", document_date="5 July")
        self.assertEqual(out, "refused: document_date is YYYY-MM-DD")

    def test_the_date_read_replaces_the_filed_one_and_names_the_file(self):
        t, doc, item, args = self.judged()
        out = self.call("record_match", **args, author="auto", document_date="2026-07-05")
        self.assertEqual(out["state"], "matched")
        self.assertEqual(self.date_of(doc), "2026-07-05")
        self.call("record_step", pass_token=t, step="judge", action="finish",
                  triage_remaining=0)
        end = self.end(self.claim()["continue"])
        pkg = self.call("build_quarterly_package", quarter="2026-Q3",
                        package_token=end["package_token"])
        names = zipfile.ZipFile(pkg["path"]).namelist()
        self.assertIn("invoices/2026-07-05_Adobe_10.00.pdf", names, names)
        self.assertFalse(any("2026-07-20" in n for n in names), names)

    def test_a_proposal_states_it_too(self):
        t, doc, item, args = self.judged()
        out = self.call("propose_match", **args, document_date="2026-07-04")
        self.assertEqual(out["state"], "proposed")
        self.assertEqual(self.date_of(doc), "2026-07-04")

    def test_the_filed_date_confirmed_changes_nothing(self):
        t, doc, item, args = self.judged()
        self.call("record_match", **args, author="auto", document_date="2026-07-20")
        self.assertEqual(self.date_of(doc), "2026-07-20")


if __name__ == "__main__":
    unittest.main()
