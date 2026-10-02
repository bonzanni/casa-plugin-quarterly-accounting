# tests/test_skill.py
"""The skills are the only thing that makes agents do what the server assumes.
These pin the load-bearing sentences so an edit cannot quietly drop one.

Two skills since S2: Ellen's (`quarterly-accounting`: asking for work, the relay,
answering, replies, packages) and the job's (`quarterly-job`: the finance specialist's
procedure, one unit at a time)."""
import re
import unittest

from tests._base import ROOT, TempEnv
import qa_server  # noqa: E402


def _read(rel):
    p = ROOT / rel
    return p.read_text() if p.exists() else ""


SKILL = _read("skills/quarterly-accounting/SKILL.md")
JOB = _read("skills/quarterly-job/SKILL.md")
TRIGGER = """name:     quarterly_accounting_pass
type:     cron        schedule: 0 9 * * 1        channel: telegram
prompt:   Run the quarterly-accounting background pass. It covers every
          open item, not just the current quarter. If it reports
          something that needs me, send me that
          and nothing else; then output the sentinel `<silent/>`. If it
          reports nothing, output `<silent/>` and nothing else."""
EXTERNAL = {"sync", "list_accounts", "list_backups", "export_history", "get_transaction",
            "tag_transaction", "untag_transaction", "add_note", "search_emails", "get_email",
            "download_attachment", "list_attachments", "send_email", "delegate_to_agent",
            "send_message",
            "send_media", "list_inbound_files", "share_inbound_file", "Read", "WebSearch",
            "list_transactions", "restore_backup",
            # Casa's job tools (S1): Ellen starts the job; the job reports and completes
            "start_job", "report_job_progress", "emit_completion"}
# Sentences the skill tells Ellen to say in her own words; they reach the operator.
OPERATOR_LINES = (
    "I can't read the accounting right now",
    "Nothing more to show.",
    "I couldn't start the check yet (<its message>) — I'll start it the next time we talk "
    "about accounting.",
)
JOB_OPERATOR_LINES = ("Tell me that in the main chat, where you saw the list.",)
NOT_TOOLS = {"workflow", "expected_generation", "pass_token", "render_id", "row_digest",
             "resolves", "candidate_ids", "not_found", "write_error", "observed_tags",
             "observed_notes", "instructions", "speak", "reshow", "true", "false", "filed_refs",
             "bank_writes", "request_id", "labels", "runners_up", "can_run",
             "remaining_in_cycle", "erase_candidates", "expected_ledger", "receipt_pages",
             "not_fresh", "not_searched",
             # S2: the job's unit fields and job_report's answer
             "documents_first", "page_next", "triage_remaining", "start_job", "end_batch",
             "package_token", "delivery_id", "dates_unread", "job_busy"}


def flat(text):
    return " ".join(text.split())


def section(text, head, until=None):
    start = text.index(head)
    end = text.index(until, start + len(head)) if until else len(text)
    return text[start:end]


class TestBothSkills(TempEnv):
    def test_frontmatter(self):
        self.assertTrue(SKILL.startswith("---\nname: quarterly-accounting\ndescription: "))
        self.assertTrue(JOB.startswith(
            "---\nname: quarterly-job\ndescription: The quarterly-accounting job's procedure, "
            "for the finance specialist inside the \"Accounting check\" job only. Use when the "
            "turn's brief names the job quarterly-accounting:work.\n---\n"))
        desc = SKILL.split("---")[1]
        self.assertIn("or when a notification says the accounting job ended", desc)
        self.assertNotIn("delegation to the finance specialist", desc)

    def test_the_job_skill_fits_its_budget(self):
        self.assertLessEqual(len(JOB), 20000, len(JOB))

    def test_every_backticked_tool_exists(self):
        import tools  # noqa: F401
        ours = set(qa_server.TOOLS)
        params = {k for t in qa_server.TOOLS.values() for k in t["schema"]["properties"]}
        for name, text in (("quarterly-accounting", SKILL), ("quarterly-job", JOB)):
            named = (set(re.findall(r"`([a-z_]+)\(", text))
                     | set(re.findall(r"`([a-z_]+)`", text)))
            for n in named:
                if n in params or n.endswith("_") or n in NOT_TOOLS:
                    continue
                if "_" in n:
                    self.assertIn(n, ours | EXTERNAL, (name, n))

    def test_every_named_argument_exists_on_its_tool(self):
        """`tool(arg=…, …)`: each keyword a call of OUR tool names is in that
        tool's schema, so the skill cannot teach an argument the server drops."""
        import tools  # noqa: F401
        for text in (SKILL, JOB):
            for m in re.finditer(r"`([a-z_]+)\(([^`]*)\)`", text):
                name, args = m.group(1), m.group(2)
                if name not in qa_server.TOOLS:
                    continue
                props = set(qa_server.TOOLS[name]["schema"]["properties"])
                for kw in re.findall(r"(?:^|[(,\s])([a-z_]+)=", args):
                    self.assertIn(kw, props, f"{name}({kw}=)")

    def test_no_removed_tool_is_named(self):
        for text in (SKILL, JOB):
            for gone in ("begin_pass", "end_pass", "continue_pass", "record_step", "more_work",
                         "delegate_to_agent"):
                self.assertNotIn(gone, text, gone)

    def test_every_bank_feed_write_carries_workflow_and_generation(self):
        for line in (SKILL + JOB).splitlines():
            if re.search(r"`(tag_transaction|untag_transaction|add_note)[`(]", line):
                self.assertIn("workflow", line, line)
                self.assertIn("expected_generation", line, line)
                self.assertIn("expected_ledger", line, line)
        self.assertIn("`untag_transaction(", JOB)

    def test_refusals_are_relayed_not_retried(self):
        for text in (SKILL, JOB):
            self.assertIn("`refused: `", text)
            self.assertIn("never retry it blindly", text)

    def test_operator_lines_carry_no_machinery(self):
        import views
        for text, lines in ((SKILL, OPERATOR_LINES), (JOB, JOB_OPERATOR_LINES)):
            for line in lines:
                self.assertIn(line, flat(text), line)
                for word in views.FORBIDDEN:
                    self.assertIsNone(re.search(r"(?<![A-Za-z])" + re.escape(word)
                                                + r"(?![A-Za-z])", line), (word, line))
                self.assertIsNone(re.search(r"\bline \d", line), line)


class TestEllen(TempEnv):
    def test_trigger_text_is_verbatim(self):
        self.assertIn(TRIGGER, SKILL)

    def test_no_invention_rules(self):
        for phrase in ("I can't read the accounting right now", "VERBATIM",
                       "never from memory", "mark_rendering_delivered", "new tool call",
                       "Ellen may phrase, never compute"):
            self.assertIn(phrase, SKILL, phrase)

    def test_more_and_all_of_them_follow_next(self):
        self.assertIn("with exactly the arguments in its `next`", SKILL)

    def test_send_it_again_stages_the_offered_file(self):
        self.assertIn('stage_for_delivery(channel="telegram", resend=true)', SKILL)
        self.assertIn("never pick a package yourself", SKILL)

    def test_reset_loop(self):
        sec = SKILL[SKILL.index("## Test install and reset"):]
        self.assertIn("`reset_store()`", sec)
        self.assertIn("try again later", sec)

    def test_reset_never_picks_among_versions(self):
        sec = SKILL[SKILL.index("## Test install and reset"):]
        self.assertIn("never pick a\n   backup otherwise", sec)
        self.assertNotIn("under\n   `bank_writes`", sec)

    def test_every_receipt_page_is_sent(self):
        self.assertIn("`receipt_pages` — send EVERY page, in order", SKILL)

    def test_the_quarter_format_and_the_uncertain_offer(self):
        self.assertIn('("give me Q3" is `quarter="Q3"`)', flat(SKILL))
        pack = flat(section(SKILL, "## Packaging", "## Install"))
        self.assertIn("`record_delivery` then returns `speak`", pack)
        self.assertIn("that is what \"send it again\" binds to", pack)

    def test_only_the_operator_binds_the_account(self):
        self.assertIn("never by the specialist", SKILL)

    # --- S2: Ellen only asks, relays verbatim and sends (spec §1, §7) -----------------
    def asking(self):
        return section(SKILL, "## Ellen: asking for work", "## Ellen: the job's results")

    def test_ellen_records_the_ask_before_start_job(self):
        ask = self.asking()
        flows = re.split(r"\n- \*\*", ask)[1:]
        self.assertEqual(len(flows), 4, flows)
        for flow in flows:
            f = flat(flow)
            first = min(f.index(c) for c in ("`request_work(", "`request_package(")
                        if c in f)
            self.assertLess(first, f.index("`start_job`"), f)
        pack = flat(section(SKILL, "## Packaging", "## Install"))
        self.assertLess(pack.index("`request_package("), pack.index("`start_job`"))
        self.assertLess(pack.index("`start_job`"),
                        pack.index("`build_quarterly_package(quarter, package_token)`"))

    def test_every_flow_and_its_line(self):
        ask = flat(self.asking())
        for phrase in ('`request_work(kind="check", trigger="cron")`',
                       '`request_work(kind="check", trigger="operator")`',
                       '`request_work(kind="handover", trigger="operator", doc_ids=[…])`',
                       "`request_package(quarter, channel)`",
                       "say the returned line", "`<silent/>`",
                       'source="manual-telegram"', 'extraction_author="resident"',
                       "source_ref=<the path list_inbound_files showed>",
                       "`pending` or `job_busy`: done"):
            self.assertIn(phrase, ask, phrase)
        self.assertIn("check emailed invoices", ask)

    def test_the_results_are_relayed_verbatim_in_order(self):
        res = flat(section(SKILL, "## Ellen: the job's results",
                           "## Ellen: answering anything"))
        for phrase in ('`job_report(job_id=<the id in "(id …)">, status=',
                       "At the start of every accounting turn: `job_report()`",
                       "Send `speak` first, then every `texts` entry in the order given",
                       "each then `mark_rendering_delivered`",
                       "If `more` is `true`, call `job_report()` again",
                       "If `continue` is set, do Packaging step 3",
                       "If `start_job` is set, call `start_job` with it"):
            self.assertIn(phrase, res, phrase)

    def test_ellen_never_relays_a_notification_text(self):
        res = flat(section(SKILL, "## Ellen: the job's results",
                           "## Ellen: answering anything"))
        self.assertIn("Never relay a notification's own text", res)
        self.assertIn("never from the notification", res)

    def test_a_package_continuation_never_resends(self):
        pack = flat(section(SKILL, "## Packaging", "## Install"))
        self.assertIn("`continue` carries `package_token` and `next`", pack)
        self.assertIn("Never send the file again yourself", pack)
        self.assertIn("never write a failure line of your own", pack)
        self.assertIn('`send_media(path, kind="zip", filename=<the returned filename>', pack)
        self.assertIn("ships unclassified with its documents set aside", pack)

    def test_a_refused_build_or_stage_sends_ellen_back_to_the_report(self):
        pack = flat(section(SKILL, "## Packaging", "## Install"))
        self.assertIn("the first `stage_for_delivery` of a package, is refused because the bank "
                      "was re-read", pack)
        self.assertIn("A resend (\"send it again\") is the exact file already sent", pack)
        self.assertIn("fails because the file is gone, or `record_delivery` answers that the "
                      "bank was re-read before it was sent", pack)
        self.assertIn("A send already under way at the moment of the check cannot be stopped",
                      pack)
        self.assertIn("never build again with the old token", pack)

    def test_a_closed_request_is_said_and_email_after_telegram_is_a_new_request(self):
        pack = flat(section(SKILL, "## Packaging", "## Install"))
        self.assertIn("\"email it to me\" after a Telegram delivery is a new request — "
                      "`request_package` again with `channel=\"email\"`", pack)
        self.assertIn("never stopped on in silence", pack)


class TestJob(TempEnv):
    def units(self, unit, nxt):
        return flat(section(JOB, f"### `{unit}`", nxt))

    def every_turn(self):
        return flat(section(JOB, "## Every turn", "## Units"))

    def topic(self):
        return flat(section(JOB, "**An operator message in the job's topic", "## Units"))

    def test_the_job_skill_starts_every_turn_with_job_next(self):
        turn = self.every_turn()
        self.assertIn("Your first call is `job_next(job_id=<the Job id line of your brief>)`",
                      turn)
        first_call = re.search(r"`([a-z_]+)\(", turn).group(1)
        self.assertEqual(first_call, "job_next")
        for phrase in ("call `job_next(pass_token=…)` again",
                       "`report: true` → `report_job_progress` with its `progress` verbatim",
                       '`end-batch` → end the turn',
                       '`complete` → `report_job_progress` with its `progress`, then '
                       '`emit_completion(status="ok", text=<its text>)`',
                       "call `job_next(job_id=…)` once more; if that is refused too, end the "
                       "turn"):
            self.assertIn(phrase, turn, phrase)

    def test_the_job_skill_never_calls_job_next_in_a_topic_message(self):
        topic = self.topic()
        self.assertIn("**Never call `job_next` in a topic message.**", topic)
        self.assertNotIn("`job_next(", topic)
        self.assertIn("Tell me that in the main chat, where you saw the list.", topic)
        self.assertIn("never call `mark_rendering_delivered` in the topic", topic)

    def test_a_topic_message_turn_ends_with_job_status(self):
        topic = self.topic()
        order = ["Last, always: `job_status(job_id=<the Job id line of your brief>)`",
                 "If `done`, call `report_job_progress(summary=<its text>, progressed=true)`",
                 '`emit_completion(status="ok", text=<its text>)`']
        pos = [topic.index(k) for k in order]
        self.assertEqual(pos, sorted(pos))

    def test_the_specialist_order_matches_the_design(self):
        units = section(JOB, "## Units", "## Never")
        order = ["### `probes`", "record_probe", "sync", "### `snapshot`",
                 "import_ledger_export", "not_found", "### `sweep`", "list_projections",
                 "### `gmail-probe`", "### `filing`", "### `item`", "### `judge`",
                 "list_quarter_state"]
        pos = [units.index(k) for k in order]
        self.assertEqual(pos, sorted(pos))

    def test_the_probes_carry_the_acquisition_the_queue_and_missing(self):
        probes = self.units("probes", "### `snapshot`")
        self.assertIn('`record_probe(pass_token, kind="bank_sync", ok=…, detail=…, '
                      'acq=<the unit\'s acq>, data=', probes)
        self.assertIn("`Queue:` line", probes)
        self.assertIn('"missing": [<each workflow it marks FILE MISSING>]', probes)
        self.assertIn("never wait for it", probes)
        snap = self.units("snapshot", "### `sweep`")
        self.assertIn("`import_ledger_export(path, pass_token, ledger_instance=<the reply's "
                      "\"Ledger instance:\" id>, acq=<the unit's acq>)`", snap)
        self.assertIn("the export you made in THIS unit", snap)

    def test_bank_writes_refused_means_write_nothing(self):
        self.assertIn("If `bank_writes` is not allowed, make no bank-feed write", JOB)

    def test_sweep_transcribes_the_sim(self):
        sweep = section(JOB, "### `sweep`", "### `gmail-probe`")
        for phrase in ("observed_tags", "observed_notes", "observed_first_seen",
                       "observed_tag_revision", "`Tag revision:` line", "Other workflows' tags",
                       "first seen", "write_error",
                       "Never make two writes without a read between them",
                       "the bank ledger changed during this pass"):
            self.assertIn(phrase, sweep, phrase)
        order = ["`get_transaction(row_id)`", "`record_observation(pid, pass_token, "
                 "snapshot_id, observed_tags=", "`untag_transaction(", "read the row again",
                 "write_error=", "record it again"]
        pos = [sweep.index(k) for k in order]
        self.assertEqual(pos, sorted(pos))
        f = flat(sweep)
        self.assertIn("`list_projections(pass_token, quarter=<the unit's quarter>, limit=10)`", f)
        self.assertIn("Work in batches", f)
        self.assertIn("one write per row, then that row read again", f)

    def test_every_observation_names_the_import_it_was_read_under(self):
        units = flat(section(JOB, "## Units", "## Never"))
        self.assertIn("snapshot_id=<the import's snapshot>, not_found=true", units)
        self.assertIn("passes the `snapshot_id` that `list_projections` returned", units)
        self.assertIn("read the payment again with its new `snapshot_id`", units)

    def test_a_refused_import_stops_the_unit_including_a_failed_withdrawal(self):
        snap = self.units("snapshot", "### `sweep`")
        self.assertIn("If the import is refused, the unit stops there", snap)
        self.assertIn("could not withdraw a staged package — nothing was imported", snap)

    def test_the_gmail_probe_is_always_made(self):
        g = self.units("gmail-probe", "### `filing`")
        self.assertIn("Always make it", g)
        self.assertNotIn("skip it when", g)
        self.assertIn('`record_probe(pass_token, kind="gmail", ok=false, absent=true)`', g)

    def test_the_filing_is_capped_and_skips_what_is_filed(self):
        f = self.units("filing", "### `item`")
        for phrase in ("Skip every file whose ref is in the unit's `filed_refs`",
                       "the filing uses at most 24 calls",
                       "failed ones too — so at most 8 files, newest first, each once",
                       "`<message id>:<attachment_id>`", "its own `source_ref`",
                       "`record_filing(pass_token)`"):
            self.assertIn(phrase, f, phrase)

    def test_an_item_is_worked_within_its_limits(self):
        it = self.units("item", "### `judge`")
        for phrase in ("ONE payment", "or after 4 queries",
                       "Then at most 2 tries: a try is the message's `list_attachments` (when "
                       "you need it)",
                       "a listing that shows nothing plausible, or a failed listing or "
                       "download, uses a try",
                       "`incomplete=true` when you stopped at the 4 queries",
                       "search it all the same", "`record_search(pid, pass_token, queries=[…]",
                       "`queries` are exactly the queries you ran with `search_emails` for "
                       "this item in this turn",
                       "Search and record only the item you were handed",
                       "`search_hint`", "`window_days`", "`has:attachment`", "search Sent",
                       'extraction_author="specialist"'):
            self.assertIn(phrase, it, phrase)
        self.assertNotIn("record_search(pid, pass_token, incomplete=true)", JOB)
        self.assertNotIn("work order", JOB)

    def judge(self):
        return self.units("judge", "## Never")

    def test_the_judge_echoes_its_unit(self):
        j = self.judge()
        self.assertIn("`job_next(pass_token=…, judged={judgment: <the unit's judgment>, after: "
                      "<the unit's after>, page_next: <the page's next>, triage_remaining: "
                      "<the page's remaining>, documents: {<doc_id>: <verdict>, …}})`", j)
        self.assertIn("Echo the unit's `judgment` and `after` exactly as handed out", j)
        self.assertIn("Judge the unit's `documents_first` first", j)
        self.assertIn("`list_quarter_state(triage=true, quarter=<the unit's quarter>, "
                      "after=<the unit's after>, limit=8, pass_token=…)`", j)

    def test_only_payments_read_since_the_import_are_judged(self):
        j = self.judge()
        self.assertIn("Only the items that say `fresh: true`", j)
        self.assertIn("Triage does not wait for the sweep", j)

    def test_row_digest_comes_from_the_listing(self):
        j = self.judge()
        self.assertIn("`row_digest` from `list_quarter_state` as `row_digest`", j)
        self.assertIn("re-read that payment with `list_quarter_state(pid=…, pass_token=…)`", j)
        self.assertIn("pass all its `candidate_ids` in `resolves`", j)
        self.assertNotIn("row_snapshot", JOB)

    def test_a_package_judgment_confirms_the_dates_its_files_are_named_by(self):
        j = self.judge()
        self.assertIn("`list_quarter_state(quarter=<the unit's quarter>, dates_unread=true, "
                      "limit=5, pass_token=…)`", j)
        self.assertIn("`update_document_metadata(doc_id, document_date=<that date>, "
                      "pass_token=…)` — the same date when the filed one was right", j)

    def test_the_specialist_states_the_date_it_read(self):
        j = self.judge()
        self.assertIn("passes it as `document_date`", j)
        self.assertIn("its issue date, not a due, delivery or email date", j)

    def test_a_foreign_currency_invoice_is_proposed_and_amounts_are_read(self):
        j = self.judge()
        self.assertIn("prints the payment's exact amount in the payment's currency", j)
        self.assertIn("prints no amount in the payment's currency: `propose_match`", j)
        self.assertIn("A pairing needs the document's amount", j)

    def test_the_specialist_never_binds_and_sets_only_a_vendor_kind(self):
        never = flat(section(JOB, "## Never"))
        self.assertIn("never call `bind_account`,", never)
        self.assertIn("never call `request_work`, `request_package` or `job_report`", never)
        self.assertIn("never speak to the operator", never)
        j = self.judge()
        self.assertIn('set_expectation(scope_type="counterparty"', j)
        self.assertIn('author="specialist", pass_token=…)', j)
        self.assertIn("a kind the mapping did not predict", j)
        self.assertIn("`upsert_counterparty(name, search_hint=…, pass_token=…)`", j)


if __name__ == "__main__":
    unittest.main()
