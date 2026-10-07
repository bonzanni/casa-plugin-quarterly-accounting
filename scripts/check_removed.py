#!/usr/bin/env python3
"""The closing gate: no removed name (module, function, constant, tool, argument, table,
column or test helper) has a caller in server/, tests/, scripts/, skills/, README.md,
CHANGELOG.md or the manifest. Prints `file:line: pattern (what was removed)`; exit 1 on a hit.

Kept on purpose (so not listed): job.TURNS_PER_BATCH (the claim's batch logic, pinned to the
manifest) and asks.requeue_taken (the interrupt requeue); `resolves` is removed only as a tool
argument (a quoted string), lineage's own `resolves=` stays.
Exemptions: server/db.py inside MIGRATIONS (schema history), tests/schema_history.py, this
script and its test, a line carrying `# removed-name: asserted absent` or `# removed-name: schema history`, and a
block between `# removed-name: schema history begin` and `... end` (frozen old DDL).
tests/upstream (vendored bank-feed releases) is not scanned."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = "# removed-name:"          # asserted absent | schema history (one line)
BEGIN, END = "# removed-name: schema history begin", "# removed-name: schema history end"

REMOVED = [
    # 0.11.2 (#55): the start-from reading; getting a quarter done moves the start instead
    (r"\bset_watermark_in_tx\b", "work.set_watermark_in_tx (asks.request_work moves the start)"),
    # modules
    (r"\bimport (steps|sweep)\b|\b(steps|sweep)\.[a-z_]", "server/steps.py, server/sweep.py"),
    (r"\blegacy_tools\b|from tests import [^\n]*\bsim\b|\btests\.sim\b|\bsim\.(run_pass|"
     r"package_pass|observe_and_repair|ledger_state|sweep_)", "tests/legacy_tools.py, tests/sim.py"),
    # job.py
    (r"\bjob\.(credit|credit_sweep|credit_search|_acq|hand_acquisition|fresh_reason|require_fresh|"
     r"measure|stop_exhausted_pass|_choose|_take|run_passes|done|_done_now|_sends|_oversize|"
     r"_close_oversize|_left_owed|_left_render|_exhausted_alerts|_posts|_accounts_owed|"
     r"_record_offers|_begin_next|_first|_step|_poisoned|_acquisition|_continue_acquisition|"
     r"_stop|_sweep|_gmail|_judge|_start_judgment|restart_cause|bounded|_judge_unit|"
     r"_unjudged_handovers|_doc_key|_judged|_credit_page|_report_extras|_read_age_note|"
     r"_outcome|record_filing|_account|_summary|_settle_recovered|_close_left_behind)\b",
     "the S2/S7 cursor"),
    (r"\b(ADOPTIONS_MAX|LATE_TAKES_MAX|MAX_PASSES_PER_JOB|K_STATES|K_SEARCH|UNIT_COST|"
     r"BATCH_RESERVE|W_S|W_REFRESH_MAX|LEFT_WAITING|LEFT_BEHIND|BOUNDED|"
     r"UNBOUNDED|EMPTY_CHUNK)\b|\bjob\.(NOT_READ|OTHER_JOB|LATE_ASK|UNSWEPT|STALE)\b",
     "the cursor's budgets and freshness"),
    # passes.py
    (r"\bpasses\.(begin_pass|_open_request|_open_request_channel|_bind_round|queued_waiting|"
     r"requeue|_terminalize|snapshot_fate|round_fate|settle_snapshot_request|_close|"
     r"open_request|check_package_token|throughput|stored_report|end_pass|_end_pass_tx|"
     r"_round_judged|_judgment_uncovered|judgment_gap|_judgment_owed|_hand_over|"
     r"close_delegation_pass_on_upgrade|OPEN_REQUEST|CLOSED_WORD|BUSY)\b",
     "the delegation and package-request machinery"),
    # asks.py
    (r"\basks\.(request_package|record_verdicts|handover_covered|mark_reported|"
     r"_undelivered|_result_class|_result_tx|_sibling_ids|_render_result|_insert|_pages|"
     r"_stop_line|_case_lines|_case|_doc_label|_amount)\b", "the ask results and package asks"),
    # work.py, delivery.py, package.py, lineage.py, reducer.py, matches.py, views.py, db.py
    (r"\bwork\.(chunk_size|CHUNK_FIRST|CHUNK_LATER|searched_since|handled_since|hand_order|"
     r"searched_for|check_work|grow_owed|check_report|package_work|judge_due|judge_due_pids|"
     r"judge_due_state|_fx_fits|work_list|cut|judge_whole|package_check|work_item|"
     r"dates_unread|ELLEN_TURNS|TRIAGE_LIMIT)\b", "the chunk and judge machinery"),
    (r"\bdelivery\.(request_of_package|_staged_again|_package_note)\b|\bnote_render_id\b",
     "the request-bound sends and the package note"),
    (r"\bpackage\.(_request_for_build|RECHECK|stale_check|_Recheck|_caption)\b|"
     r"\bbound\s*=\s*(True|False)\b", "request-bound builds"),
    (r"\blineage\.(STATUS_PHRASE|_note_body|note_text|_doc_kinds)\b|Accounting revision",
     "the revision-numbered note"),
    (r"\b(kind_verdict|effective_kind|_why_not_kind|_walk_next)\b|\bmatches\._machine\b",
     "the deleted pairing gates and the walk's Next"),
    (r"\bdb\.(set_epoch|epoch)\b|store_epoch_at|['\"](package-note|job-left|package-stopped|"
     r"package-failed)['\"]|\balerts\.pass_notices\b", "the note epoch and the dropped kinds"),
    # tools and arguments
    (r"\b(list_projections|record_observation|request_package|more_work|continue_pass|"
     r"record_step)\b|[\"'`]build_quarterly_package",
     "removed tools (the function package.build_quarterly_package stays; its TOOL goes)"),
    (r"\b(package_token|judged|unread_dates|dates_unread)\s*=|\blate\s*=\s*(True|False)|"
     r"['\"](judged|package_token|dates_unread)['\"]",
     "removed arguments (`resolves` as a tool argument is checked on the tool schemas by tests/test_check_removed.py: lineage keeps its own)"),
    # tables and columns (schema 12 drops)
    (r"\b(pass_steps|package_requests)\b|\b(FROM|INTO|UPDATE|TABLE)\s+(credits|cursor)\b",
     "dropped tables (`cursor` and `credits` qualified: views pages by a cursor)"),
    (r"\b(orphaned_by|adoptions|adopters_json|read_seq|w_refreshes|judge_after|w_pending|"
     r"judge_epoch|late_takes|swept_at|observed_revision|note_seen_seq|note_seen_rev|"
     r"note_seen_at|note_issued_at|note_issued_seq|note_other_issued_at|readback_owed|"
     r"note_issued_gen|note_other_issued_gen|note_seen_gen|read_snapshot|note_seq|note_body|"
     r"claimed_step|verdicts_json)\b", "dropped columns"),
    (r"\bspent=|sum\(spent\)|\breported=1|runs\(job_id,\s*passes|SET passes\s*=|"
     r"(packages|deliveries)\([^)]*\brequest_id", "dropped columns (qualified)"),
    # the work queues (operator ruling A) replace the per-unit continuation state
    (r"\b(record_filing|hand_progressed|idle_hands|HAND_MAX|_settle_hand|unfiled_total)\b|"
     r"\bruns\.filed_at\b|run\[['\"]filed_at['\"]\]|SET filed_at|['\"]unfiled['\"]",
     "the per-unit continuation state the queues replace"),
    # rev 18.4: payment by payment, the model decides (vendor units, group splitting, the
    # once-per-run search marks, reopening by a later document, considered_seq)
    (r"\b(vendor_unit|vendor_unit_in_tx|GROUP_MAX|_vendor_searches|owing_vendors|"
     r"unit_of_vendor|_mark_vendor_search|_queue_refs|considered_seq|handed_upto|_reopening|"
     r"vendor_queries)\b|\bsearches\.(hinted|plain)\b|['\"](reopen|competitor)['\"]",
     "the vendor unit and the reopening rev 18.4 deletes"),
    # the call budget (OPERATOR RULING 2026-10-07, after diff round f3)
    (r"\b(calls_made|CALLS_SOFT|CALLS_HARD|unit_room|unit_fits|MIN_WORK|max_calls|UnitBudget|"
     r"CLOSING_TOOLS)\b|\bloop\.CLOSING\b|\bqueues\.COST\b|['\"]end-batch['\"]",
     "the call budget: calls_made, max_calls, end-batch (operator ruling 2026-10-07)"),
    # test helpers
    (r"\bself\.(handed|close_chunk|hand_empty_chunk|end_with_counts|package_built_unsent|"
     r"stage_stalled_package|check_round|bind_round_and_take|sweep_to_zero|start_job_pass|"
     r"drive_to_staged|delivered_package|package_token)\(|(?<![.\w])(hand|close_chunk)\(|"
     r"\bimport[^\n]*\b(hand|close_chunk)\b", "deleted test helpers (StoreCase methods, and "
     "tests._base's module functions hand / close_chunk; a qualified x.hand_calls( is not one)"),
]

SCAN = ("server", "tests", "scripts", "skills")
SKIP_DIRS = ("tests/upstream/",)     # vendored bank-feed releases the tests install, not our code
SKIP = {"tests/schema_history.py", "scripts/check_removed.py", "tests/test_check_removed.py"}


def files():
    for d in SCAN:
        for ext in ("*.py", "*.md"):
            yield from sorted((ROOT / d).rglob(ext))
    for f in ("README.md", "CHANGELOG.md", ".claude-plugin/plugin.json"):
        if (ROOT / f).exists():
            yield ROOT / f


def scan():
    pats = [(re.compile(rx), what) for rx, what in REMOVED]
    hits = []
    for path in files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in SKIP or rel.startswith(SKIP_DIRS):
            continue
        in_migrations = history = False
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if rel == "server/db.py":
                if line.startswith("MIGRATIONS: dict"):
                    in_migrations = True
                elif in_migrations and line.startswith("}"):
                    in_migrations = history = False
                    continue
                if in_migrations:
                    continue
            if line.strip() == BEGIN:
                history = True
            if history or MARK in line:
                history = history and line.strip() != END
                continue
            for rx, what in pats:
                m = rx.search(line)
                if m:
                    hits.append(f"{rel}:{n}: {m.group(0)!r} ({what})")
    return hits


def main() -> int:
    hits = scan()
    for h in hits:
        print(h)
    print(f"{len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
