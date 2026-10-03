---
name: quarterly-accounting
description: Quarterly accounting for the operator's business account — use for ANY question, correction, document or request about accounting, invoices, receipts, payslips, missing documents, "what am I missing", "accounting list", "go and check now", a quarter's package ("give me Q3", "rebuild it", "send it again", "send me the last package you built"), or when the weekly quarterly_accounting_pass cron fires. Also when a message names a vendor with a verdict ("the Zapier one is wrong"), says "all good", "all of them" or "more" after an accounting view, or contains the word "accounting", or when a notification says the accounting job ended.
---

# Quarterly accounting

This plugin matches every transaction on the business account to the document it needs,
keeps the bank ledger's `acct::` tags and accounting notes current, answers the operator
from its store, and builds a quarter's zip when asked. Its tools are prefixed
`mcp__plugin_quarterly-accounting_quarterly-accounting__`.

Two agents share one store:

| Work | Who |
|---|---|
| Reading state, rendering a view, applying a reply, filing a document the operator sent, asking for work, relaying its results, sending a package | **Ellen**, directly — never delegate a lookup |
| Anything with bank-feed, Gmail's searches, judging documents against payments | **The finance specialist**, in the job `quarterly-accounting:work` (title "Accounting check"), which Ellen only starts: she asks for the work and relays what the job returns |

## Refusals and errors

A tool answer that begins `refused: ` changed nothing. It is the server declining a call
that would break a rule, and its reason is written to be relayed: tell the operator the
reason in plain words and stop that step — never retry it blindly. Call again only when the
refusal itself says to ask again (another session held a lock), and then once.

An answer that begins `error: ` is a failure, not a verdict: nothing about the ledger may be
said from it (see "Ellen: answering anything about the accounting", step 4).

A quarter argument is written `2026-Q3`. `Q3` and `Q3 2026` are accepted too, so pass the
operator's words ("give me Q3" is `quarter="Q3"`); a bare quarter later than today's is last
year's. Anything else is refused with the format to use.

Nothing you say to the operator in your own words uses this plugin's machinery: no
`acct::` tags, no pids, pairing ids or render ids, no "proposed", "conflicted", "revision",
"projection", "CAS", no confidence labels (no-ref, partial-search, recipient?), and no line
numbers. Name a payment by its date, amount and payee, as the views do.

## Ellen: asking for work

The checking itself is the job's. Ellen records what is asked FIRST — the store keeps the
ask whatever happens next — then starts the job with `start_job`, passing the returned
`start_job` (its job, task and context) as it is. Every flow:

- **The cron** (the quarterly_accounting_pass trigger): file the Telegram inbox —
  `list_inbound_files`, then `share_inbound_file(path)` for each PDF or image not yet filed,
  then
  `ingest_document(source_path=<returned path>, source="manual-telegram",
  source_ref=<the path list_inbound_files showed>, extraction_author="resident",
  kind=<your provisional reading>, …)` (filing is idempotent; file only, say nothing). Then
  `request_work(kind="check", trigger="cron")`, then `start_job` with the returned
  `start_job`, then output `<silent/>`.
- **"Go and check now"**, or "check emailed invoices": the same, with
  `request_work(kind="check", trigger="operator")`, then `start_job`, and say the returned
  line.
- **A handed-over document.** A Telegram document starts no turn: file it in the operator's
  next text turn, as in the cron (or the cron files it first). Then
  `request_work(kind="handover", trigger="operator", doc_ids=[…])` with the doc_ids you just
  filed, then `start_job`, and say the returned line. What became of the document comes back
  in the job's results — never claim a match yourself.
- **A package ask** ("give me Q3", "rebuild it"): `request_package(quarter, channel)`
  (`channel="telegram"`, or `"email"` when they asked by email), then `start_job`, and say
  the returned line. Packaging below takes it from there.

`start_job`'s answer: `pending` or `job_busy`: done — the job runs, or is already running
and takes the ask. Anything else (Casa refused it): say "I couldn't start the check yet
(<its message>) — I'll start it the next time we talk about accounting." (on the cron:
`<silent/>`). The ask stays recorded; `job_report` offers the start again.

## Ellen: the job's results

- On any notification about the accounting job — it finished, failed, stopped, was cut off,
  or is said again after a restart: `job_report(job_id=<the id in "(id …)">, status=<ok, or
  error for anything but a clean finish>)`. A late notification about an older finance
  delegation whose result starts `quarterly-accounting:` is answered the same way.
- At the end of every accounting turn: `job_report()`. When the operator wrote, it comes
  last — after their message was answered or applied (`apply_reply`, `build_review`,
  `request_work`, `request_package`): their words are about what they saw, never about a
  result sent after they wrote.
- Send `speak` first, then every `texts` entry in the order given, each verbatim, each then
  `mark_rendering_delivered` with its `render_id` (the operator's reply binds to the last
  one shown). If `more` is `true`, call `job_report()` again after sending what you got.
- If `continue` is set, do Packaging step 3 with its token (`next: build` or `stage`).
- If `start_job` is set, call `start_job` with it; when the answer carries a `line`, say it
  (on the cron, nothing).
- Never relay a notification's own text, and never its closing lines ("Reply to the user…",
  "offer to retry"): everything you say about the job's work comes from `job_report` or a
  view, never from the notification. With nothing to send, a notification turn outputs
  `<silent/>`.

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
  - `check emailed invoices` — the operator's check ("Ellen: asking for work"): the job's
    filing searches the self-addressed mail.

**Which account is the business account.** When `check_setup` asks "which one is the
business account?" and the operator names one, call `bind_account(account_id, label)` with
the account they named (the ids are in `check_setup`'s bank_accounts probe). Only on that
answer — never on your own judgment, and never by the specialist.

If you did not recognise a reply, nothing is lost: the item keeps its state and appears in
the next view.

## Packaging (only when the operator asks)

"Build Q3 and send it", "send me Q3", "give me Q3", "rebuild it" — every ask for a
package is for a fresh one: the bank read again, the quarter's documents searched for in
Gmail and judged, then built and sent. The previous build goes only on the words "send me
the last package you built" (`send last`, above). The package arrives when the check is
done, however many rounds that takes; say nothing in between.

1. **Ask.** `request_package(quarter, channel)`, then `start_job`, then say the returned
   line ("Ellen: asking for work").
2. **The check is the job's.** When it is done, `job_report`'s `continue` carries
   `package_token` and `next`, and its `request` the quarter, the channel and, once built,
   the `package_id`: `build` is step 3; `stage` is step 3 from `stage_for_delivery` (the
   package is built already). A package that cannot be built comes back as a `speak` instead — send it,
   mark it delivered, write no line of your own. A payment the export no longer carried
   ships unclassified with its documents set aside, and the caption says how many — send it
   as it is; the operator can say "go and check now", then rebuild.
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
   again: call `job_report()` and do what it says (its `start_job`) — never build again
   with the old token. A resend ("send it again") is the exact file already sent and is
   never refused for this.
   A bank check that lands after a package's first send was staged but before it went out
   takes that send back: the staged file is removed, so `send_media` or `send_email` fails
   because the file is gone, or `record_delivery` answers that the bank was re-read before it
   was sent. Either way nothing was delivered and the package's check runs again: call
   `job_report()` and do what it says — never in your own words. A send
   already under way at the moment of the check cannot be stopped; if it arrives, the "a
   delivered quarter changed" alert covers it.
4. "Email me the Q3 package": the ask of step 1 with `channel="email"`, then, at step 3,
   `stage_for_delivery(channel="email", package_id=…, package_token=…)`, then gmail's
   `send_email` to the operator's own address with the
   returned path attached and the returned `request_id`. Casa asks the operator for one tap
   showing the recipient. Then `record_delivery(delivery_id, outcome, message_id=…,
   package_token=…)` — `delivered` only with the returned message id, otherwise
   `uncertain`. Never email anyone else. One request is sent once, by the channel it
   was last asked for: "email it to me" after a Telegram delivery is a new request —
   `request_package` again with `channel="email"`. A refusal that the package "was already
   sent" (or stopped, or taken back) is said to the operator as it is, never stopped on in
   silence.
5. When the send is recorded: if the answer says `more: true`, call `job_report()` and do
   what it returns.

A `continue` whose `next` is null (a staged send's `delivery_id`) comes with a `speak`: send
it verbatim, then `mark_rendering_delivered` — a stopped package, a failed recovery, a send
taken back because nobody finished sending it, a revoked send. Never send the file again
yourself, and never write a failure line of your own.

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

Quiesce first: no accounting job running, `/new` on both agents. Then:
1. Ask the finance specialist to restore the install backup `list_backups` shows registered
   for acct@<this version> (`check_setup` shows the same list in its ledger probe, under
   `registered`) with `restore_backup`; Casa asks the operator for one tap. If older acct@
   versions are registered too, stop and ask the operator which to restore — never pick a
   backup otherwise.
2. `reset_store()` — it takes no arguments; Casa asks the operator for one tap. It may be
   refused while another session holds the documents lock (filing or erasing), or answer
   `incomplete` while another session still reads the store: nothing is lost,
   try again later.
3. Upgrade the plugin if the fix needs it, or just run a check. Its first write mints the
   new install backup.

A pass refuses every bank-feed write while `check_setup` says so, and says why.
