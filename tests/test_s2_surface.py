# tests/test_s2_surface.py
"""S2 (spec §3, §6.4, §8, §13): the job's tool surface and casa.jobs, the no-Gmail
wording, the classification and freshness notes, and a queued check made visible."""
import datetime as _dt
import json, pathlib, subprocess, sys
import unittest
from tests._base import StoreCase, ROOT

A_JOB = "aaaaaaaa-1"


class Surface(StoreCase):
    def test_tool_lists_agree_and_old_tools_are_gone(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/check_tool_agreement.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        import qa_server, tools  # noqa: F401
        for gone in ("begin_pass", "end_pass", "continue_pass", "record_step", "more_work",  # removed-name: asserted absent
                     "job_report",                      # S7 §9: the relay is deleted
                     "request_package", "build_quarterly_package",  # removed-name: asserted absent
                     "list_projections", "record_observation"):     # removed-name: asserted absent
            self.assertNotIn(gone, qa_server.TOOLS)
        for new in ("job_next", "job_status", "request_work", "set_aside"):
            self.assertIn(new, qa_server.TOOLS)

    def test_the_job_declaration_is_verbatim(self):
        m = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(m["casa"]["jobs"], [{
            "name": "work", "skill": "quarterly-job", "title": "Accounting check",
            "summary": "Checks the bank and Gmail, matches invoices, keeps the bank ledger's "
                       "notes current",
            "batches": 20, "turnsPerBatch": 80, "session": "fresh",
            "host": "specialist", "quietWhenScheduled": True}])
        self.assertEqual(m["version"], "0.11.23")
        import job                  # the batch budget and the batch window's claim count
        self.assertEqual(job.TURNS_PER_BATCH, m["casa"]["jobs"][0]["turnsPerBatch"])

    def test_the_readme_and_changelog_name_the_casa_floor(self):
        for f in ("README.md", "CHANGELOG.md"):
            text = (ROOT / f).read_text()
            self.assertIn("#1301, #1302 and #1303", text, f)
            self.assertIn("v0.344.39", text, f)
            self.assertIn("quietWhenScheduled", text, f)
        log = (ROOT / "CHANGELOG.md").read_text()
        self.assertNotIn("never released", log)
        self.assertIn("## 0.10.0", log)

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
        self.end_live_pass("complete", {"read_age_min": 75})
        self.assertIn("bank read from 75 minutes",
                      views.build_review(self.conn, view="status")["text"])

    def test_payments_awaiting_classification_are_said(self):
        import views
        tok = self.pass_("cron")
        self.end_live_pass("complete", {"awaiting_classification": 3})
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
        self.end_live_pass("complete", {"read_age_min": 75,
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

    def test_the_gmail_absent_alert_is_not_a_reauthorisation(self):
        import alerts, passes
        tok = self.pass_("cron")
        passes.record_probe(self.conn, tok, "gmail", False, absent=True)
        self.streak()
        speak = alerts.pending_rendering(self.conn)
        self.assertIn("Gmail isn't connected for the finance specialist — invoices aren't "
                      "being searched.", " ".join(speak["text"].split()))
        self.assertNotIn("Re-authorise", speak["text"])
        # a Gmail that stopped letting the specialist in is still the re-authorisation
        tok = self.pass_("cron")
        passes.record_probe(self.conn, tok, "gmail", True)
        passes.record_probe(self.conn, tok, "gmail", False, "token expired")
        self.streak()
        self.assertIn("Re-authorise Gmail", alerts.pending_rendering(self.conn)["text"])

    def streak(self):
        """D10 (Task 10): the Gmail line is said once the probe failed on
        alerts.GMAIL_RUNS runs in a row."""
        import alerts, db
        with db.tx(self.conn):
            self.conn.execute("UPDATE probes SET fail_runs=? WHERE kind='gmail'",
                              (alerts.GMAIL_RUNS,))


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

    def test_the_descriptions_say_keep_going_and_never_a_claim(self):
        import qa_server, tools  # noqa: F401
        nxt = " ".join(qa_server.TOOLS["job_next"]["description"].split())
        self.assertIn("then after each unit job_next(pass_token=…)", nxt)
        self.assertIn("keep going until `complete`; Casa ends the turn when its batch is "
                      "full, and an unfinished unit comes again", nxt)
        self.assertIn("`report` → report_job_progress with its `progress` verbatim, then "
                      "job_next", nxt)
        status = " ".join(qa_server.TOOLS["job_status"]["description"].split())
        self.assertIn("never a claim", status)


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
