# tests/test_skill.py
"""The skill is the only thing that makes agents do what the server assumes.
These pin the load-bearing sentences so an edit cannot quietly drop one."""
import re
import unittest

from tests._base import ROOT, TempEnv
import qa_server  # noqa: E402

SKILL = (ROOT / "skills/quarterly-accounting/SKILL.md").read_text() if (
    ROOT / "skills/quarterly-accounting/SKILL.md").exists() else ""
TRIGGER = """name:     quarterly_accounting_pass
type:     cron        schedule: 0 9 * * 1        channel: telegram
prompt:   Run the quarterly-accounting background pass. It covers every
          open item, not just the current quarter. If it reports
          something that needs me, send me that
          and nothing else; then output the sentinel `<silent/>`. If it
          reports nothing, output `<silent/>` and nothing else."""
EXTERNAL = {"sync", "list_accounts", "list_backups", "export_history", "get_transaction",
            "tag_transaction", "untag_transaction", "add_note", "search_emails", "get_email",
            "download_attachment", "send_email", "delegate_to_agent", "send_message",
            "send_media", "list_inbound_files", "share_inbound_file", "Read", "WebSearch",
            "list_transactions", "restore_backup"}
# Sentences the skill tells Ellen to say in her own words; they reach the operator.
OPERATOR_LINES = (
    "I can't read the accounting right now",
    "Matched to the EUR 12.10 payment of 18 Sep. Q3.",
    "Filed. No payment matches EUR 12.10 yet — the charge may not have posted. It'll match "
    "when it appears.",
    "Filed. I see a EUR 12.10 Twitter payment on 18 Sep, but it's already matched to "
    "invoice V-918. Which one is right?",
    "Filed, but I can't read an amount from it — is it EUR 12.10?",
    "Nothing more to show.",
    "Filed. Nothing in Q3 is close to EUR 340.00. Is this for a different quarter?",
    # issue #2: the pending lines, the answers to a notice and to busy, the handover cases
    "Checking the bank — this takes a few minutes; I'll send the result here.",
    "Filed. Checking it against the payments — I'll tell you shortly.",
    "Reading the bank first — the <quarter> package follows in a few minutes.",
    "A check is running — ask again in a few minutes.",
    "A check is running — started N minutes ago.",
    "Ask again in a few minutes.",
    "Filed. It could fit more than one payment — it's in 'anything I should check?'.",
    "Filed. I'll match it at the next check.",
    "Filed. It doesn't look like an invoice for any payment — say if it is one.",
    "I can't find that document in what I've filed — send it again?",
)
OUTCOME_RULE = ("The outcome for `end_pass`: `stopped` when `can_run` is false or the step's "
                "finish says `stopped`; `failed` when the step ended unfinished and nothing was "
                "imported this pass; `interrupted` when anything remains (`remaining_in_cycle`, "
                "`triage_remaining`, `work` truncated or `not_fresh`, an item not searched) or "
                "the step ended unfinished after the import; `complete` otherwise.")


class TestSkill(TempEnv):
    def test_frontmatter(self):
        self.assertTrue(SKILL.startswith("---\nname: quarterly-accounting\ndescription: "))

    def test_trigger_text_is_verbatim(self):
        self.assertIn(TRIGGER, SKILL)

    def test_every_backticked_tool_exists(self):
        import tools  # noqa: F401
        named = set(re.findall(r"`([a-z_]+)\(", SKILL)) | set(re.findall(r"`([a-z_]+)`", SKILL))
        ours = set(qa_server.TOOLS)
        for n in named:
            params = {k for t in qa_server.TOOLS.values() for k in t["schema"]["properties"]}
            if n in params:
                continue
            if n.endswith("_") or n in {"workflow", "expected_generation", "pass_token",
                                        "render_id", "row_snapshot", "resolves", "not_found",
                                        "write_error", "observed_tags", "observed_notes",
                                        "instructions", "speak", "reshow", "true", "false",
                                        "bank_writes", "request_id", "labels", "runners_up",
                                        "can_run", "remaining_in_cycle", "erase_candidates",
                                        "expected_ledger", "receipt_pages", "not_fresh",
                                        "time_up", "wrap_up", "time_left_s"}:
                continue
            if "_" in n:
                self.assertIn(n, ours | EXTERNAL, n)

    def test_every_named_argument_exists_on_its_tool(self):
        """`tool(arg=…, …)`: each keyword a call of OUR tool names is in that
        tool's schema, so the skill cannot teach an argument the server drops."""
        import tools  # noqa: F401
        for m in re.finditer(r"`([a-z_]+)\(([^`]*)\)`", SKILL):
            name, args = m.group(1), m.group(2)
            if name not in qa_server.TOOLS:
                continue
            props = set(qa_server.TOOLS[name]["schema"]["properties"])
            for kw in re.findall(r"(?:^|[(,\s])([a-z_]+)=", args):
                self.assertIn(kw, props, f"{name}({kw}=)")

    def test_every_bank_feed_write_carries_workflow_and_generation(self):
        for line in SKILL.splitlines():
            if re.search(r"`(tag_transaction|untag_transaction|add_note)[`(]", line):
                self.assertIn("workflow", line, line)
                self.assertIn("expected_generation", line, line)
                self.assertIn("expected_ledger", line, line)

    def test_no_invention_rules(self):
        for phrase in ("I can't read the accounting right now", "VERBATIM",
                       "never from memory", "mark_rendering_delivered", "new tool call",
                       "Ellen may phrase, never compute"):
            self.assertIn(phrase, SKILL, phrase)

    def test_the_specialist_order_matches_the_design(self):
        order = ["record_probe", "sync", "import_ledger_export", "not_found", "list_projections",
                 "list_quarter_state"]
        section = SKILL[SKILL.index("## The specialist's pass"):]
        positions = [section.index(k) for k in order]
        self.assertEqual(positions, sorted(positions))

    def test_bank_writes_refused_means_write_nothing(self):
        self.assertIn("If `bank_writes` is not allowed, make no bank-feed write", SKILL)

    # --- execution-time contracts (tools.py as reviewed; tests/sim.py) ---------------
    def test_more_and_all_of_them_follow_next(self):
        self.assertIn("with exactly the arguments in its `next`", SKILL)

    def test_send_it_again_stages_the_offered_file(self):
        self.assertIn('stage_for_delivery(channel="telegram", resend=true)', SKILL)
        self.assertIn("never pick a package yourself", SKILL)

    def test_sweep_transcribes_the_sim(self):
        section = SKILL[SKILL.index("## The specialist's pass"):SKILL.index("## Packaging")]
        for phrase in ("observed_tags", "observed_notes", "observed_first_seen",
                       "observed_tag_revision", "`Tag revision:` line", "Other workflows' tags", "first seen", "write_error",
                       "Never make two writes without a read between them",
                       "the bank ledger changed during this pass"):
            self.assertIn(phrase, section, phrase)
        # read -> record -> write -> read again, in that order
        order = ["`get_transaction(row_id)`", "`record_observation(pid, pass_token, "
                 "snapshot_id, observed_tags=", "`untag_transaction(", "read the row again",
                 "write_error=", "record it again"]
        sweep = section[section.index("**Sweep.**"):section.index("**Triage.**")]
        positions = [sweep.index(k) for k in order]
        self.assertEqual(positions, sorted(positions))

    def test_search_bookkeeping_carries_the_token(self):
        self.assertIn("record_search(pid, pass_token, incomplete=true)", SKILL)
        self.assertIn("spends nothing", SKILL)
        self.assertIn("`end_pass(pass_token, outcome", SKILL)

    def test_refusals_are_relayed_not_retried(self):
        self.assertIn("`refused: `", SKILL)
        self.assertIn("never retry it blindly", SKILL)

    def test_reset_loop(self):
        section = SKILL[SKILL.index("## Test install and reset"):]
        self.assertIn("`reset_store()`", section)
        self.assertIn("try again later", section)

    def test_operator_lines_carry_no_machinery(self):
        import views
        for line in OPERATOR_LINES:
            self.assertIn(line, SKILL, line)
            for word in views.FORBIDDEN:
                self.assertIsNone(re.search(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])",
                                            line), (word, line))
            self.assertIsNone(re.search(r"\bline \d", line), line)

    # --- fix round 1 (Task 22 review) ---------------------------------------------------
    def section(self, head, until):
        return SKILL[SKILL.index(head):SKILL.index(until)]

    def test_every_receipt_page_is_sent(self):
        self.assertIn("`receipt_pages` — send EVERY page, in order", SKILL)

    def test_the_gmail_probe_is_always_made(self):
        rnd = self.section("**Gmail round.**", "\n5. If anything was filed")
        self.assertIn("Always make the Gmail probe first", rnd)
        self.assertNotIn("skip it when", rnd)

    def test_row_snapshot_comes_from_the_listing(self):
        triage = self.section("**Triage.**", "7. **Identity")
        self.assertIn("`row_snapshot` from `list_quarter_state` verbatim", triage)
        self.assertNotIn("pass its facts as `row_snapshot`", SKILL)

    def test_the_specialist_never_binds_and_sets_only_a_vendor_kind(self):
        spec = " ".join(self.section("## The specialist's pass", "1. **Probes.**").split())
        self.assertIn("never call `bind_account`,", spec)
        self.assertIn("never by the specialist", SKILL)
        triage = " ".join(self.section("**Triage.**", "7. **Identity").split())
        self.assertIn('set_expectation(scope_type="counterparty"', triage)
        self.assertIn('author="specialist", pass_token=…)', triage)
        self.assertIn("a kind the mapping did not predict", triage)

    def test_the_handover_suspects_the_data_first(self):
        doc = self.section("## Ellen: a document the operator hands over", "## Ellen: the pass")
        self.assertIn('begin_pass(trigger="handover")', doc)
        self.assertIn("suspect the data before the document: `sync` again", doc)
        self.assertIn("stop if `can_run` is false", doc)

    def test_reset_never_picks_among_versions(self):
        section = SKILL[SKILL.index("## Test install and reset"):]
        self.assertIn("never pick a\n   backup otherwise", section)
        self.assertNotIn("under\n   `bank_writes`", section)

    def test_packaging_sweeps_between_import_and_build(self):
        # round E1 (Astra S1); since issue #1 the import refreshes the classification and
        # the sweep puts the quarter's tags and notes right before the build
        pack = " ".join(self.section("## Packaging", "## Install").split())
        order = ["the snapshot of step 3", "the ends of step 4", "the sweep of step 5",
                 "`end_pass`", "`build_quarterly_package(quarter, package_token)`"]
        positions = [pack.index(k) for k in order]
        self.assertEqual(positions, sorted(positions))
        doc = " ".join(self.section("## Ellen: a document the operator hands over",
                                    "## Ellen: the pass").split())
        self.assertLess(doc.index("the sweep of step 5"), doc.index("judge ONLY that document"))

    def test_only_payments_read_since_the_import_are_judged(self):
        # fix E2 (controller ruling): triage and the handover judge only fresh items
        triage = " ".join(self.section("**Triage.**", "7. **Identity").split())
        self.assertTrue(triage.startswith("**Triage.** Only the items that say `fresh: true`"))
        self.assertIn("the pass ends `interrupted`", triage)
        doc = " ".join(self.section("## Ellen: a document the operator hands over",
                                    "## Ellen: the pass").split())
        self.assertIn("only against payments whose item says `fresh: true`", doc)
        self.assertIn("end the pass `interrupted`", doc)
        pack = " ".join(self.section("## Packaging", "## Install").split())
        self.assertIn("ships unclassified with its documents set aside", pack)
        # issue #1: the import, not the sweep, tells the store what each payment is
        self.assertIn("Triage does not wait for the sweep", triage)
        self.assertIn("the import tells the store every payment's classification", doc)

    def test_a_refused_import_stops_the_pass_including_a_failed_withdrawal(self):
        # round E7: a withdrawal that fails refuses the whole import
        snap = " ".join(self.section("**Snapshot.**", "4. **Ends.**").split())
        self.assertIn("If the import is refused, stop: return the refusal, and the pass ends "
                      "`stopped` — nothing after this step runs.", snap)
        self.assertIn("could not withdraw a staged package — nothing was imported", snap)

    def test_every_observation_names_the_import_it_was_read_under(self):
        # round E3 (Astra S1): a read recorded after a newer import is refused
        section = " ".join(self.section("## The specialist's pass", "## Packaging").split())
        self.assertIn("snapshot_id=<the import's snapshot>, not_found=true", section)
        self.assertIn("passes the `snapshot_id` that `list_projections` returned", section)
        self.assertIn("read the payment again with its new `snapshot_id`", section)
        pack = " ".join(self.section("## Packaging", "## Install").split())
        self.assertIn("the first `stage_for_delivery` of a package, is refused because the bank "
                      "was re-read", pack)
        self.assertIn("A resend (\"send it again\") is the exact file already sent", pack)
        # round E5: an import takes back an unsent first send
        self.assertIn("fails because the file is gone, or `record_delivery` answers that the "
                      "bank was re-read before it was sent", pack)
        self.assertIn("A send already under way at the moment of the check cannot be stopped",
                      pack)


    # --- fix wave F -----------------------------------------------------------------------
    def test_ellen_holds_the_package_and_handover_passes(self):
        # (5) a delegation that dies must not strand the marker: Ellen begins and ends
        for head, until, begin in (("## Ellen: a document the operator hands over",
                                    "## Ellen: the pass", 'begin_pass(trigger="handover")'),
                                   ("## Packaging", "## Install",
                                    'begin_pass(trigger="package", reply="telegram")')):
            sec = " ".join(self.section(head, until).split())
            self.assertIn(f"`{begin}` yourself", sec, head)
            self.assertLess(sec.index("`continue_pass()`"), sec.index(f"`{begin}`"), head)
            self.assertRegex(sec.lower(), r"then `end_pass(\([^)]*\))?` yourself", head)
            self.assertIn("never calls `begin_pass` or `end_pass` here", sec, head)
            self.assertIn("by the outcome rule above", sec, head)
            self.assertNotIn("ran out of turns", sec, head)
        spec = " ".join(self.section("## The specialist's pass", "## Packaging").split())
        self.assertNotIn("begin_pass(", spec)

    def test_the_package_pass_sweeps_its_quarter(self):
        pack = " ".join(self.section("## Packaging", "## Install").split())
        self.assertIn("`list_projections(pass_token, quarter=<the quarter>)`", pack)

    def test_the_sweep_is_batched_and_ends_interrupted_when_the_budget_runs_out(self):
        sweep = " ".join(self.section("5. **Sweep.**", "6. **Triage.**").split())
        self.assertIn("Work in batches", sweep)
        self.assertIn("one write per row, then that row read again", sweep)
        self.assertIn("the pass ends `interrupted`", sweep)
        triage = " ".join(self.section("**Triage.**", "7. **Identity").split())
        self.assertIn("a page at a time", triage)
        self.assertIn("while its `next` is set and there is time, list again with "
                      "`after=<next>`", triage)
        self.assertIn("the last page's `remaining` count as `triage_remaining`", triage)

    def test_a_post_triage_sweep_mirrors_what_triage_decided(self):
        # aligned to tests/sim.run_pass: triage, then the sweep once more
        ident = " ".join(self.section("7. **Identity", "8. **Finish").split())
        self.assertIn("run it once more", ident)

    # --- issue #2: a pass that outlives its delegation ------------------------------------
    def test_a_delegation_that_answers_later(self):
        flat = " ".join(SKILL.split())
        self.assertIn("a system notification says a delegation to the finance specialist "
                      "returned or failed", SKILL.split("---")[1])
        later = " ".join(self.section("## Ellen: a delegation that answers later",
                                      "## Ellen: answering anything").split())
        for phrase in ("call `continue_pass()` first", "every continuation gets a NEW token",
                       "Never use a token, step or work list from an earlier turn or from the "
                       "notification", "this pass is no longer the current one",
                       "this package request has been taken over"):
            self.assertIn(phrase, later, phrase)
        self.assertIn(OUTCOME_RULE, flat)
        self.assertNotIn("`failed` if the delegation errored or ran out of turns", flat)
        import tools  # noqa: F401
        # the tool the model reads at the moment it gets the new token says it too
        self.assertIn("use only that one from now on",
                      qa_server.TOOLS["continue_pass"]["description"])

    def test_every_flow_continues_before_it_begins_and_starts_its_step(self):
        for head, until, step in (("## Ellen: a document the operator hands over",
                                   "## Ellen: the pass", "handover"),
                                  ("## Ellen: the pass", "## The specialist's pass", "sweep"),
                                  ("## Packaging", "## Install", "snapshot")):
            sec = " ".join(self.section(head, until).split())
            self.assertLess(sec.index("`continue_pass()`"), sec.index("`begin_pass("), head)
            start = sec.index(f'record_step(pass_token, step="{step}", action="start"')
            self.assertLess(sec.index("`begin_pass("), start, head)
            self.assertIn("`status: pending`", sec, head)

    def test_the_gmail_round_works_from_the_store(self):
        rnd = " ".join(self.section("**Gmail round.**", "\n5. If anything was filed").split())
        self.assertIn("The work list is the continuation's `work`", rnd)
        self.assertIn("never a list from the specialist's reply", rnd)
        for field in ("`search_hint`", "`window_days`", "`has:attachment`", "search Sent"):
            self.assertIn(field, rnd, field)
        self.assertNotIn("work order", SKILL)

    def test_the_handover_reads_the_recorded_pairing(self):
        doc = " ".join(self.section("## Ellen: a document the operator hands over",
                                    "## Ellen: the pass").split())
        self.assertIn("from the continuation's `documents`", doc)
        self.assertIn("Never infer a match", doc)

    def test_the_specialist_finishes_inside_the_clock(self):
        spec = " ".join(self.section("## The specialist's pass", "## Packaging").split())
        self.assertIn("Your last action, always — done, stopped or out of time — is "
                      "`record_step(pass_token, step=<the step Ellen named>, "
                      "action=\"finish\", remaining_in_cycle=…, triage_remaining=…)`", spec)
        self.assertIn("the first line being `quarterly-accounting: <step> finished`", spec)
        self.assertIn("If a write answers that this pass is no longer the current one, stop "
                      "and return: the pass has moved on and your recorded work is kept.", spec)
        sweep = " ".join(self.section("5. **Sweep.**", "6. **Triage.**").split())
        self.assertIn("until `remaining_in_cycle` is 0 or `time_up`", sweep)
        self.assertNotIn("turn budget", spec)
        self.assertIn("`list_quarter_state(triage=true, pass_token=…)`", spec)
        self.assertIn("`upsert_counterparty(name, search_hint=…, pass_token=…)`", spec)

    def test_package_continuations_never_resend(self):
        pack = " ".join(self.section("## Packaging", "## Install").split())
        self.assertIn("Its answer carries `package_token` and `next`", pack)
        self.assertIn("Never send the file again yourself", pack)
        self.assertIn("never write a failure line of your own", pack)
        self.assertIn('`send_media(path, kind="zip", filename=<the returned filename>',
                      pack)
        self.assertNotIn("Every `speak`", pack)          # said once, in the answers-later rules

    # --- code review round C1 ---------------------------------------------------------------
    def later(self):
        return " ".join(self.section("## Ellen: a delegation that answers later",
                                     "## Ellen: answering anything").split())

    def test_every_speak_is_sent_by_one_rule_beside_the_null_rule(self):
        later = self.later()
        self.assertIn("7. Every `speak` a tool returns — `begin_pass`, `end_pass`, "
                      "`continue_pass`, `record_delivery` — is sent verbatim and marked "
                      "delivered", later)
        self.assertIn("`continue` is null: write nothing (a `speak` is still sent — rule 7)",
                      later)
        self.assertEqual(" ".join(SKILL.split()).count("Every `speak` a tool returns"), 1)

    def test_every_delegation_names_its_step_and_ellen_always_finishes_it(self):
        flat = " ".join(SKILL.split())
        for step in ("sweep", "judge", "handover", "snapshot"):
            self.assertIn(f"`pass_token=<token>, step={step}`", flat, step)
        later = self.later()
        self.assertIn("pass that same token AND the step's name to the specialist", later)
        self.assertIn("When the delegation answers in this turn — whatever it answered, failed "
                      "or not — `record_step(pass_token, step=…, action=\"finish\")`", later)
        self.assertNotIn("its reply does not start", later)

    def test_the_specialist_never_claims_or_starts(self):
        spec = " ".join(self.section("## The specialist's pass", "1. **Probes.**").split())
        self.assertIn("never call `continue_pass`, and never `record_step(…, "
                      "action=\"start\")`", spec)

    def test_a_continuation_found_first_never_drops_the_request(self):
        later = self.later()
        self.assertIn("Then return to what was asked — `continue_pass()` again, and when it "
                      "has nothing, `begin_pass` and the requested flow", later)
        self.assertIn("The operator's request is never dropped.", later)

    def test_a_closed_request_is_said_and_email_after_telegram_is_a_new_request(self):
        pack = " ".join(self.section("## Packaging", "## Install").split())
        self.assertIn("\"email it to me\" after a Telegram delivery is a new request — step 1 "
                      "again with `channel=\"email\"`", pack)
        self.assertIn("never stopped on in silence", pack)

    def test_the_quarter_format_and_the_uncertain_offer(self):
        self.assertIn('("give me Q3" is `quarter="Q3"`)', " ".join(SKILL.split()))
        pack = " ".join(self.section("## Packaging", "## Install").split())
        self.assertIn("`record_delivery` then returns `speak`", pack)
        self.assertIn("that is what \"send it again\" binds to", pack)


class TestReviewC2(TempEnv):
    def test_an_earlier_continuation_never_silences_the_operators_request(self):
        later = " ".join(SKILL[SKILL.index("## Ellen: a delegation that answers later"):
                               SKILL.index("## Ellen: answering anything")].split())
        rule4 = later[later.index("4. When it answers `status: pending`"):later.index("5. On ANY")]
        rule6 = later[later.index("6. A refusal that"):later.index("7. Every `speak`")]
        self.assertIn("If it was an earlier continuation you were doing ahead of the "
                      "operator's own request (rule 1), their request did not run", rule4)
        self.assertIn("unless it was an earlier continuation you were doing ahead of the "
                      "operator's own request (rule 1)", rule6)
        for rule in (rule4, rule6):
            self.assertIn('"A check is running — ask again in a few minutes." — never '
                          "`<silent/>`", rule)


class TestReviewC3(TempEnv):
    def test_a_continuation_with_nothing_next_is_told_whatever_it_carries(self):
        pack = " ".join(SKILL[SKILL.index("## Packaging"):SKILL.index("## Install")].split())
        self.assertIn("A continuation whose `next` is null (a package request's, or a staged "
                      "send's `delivery_id`) carries a `speak`: send it verbatim, then "
                      "`mark_rendering_delivered`", pack)

    def test_begin_pass_speaks_too(self):
        flat = " ".join(SKILL.split())
        self.assertIn("If it answers `busy`: on the cron, output `<silent/>`; for the "
                      "operator, send its text", flat)
        self.assertIn("A `begin_pass` that reclaimed an abandoned pass may return `speak` "
                      "too (rule 7)", flat)


if __name__ == "__main__":
    unittest.main()
