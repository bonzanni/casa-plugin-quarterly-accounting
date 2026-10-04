# tests/test_s2_surface.py
"""S2 (spec §3, §6.4, §8, §13): the job's tool surface and casa.jobs, the no-Gmail
wording, the classification and freshness notes, and a queued check made visible."""
import datetime as _dt
import json, pathlib, subprocess, sys
from tests._base import StoreCase, ROOT

A_JOB = "aaaaaaaa-1"


class Surface(StoreCase):
    def test_tool_lists_agree_and_old_tools_are_gone(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/check_tool_agreement.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        import qa_server, tools  # noqa: F401
        for gone in ("begin_pass", "end_pass", "continue_pass", "record_step", "more_work"):
            self.assertNotIn(gone, qa_server.TOOLS)
        for new in ("job_next", "job_status", "job_report", "request_work",
                    "request_package", "record_filing"):
            self.assertIn(new, qa_server.TOOLS)

    def test_the_job_declaration_is_verbatim(self):
        m = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(m["casa"]["jobs"], [{
            "name": "work", "skill": "quarterly-job", "title": "Accounting check",
            "summary": "Checks the bank and Gmail, judges documents, prepares packages",
            "batches": "unlimited", "turnsPerBatch": 80, "session": "fresh",
            "host": "specialist"}])
        self.assertEqual(m["version"], "0.9.0")
        import job                  # the batch budget and the batch window's claim count
        self.assertEqual(job.TURNS_PER_BATCH, m["casa"]["jobs"][0]["turnsPerBatch"])

    def test_gmail_absent_says_not_connected_not_reauthorise(self):
        import alerts, passes, views
        tok = self.pass_("cron")
        passes.record_probe(self.conn, tok, "gmail", False, absent=True)
        text = views.build_review(self.conn, view="status")["text"]
        self.assertIn("isn't connected for the finance specialist", text)
        self.assertNotIn("Re-authorise", text)

    def test_a_waived_freshness_window_is_disclosed(self):
        import views
        tok = self.pass_("cron")
        self.end_with_counts(tok, "complete", {"read_age_min": 75})
        self.assertIn("bank read from 75 minutes",
                      views.build_review(self.conn, view="status")["text"])

    def test_payments_awaiting_classification_are_said(self):
        import views
        tok = self.pass_("cron")
        self.end_with_counts(tok, "complete", {"awaiting_classification": 3})
        self.assertIn("3 payments still await classification",
                      views.build_review(self.conn, view="status")["text"])

    def test_a_queued_request_is_visible_in_status(self):
        import asks, views
        asks.request_work(self.conn, "check", "operator")
        self.assertIn("A check is waiting to start",
                      views.build_review(self.conn, view="status")["text"])


class SurfaceBound(StoreCase):
    """The same notes on a bound store's status sheet (not the not-set-up lines)."""
    def setUp(self):
        super().setUp()
        self.bind()

    def test_the_notes_lead_the_bound_status_sheet(self):
        import asks, db, views
        tok = self.pass_("cron")
        self.end_with_counts(tok, "complete", {"read_age_min": 75,
                                               "awaiting_classification": 1})
        t0 = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
        with self.patch_clock(t0):
            asks.request_work(self.conn, "check", "operator")
        with self.patch_clock(t0 + _dt.timedelta(minutes=12)):
            text = " ".join(views.build_review(self.conn, view="status")["text"].split())
        self.assertIn("These results use a bank read from 75 minutes before they were "
                      "finished.", text)
        self.assertIn("1 payment still awaits classification — it's checked again once "
                      "classified.", text)
        self.assertIn("A check is waiting to start, asked 12 minutes ago.", text)
        self.assertLess(text.index("A check is waiting"), text.index("Accounting ·"))
        # a job that is draining the queue is not "waiting to start"
        with db.tx(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES"
                              " ('drain', 'job-00000001')")
        self.assertNotIn("waiting to start",
                         views.build_review(self.conn, view="status")["text"])

    def test_the_gmail_absent_alert_is_not_a_reauthorisation(self):
        import alerts, passes
        tok = self.pass_("cron")
        passes.record_probe(self.conn, tok, "gmail", False, absent=True)
        speak = alerts.pending_rendering(self.conn)
        self.assertIn("Gmail isn't connected for the finance specialist — invoices aren't "
                      "being searched.", " ".join(speak["text"].split()))
        self.assertNotIn("Re-authorise", speak["text"])
        # a Gmail that stopped letting the specialist in is still the re-authorisation
        tok = self.pass_("cron")
        passes.record_probe(self.conn, tok, "gmail", True)
        passes.record_probe(self.conn, tok, "gmail", False, "token expired")
        self.assertIn("Re-authorise Gmail", alerts.pending_rendering(self.conn)["text"])


class ToolLayer(StoreCase):
    """The new tools through qa_server.handle, against this test's store."""
    def setUp(self):
        super().setUp()
        from tests.test_tools import _fresh_conn
        _fresh_conn(self)._CONN = self.conn

    def call(self, name, **args):
        import qa_server
        out = qa_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": args}})
        text = out["result"]["content"][0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def test_record_probe_takes_acq_and_absent(self):
        import qa_server, tools  # noqa: F401
        props = qa_server.TOOLS["record_probe"]["schema"]["properties"]
        self.assertEqual((props["acq"]["type"], props["absent"]["type"]),
                         ("integer", "boolean"))
        self.assertIn("acq", qa_server.TOOLS["import_ledger_export"]["schema"]["properties"])
        tok = self.pass_("cron")
        self.call("record_probe", pass_token=tok, kind="gmail", ok=False, absent=True)
        row = self.conn.execute("SELECT data_json FROM probes WHERE kind='gmail'").fetchone()
        self.assertTrue(json.loads(row[0])["absent"])
        self.call("record_probe", pass_token=tok, kind="bank_sync", ok=True, acq=7)
        row = self.conn.execute("SELECT data_json FROM probes WHERE kind='bank_sync'").fetchone()
        self.assertEqual(json.loads(row[0])["acq"], 7)
        self.assertEqual(self.call("record_probe", pass_token=tok, kind="gmail", ok=False,
                                   absent="true"), "refused: absent must be true or false")
        self.assertTrue(self.call("record_probe", pass_token=tok, kind="ledger", ok=True,
                                  acq=7).startswith("refused: acq goes only with"))

    def test_request_work_then_job_status_then_job_next(self):
        out = self.call("request_work", kind="check", trigger="operator")
        self.assertEqual(out["start_job"]["job"], "quarterly-accounting:work")
        self.assertEqual(out["line"], "Checking the bank and your email — I'll post the "
                                      "result here.")
        self.assertEqual(self.call("job_status", job_id="0123abcd-0000"),
                         {"done": False, "text": None})
        first = self.call("job_next", job_id="0123abcd-0000")
        self.assertIsInstance(first["pass_token"], int)
        self.assertEqual(first["unit"], "probes")
        self.assertTrue(self.call("job_next").startswith("refused: "))

    def test_request_package_and_job_report(self):
        out = self.call("request_package", quarter="Q3 2026")
        self.assertEqual(out["status"], "asked")
        rep = self.call("job_report")
        self.assertEqual(rep["start_job"]["job"], "quarterly-accounting:work")
        self.assertFalse(rep["more"])

    def test_the_descriptions_carry_the_echo_and_the_call_again(self):
        import qa_server, tools  # noqa: F401
        nxt = " ".join(qa_server.TOOLS["job_next"]["description"].split())
        self.assertIn("echo the judge unit's `judgment` and `after`", nxt)
        rep = " ".join(qa_server.TOOLS["job_report"]["description"].split())
        self.assertIn("`more: true`", rep)
        self.assertIn("call job_report again", rep)
        self.assertIn("speak` first", rep)
        status = " ".join(qa_server.TOOLS["job_status"]["description"].split())
        self.assertIn("never a claim", status)


class SupersededJudgeAnswer(StoreCase):
    """Fix round 1 (Task 12 review, I1): a judge answer travels only with the pass_token of
    the turn that judged. `job_next(job_id=…, judged=…)` is refused BEFORE claiming, so a
    superseded turn cannot launder its answer through a fresh claim's token."""
    B = "bbbbbbbb-2"

    call = ToolLayer.call

    def setUp(self):
        super().setUp()
        from tests.test_tools import _fresh_conn
        _fresh_conn(self)._CONN = self.conn
        self.bind()
        from tests.sim_job import JobDriver
        self.drv = JobDriver(self)

    def test_judged_without_a_pass_token_is_refused_before_the_claim(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        u = self.drv.run_until(A_JOB, "judge")
        t1 = self.drv.token
        judged = self.drv.do(u, t1)
        job.claim(self.conn, self.B)                     # another job takes the pass
        self.assertTrue(self.call("job_next", pass_token=t1, judged=judged)
                        .startswith("refused: "))
        gen = self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
        marker = self.conn.execute("SELECT generation FROM pass_marker").fetchone()[0]
        out = self.call("job_next", job_id=A_JOB, judged=judged)
        self.assertEqual(out, "refused: judged goes with the pass_token of the turn that "
                              "judged: call job_next(job_id=…) without it")
        self.assertEqual(self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0], gen)
        self.assertEqual(self.conn.execute("SELECT generation FROM pass_marker")
                         .fetchone()[0], marker)
        row = self.conn.execute("SELECT finished_at FROM pass_steps WHERE step='judge'"
                                " ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertIsNone(row["finished_at"])
        # the same call without `judged` is a plain claim, and hands the judge step out again
        again = self.call("job_next", job_id=A_JOB)
        self.assertGreater(again["pass_token"], gen)


class RefusalsNameNoRemovedTool(StoreCase):
    """Fix round 1 (Task 12 review, M1): a refusal the job can reach names the job's own
    tools, never a removed one."""
    def test_an_import_without_a_token_points_at_job_next(self):
        import db, ledger
        with self.assertRaises(db.Refusal) as cm:
            ledger.import_ledger_export(self.conn, path="x.csv", token=None,
                                        ledger_instance="whatever")
        self.assertEqual(str(cm.exception), "an import belongs to a pass: pass the "
                                            "pass_token job_next handed out")


class ReportOrder(StoreCase):
    """Carry (Task 10): the operator's reply binds to the LAST delivered rendering, so
    job_report lists a status view after every handover and stop page."""
    def test_status_views_come_last(self):
        import asks, db, views
        with db.tx(self.conn):
            for kind, trigger, outcome in (("check", "operator", "complete"),
                                           ("handover", "operator", "complete"),
                                           ("check", "cron", "stopped")):
                self.conn.execute(
                    "INSERT INTO work_requests(kind, trigger, doc_ids_json, created_seq,"
                    " created_at, state, outcome) VALUES (?,?,?,?,?, 'done', ?)",
                    (kind, trigger, "[999]" if kind == "handover" else "[]",
                     db.next_seq(self.conn), db.now(), outcome))
        rep = asks.job_report(self.conn)
        kinds = [self.conn.execute("SELECT kind FROM renders WHERE render_id=?",
                                   (t["render_id"],)).fetchone()[0] for t in rep["texts"]]
        self.assertEqual(kinds, ["handover", "job-stop", "status"])
        self.assertIn(asks.NOT_FOUND, rep["texts"][0]["text"])

    def test_the_not_found_line_offers_no_resend(self):
        import asks, reply
        self.assertNotIn("send it again", asks.NOT_FOUND)
        for clause in reply._clauses(asks.NOT_FOUND):
            self.assertEqual(reply._parse(clause), (None, None), clause)
        self.assertEqual(asks.NOT_FOUND, "I can't find that document in what I've filed — "
                                         "please send the file once more.")
