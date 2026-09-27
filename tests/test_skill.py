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
)


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
                                        "expected_ledger", "receipt_pages"}:
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
                       "Other workflows' tags", "first seen", "write_error",
                       "Never make two writes without a read between them",
                       "the bank ledger changed during this pass"):
            self.assertIn(phrase, section, phrase)
        # read -> record -> write -> read again, in that order
        order = ["`get_transaction(row_id)`", "`record_observation(pid, pass_token, "
                 "observed_tags=", "`untag_transaction(", "read the row again",
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
        rnd = self.section("**Gmail round.**", "\n5. ")
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
        # round E1 (Astra S1): only the sweep's reads refresh the classification
        pack = " ".join(self.section("## Packaging", "## Install").split())
        order = ["the snapshot of step 3", "the ends of step 4", "the sweep of step 5",
                 "`end_pass`", "`build_quarterly_package(quarter)`"]
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


if __name__ == "__main__":
    unittest.main()
