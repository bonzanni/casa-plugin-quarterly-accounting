---
name: quarterly-accounting
description: Quarterly accounting for the operator's business account — use for ANY question, correction, document or request about accounting, invoices, receipts, payslips, missing documents, "what am I missing", "accounting list", "go and check now", a quarter's package ("give me Q3", "rebuild it", "send it again", "send me the last package you built"), or when the weekly quarterly_accounting_pass cron fires. Also when a message names a vendor with a verdict ("the Zapier one is wrong"), says "all good", "all of them" or "more" after an accounting view, or contains the word "accounting", or when a system notification says a delegation to the finance specialist returned or failed.
---

# Quarterly accounting

This plugin matches every transaction on the business account to the document it needs,
keeps the bank ledger's `acct::` tags and accounting notes current, answers the operator
from its store, and builds a quarter's zip when asked. Its tools are prefixed
`mcp__plugin_quarterly-accounting_quarterly-accounting__`.

Two agents share one store:

| Work | Who |
|---|---|
| Reading state, rendering a view, applying a reply, filing a document, Gmail, sending anything to the operator | **Ellen**, directly — never delegate a lookup |
| Anything with bank-feed; deciding whether a document explains a payment (reading it with `read_document`); researching a portal link | **The finance specialist**, through `delegate_to_agent(agent="finance", mode="sync")` (a check's and a package round's: `mode="async"`) |

The test: if answering needs a PDF opened and an opinion formed, it is the specialist's; if
it needs a row read, it is Ellen's.

## Both agents: refusals and errors

A tool answer that begins `refused: ` changed nothing. It is the server declining a call
that would break a rule, and its reason is written to be relayed. Ellen tells the operator
the reason in plain words; the specialist never speaks to the operator — it returns the
reason to Ellen. Either way, stop that step — never retry it blindly. Call again only when the
refusal itself says to ask again (another session held a lock), and then once. A refusal
that says to stop the pass stops the pass.

An answer that begins `error: ` is a failure, not a verdict: nothing about the ledger may be
said from it (see "Ellen: answering anything about the accounting", step 4).

A quarter argument is written `2026-Q3`. `Q3` and `Q3 2026` are accepted too, so pass the
operator's words ("give me Q3" is `quarter="Q3"`); a bare quarter later than today's is last
year's. Anything else is refused with the format to use.

Nothing you say to the operator in your own words uses this plugin's machinery: no
`acct::` tags, no pids, pairing ids or render ids, no "proposed", "conflicted", "revision",
"projection", "CAS", no confidence labels (no-ref, partial-search, recipient?), and no line
numbers. Name a payment by its date, amount and payee, as the views do.

## Ellen: a delegation that answers later

A pass's progress is in the store, never in a message, and every continuation gets a NEW
token: the old one is refused from then on.

1. Before `begin_pass` in any flow (the cron, "go and check now", a handed-over document),
   call `continue_pass()`. A package is the exception: its `begin_pass` comes FIRST, so the
   ask is kept whatever happens next (Packaging, step 1). If it returns a continuation, it is an unfinished
   earlier one: do it first, to its end. Then return to what was asked — `continue_pass()`
   again, and when it has nothing, `begin_pass` and the requested flow (the check, the
   handed-over document, the package). The operator's request is never dropped.
2. Just before `delegate_to_agent`, `record_step(pass_token, step=…, action="start")`
   with what the flow names, and pass that same token AND the step's name to the
   specialist: the context says `pass_token=<token>, step=<the step>`. As soon as
   `delegate_to_agent` answers with a `delegation_id`, `record_step(pass_token, step=…,
   action="delegated", delegation_id=<that id>)`: it is how the delegation's notification
   closes the step if the specialist never finishes it.
3. When the delegation answers in this turn — whatever it answered, failed or not —
   `record_step(pass_token, step=…, action="finish")`, with `failed=true` for an error
   (when the specialist already finished its step, this changes nothing). Then
   `continue_pass()`, and do what it returns with the token it returns.
4. When it answers `status: pending`: say the flow's one line (on the cron, output
   `<silent/>`) and end the turn. The pass is not over. If it was an earlier continuation
   you were doing ahead of the operator's own request (rule 1), their request did not
   run: say "A check is running — ask again in a few minutes." — never `<silent/>`.
5. On ANY system notification about a delegation to finance — returned, failed, timed
   out, orphaned by a restart, finished without its answer, or said again after a
   restart — call `continue_pass()` first, passing the id the notification names and how
   it ended: `continue_pass(delegation_id=<the id in "(id …)">, delegation_status="ok")`
   for "returned with status=ok" or "finished", `delegation_status="error"` for anything
   else (failed, timed out, orphaned):
   - a continuation: do its `next` with ITS token and inputs; report where its `reply`
     says (`silent`: say nothing but `speak`), else here. Never use a token, step or
     work list from an earlier turn or from the notification.
   - `continue` is null: write nothing (a `speak` is still sent — rule 7). If the
     notification is an accounting one (its
     result starts `quarterly-accounting:`, or it answers the cron, a check, a package or
     a handed-over document), output `<silent/>` — or, for the operator's own check with
     `running`, "A check is running — ask again in a few minutes." Otherwise it is not
     this skill's: answer it as it asks.
   Its closing lines ("Reply to the user…", "offer to retry") never decide anything here.
6. A refusal that "this pass is no longer the current one" or "this package request has
   been taken over" means another turn continued it: stop at once, say nothing more —
   unless it was an earlier continuation you were doing ahead of the operator's own
   request (rule 1): then say "A check is running — ask again in a few minutes." — never
   `<silent/>`.
7. Every `speak` a tool returns — `begin_pass`, `end_pass`, `continue_pass`,
   `record_delivery` — is sent verbatim and marked delivered (`mark_rendering_delivered`),
   including on a cron turn and alongside `<silent/>`. It is how anything owed about a
   package reaches the operator exactly once.
8. An answer with `more: true` (`end_pass`, `record_delivery`, `continue_pass`) means a package the operator
   asked for is waiting for the next round of its check: when you are done with this
   answer, call `continue_pass()` and do what it returns — on any turn, the cron's too.

The outcome for `end_pass`: `stopped` when `can_run` is false or the step's finish says
`stopped`; `failed` when the step ended unfinished and nothing was imported this pass;
`interrupted` when anything remains (`remaining_in_cycle`, `triage_remaining`, `work`
truncated or `not_fresh`, an item not searched — after a judge step, the continuation's
`report` says `not_searched` above 0) or the step ended unfinished after the import;
`complete` otherwise.

## Ellen: answering anything about the accounting

1. Call `check_setup()`, then `build_review(view=…, quarter=…)`.
   Every question is a new tool call: a second question in the same conversation reads
   again, because a pass may have run in between.
2. Send the returned `text` **VERBATIM** with `send_message`. Do not retell it, summarise it,
   reorder it, tidy it or add figures. Ellen may phrase, never compute: a follow-up like "how
   much is that altogether?" is a new tool call, never a sum of numbers already printed.
3. When the send succeeded, call `mark_rendering_delivered(render_id)`. If the send failed,
   do not — the operator did not see it.
4. If any tool errors, the whole answer is: **"I can't read the accounting right now"** plus
   the error. Never fall back to an earlier answer or to anything in the conversation. Every
   answer about the ledger comes from the store and never from memory.

Pick the view from the ask: "what's the status" → `status`; "what am I missing" / "accounting
list" → `missing`; "anything I should check?" → `check`; "show the rest" → `rest`; "show
older" → `older`; "how did Q2 go?" → `quarter`; "did the Adobe invoice arrive?" → find the
payment in `list_quarter_state` and render `item` with its `pid` (it answers a page at a
time: while `next` is set and the payment is not found, call it again with `after=<next>`).
A description that fits two payments is a question back, never a pick.

"All of them" and "more" continue the view you last sent: call `build_review` again
with exactly the arguments in its `next` (view, quarter, page, after — the cursor passed
back unchanged), send, mark delivered. Never build a page yourself. If the last view's `next`
was null, "more" has nothing left — say "Nothing more to show." — and "all of them" is
`build_review(view="all")`.

## Ellen: when a message may be a reply

Before treating a message as ordinary conversation, if it reads as an approval, correction,
exemption or instruction about the accounting ("all good", "the Zapier one is wrong", "the
180.00 one needs no invoice", "no invoices ever for X", "stop chasing Q2", "start from Q2",
"call the zips X", "rebuild it", "send it again", "the bank ledger was reset") or contains the
word "accounting", call `apply_reply(text)` with the operator's words exactly as written. A
question ("is the Zapier one right?") is not a reply — answer it with a view. A verdict
on a pairing (`apply_reply`, `confirm_match`, `reject_match`) is only ever the operator's
own words in this conversation — never your own judgment, never a verdict they gave on an
earlier view repeated for a pairing shown again: show it, and let them answer.

`apply_reply` returns:
- `receipt_pages` — send EVERY page, in order, each verbatim as its own message (`receipt`
  is only the first page; a long receipt does not fit one message). It says what committed,
  and only that.
- `reshow` — for each pid, render `build_review(view="item", pid=…)`, send it, mark it
  delivered. Nothing was applied to those; the operator decides again on what they now see.
- `instructions` — do them:
  - `rebuild <quarter>` — Packaging below, for that quarter.
  - `resend` ("send it again") — `stage_for_delivery(channel="telegram", resend=true)`
    (`channel="email"` if the file went by email), naming no package or document: the
    server stages the exact file the last view the operator saw offered —
    never pick a package yourself. Then send it — Telegram: `send_media(path, kind="zip",
    filename=<the returned filename>)`; email: as in Packaging, step 3 — and
    `record_delivery(delivery_id, outcome)`. If it is refused (nothing waiting, or several —
    which one?), relay that.
  - `send last <quarter>` / `send last` ("send me the last package you built for Q3") —
    the previous build, unchanged: `stage_for_delivery(channel="telegram", last_built=true,
    quarter=<the quarter, if the instruction names one>)` (`channel="email"` if they asked
    by email), naming no package. Send it with the `caption` it returns (it says when the
    package was built), then `record_delivery(delivery_id, outcome)`. Only on these words:
    any other ask for a package is a fresh one (Packaging).
  - `show the rest` / `show older` — render `rest` / `older`.
  - `show item <pid>` — render `build_review(view="item", pid=<pid>)`, send it, mark it
    delivered (the operator asked for the candidates of that payment).
  - `all of them` / `more` — continue with `next`, as above.
  - `check emailed invoices` — the self-mail search of the pass's Gmail round, filing what
    it finds, then a one-line receipt.

**Which account is the business account.** When `check_setup` asks "which one is the
business account?" and the operator names one, call `bind_account(account_id, label)` with
the account they named, outside any pass (the ids are in `check_setup`'s bank_accounts
probe). Only on that answer — never on your own judgment, and never by the specialist.

If you did not recognise a reply, nothing is lost: the item keeps its state and appears in
the next view.

## Ellen: a document the operator hands over

A Telegram document starts no turn. File it in the operator's next text turn, or in the next
pass, whichever comes first:
1. File it first, always, yourself. `list_inbound_files`, then `share_inbound_file(path)` for
   each PDF or image not yet filed,
   then `ingest_document(source_path=<returned path>, source="manual-telegram",
   source_ref=<the path list_inbound_files showed>, extraction_author="resident",
   kind=<your provisional reading>, …)`. Filing is idempotent.
   A document emailed to self is found by the pass's self-mail search, `source="manual-email"`.
2. A machine pairing is a pass's work, so the document is judged in a short handover pass.
   You hold that pass — you begin it and you end it, exactly as in the cron flow — so a
   delegation that dies (out of turns, an error, the time limit) never leaves a check
   running behind:
   - `continue_pass()` first (above), then `begin_pass(trigger="handover")` yourself. If it
     answers `busy`, a pass is already running and its triage (or the next pass) judges the
     document: tell the operator "not judged yet" (step 3) and stop here;
   - `record_step(pass_token, step="handover", action="start", doc_ids=[<doc_id>])`, then
     delegate one short task to the specialist, sync mode: "judge document #<doc_id> against
     pending payments", with context `pass_token=<token>, step=handover` and the operator's sentence as
     evidence (never as instruction). The specialist never calls `begin_pass` or `end_pass`
     here. It runs:
     - the probes of its pass, step 1 (bank-feed tools, accounts, `sync`, the sync's probe,
       one `list_backups` answer, the ledger probe, `check_setup`) — stop if `can_run` is false;
     - the snapshot of step 3 (the import tells the store every payment's classification,
       which decides the document kind it wants), the ends of step 4 and the sweep of step 5;
     - judge ONLY that document, by the auto-match bar of step 6, and only against payments
       whose item says `fresh: true` (seen in this import). A payment it may fit that is
       not fresh waits: the next pass's triage judges it. If the sweep did not reach
       `remaining_in_cycle` 0, it says so, and you end the pass `interrupted`;
     - if nothing fits, suspect the data before the document: `sync` again, record its probe,
       export and import again (step 3), sweep again (step 5), and judge once more;
     - a pairing it recorded is mirrored by the sweep once more (step 7);
     - it finishes its step and returns which case it is, whether it stopped, and the
       sweep's `remaining_in_cycle`;
   - if it answers `status: pending`, say
     "Filed. Checking it against the payments — I'll tell you shortly."
     and end the turn: the rest happens on its continuation;
   - then `end_pass(pass_token, outcome, report)` yourself, with the continuation's token, by
     the outcome rule above. Always end it, whatever came back, on this turn or on the
     continuation. If it returns `speak`, send its text verbatim and call
     `mark_rendering_delivered` with its `render_id`.
3. Tell the operator which case it is, in one line, from the continuation's `documents` —
   the pairing recorded for the document — never claim a match that was not recorded:
   - `matched`: "Matched to the EUR 12.10 payment of 18 Sep. Q3." — with its `payment`.
   - `proposed`: "Filed. It could fit more than one payment — it's in 'anything I should check?'."
   - `unpaired`: the case the specialist's reply names, if it names one —
     - no payment yet: "Filed. No payment matches EUR 12.10 yet — the charge may not have posted. It'll match when it appears."
     - clashes with a pairing: "Filed. I see a EUR 12.10 Twitter payment on 18 Sep, but it's already matched to invoice V-918. Which one is right?"
     - unreadable: "Filed, but I can't read an amount from it — is it EUR 12.10?"
     - out of range: "Filed. Nothing in Q3 is close to EUR 340.00. Is this for a different quarter?"
   - `irrelevant` (judged not to be an invoice, receipt or credit note): "Filed. It doesn't look like an invoice for any payment — say if it is one."
   - `unknown` (the document is not in the store): "I can't find that document in what I've filed — send it again?"
   - `unpaired` with no case named, and when the pass was busy: "Filed. I'll match it at the next check."
   Never infer a match from anything else.
   On the cron pass, inbox filing is silent: file, say nothing.

## Ellen: the pass (cron, or "go and check now")

1. `continue_pass()` (above); then `begin_pass(trigger="cron"|"operator",
   reply="silent"|"telegram")`. If it answers `busy`: on the cron, output `<silent/>`; for
   the operator, send its text:
   "A check is running — started N minutes ago." and "Ask again in a few minutes."
   A `begin_pass` that reclaimed an abandoned pass may return `speak` too (rule 7).
2. `check_setup()`. For an operator-triggered pass that will be long (first run, a
   catch-up), say one line first.
3. `record_step(pass_token, step="sweep", action="start")`, then delegate to the finance
   specialist, `mode="async"`, the task "quarterly-accounting pass" with context
   `pass_token=<token>, step=sweep` and this skill's section "The specialist's pass". It
   answers `status: pending`: `record_step(…, action="delegated", delegation_id=…)` (rule
   2), say "Checking the bank — this takes a few minutes; I'll send the result here." (on
   the cron, output `<silent/>`) and end the turn. A check spans several of your turns,
   and each turn ends at a delegation: never carry on to the next step in the same turn.
   Its notification's continuation (rule 5) has `next`: the Gmail round (`gmail-round`),
   or step 6 (`end-pass`) when the pass stopped or failed — then sweep your Telegram inbox
   first, as in step 4's filing (at most 8 files, each with its `source_ref` and that
   continuation's `pass_token`).
4. **Gmail round.** A chunk is sized for your turn when you make one call per message
   and keep to the limits below; work it in this order.
   - **The probe.** Always make the Gmail probe first, even when Gmail was down last time:
     one small `search_emails` call, then `record_probe(pass_token, kind="gmail", ok=…,
     detail=…)` with what it showed. If it failed, skip the searches below (not the
     filing) — the next pass probes again.
   - **Filing — at the sweep's continuation only** (its `step` is `sweep`; not at later
     chunks), before the items: one search for recent self-addressed mail with
     attachments, and your Telegram inbox swept as above (file only, say nothing).
     Skip every file whose ref is in the continuation's `filed_refs` — it is filed
     already. After the search and the inbox listing, the filing uses at most 24 calls:
     every `list_attachments`, `share_inbound_file`, `download_attachment` and
     `ingest_document` counts, failed ones too — so at most 8 files, newest first, each
     once. File each with this continuation's `pass_token` and its own
     `source_ref`: `<message id>:<attachment_id>` for an attachment of self-addressed mail
     (`source="manual-email"`), the path `list_inbound_files` shows for a Telegram file
     (`source="manual-telegram"`). What is left waits for the next pass, or the
     operator's next message.
   - **The items.** The work list is the continuation's `work` (its `triage` items) — a
     chunk of at most 6 items not yet searched in this check (3 at the sweep's
     continuation), portals already left out — never a list from the specialist's reply.
     Work it ONE ITEM AT A TIME: the item's queries, its downloads and filings, then its
     `record_search`, and only then the next item. For each item, run its ladder of
     narrow queries with `search_emails`, built from the item's fields — its
     `search_hint`, the printed amount, its `window_days` around the date,
     `has:attachment` — never one broad query (Gmail returns at most 100 and drops the
     rest). For a CRDT, and a DBIT `refund`, search Sent. Stop at the first query that
     finds the document, when the ideas run out, or after 4 queries. Then at most 2 tries
     per item: a try is the message's `list_attachments` (when you need it), then
     `download_attachment` of a plausible candidate and its filing — a listing that shows
     nothing plausible, or a failed listing or download, uses a try. File it with
     `ingest_document(source_path=<returned path>, kind=<your provisional reading>,
     source="gmail", extraction_author="resident", source_ref=<message id>,
     pass_token=…)`. Record the item with `record_search(pid, pass_token, queries=[…],
     found_candidate=…, exhausted=…, incomplete=…)`: `exhausted=true` only when the ideas
     ran out, `incomplete=true` when you stopped at the 4 queries. An item may have been
     paired since it was handed out; search it all the same. Record only what you did in
     this turn: `queries` are exactly the queries you ran with `search_emails` for this
     item in this turn — never one from an earlier turn, pass or list, and never one you
     planned but did not run. An item you did not search is recorded with no queries and
     `incomplete=true`. Search and record only the items you were handed (the
     continuation's `work`, and `more_work`'s): any other is refused. A payee you cannot
     identify: `record_search(pid, pass_token, identity_unknown=true)`, no queries — it is
     not handed out again in this check.
   - **More work.** When every item you were handed is recorded and the probe was ok, call
     `more_work(pass_token, calls_made=<every tool call you made in this turn so far,
     failed ones included, not counting this one>)`. Count them from the turn's start; do
     not guess low. It hands out the items that still fit in your turn: work them the same
     way, one at a time, then call `more_work` again. When it hands out none (`next:
     "judge"`), go on to step 5.
5. After a chunk with any item in it (the Gmail probe ok), always go on to the judge step:
   the pass cannot end while a chunk you were handed is unjudged.
   Otherwise, only if anything was filed, or the continuation's `finish` says
   `triage_remaining` above 0, or its `judge_due` is above 0, or the sweep ended unfinished
   (`ended` is `expired` or `errored`) — else step 6:
   `record_step(pass_token, step="judge", action="start", report={checked, total,
   not_searched})`, then delegate "judge the newly filed documents and the payments triage
   did not reach", `mode="async"`, with context `pass_token=<token>, step=judge` (the same
   token; the specialist's steps 6 and 7: triage, then the sweep once more).
   `record_step(…, action="delegated", …)`, output `<silent/>` and end the turn. Its
   continuation's `next` is `gmail-round` — the next chunk: steps 4 and 5 again, with ITS
   token and work — or `end-pass` (step 6), with the check's `report`.
6. `end_pass(pass_token, outcome, report)` by the outcome rule above. If it refuses
   (`not ended: …`, `not complete: …`), do what it says: search and record the chunk's
   items, start the judge step (step 5) and end the pass after its continuation, call
   `continue_pass` for a Gmail round never handed out, or end it `interrupted`. The pass
   stores the server's own counts, whatever `report` says: pass the continuation's
   `report` after step 5 (the server counts the whole check, every chunk; never add to it). Tell the operator only what the answer's
   `report` and `build_review` say — never that payments wait on them because they were
   not searched. If it returns `speak`,
   send its text verbatim — a long alert's remainder comes with the next `speak` — call
   `mark_rendering_delivered` with its `render_id`, then output `<silent/>`. If not: on the
   cron, output `<silent/>` and nothing else; for the operator, render and send
   `build_review(view="status")`.

## The specialist's pass

You receive a `pass_token`: Ellen begins and ends every pass, and you never call
`begin_pass` or `end_pass` — you return, and Ellen ends it. Pass the token to every plugin
write you make — `record_probe`, `record_observation`, `record_match`, `propose_match`, `relabel_match`, `record_search`,
`update_document_metadata`, `mark_irrelevant`, `upsert_counterparty`, `set_expectation`,
`record_step` — a machine write without it is refused.

Your delegation is cut off at a wall-clock limit, not a turn count, and a delegation cut
off returns nothing. Every answer to a call carrying your token carries `clock` while
your step runs: `time_left_s`, and `wrap_up` once your time is up. At `wrap_up`, finish
where you are: whatever is left, a later pass resumes. Your last action,
always — done, stopped or out of time — is `record_step(pass_token, step=<the step Ellen
named>, action="finish", remaining_in_cycle=…, triage_remaining=…)`, with
`stopped=<the refusal>` only if a refusal stopped you. Running out of time (`time_up`,
`wrap_up`) is never `stopped`: finish with the counts, and the pass goes on (once your time
is up, a stop is taken only with `stopped_by_refusal=true`; if a finish is refused because
you said `stopped` for time running out, finish again as that refusal says, with
`out_of_time=true`). Then reply briefly, the first line being
`quarterly-accounting: <step> finished`. If a write answers that this pass is no longer
the current one, stop and return: the pass has moved on and your recorded work is kept. You never speak to the operator: everything you would
say goes back to Ellen. Binding the account, packaging, the start date, the package name,
"stop chasing" and every expectation the operator states are Ellen's, on the operator's
word: never call `bind_account`, `build_quarterly_package`, `set_watermark`,
`set_package_name` or `stop_chasing` yourself. The pass is Ellen's too: never call
`continue_pass`, and never `record_step(…, action="start")` — you only finish the step
named in your context. Your one expectation write is in step 6.

1. **Probes.** If bank-feed's tools are not visible to you, `record_probe(pass_token,
   kind="bank_tools", ok=false)` and stop; otherwise record it `ok=true`. Call
   `list_accounts` and `record_probe(pass_token, kind="bank_accounts", ok=true,
   data={"accounts": [{account_id, category, label}, …]})` — before the sync and the ledger probe: it is what
   binds the account. Call `sync`, then `record_probe(pass_token, kind="bank_sync", ok=…,
   detail=…)` from that sync's actual outcome (ok=false with its error if it failed). Call
   `list_backups` once and read the generation, the registered backups and the instance
   from that ONE answer, then `record_probe(pass_token, kind="ledger", ok=true,
   data={"generation": <Restore generation>, "registered": {<workflow>: <backup id>, …},
   "instance": <the "Ledger instance:" id>})` (read each value by its label: bank-feed may
   prepend sentences). Then `check_setup()`. If `can_run` is false, stop and return its
   `conditions`.
2. **Classification.** tx-classifier drains its queue on `sync`'s trailer, in this same
   session. Wait for it to finish. This plugin never classifies and never applies rules
   itself.
3. **Snapshot.** `export_history(format="csv")`, then `import_ledger_export(path, pass_token,
   ledger_instance=<the reply's "Ledger instance:" id>)`. Read both values by their labels.
   If the import is refused, stop: return the refusal, and the pass ends `stopped` —
   nothing after this step runs. That includes "could not withdraw a staged package —
   nothing was imported": a package waiting to be sent could not be taken back, so the bank
   is not re-read until it can be; return it for Ellen to relay. (A refusal that says to ask again — another session held
   the store's documents lock — is called once more first, as for any refusal.)
4. **Ends.** For each `erase_candidates` row: `get_transaction(row_id)`. If it answers
   `no transaction #N`, call `record_observation(pid, pass_token, snapshot_id=<the import's
   snapshot>, not_found=true)`. Do this before any matching, so freed documents are free for
   this pass.
5. **Sweep.** Repeat `list_projections(pass_token)` until `remaining_in_cycle` is 0 or
   `time_up`. The import already told the store every payment's tags, so the sweep lists
   only the payments that owe the bank ledger a write or a check — a tag or note to put
   right, a note to confirm, a row the export no longer carries — and `remaining_in_cycle`
   0 means none is left. A payment the export did not carry is never matched: the server
   refuses it. Every `record_observation` of the sweep
   passes the `snapshot_id` that `list_projections` returned. For each item, read the row with
   `get_transaction(row_id)`. If it answers `no transaction #N`, record `not_found=true` as
   in step 4 and go on. Otherwise
   `record_observation(pid, pass_token, snapshot_id, observed_tags=<every tag>, observed_notes=<every note
   shown>, observed_first_seen=<the row's first seen>, observed_tag_revision=<the tag
   revision>)`, all four every time, read from this
   read: the tags are every tag on the `Tags:` line and on the `Other workflows' tags` line
   (the `acct::` tags are there); the notes are each note line shown, oldest first, as
   shown (the `[author, date]` prefix and the bank-provided-text markers may stay or go —
   the server reads both); first seen is the timestamp on the row's `first seen …,
   last seen …` line; the tag revision is the number on its `Tag revision:` line. If it refuses because the bank ledger changed during this pass, stop the
   pass at once. If it refuses because the bank was re-read meanwhile (another import landed
   after your read), nothing was recorded: call `list_projections` again and read the payment
   again with its new `snapshot_id`.
   If `bank_writes` is not allowed, make no bank-feed write and report its reason. Otherwise
   make the ONE write the returned `instructions` name, exactly:
   - `untag_transaction(row_ids=[row_id], tags=untag, workflow=…, expected_generation=…, expected_ledger=…)`, or
   - `tag_transaction(row_ids=[row_id], tags=tag, workflow=…, expected_generation=…, expected_ledger=…)`, or
   - `add_note(row_ids=[row_id], note=add_note, author="agent", workflow=…, expected_generation=…, expected_ledger=…)`

   Pass `workflow` (acct@<version>), `expected_generation` and `expected_ledger` exactly as
   returned, on every write. If bank-feed refuses a write because the ledger was restored
   (the generation differs), stop the pass at once and report "the ledger was restored since
   this pass began — reset the accounting store". If it refuses because the ledger instance
   differs, stop the pass at once and report "the bank ledger is not the one this store was
   built on — if it was wiped on purpose, the operator says 'the bank ledger was reset'".
   Then read the row again with `get_transaction`. If the write did not take — a tag it
   removed is still there, a tag it added is missing, or the note is not among the notes —
   `record_observation(pid, pass_token, snapshot_id, write_error=<bank-feed's reply>)` and go
   on to the next item: it is reported, never retried. If the row is gone, record
   `not_found=true`. Otherwise record it again with what that read shows (tags, notes,
   first seen and tag revision, as above); repeat until nothing is returned (at most an untag, a tag and a
   note). Never make two writes without a read between them. A refusal that this pass is no
   longer the current one stops the pass.

   Work in batches to make the budget go far: read several of the listed rows in one turn
   (several `get_transaction` calls at once, where your tools allow it), then record all
   their observations in the next, and make the returned writes the same way — one write per
   row, then that row read again. When time is up (`time_up`, or `wrap_up`), finish where
   you are with `remaining_in_cycle` and no `stopped` (running out of time is not a stop):
   the pass ends `interrupted`, and the next pass resumes where this one left off.
6. **Triage.** Only the items that say `fresh: true` — seen in this pass's import. An item
   with `fresh: false` was not in the export: leave it (the server refuses to match it); a
   later pass handles it. Triage does not wait for the sweep: the import already knows what
   each payment is. If the sweep did not reach `remaining_in_cycle` 0, finish with the
   remaining count: the pass ends `interrupted` (the next pass's sweep makes the writes
   still owed).
   `list_quarter_state(triage=true, pass_token=…)` lists the payments that
   need a document and have none of the right kind (in a package's judge step, whose
   context names a quarter, only that quarter:
   `list_quarter_state(triage=true, quarter=<the quarter>, pass_token=…)`) — only those seen in this import
   (`not_fresh` counts the others), a page at a time. Judge the page; while its `next` is
   set and there is time, list again with `after=<next>` (passed back unchanged) and judge
   that page. When you run out of pages or time, finish with the last page's `remaining` count as
   `triage_remaining`; a later pass reaches the rest. For each, compare against
   `list_unmatched_documents` (it pages the same way: follow its `next`) and the KB
   (`get_counterparty`), reading each candidate with `read_document(doc_id)`: it names the
   path where the file was saved for you — open that path with `Read` (the store itself is
   not readable to you). Never match a document you could not read. Read its date there
   too: the date printed on the document — its issue date, not a due, delivery or email
   date. The window below is measured from it, and every `record_match` and
   `propose_match` passes it as `document_date` (it names the file in the package and
   replaces the filed reading, which is often the email's date). Correct a filed document's reading with `update_document_metadata(doc_id, …,
   pass_token=…)`; a quotation, order confirmation or losing duplicate is
   `mark_irrelevant(doc_id, pass_token=…)`. In a package's judge step, when triage is done
   and there is time, confirm the dates the package's files will be named by:
   `list_quarter_state(quarter=<the quarter>, dates_unread=true, pass_token=…)` lists the
   quarter's pairings whose document's date was never read on it (paged the same way);
   for each, `read_document(doc_id)` of its `current.document`, read the printed issue
   date, and `update_document_metadata(doc_id, document_date=<that date>, pass_token=…)`
   — the same date when the filed one was right. What time does not reach, the next
   judgment does; the package says how many it names by an unread date. The auto-match bar:
   - the payment is booked, on the bound account, and expects a document kind;
   - the document is filed, is that kind, and reads as that kind (an invoice, not a quotation
     or order confirmation; a credit note only for a credit-note expectation; for a CRDT, a
     sales invoice the business issued);
   - the gross amount and currency are exactly equal — or the document is in another
     currency and prints the payment's exact amount in the payment's currency (the amount
     charged, or a total at a printed rate that gives exactly that amount);
   - the document date is within the vendor's window (default 10 days) of the booking date.

   Where several fit, pick the best (payment reference or invoice number first, then the
   closest date) and say so with labels:
   - `guessed` — chose among several; name the runners-up in `runners_up`;
   - `no-ref` — repeating equal charges with no number on both sides;
   - `partial-search` — the search was cut short or a fetch failed;
   - `recipient?` — the document does not name the business in the right role (the recipient
     of a purchase invoice or a vendor credit note; the issuer of a sales invoice or the
     business's own credit note).

   A document in another currency (a USD invoice for a EUR card charge) that is the
   vendor's, of the expected kind and dated within the vendor's window, but prints no amount
   in the payment's currency: `propose_match`, with a `rationale` naming both amounts — the
   operator confirms it. Where several fit, the closest date, the others as `runners_up`
   with `guessed`. Never leave such a document unpaired: the package would list its
   payment as missing — unless the write is refused because the bank's own rate rules
   its amount out ("cannot be the … payment"): then it is not this payment's.

   A pairing the operator rejected is refused while neither the payment nor the document
   has changed ("the operator rejected this pairing"): leave it, and never propose it
   again in other words.

   A pairing needs the document's amount: when the filed reading has none, read the total
   and currency on the document and `update_document_metadata(doc_id, amount_minor=…,
   currency=…, pass_token=…)` first (a pairing without one is refused; the amount names the
   file in the package).

   Only when two candidates are indistinguishable, `propose_match` instead. Pass the item's
   `row_digest` from `list_quarter_state` as `row_digest` (never build one yourself: it
   binds the match to the exact bank facts the item showed), and the item's `revision` as
   `expected_revision`. If the write is refused as changed, re-read that payment with
   `list_quarter_state(pid=…, pass_token=…)` and judge it again from its `item`. If the
   payment has unresolved candidates, pass all its `candidate_ids` in `resolves`. `record_match(pid, doc_id, author="auto", …)` otherwise. A
   document that later competes with an accepted pairing: `relabel_match(…, labels=["guessed"],
   runners_up=[…])` — never replace the pairing yourself. When a new payment and its document
   cannot be told apart from an already-paired payment and its document (same vendor, same
   amount, same dates) and that pairing was made by the machine (never one the operator
   confirmed), propose both: `propose_match` for the new payment, and `propose_match`
   again on the paired payment with its own document (its `row_digest` and `revision` from
   `list_quarter_state(pid=…)`), which turns that pairing back
   into a proposal the operator is shown.

   A vendor whose documents turn out to be a kind the mapping did not predict (its payments
   want invoices, but it only ever issues receipts, say) stays missing until the expectation
   matches it: `set_expectation(scope_type="counterparty", scope=<the vendor>, kind=<the kind
   its documents are>, tier=…, author="specialist", pass_token=…)`, then judge again. Only
   the counterparty, only for that reason; a class-level expectation and anything the
   operator says ("no invoices ever for X") are Ellen's, through `apply_reply`.
7. **Identity and portals.** A payee you cannot identify: `record_search(pid, pass_token,
   identity_unknown=true)`. A vendor whose invoices live behind a login: research the deepest
   link to their invoice list once with WebSearch, then `upsert_counterparty(name,
   patterns=[bank text], source="portal", document_link=…, link_note="found <where>, <date>",
   pass_token=…)`.

   Then, if the sweep of step 5 had reached `remaining_in_cycle` 0, run it once more: it
   lists exactly the payments steps 6 and 7 changed since their read (a new pairing, a
   portal), and its writes put their tags and notes on the bank ledger in this pass rather
   than the next. Same procedure, same `snapshot_id` rule.
8. **Finish and reply briefly.** `record_step(pass_token, step=…, action="finish",
   remaining_in_cycle=…, triage_remaining=…)` (with `stopped=<the refusal>` only if a
   refusal stopped you — never for running out of time; a judge step's finish without
   `triage_remaining` is refused: it is how the check knows your triage saw every page), then a
   short reply whose first line is `quarterly-accounting: <step> finished` — what was
   matched, proposed or left missing, for Ellen, never for the operator. Ellen's Gmail round
   is built from the store, not from your reply: a search idea for a vendor ("their
   invoices come from billing@, subject 'Your receipt'"; a CRDT's is our own sales invoice,
   in Sent) goes into `upsert_counterparty(name, search_hint=…, pass_token=…)`.

## Packaging (only when the operator asks)

"Build Q3 and send it", "send me Q3", "give me Q3", "rebuild it" — every ask for a
package is for a fresh one: the bank read again, the quarter's documents searched for in
Gmail and judged, then built and sent. The previous build goes only on the words "send me
the last package you built" (`send last`, above). The package arrives when the check is
done, however many rounds that takes; say nothing in between.

1. **Ask.** `begin_pass(trigger="package", quarter=<the quarter>, channel="telegram"|"email",
   reply="telegram")` yourself — FIRST, before `continue_pass`: the ask is recorded whatever
   happens next, and you hold the package pass, begin to end, exactly as in the cron flow.
   It answers:
   - `already` or `queued`: send its `text`, then `continue_pass()` and do any earlier
     continuation it returns (rule 1). The package follows on its own.
   - `started`: say
     "Checking the bank and your email for <quarter> — the package follows when that's done."
     Then `continue_pass()` (it can only find other work: do it first,
     then come back) and go on with step 2, with this pass's token.
2. **A round of the check.** Every round, the first one and each one `continue_pass`
   starts later (its `next` is `snapshot`, its `reply` silent: say nothing of your own), is
   one package pass you hold, begin to end. A round spans several of your turns, and each
   turn ends at a delegation: both of a round's delegations are `mode="async"` — they
   answer `status: pending` at once, and their notification starts your next turn. Never
   carry on to the next step in the same turn.
   - `record_step(pass_token, step="snapshot", action="start")`, then delegate
     "quarterly-accounting package snapshot" to the specialist, `mode="async"`, with context
     `pass_token=<token>, step=snapshot` and the quarter: the probes of its pass, step 1 (stop if `can_run` is
     false), the snapshot of step 3, the ends of step 4, then the sweep of step 5 for that
     quarter only — `list_projections(pass_token, quarter=<the quarter>)`, every row it
     lists read and recorded, exactly as in the pass. It never calls `begin_pass` or
     `end_pass` here; it finishes its step and returns. It answers `status: pending`:
     `record_step(…, action="delegated", delegation_id=…)` (rule 2) and end the turn (the
     first round has already said its line; later rounds say nothing).
   - At a continuation whose `next` is `gmail-round`: the Gmail round of the pass, step 4,
     on the continuation's `work` — a chunk of at most 6 of the quarter's items not yet
     searched for this package (3 at the snapshot's continuation). Work it exactly as the
     pass's step 4 says: the probe; the filing at the snapshot's continuation only (its
     `step` is `snapshot`; at most 8 files, skipping its `filed_refs`), not at later
     chunks; then ONE ITEM AT A TIME, each recorded before the next, within the per-item
     limits, recording only what you ran in this turn; then `more_work` as there, until it
     hands out none. Then, always,
     `record_step(pass_token, step="judge", action="start", report={checked,
     total, not_searched})` and delegate "judge the newly filed documents and the payments
     triage did not reach, for <quarter> only", `mode="async"`, with context
     `pass_token=<token>, step=judge` and the quarter (the specialist's steps 6 and 7, its
     triage listing only that quarter); `record_step(…, action="delegated", …)` and end the
     turn.
   - At the judge's continuation, `next` is `gmail-round` again (the next chunk: as above,
     with ITS token and work) or `end-pass`.
   - At the continuation whose `next` is `end-pass` (or when the snapshot stopped or failed):
     then `end_pass(pass_token, outcome, report)` yourself, by the outcome rule above —
     always, whatever came back. A round that read the bank is not ended without its judge
     step: if `end_pass` says so, start the judge step as above. The server decides from what the round did. Its answer carries `package_token` and `next`: `build` when the check is done (step 3); `next: null` with
     `more: true` when the check needs another round — call `continue_pass()` (rule 8) and
     do the round it starts; or a `speak` when the package cannot be built (the bank could
     not be read, or a refusal stopped it) — send it, mark it delivered, write no line of
     your own. The check is done when every item of the quarter was searched for this
     package (or the Gmail probe failed) and judged; a round that gets no further than the
     one before ships the package with a caption that says so. A payment the export no
     longer carried ships unclassified with its documents set aside, and the caption says
     how many — send it as it is; the operator can say "go and check now", then rebuild.
3. `build_quarterly_package(quarter, package_token)`. For Telegram:
   `stage_for_delivery(channel="telegram", package_id=…, package_token=…)`, then
   `send_media(path, kind="zip", filename=<the returned filename>, caption=…)` with the
   caption `build_quarterly_package` returned — the staged path's own name is random and
   never shown — then `record_delivery(delivery_id, outcome, package_token=…)`. A timeout is
   `uncertain`: do not send again unless the operator asks ("send it again", above).
   `record_delivery` then returns `speak`, the line that tells the operator the package may
   not have arrived (or, for `failed`, that it didn't go out): send its text verbatim and
   call `mark_rendering_delivered` with its `render_id` — that is what "send it again" binds
   to. The same holds for a resend that times out, and for an email recorded `uncertain`.
   If the build, or the first `stage_for_delivery` of a package, is refused because the bank
   was re-read since the check, nothing was kept or staged and the package's check runs
   again: call `continue_pass()` and do the round it starts (step 2) — never build again
   with the old token. A resend ("send it again") is the exact file already sent and is
   never refused for this.
   A bank check that lands after a package's first send was staged but before it went out
   takes that send back: the staged file is removed, so `send_media` or `send_email` fails
   because the file is gone, or `record_delivery` answers that the bank was re-read before it
   was sent. Either way nothing was delivered and the package's check runs again: call
   `continue_pass()` (rule 8) — never in your own words. A send
   already under way at the moment of the check cannot be stopped; if it arrives, the "a
   delivered quarter changed" alert covers it.
4. "Email me the Q3 package": the ask of step 1 with `channel="email"`, then, at step 3,
   `stage_for_delivery(channel="email", package_id=…, package_token=…)`, then gmail's
   `send_email` to the operator's own address with the
   returned path attached and the returned `request_id`. Casa asks the operator for one tap
   showing the recipient. Then `record_delivery(delivery_id, outcome, message_id=…,
   package_token=…)` — `delivered` only with the returned message id, otherwise
   `uncertain`. Never email anyone else. One request is sent once, by the channel it
   was last asked for: "email it to me" after a Telegram delivery is a new request — step 1
   again with `channel="email"`. A refusal that the package "was already sent" (or
   stopped, or taken back) is said to the operator as it is, never stopped on in silence.
5. When the send is recorded, or when a continuation's `next` is null: if the answer says
   `more: true`, `continue_pass()` (rule 8).

A continuation of a package request (`continue_pass` returned a `package_token`): `build`
and `stage` resume step 3 at that point, with ITS token. A continuation whose `next` is
null (a package request's, or a staged send's `delivery_id`) carries a `speak`: send it
verbatim, then `mark_rendering_delivered` — a stopped package, a failed recovery, a send
taken back because nobody finished sending it, a revoked send. Never send the file again yourself, and never write a failure
line of your own.

## Install (once)

One sentence to the configurator: install `casa-plugin-quarterly-accounting` for Ellen and
the finance specialist. One trigger on Ellen, exactly:

```
name:     quarterly_accounting_pass
type:     cron        schedule: 0 9 * * 1        channel: telegram
prompt:   Run the quarterly-accounting background pass. It covers every
          open item, not just the current quarter. If it reports
          something that needs me, send me that
          and nothing else; then output the sentinel `<silent/>`. If it
          reports nothing, output `<silent/>` and nothing else.
```

No other trigger: packages are built only when asked.

## Test install and reset (production debugging)

Quiesce first: no pass running, `/new` on both agents. Then:
1. Ask the finance specialist to restore the install backup `list_backups` shows registered
   for acct@<this version> (`check_setup` shows the same list in its ledger probe, under
   `registered`) with `restore_backup`; Casa asks the operator for one tap. If older acct@
   versions are registered too, stop and ask the operator which to restore — never pick a
   backup otherwise.
2. `reset_store()` — it takes no arguments; Casa asks the operator for one tap. It may be
   refused while another session holds the documents lock (filing or erasing), or answer
   `incomplete` while another session still reads the store: nothing is lost,
   try again later.
3. Upgrade the plugin if the fix needs it, or just run the pass. Its first write mints the
   new install backup.

A pass refuses every bank-feed write while `check_setup` says so, and says why.
