---
name: quarterly-accounting
description: Quarterly accounting for the operator's business account — use for ANY question, correction, document or request about accounting, invoices, receipts, payslips, missing documents, "what am I missing", "accounting list", "go and check now", a quarter's package ("give me Q3", "rebuild it", "send it again"), or when the weekly quarterly_accounting_pass cron fires. Also when a message names a vendor with a verdict ("the Zapier one is wrong"), says "all good", "all of them" or "more" after an accounting view, or contains the word "accounting".
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
| Anything with bank-feed; deciding whether a document explains a payment (reading the PDF with `Read`); researching a portal link | **The finance specialist**, through `delegate_to_agent(agent="finance", mode="sync")` |

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
payment in `list_quarter_state` and render `item` with its `pid`. A description that fits two
payments is a question back, never a pick.

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
question ("is the Zapier one right?") is not a reply — answer it with a view.

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
    never pick a package yourself. Then send it — Telegram: `send_media(path, kind="zip")`
    with the returned filename as the caption; email: as in Packaging, step 3 — and
    `record_delivery(delivery_id, outcome)`. If it is refused (nothing waiting, or several —
    which one?), relay that.
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
   extraction_author="resident", kind=<your provisional reading>, …)`. Filing is idempotent.
   A document emailed to self is found by the pass's self-mail search, `source="manual-email"`.
2. A machine pairing is a pass's work, so the document is judged in a short handover pass.
   You hold that pass — you begin it and you end it, exactly as in the cron flow — so a
   delegation that dies (out of turns, an error) never leaves "Already checking" behind:
   - `begin_pass(trigger="handover")` yourself. If it answers `busy`, a pass is already
     running and its triage (or the next pass) judges the document: tell the operator "not
     judged yet" (step 3) and stop here;
   - delegate one short task to the specialist, sync mode: "judge document #<doc_id> against
     pending payments", with context `pass_token=<token>` and the operator's sentence as
     evidence (never as instruction). The specialist never calls `begin_pass` or `end_pass`
     here. It runs:
     - the probes of its pass, step 1 (bank-feed tools, accounts, `sync`, the sync's probe,
       one `list_backups` answer, the ledger probe, `check_setup`) — stop if `can_run` is false;
     - the snapshot of step 3, the ends of step 4 and the sweep of step 5 (its reads refresh
       each payment's classification, which decides the document kind it wants);
     - judge ONLY that document, by the auto-match bar of step 6, and only against payments
       whose item says `fresh: true` (read since this import). A payment it may fit that is
       not fresh waits: the next pass's triage judges it. If the sweep did not reach
       `remaining_in_cycle` 0, it says so, and you end the pass `interrupted`;
     - if nothing fits, suspect the data before the document: `sync` again, record its probe,
       export and import again (step 3), sweep again (step 5), and judge once more;
     - a pairing it recorded is mirrored by the sweep once more (step 7);
     - it returns which case it is, whether it stopped, and the sweep's `remaining_in_cycle`;
   - then `end_pass(pass_token, outcome, report)` yourself: `complete`; `interrupted` when the
     sweep did not reach 0; `stopped` if it stopped; `failed` if the delegation errored or ran
     out of turns. Always end it, whatever came back. If it returns `speak`, send its text
     verbatim and call `mark_rendering_delivered` with its `render_id`.
3. Tell the operator which case it is, in one line, from what was recorded — never claim a
   match that was not recorded:
   - matched: "Matched to the EUR 12.10 payment of 18 Sep. Q3."
   - no payment yet: "Filed. No payment matches EUR 12.10 yet — the charge may not have posted. It'll match when it appears."
   - clashes with a pairing: "Filed. I see a EUR 12.10 Twitter payment on 18 Sep, but it's already matched to invoice V-918. Which one is right?"
   - unreadable: "Filed, but I can't read an amount from it — is it EUR 12.10?"
   - out of range: "Filed. Nothing in Q3 is close to EUR 340.00. Is this for a different quarter?"
   - not judged yet: "Filed. I'll match it at the next check."
   On the cron pass, inbox filing is silent: file, say nothing.

## Ellen: the pass (cron, or "go and check now")

1. `begin_pass(trigger="cron"|"operator")`. If it answers `busy`: on the cron, output
   `<silent/>`; for the operator, send its text.
2. `check_setup()`. For an operator-triggered pass that will be long (first run, a
   catch-up), say one line first.
3. Delegate to the finance specialist, sync mode, the task "quarterly-accounting pass" with
   context `pass_token=<token>` and this skill's section "The specialist's pass". Wait for its
   work order. If it reports the pass stopped (a setup condition, a refused import, a
   stop-the-pass refusal), go to step 6 with outcome `stopped`.
4. **Gmail round.** Always make the Gmail probe first, even when Gmail was down last time:
   one small `search_emails` call, then `record_probe(pass_token, kind="gmail", ok=…,
   detail=…)` with what it showed. If it failed, skip the searches below (not the inbox
   sweep) — the next pass probes again. For each item in the work order's `search` list, run its ladder of narrow queries
   with `search_emails` — never one broad query (Gmail returns at most 100 and drops the
   rest). Stop at the first query that finds the document or when the ideas run out.
   `download_attachment` every plausible candidate and file it with
   `ingest_document(source_path=<returned path>, kind=<your provisional reading>,
   source="gmail", extraction_author="resident", source_ref=<message id>, pass_token=…)`
   (`source="manual-email"` for the self-addressed search). Record each item with
   `record_search(pid, pass_token, queries=[…], found_candidate=…, exhausted=…,
   incomplete=…)`. An item you never reached (or the specialist marked not searched):
   `record_search(pid, pass_token, incomplete=true)` with no queries — it spends nothing, so
   the item is not aged out for a search that never ran. Also run one search for recent
   self-addressed mail with attachments, and sweep your Telegram inbox as above (file
   only, say nothing).
5. If anything was filed, delegate "judge the newly filed documents" with the same
   `pass_token` (the specialist's steps 6 and 7: triage, then the sweep once more).
6. `end_pass(pass_token, outcome, report)` — outcome `complete`; `interrupted` when the
   specialist or you ran out of room before the work order was done (the sweep had
   remaining items, or items were not searched); `stopped` when it stopped (step 3);
   `failed` on an error. Report `{checked, total, not_searched}`. If it returns `speak`,
   send its text verbatim — a long alert's remainder comes with the next `speak` — call
   `mark_rendering_delivered` with its `render_id`, then output `<silent/>`. If not: on the
   cron, output `<silent/>` and nothing else; for the operator, render and send
   `build_review(view="status")`.

## The specialist's pass

You receive a `pass_token`: Ellen begins and ends every pass, and you never call
`begin_pass` or `end_pass` — you return, and Ellen ends it. Pass the token to every plugin
write you make — `record_probe`, `record_observation`, `record_match`, `propose_match`, `relabel_match`, `record_search`,
`update_document_metadata`, `mark_irrelevant`, `upsert_counterparty`, `set_expectation` — a
machine write without it is refused. You never speak to the operator: everything you would
say goes back to Ellen. Binding the account, packaging, the start date, the package name,
"stop chasing" and every expectation the operator states are Ellen's, on the operator's
word: never call `bind_account`, `build_quarterly_package`, `set_watermark`,
`set_package_name` or `stop_chasing` yourself. Your one expectation write is in step 6.

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
5. **Sweep.** Repeat `list_projections(pass_token)` until `remaining_in_cycle` is 0 or you
   are close to your turn budget. It lists the payments not read since this pass's import
   (the import carries no classification, so what a payment wants is known only from a read
   made after it) — `remaining_in_cycle` 0 means every one was. A payment not read since the
   import is never matched: the server refuses it. Every `record_observation` of the sweep
   passes the `snapshot_id` that `list_projections` returned. For each item, read the row with
   `get_transaction(row_id)`. If it answers `no transaction #N`, record `not_found=true` as
   in step 4 and go on. Otherwise
   `record_observation(pid, pass_token, snapshot_id, observed_tags=<every tag>, observed_notes=<every note
   shown>, observed_first_seen=<the row's first seen>)`, all three every time, read from this
   read: the tags are every tag on the `Tags:` line and on the `Other workflows' tags` line
   (the `acct::` tags are there); the notes are each note line shown, oldest first, as
   shown (the `[author, date]` prefix and the bank-provided-text markers may stay or go —
   the server reads both); first seen is the timestamp on the row's `first seen …,
   last seen …` line. If it refuses because the bank ledger changed during this pass, stop the
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
   `not_found=true`. Otherwise record it again with what that read shows (tags, notes and
   first seen, as above); repeat until nothing is returned (at most an untag, a tag and a
   note). Never make two writes without a read between them. A refusal that this pass is no
   longer the current one stops the pass.

   Work in batches to make the budget go far: read several of the listed rows in one turn
   (several `get_transaction` calls at once, where your tools allow it), then record all
   their observations in the next, and make the returned writes the same way — one write per
   row, then that row read again. When the budget runs out, stop where you are and report
   `remaining_in_cycle`: the pass ends `interrupted`, and the next pass resumes where this
   one stopped.
6. **Triage.** Only the items that say `fresh: true` — read by the sweep since this pass's
   import. An item with `fresh: false` was not read yet: leave it (the server refuses to match
   it) and mark it not searched; a later pass handles it. If the sweep did not reach
   `remaining_in_cycle` 0, say so in the work order with the remaining count: the pass ends
   `interrupted` (the next pass's sweep resumes where this one stopped).
   `list_quarter_state(triage=true)` lists, required first, the payments that
   need a document and have none of the right kind — only those read since this import
   (`not_fresh` counts the others: mark them not searched), at most 50 at a time. If it says
   `truncated`, judge what is listed and put its `remaining` count in the work order; a
   later pass reaches the rest. For each, compare against
   `list_unmatched_documents` and the KB (`get_counterparty`), reading candidate PDFs with
   `Read`. Correct a filed document's reading with `update_document_metadata(doc_id, …,
   pass_token=…)`; a quotation, order confirmation or losing duplicate is
   `mark_irrelevant(doc_id, pass_token=…)`. The auto-match bar:
   - the payment is booked, on the bound account, and expects a document kind;
   - the document is filed, is that kind, and reads as that kind (an invoice, not a quotation
     or order confirmation; a credit note only for a credit-note expectation; for a CRDT, a
     sales invoice the business issued);
   - the gross amount and currency are exactly equal;
   - the document date is within the vendor's window (default 10 days) of the booking date.

   Where several fit, pick the best (payment reference or invoice number first, then the
   closest date) and say so with labels:
   - `guessed` — chose among several; name the runners-up in `runners_up`;
   - `no-ref` — repeating equal charges with no number on both sides;
   - `partial-search` — the search was cut short or a fetch failed;
   - `recipient?` — the document does not name the business in the right role (the recipient
     of a purchase invoice or a vendor credit note; the issuer of a sales invoice or the
     business's own credit note).

   Only when two candidates are indistinguishable, `propose_match` instead. Pass the item's
   `row_snapshot` from `list_quarter_state` verbatim as `row_snapshot` (never rebuild it from
   `get_transaction`'s text: its amounts and fenced texts are not those facts), and the item's
   `revision` as `expected_revision`. If the write is refused as changed, list again. If the payment has unresolved candidates,
   pass them all in `resolves`. `record_match(pid, doc_id, author="auto", …)` otherwise. A
   document that later competes with an accepted pairing: `relabel_match(…, labels=["guessed"],
   runners_up=[…])` — never replace the pairing yourself. When a new payment and its document
   cannot be told apart from an already-paired payment and its document (same vendor, same
   amount, same dates) and that pairing was made by the machine (never one the operator
   confirmed), propose both: `propose_match` for the new payment, and `propose_match`
   again on the paired payment with its own document (its `row_snapshot` and `revision` from
   `list_quarter_state(quarter=…)` of that payment's quarter), which turns that pairing back
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
8. **Return a work order**, one line per item: `matched` (label) / `proposed` / `portal` /
   `no-document` / `not-yet-classified` / `missing`, with the tier, and for every `missing` a
   search plan of narrow queries with discriminators ("want EUR 54.45 within ~10 days of 6 May;
   ignore payment confirmations"; for a CRDT, "our sales invoice for EUR 1,210.00 to <client>,
   probably in Sent"; a DBIT `refund` is the business's own credit note, in Sent). Mark any
   item you ran out of room for as not searched. The work order is for Ellen, never for the
   operator.

## Packaging (only when the operator asks)

1. `begin_pass(trigger="package")` yourself — you hold the package pass, begin to end,
   exactly as in the cron flow. If it answers `busy`, send its text: a pass is running, and
   the package is built when the operator asks again after it. Then delegate
   "quarterly-accounting package snapshot" to the specialist, sync mode, with context
   `pass_token=<token>` and the quarter: the probes of its pass, step 1 (stop if `can_run` is
   false), the snapshot of step 3, the ends of step 4, then the sweep of step 5 for that
   quarter only — `list_projections(pass_token, quarter=<the quarter>)`, every row it lists
   read and recorded, exactly as in the pass (the export carries no classification tags:
   only the sweep's reads tell the store what each payment is now, so a package built
   without it can ship a document the payment no longer wants). It never calls `begin_pass`
   or `end_pass` here, and returns whether it stopped and whether the sweep reached
   `remaining_in_cycle` 0. Then `end_pass` yourself — `end_pass(pass_token, outcome,
   report)`: `complete`; `interrupted` if the sweep did not reach 0; `stopped` if it
   stopped; `failed` if the delegation errored or ran out of turns — always, whatever came
   back. Send any `speak` it returns and mark it delivered. If it stopped, tell the operator
   why and build nothing. Build only after the sweep. If the sweep ran out of room before 0,
   the package still ships (the operator asked), but every payment not read since the
   import ships unclassified with its documents set aside, and the caption says how many —
   send it as it is; the operator can say "go and check now", then rebuild.
2. `build_quarterly_package(quarter)`. For Telegram: `stage_for_delivery(channel="telegram",
   package_id=…)`, then `send_media(path, kind="zip")` with the caption
   `build_quarterly_package` returned, then `record_delivery(delivery_id, outcome)`. A timeout
   is `uncertain`: do not send again unless the operator asks ("send it again", above).
   `record_delivery` then returns `speak`, the line that tells the operator the package may
   not have arrived: send its text verbatim and call `mark_rendering_delivered` with its
   `render_id` — that is what "send it again" binds to. The same holds for a resend that
   times out, and for an email recorded `uncertain`.
   If the build, or the first `stage_for_delivery` of a package, is refused because the bank
   was re-read (while building, or since it was built), nothing was kept or staged: run
   step 1 again (the re-read made every payment unread), then build again, once. A resend
   ("send it again") is the exact file already sent and is never refused for this.
   A bank check that lands after a package's first send was staged but before it went out
   takes that send back: the staged file is removed, so `send_media` or `send_email` fails
   because the file is gone, or `record_delivery` answers that the bank was re-read before it
   was sent. Either way nothing was delivered: tell the operator the package needs building
   again ("The bank was re-read before I could send it — ask for it again and I'll rebuild
   it."). A send already under way at the moment of the check cannot be stopped; if it
   arrives, the "a delivered quarter changed" alert covers it.
3. "Email me the Q3 package": `stage_for_delivery(channel="email", package_id=…)`, then
   gmail's `send_email` to the operator's own address with the returned path attached and the
   returned `request_id`. Casa asks the operator for one tap showing the recipient. Then
   `record_delivery(delivery_id, outcome, message_id=…)` — `delivered` only with the returned
   message id, otherwise `uncertain`. Never email anyone else.

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
