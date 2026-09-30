# casa-plugin-quarterly-accounting — design

Status: draft for operator review · 2026-08-10
Review status: **converged 2026-09-22 at rounds 10–22** — Astra (`gpt-6-astra`, medium) and
Terra (`gpt-5.6-terra`, medium) both **SHIP** at the round-22 tree, nothing at S1/S2, on
harnesses that reproduce against bank-feed 0.10.1, Casa 0.326.0–0.328.0 and gmail 0.9.0
source. Each round's findings and fixes are in that round's commit message (`git log`).
All three judgment calls made during convergence are now ruled. **Ruling 1 (operator,
2026-09-22) reshaped the scope**: every transaction on the account is managed, both
directions; "open" means "a required supporting document is missing", not "an invoice";
what document a transaction needs follows from tx-classifier's classification through a
stored expectation mapping — this plugin classifies nothing (§"Document expectation"). That
change is a new mechanism and went back through review: rounds 25–30, ending with Astra
and Terra both **SHIP** at the round-30 tree (b0508f9), nothing at S1/S2. A further
addition — **test install and reset on production**, with bank-feed minting the restore
point on the first accounting write (§Setup, "Test install") — went through rounds 31–40,
ending with both **SHIP** at the round-40 tree (1a8e078); round 33 was Terra-only, Astra's
run having been cut off by OpenAI's content filter, and is recorded as such. The other two were
ruled the same day: the admission
watermark defaults to the binding quarter, and emailing the package to the operator's own
mailbox is on the free rung (the branch is restored; the recipient is checked by Casa's
one-tap approval of the exact send, and email is under the delivery log). Rounds 23–24
re-reviewed those rulings: round 24 Astra SHIP, Terra SHIP WITH FIXES on this status line
alone, corrected here without a further round.
Revised 2026-09-24 — **ended lineages**, pending review. After #39 shipped, bank-feed
0.12.0 (component 0.13.0) gave `purge` a `user_work` choice; re-checking the spec against
component 0.13.2 found that a row erased outright, and a tombstoned one, had no defined
effect on the fold, and that an expectation turning unknown would have retired every
machine pairing after a `purge(user_work=erase)`. Written in §Match records ("A lineage
can end"; "An expectation that becomes unknown"), reducer step 0, §"The projection",
§"The sweep", §Setup ("Erasures are not restores"), §Testing; the bank-feed floor moves
to 0.11.0. **Round 41** (tree d8ac37b): Astra and Terra both DO NOT SHIP, one S1 each.
Terra: ends were judged in the repair sweep, which runs before the pass's snapshot exists,
so they now commit at the import, before triage. Astra, reproduced: a date `purge` cuts
supersession chains, so an erased row did not prove the payment ended. **Operator ruling
2026-09-25:** fixed upstream as
[casa-specialist-finance#56](https://github.com/bonzanni/casa-specialist-finance/issues/56)
(lineage-closed purge), and the bank-feed floor rises to the release that closes it.
**Round 42** (tree a6c8365): Astra SHIP WITH FIXES (one S2, fixed in 0203101: an unknown
expectation keeps the last known kind verdict), Terra DO NOT SHIP (one S1: a purge
between a sync's plan and its apply). A refutation-defense run (Astra) found the
dismissal holds, since that race is the ruled returning-payment case, and the spec now
states it (792a5fa). **Converged at round 43: Astra SHIP, Terra SHIP, at 792a5fa,**
nothing at S1/S2. The #56 floor stays mandatory: the newest bank-feed is below it.
Revised 2026-09-22 — re-verified against casa **v0.328.0** and bank-feed **0.10.1**
after ha-casa-app #486, #1036, #1038, #1040 and casa-specialist-finance #30, #31 landed.
Required floors: casa **0.326.0**, bank-feed **0.20.0** (casa-specialist-finance component 0.21.0: the export carries each row's tags and `tag_revision`, #86, on top of #69's ledger instance id and `expected_ledger` in 0.15.0 and #56's lineage-closed purge in 0.13.0; §Casa baseline). Revised 2026-09-28.
Revised 2026-09-28 — **classification from the export** (issue #1, operator ruling "option A"):
the import is the classification observation, and the sweep reads only what owes bank-feed a
write, a read-back or an erasure check. Design and its review rounds D1–D2:
`docs/superpowers/specs/2026-09-28-issue-1-classification-from-export-design.md`.
Re-verified 2026-09-27 against casa **v0.328.6** and component **0.14.4**: nothing in between
changes a contract this document relies on.
Implementation plan: `docs/superpowers/plans/2026-09-27-quarterly-accounting.md`, converged at
round p10 (b93f71a). Errata D1, D4, D8 and D9 were accepted and applied 2026-09-27; see the plan's
"Decisions and errata".
Revised the same day for bank-feed 0.15.0–0.18.0 (#69 ledger instance id; the two erasers) and
Casa's uninstall eraser (`reset_store`). The plan re-converged at round p13 (ac9b598).
Revised 2026-09-20 — re-verified against casa **v0.323.0**. Both scheduled-turn
dependencies landed; the contracts they landed with (one attention lane, durable
asks, background jobs) change the weekly pass. See “Casa baseline”.
Revised 2026-09-21 after an independent design by Astra (`gpt-6-astra`, medium) and
its head-to-head comparison round: eight of its factual claims were re-verified here
against bank-feed, casa and gmail source before folding in. Its verdict was a hybrid —
this document's execution model, its acceptance and custody model — and it withdrew its
own three upstream prerequisites as non-blocking under coordination cost.

## Purpose

Automate the quarterly accounting preparation for the operator's B.V.: match every
transaction on the business bank account (already ingested by bank-feed) against its
supporting document — a vendor's invoice, the sales invoice the operator issued, a
payslip, or nothing at all when none exists — collect the documents, and deliver, whenever
the operator asks for a quarter, a single zip containing a SnelStart-ready invoice folder,
a ledger, and a notes file.
Along the way, keep bank-feed tags and notes authoritative so the bank ledger itself is
progressively annotated.

## How it works, in three sentences

**1. The work runs on its own, weekly, and says nothing.** A scheduled pass syncs the
bank feed, lets the classifier say what each new transaction is, matches transactions to
the documents they need, chases whatever is still open from earlier quarters, annotates
the operator's own ledger, and delivers no message. The only thing it ever sends unprompted is a fault that would
otherwise go unnoticed (§"When the plugin may speak first").

**2. The operator interrogates it whenever they have time.** How does the quarter stand,
what is missing, what did the machine guess at, did a particular invoice ever arrive —
and, in the same conversation, they close gaps: hand over a PDF, say who an unknown
counterparty was, correct a wrong pairing. Answers are read from the store, never
recalled (§"Ellen never invents the state of the ledger").

**3. The operator can run the pass itself, on demand.** "Go and check now" does exactly
what the cron does, at a moment of their choosing — before sitting down to do accounting,
after a burst of spending, or whenever the coverage line looks older than they like.

Everything else in this document is the detail under those three.

## Goals

- Weekly, mostly-autonomous matching of every transaction, both directions, to the
  document it needs — purchase invoices and sales invoices first, because those are what
  the VAT return turns on (operator, 2026-09-22); payslips and statements as a second
  tier. The plugin **decides and shows** rather than asking: a loose match that is visible and reversible
  beats a strict one that hands the work back (see §“The reversibility ladder”).
- **It must be faster than doing it by hand, and it must cost nothing when ignored.**
  The plugin sends nothing on a schedule; asking it for the picture and correcting one
  pairing is a message out and a message back, modelled at under a minute. A week the
  operator never thinks about costs them zero messages, which is the real bar: if the
  plugin becomes a chore it has failed, whatever its state machine guarantees.
- **Stated honestly, because the difference matters**: that budget covers reviewing the
  machine's work. It does not cover *collecting* the invoices it could not find — if
  three are missing, fetching them is still three errands, and no presentation trick
  makes twelve manual acquisitions fit in a minute. The plugin's claim is that it finds
  what it can, tells you exactly what it could not, and never makes you re-derive that
  list yourself. Deferred missing invoices are never counted as work saved.
- A standing answer to “what am I missing?”, **retrievable at the moment the operator
  chooses**, with the links in it. Not a list repeated every Monday at an hour when
  nobody can act on it (§“Push tells, pull works”).
- A zip package delivered over Telegram whenever the operator asks for a quarter:
  `invoices/` (bulk-uploadable to SnelStart), `ledger.csv`, `ledger.xlsx`, `notes.md`.
- A lean vendor knowledge base whose primary asset is the **researched invoice
  deep-link** per portal vendor — found once, at real effort, reused every quarter.
- Bank-feed stays consistent: every match decision is mirrored into bank-feed tags and
  notes, with drift detected and repaired — so filtering the operator's own ledger by
  `acct::open` answers "which transactions still lack a required document" without
  asking the plugin anything.

## Non-goals (v1)

- No vendor-portal scraping or credentialed browser automation. Portal invoices are
  link-only: the ledger carries the most precise link the agent could research.
- **No classification by this plugin.** What a transaction *is* — a software purchase, a
  salary, a client payment, an internal transfer, a tax refund — is tx-classifier's
  judgment, made in the same specialist session, and this plugin never duplicates it
  (operator, 2026-09-22). It reads the classification and derives what document, if any,
  the transaction needs (§"Document expectation"). Until a row is classified this plugin
  does not know what to look for and does not guess.
- **No sales-invoice source beyond the mailbox and the operator's hand.** A client
  payment's sales invoice is found in Gmail (the sent copy) or handed over; reading it
  straight from the operator's invoicing tool is a v2 source, not a v1 dependency.
- Incoming transactions are matched like outgoing ones — to the sales invoice they settle
  — but nothing in v1 does anything with the sales invoice beyond filing and packaging
  it. Round 10 had cut incoming rows out entirely to stop `acct::open` reading as "missing
  an invoice" on a refund; the operator's ruling fixed the meaning of the tag instead.
- No public release. Private GitHub repo; casa installs it via the authenticated
  fetch path (`GITHUB_TOKEN` through `git-credential-casa.sh`).
- No bootstrap pass over historical quarters. The KB starts empty and earns entries
  during real passes.

## Architecture

### Placement

One Casa plugin, standard layout (`.claude-plugin/plugin.json`, `.mcp.json`,
`server/`, `skills/quarterly-accounting/SKILL.md`), Python 3.11 **stdlib only**
(bank-feed's discipline; casa provisioning stays a no-op). Assigned to **both** the
resident (Ellen) and the finance specialist. Its `$CLAUDE_PLUGIN_DATA` is the shared
store and the only channel between the two agents' work.

The MCP server is deterministic custody only: storage, naming, indexing, zip
assembly, outbox staging. **No LLM judgment, no PDF parsing, no network access in the
server.** All document reading and all matching judgment happen in agents via `Read`
(both roles have it; `Read` handles PDFs natively).

**Which files an agent may `Read` (issue #6).** Having `Read` is not reaching the store:
Casa gates every `Read` with its `path_scope` hook, and neither role's readable prefixes
cover `$CLAUDE_PLUGIN_DATA` (a delegated specialist's list is empty; Casa 0.332.10), nor
can a plugin declare one (asked for as ha-casa-app#1101). So an agent reads a filed
document through `read_document(doc_id)`: the server checks the held bytes against the
hash custody is by and answers with a short, bounded text block (the filed reading,
marked as data) and the bytes as an MCP `resource` blob. Claude Code (2.1.273) shows an
image blob inline and saves any other blob under the calling session's own
`tool-results/`, naming the path in the answer; Casa lets a session `Read` its own tool
results (ha-casa-app#1082), and `Read` opens the PDF. The server still parses nothing.
`Read` refuses a PDF whose first bytes are not `%PDF-`, and a vendor's file can carry a
prefix (a UTF-8 BOM, seen in a real quarter; issue #8). So when a held PDF does not start
with `%PDF-` but the header lies wholly within its first 1024 bytes, the reading copy
starts at the header and the text block says how many bytes were dropped; the hash is
checked on the held bytes first, and the held file and the package copy keep the filed
bytes. With no header in that window, `read_document` refuses, naming the document as
unreadable, rather than send a file `Read` would reject. Finding the header is the only
look inside a file the server takes.
An XML invoice goes as `text/plain` (saved as `.txt`); HEIC and TIFF are saved as `.bin`,
which `Read` cannot show — the residual. A blob never counts against Claude Code's
answer cap: only the text block does. When ha-casa-app#1101 lands, a declared read grant
over `documents/` makes this hop optional; `read_document` stays correct either way.

### Division of labor

| Actor | Responsibilities |
|---|---|
| Casa core | Fires the weekly trigger at the resident; enforces tool gates. |
| Resident (Ellen) | Orchestrates passes; all Gmail work (targeted searches, attachment download, ingest); **all** operator conversation (the review sheet, its free-text replies, the occasional one-line question folded into the sheet); sends the zip. |
| Finance specialist | All matching judgment, grounded in its own reading of the actual PDFs (`read_document`, then `Read`); all bank-feed tagging/notes; portal-link research (WebSearch); returns structured work orders. Never talks to the operator (structurally cannot: `ask_user` requires direct execution). |
| Plugin MCP server | Document store, counterparty KB and expectation mapping, match records, quarter workbook, filename normalization, package/zip build, outbox staging. |
| Operator | Reads a short weekly message; corrects a line in free text when they disagree; pulls the collection list when they sit down to do it; supplies invoices if they feel like it; receives one zip per quarter. Every one of those is optional. |

Rationale for the split (verified against casa code, 2026-08-10):

- Scheduled triggers are **resident-only** — `triggers.yaml` is in the forbidden file
  set for specialist and executor tiers (`agent_loader.py:79-97`, re-verified v0.323.0). Ellen being the
  scheduled entry point is structural.
- A delegated specialist cannot use `ask_user`: the gate demands a direct, genuine
  inbound-DM turn (`tools.py:939-999`, re-verified at v0.323.0), and the two-turn ask
  shape cannot outlive an ephemeral delegation anyway. Operator UX therefore belongs
  to the resident.
- `send_media` and `ask_user` now reach the operator **from the cron turn itself**
  (#485, #573) — but through an eligibility rule that re-states the same split. It
  requires all of: the reserved `_scheduled_delivery` marker, stamped only by Casa's
  own time-based dispatch sites and stripped from every external ingress
  (`tools.py:405-465`, `provenance.py:66`); the telegram channel; a configured
  operator; and genuinely **direct** execution — no engagement bound, executing role
  equal to the origin's role. A delegated specialist inherits the parent origin,
  marker included, and deliberately does **not** inherit the delivery target.
- The specialist reads PDFs itself rather than receiving curated extractions:
  matching judgment ("does this document explain this transaction?") must be made by
  the agent that sees both sides at full fidelity. Ellen's ingest-time extraction is
  provisional, for filing only, never load-bearing.

### The reversibility ladder

**Operator ruling, 2026-09-21, and the spine of the design:** gates go where an action
cannot be taken back, and nowhere else. A tool that asks permission for things it could
simply undo becomes a chore, and a chore stops being used — at which point its careful
correctness properties protect nothing. Three rungs:

| Rung | What is on it | Discipline |
|---|---|---|
| **Free** | Everything v1 does: ingest, auto-match, demote, retarget, reject, re-label; bank-feed `acct::*` tags (an `untag_transaction` away from undone); notes (append-only, corrected by appending); **and the quarterly package itself** — the operator asks for a zip, checks it, corrects what is wrong and asks again (operator, 2026-09-21). A rebuild costs one message. Emailing a package or an invoice **to their own mailbox** (operator, 2026-09-22): it stays in their hands. Casa asks one approval tap per email, showing the recipient — Casa's gate, not this plugin's. | Act. No question, no confirmation, no ceremony. |
| **Gated** | Nothing, in v1. | — |

**v1 therefore has no routine gates and no routine button questions.** Every step is
either undoable in one word or cheap to redo, so a gate would only buy ceremony. Two
things would put something on the gated rung and are deliberately out of scope: sending
the package anywhere the operator cannot retract it from (mailing the accountant
directly, filing with the tax authority), and deleting retained documents. v1 deletes
documents only through `reset_store`, which is protected: Casa asks for one tap. That covers
both the test-install reset loop and Casa's uninstall "Erase everything" (operator ruling
2026-09-27; §Setup, "The uninstall eraser").

Two consequences worth stating, because they overturn v1's instincts:

- **A wrong auto-match is cheap here and the design should act like it.** It is one line
  on a sheet, one word in a reply, one tag write to undo — provided it is *visible*. So
  the effort goes into the review surface and the confidence labels, not into refusing
  to decide. The expensive failure is not a wrong match; it is a wrong match that
  nobody was shown.
- **Speed is a correctness property.** If a pass cannot beat the operator doing it by
  hand, the plugin has failed on its own terms, regardless of how sound its state
  machine is. Every proposed mechanism in this document is judged against that too.

**What the operator never has to learn.** `proposed`, `conflicted`, `matched`, CAS,
`expected_revision`, projections, acceptance revisions, content hashes, the
confidence labels, quarter identifiers in tool form. None of it appears on a sheet, in a
caption or in a receipt — and neither do line numbers, which an earlier draft invented
and this one removed. The operator's entire vocabulary is what they can already see: a
vendor name, an amount, a date, and "wrong", "good", "needs no invoice", "rebuild it",
"send it again". Packages are told apart by the date in their filename, which is the
only package-identity concept the operator ever meets. Everything else is machinery, and machinery that leaks onto the sheet
is a defect.

## Casa baseline (re-verified 2026-09-22, casa **v0.328.0**; required floor **v0.326.0**)

v1 was written against v0.2xx with two scheduled-turn enhancements outstanding. Both
landed, and each brought contracts this plugin must design against, not merely enjoy.

**The required casa floor is v0.326.0**, and every step up to it is load-bearing:
v0.324.0 for #990 (below), v0.325.0 for inbound Telegram documents (#1036), and v0.326.0
for the cross-plugin handoff folder (#486) that ingest and ledger import both go through.
An earlier revision of this section named v0.324.0 as the floor; that stopped being true
when ingest moved onto the handoff folder.

**Why v0.324.0 mattered on its own** (round-5 finding). ha-casa-app#990 —
`send_message` reporting success when the channel delivered nothing — is fixed in
**v0.324.0** (`23c44160`, "send_message reports a message that reached nobody"), one
commit after the v0.323.0 tag. The distinction is not pedantry: on v0.323.0 a fault
notification — now one of only two messages this plugin ever sends unprompted — can
vanish while the turn records success and ends silently, which is the exact failure the
notification exists to prevent.

**Both dependencies are in.** `send_media(kind="zip")` ships (#482 — a `send_document`
kind with a magic-signature head test and a **20 MB** cap, `media_policies.py:163-171`);
#565 added a `text` kind (UTF-8 `.txt/.md/.csv/…`, 5 MB) that can deliver `notes.md` on
its own. Scheduled turns may deliver media (#485) and raise button questions (#573). The
two-beat degradation v1 described is **deleted, not deprecated**.

**One attention lane, and it holds one question** (INV-JOB-008 / INV-JOB-014). A
scheduled question is admitted only into an idle lane; a refused admission answers
`operator_busy` and asks nothing. A human question — an operator `ask_user`, an
authorization or consent challenge — retires a live scheduled one once that human
question is itself delivered; one-way, never the reverse. **So a pass cannot post N
confirmation keyboards in one turn.** v1 sidesteps this entirely by not asking button
questions at all (see §“The reversibility ladder”): a review sheet is a *delivery*, not
a question, so it never touches the lane. The rule is recorded because it binds any
future version that wants a keyboard.

**A scheduled ask is a durable obligation, not a message** (INV-JOB-013 / INV-JOB-007).
The record (`/data/scheduled_asks.json`) is written before the keyboard is posted,
survives a restart with its remaining timeout, and **every** terminal outcome — answered,
expired, cancelled, or settled `operator_busy` by the boot reconciler — is delivered back
to the asking session as a machine-authored scheduled turn that reports the tap in its
content, never as its speaker. The guarantee is **at-most-once**: the crash window
between "decided" and "dispatched" may lose an outcome, never duplicate it. Three
consequences:

- v1's "ask-keyboards die on casa restart" is **wrong now** — they survive. (Moot for
  this plugin, which raises none, but it was a false platform claim.)
- An outcome may still never arrive, so every pass re-derives its work from the store
  and never waits on a continuation. The workbook already gives us that; the CAS
  revision already makes a late tap safe.
- Rewriting or removing the weekly trigger **cancels its pending asks**. A configurator
  edit to `triggers.yaml` mid-week costs the open confirmations (they decay to
  `proposed`). Acceptable, but it belongs in the install notes.

**Background jobs exist now** (#1023, v0.321.0; batch-progress fix v0.323.0). A plugin
declares a job under `casa.jobs` naming one of its own skills; any specialist whose
resolved plugin set includes that plugin can host it; `start_job` runs it as batches in
its own topic, with per-batch progress lines, operator messages between batches,
`/cancel`, and restart resume. `start_job` carries no direct-execution gate, so a cron
turn can start one. A job worker cannot ask the operator. This is a real option for the
matching pass — see “Open items”.

**Unchanged, re-verified at v0.323.0:** scheduled triggers are resident-only
(`agent_loader.py:79-97`); `CLAUDE_PLUGIN_DATA` is CLI-managed and survives a plugin
uninstall (`tools.py:12659-12692`); the outbox keeps atomic-claim semantics, destructive
consumption and a 2 h orphan reap (`plugin_outbox.py:42`); a specialist role's
`max_turns` is a `role.yaml` knob defaulting to 10 (`agent_loader.py:1215`).

**Cross-plugin files: Casa 0.326.0 floor ([#486](https://github.com/bonzanni/ha-casa-app/issues/486)).**
Casa owns a handoff folder, `/data/handoff/<producer>/<id>/<filename>` (`CASA_HANDOFF_DIR`).
A producer publishes a file and returns its path; a consumer takes it with the vendored
`casa_handoff.capture(path)`, which returns `(filename, bytes)` only for a single-link
regular file in that exact layout, so the consumer never opens the path itself. Reading
never deletes; Casa removes a file 7 days after publication. Limits: 25 MB per file, 2 GB
for the folder. When the folder is full, the next publish is refused and nothing is
evicted. gmail 0.9.0's `download_attachment` publishes there, and its `send_email`
attaches handoff files. bank-feed's `export_history` publishes its ledger export there.
Casa's `share_inbound_file` copies a file the operator sent in Telegram there. This
plugin vendors `casa_handoff.py` verbatim.

**bank-feed floor: 0.20.0** (casa-specialist-finance component 0.21.0, 2026-09-28; revised
from 0.15.0). It adds [#86](https://github.com/bonzanni/casa-specialist-finance/issues/86):
`export_history` carries each row's current tags and a `tag_revision` (equal revision ⇒
equal tag set, for one ledger instance id), read in the same snapshot as the rows, and
`get_transaction` prints the same revision. The 0.15.0 floor before it added
[#69](https://github.com/bonzanni/casa-specialist-finance/issues/69):
a ledger instance id reported by `list_backups` and `export_history`, and `expected_ledger`
on `tag_transaction`, `untag_transaction` and `add_note`, checked atomically with
`expected_generation`. The earlier floor, 0.13.0 (component 0.14.0, 2026-09-25), was the
release that closed [#56](https://github.com/bonzanni/casa-specialist-finance/issues/56):
"no surviving row's superseded_by names an erased row". This plugin is not released
against anything lower. Five fixes sit at or below the floor, each
load-bearing (re-verified 2026-09-24 against component 0.13.2):

| bank-feed | Fix | Why this plugin needs it |
|---|---|---|
| 0.8.1 | [#30](https://github.com/bonzanni/casa-specialist-finance/issues/30) — a pending row is superseded once; a stale second supersession is refused (`StalePlan`) | Otherwise overlapping syncs strand migrated annotations on a row no lineage walk reaches. |
| 0.9.0 | [#31](https://github.com/bonzanni/casa-specialist-finance/issues/31) — `owner::name` tags are another workflow's | The whole `acct::` vocabulary below depends on it. |
| **0.10.0** | `export_history` publishes into Casa's handoff folder | `import_ledger_export` takes the export only through `casa_handoff.capture` and refuses any other path. On 0.9.x the export lands in bank-feed's private data directory, so **packaging fails closed**. |
| **0.13.0** | [#56](https://github.com/bonzanni/casa-specialist-finance/issues/56) — `purge` deletes a supersession chain whole or not at all | Ending a lineage on an erased row (§Match records, "A lineage can end") is only sound when an erasure cannot cut a chain. Below it, a date `purge` can delete a superseded predecessor and keep its successor, and the plugin would retire decisions about a payment that still exists (round 41). |
| **0.15.0** | [#69](https://github.com/bonzanni/casa-specialist-finance/issues/69) — the ledger instance id; `expected_ledger` on the annotation writes | The store binds to the instance id, the import checks the export's id, and every accounting write carries `expected_ledger`: a write can never land on a ledger the store was not built on (implementation plan D4). |
| **0.20.0** | [#86](https://github.com/bonzanni/casa-specialist-finance/issues/86) — `export_history` carries each row's tags and `tag_revision` | The import is the classification observation (issue #1): every exported row is classified as of the snapshot without a per-row read, so a catch-up quarter converges in a few passes. Below it the export has no tags and the import refuses: "bank-feed is below this plugin's floor". |
| **0.11.0** | [#39](https://github.com/bonzanni/casa-specialist-finance/issues/39) — backups, a protected restore, and the restore point minted on a workflow's first write; `workflow` and `expected_generation` on `tag_transaction`, `untag_transaction` and `add_note` | Every accounting write carries both (§Setup, "Test install"). Below it the `workflow` argument is refused, and the pass says the ledger is below the floor rather than writing unfenced. |

An earlier revision named 0.9.0 as the floor — correct for the namespace, one version
short once ledger import moved onto the handoff folder; the next named 0.10.0, written
before #39 shipped. The erasure tools themselves are not new: `purge`,
`forget_local_account` and `delete_all_data` have deleted rows since component 0.5.0.
0.12.0 (component 0.13.0) added `purge`'s `user_work=erase`, which also strips the tags
and notes of the rows it keeps. What the floor adds is #56, the guarantee that an
erasure never cuts a supersession chain, which is what makes an erased row evidence that
the payment ended (§Match records, "A lineage can end").

The namespace itself ([#31](https://github.com/bonzanni/casa-specialist-finance/issues/31)):
A tag written `owner::name` belongs to another workflow: bank-feed never counts it as
content classification (the classifier's untagged queue keeps the row), gives it its own
per-row budget (16 per namespace, 64 namespaced in all, apart from the 32 classification
tags), refuses it on either side of `rename_tag` (merge included) and refuses it in
auto-tagging rules. `delete_tag` still removes one; supersession carries it to the booked
successor and erasure removes it, as with notes. Below 0.9.0, `acct::` names are refused
by the tag grammar outright, so the plugin fails closed rather than mis-tagging. An
un-namespaced `acct-matched` would be content classification on every version — which is
why the whole vocabulary below is namespaced.

### Delegation timing — design assumptions

This plugin is built on the following behaviour of Casa's `delegate_to_agent`, as read
in Casa v0.331.0–v0.332.0. They are assumptions about another project, and each says
what this code does if it stops holding.

- **A1 — the 60 s degrade.** A sync delegation still running after about 60 s returns
  `{"status": "pending", …}`, and its outcome arrives on a later resident turn.
  - *If false*, the answer is inline. Ellen calls `continue_pass` in the same turn, which
    is the same path.
  - *If the degrade is shorter*, nothing changes.
- **A2 — the 600 s ceiling.** A delegated turn is cancelled about 600 s after launch, and
  the resident receives an error of kind `timeout` with no result text. The specialist's
  clock and the step's expiry are measured from a stamp Ellen writes *before* she
  delegates, so our clock runs ahead of Casa's.
  - *If the ceiling is longer*, a delegation does less than it could. Once its step has
    been continued, the rotated token refuses its writes (§"Running the pass on demand").
  - *If the ceiling is shorter than the budget*, the delegation is cut, and the step
    expires at `STEP_EXPIRY_S` anyway.
- **A3 — every outcome produces a resident turn.** This holds for ok, error, restart
  orphan, and "notice does not carry its answer". This design reads nothing from that
  turn's content.
  - *If false* (the notice is lost), the pass continues at the next check, handover or
    package. The reclaim after 3 h is the backstop.
- **A4 — a notice can arrive more than once, or late.** Casa re-announces an outcome
  after a restart until a turn *delivers* a reply. A turn that ends `<silent/>` delivers
  nothing (`agent.py` `_ack_delivery`), so a cron pass's notices come back at every
  restart.
  - Each replay calls `continue_pass`, which can only continue what is due anyway. Each
    one still costs a resident turn: residual R1 (§"Running the pass on demand").
- **A5 — a scheduled turn's notice can still deliver.** The scheduled-delivery marker
  travels with the notice.
  - *If false*, on a cron pass `speak` stays pending in `alerts` and goes out at the next
    `end_pass`, which is today's behaviour. A package continuation on such a turn fails
    its send and records it `uncertain`.
- **A6 — Ellen's own turn is not bound by A2.** If her continuation dies, its lease
  lapses after `LEASE_S`. The next `continue_pass` then claims again and rotates the
  token, and anything the dead holder's turn still attempts is refused.

Casa runs every turn under a per-session write gate keyed by channel, role and chat, and
a notice is built from the delegation's recorded origin, so it normally lands on the
delegating turn's own key and waits behind it. This design does not rely on that: the
step's start is written before `delegate_to_agent`, and concurrent continuers are
separated by the rotating claim, not by turn order.

## Data model

All state lives exclusively in `$CLAUDE_PLUGIN_DATA` (survives plugin updates by
casa's design; a plugin update or reinstall may never touch it). SQLite index +
files, schema versioned so future plugin versions migrate rather than recreate.
Nothing identifying ships in the repo.

Canonical quarter identifier everywhere (paths, records, tool arguments, tags, zip
name): **`YYYY-Qn`** (e.g. `2026-Q2`). Never a bare `Qn`.

### Document store

The store holds **supporting documents of every kind**, not only invoices (operator,
2026-09-22): a vendor's invoice, a sales invoice the operator issued, a payslip, a bank or
tax statement, a receipt. Each carries a **kind** — `invoice`, `sales-invoice`, `credit-note`, `payslip`,
`statement`, `receipt`, `other` — read provisionally at ingest and corrected by the
specialist when it judges the document. `credit-note` covers both directions: a vendor's
credit note behind a refund the B.V. received, and the B.V.'s own credit note behind a
refund it paid a client (round-26 finding, both reviewers: the classifier defines `refund`
as "a credit reversing a purchase", and a mapping that expected nothing for it would
have left VAT-relevant evidence unsought). The kind is what lets a document satisfy an
expectation (§"Document expectation"): a payslip never satisfies a transaction that needs
an invoice, whatever the amounts say.

- **Custody is by content hash, not by name.** Files live at
  `documents/<sha256[:2]>/<sha256>.<ext>`. The human-readable
  `<YYYY-MM-DD>_<counterparty>_<amount>.<ext>` is a package-time rendering, and carries a
  short hash suffix when two documents would otherwise render the same name — two
  same-day, same-amount purchases from one vendor collide on the v1 scheme, which
  specified no overwrite, refusal or disambiguation rule.
- Index row: id, content hash, size, kind, counterparty, document date, document number,
  amount, currency, recipient-as-read, source (gmail message id / manual), acquisition
  coordinates for retry, extraction author, status
  (`unmatched` / `matched` / `irrelevant`).
- **Byte identity is not document identity.** A vendor that re-renders or re-sends the
  same invoice produces different bytes and a second index row; two equal payments
  could then each take one while every per-file cardinality check passes. The server
  flags an issuer + document-number collision across differing hashes and refuses
  automatic acceptance on either side until it is resolved.
- Custody is independent of matching: rejecting a candidate never deletes its PDF, and
  v1 deletes nothing automatically. A document is `held` only once its complete bytes
  are written, hashed and atomically installed and the index row commits — a crash may
  leave an unindexed file to reap, never a row claiming custody of a partial file.

### Match records — a revisioned state machine, all mutations CAS

invoice_id ↔ bank-feed `row_id`, state `matched` / `proposed` / `conflicted` /
`rejected`, decision author (`auto` / `operator`), rationale. **`matched` is the only state that feeds packaging** — an unanswered `proposed`
never reaches `invoices/`. The v1 text made both active states feed the package, which
turned operator silence into endorsement and contradicted the packaging section three
pages later. `matched` and `proposed` are both **active for cardinality** (they occupy
slots); `conflicted` is non-active — it holds matches that lost a consistency
race (see collision handling below), is always surfaced as residue, is excluded
from packaging, and leaves the cardinality slot free; it exits only by explicit
reassignment: the operator confirms one candidate or rejects it, or the specialist
re-proposes naming every `conflicted` candidate it displaces (§Tool surface, Matching). Alongside the `row_id`, each match
snapshots the transaction's identifying facts (booking date, amount_minor, currency,
direction, counterparty) so a match is auditable even if the row it targeted changes.

**The acceptance revision moves only when the proposition moves.** A revision bump
means the thing the operator was asked about changed — the pairing, its evidence, or
the transaction facts under it. Annotation delivery lives entirely outside the match
record now (§"Mirroring decisions into bank-feed"), so it cannot bump an acceptance
revision at all: a correction to an unchanged proposal can never be refused because of
bank-feed write progress.

**Every match carries a monotonic `revision`, bumped by every change to the
proposition, and
every mutating tool call takes `expected_revision` — the server rejects a stale
mutation outright (compare-and-swap).** This is the single concurrency discipline;
there are no unversioned flags. Consequences, each closing a reviewed failure path:

- **Operator answers are bound to a revision.** A review-sheet line carries
  (match_id, revision). If the match changed underneath — retargeted after a
  bank-feed supersession, demoted on snapshot mismatch, superseded by a better
  candidate — the stale tap is rejected by CAS and Ellen re-asks with current
  facts. A confirmation can never land on a match that no longer means what the
  operator saw. (Round-2 finding: stale Confirm re-confirming a demoted €100→€90
  match.)
- **A match carries no annotation state at all** (round-8 generalization). Retarget,
  demotion and rejection change the decision; what the bank ledger should then show is
  recomputed for the whole transaction from every current decision about it, and
  reconciled by the projection sweep. A match cannot attest that shared bank state is
  clean, which is the mistake that produced four reproduced failures — see §"Mirroring
  decisions into bank-feed".
- **Match creation is preconditioned on a fresh row resolution.** The specialist
  re-resolves the target row (`get_transaction`, state active) immediately before
  `record_match`/`propose_match`, and passes the resolved snapshot; if a sweep
  retarget later collides with a match already created on the successor row, the
  server resolves it by the reducer's order (above): an operator-authored pairing
  wins and the other contender goes to `conflicted`; two machine contenders **both**
  go to `conflicted` — a non-active state, so the one-active-match invariant is never
  violated by the collision itself — with a residue line either way. Neither contender needs a cleanup instruction attached:
  both transactions' projections are recomputed from their new state, and the sweep
  enumerates every projection unconditionally, so the round-4 hazard (a record invisible
  to both worklists) cannot exist when there are no worklists to be invisible to; the conflict is resolved only by
  explicit reassignment. Never a silent drop, never two active matches on one economic
  transaction. (Round-2 finding: retarget/new-match collision; round-3 finding:
  two active `proposed` would themselves have violated cardinality.)

**Decisions about a lineage form one ordered log, and the desired state is a function of
it** (round-12 generalization). Rounds 10, 11 and 12 each found a lineage state the design
had not decided — an exemption against `open`, a proposal against an exemption, an exempt
predecessor merging into a successor the operator had since matched. Three instances of one
shape: decisions were scattered across match records, a vendor field and an exemption flag,
and every pairwise interaction had to be written down by hand. So the store keeps, per
lineage, **one append-only decision log**: each entry is a decision kind (pair, propose,
unpair, exempt, lift), its author (`auto` or `operator`), its sequence number from the same
store-wide sequence the note revisions use, the **row fingerprint it was made against**,
and the shown revision it was bound to when the author is the operator. A vendor's channel
(the expectation, the portal source) is **not** a lineage decision: it is a fact the
reducer derives from the KB, the mapping and the row's classification at the end (round-13 finding: listing it as an entry kind left
machine-vs-machine order undefined). **A lineage's match-record state is the fold of its log**, in store-wide sequence order,
through the one transition table in step 2 below, starting from the empty state — and **the
log holds two kinds of entry**: the decisions writers append, and the **retirements the
store itself commits** when a transition retires a pairing (`retire X → conflicted` or
`→ rejected`, with the cause and its own sequence number — a kind mismatch, an occupancy
collision, or the lineage ending). At commit the fold advances by
one writer entry plus whatever retirements it caused; at a fan-in merge the state is
recomputed by folding the **union of the two logs, retirements included**, and any new
retirements the merged fold causes are appended too. Three consequences, each a reviewed
failure:

- **A committed retirement is never undone by a re-fold — and it retires an
  activation, not a match id.** Round 19 reproduced the alternative — a fold of writer
  entries alone from the empty state: machine A and B collided and were `conflicted` at a
  merge, the operator paired B's invoice to another payment, and a later merge re-folded B
  back to active, leaving one invoice on two payments. Retirement is monotone *per
  activation* (an activation only ever moves toward `conflicted` or `rejected`), so
  recording it can never be wrong for that activation; it is **not** monotone per match
  id, because the operator can confirm the same pairing again later — a `confirm_match`
  or `record_match` naming an existing pairing is a **new activation** with its own
  sequence, and a retirement bound to the older activation does not touch it (round-20
  finding, both merge orders now agree). **Clearing an exemption is not monotone at all,
  so it is never recorded** — an operator pair with sequence *s* clears any exemption
  with a lower sequence as a derived effect inside the fold, and no `lift` entry is
  written for it (rounds 18–19: a persisted "implicit lift" was re-folded out of order and
  erased a newer exemption).
- **Merge order is history, not a free variable.** Because retirements are recorded, two
  walk orders that retire different things leave different logs, and the two ledgers
  differ. That is accepted and stated: what the ledger shows is the fold of what actually
  happened, reproducible from the log, with every retirement visible as residue. Round 18
  asked for a merge result independent of walk order and round 19 showed that goal
  contradicts durable retirement; durable retirement wins, because its failure mode is a
  double allocation of an invoice and the other's is a different candidate in the residue.
- **Every activation is checked against occupancy, under one stated serialization.**
  Whenever a fold would make a pairing active — at commit, at merge, or a re-fold — the
  store checks that the pairing's invoice is not active on another lineage; if it is, the
  pairing is retired `conflicted` (recorded) with a residue line naming the other payment.
  "Atomically" is a concrete contract, not a word (rounds 20–21): there is **one SQLite
  store and several server processes writing it** — Casa spawns the plugin's MCP server
  per agent session (`plugin-runtime.md`: "an agent's … MCP process spawn"), so Ellen's
  server and each ephemeral specialist's server are separate processes on the same
  `$CLAUDE_PLUGIN_DATA`. Round 21 reproduced what "single writer" would have hidden: a
  second `BEGIN IMMEDIATE` on a locked store fails with *database is locked*, and a
  correction the operator just gave would go unapplied. So: every match write and every
  fan-in fold **acquires the store's write lock** (`BEGIN IMMEDIATE` with a bounded
  `busy_timeout`, retried until acquired or the bound expires), **allocates its writer
  sequence number inside that transaction**, and does the fold, the occupancy read, the
  retirement appends and the commit before releasing it. The loser of a same-invoice race
  is therefore always the write that serialized later — the higher sequence — and a
  **partial unique index on the invoice over active states** (`matched`, `proposed`)
  backs the check: an index conflict is converted into the same recorded `conflicted`
  retirement for that later activation, never surfaced as an error. Lock contention that
  outlives the bound is the one thing that does surface: the tool returns the error and
  the receipt says the correction was not applied, so nothing is silently dropped. A
  race test across two connections **and two processes**, including forced lock
  contention, pins it. The invoice-cardinality invariant is therefore
  enforced on the fold, not only on the match tools' writes.

Rounds 14–16 found a different error and this restores nothing of it: those rounds
*selected* "the latest entry" instead of folding, so a retirement committed later was never
consumed. The fold consumes every entry, and a retired pairing stays retired until a later
writer entry names it again. Two principles it is built on:

- **the operator's later word wins, and a machine decision never overrides an operator
  decision.** "Later" is always the store-wide sequence number, compared between the two
  decisions in question, never "which lineage was walked first". A machine write that
  would override is refused at write time (`record_match`/`propose_match` on an exempt
  lineage, `set_exemption(auto)` does not exist) and surfaced as residue ("a document
  turned up for a payment you exempted");
- **an absence is not a decision, and a retired decision never returns on its own.** A lift
  is an entry; a lineage that was never exempted has no entry; a pairing that was rejected
  or conflicted stays so until a new entry — the specialist re-proposing, the operator
  pairing — says otherwise.

**A decision is valid only against the facts it was made against — and validity is
judged on the *current* decision alone, after precedence has chosen it** (round-13 and
round-14, both reviewers). Every pair or propose entry carries the row fingerprint it was
made against. When the live row's material facts differ from the current pairing's
fingerprint (the in-place correction case below: €100 accepted, then corrected to €90
under the same `row_id`), that pairing is **invalidated, not overridden and not
dropped**: it stays the lineage's current pairing, it counts as `proposed` rather than
`matched`, and a residue line asks the operator to confirm it against the new facts — a
fresh operator entry with the new fingerprint. It is never replaced by an older entry:
round 14 reproduced the alternative (validity as a filter over the whole log), under which
correcting €100→€90→€100 resurrected invoice A after the operator had replaced it with B.
Anything a later decision superseded is history whatever the fingerprints say. Validity is
a comparison with the live row, so a correction that is itself reverted restores the
acceptance on its own — the decision was about those facts and they are true again — and
the residue reports both changes. So "a machine decision never overrides an operator
decision" is exactly true: the fingerprint sweep appends nothing and decides nothing; it
observes whether the world the current decision was about still holds. An exemption
carries no fingerprint and survives a correction — "this payment needs no invoice" is not
about the amount — but the correction is still reported.

**The reducer, as one total function.** Everything above and the table in §"The
projection" are *derived* from this order, and an implementation follows the order, not
the prose. For one lineage, given its merged log, its live row and the vendor KB:

0. **Ended** (§"A lineage can end", under §Match records). The lineage's destination row
   is `vanished` or erased → `∅`, and nothing below applies. The fold is still computed —
   the `retire` entries the end caused are in it — so the residue and the log stay
   reproducible.
1. **Eligibility.** Destination row not eligible (§"The projection", admission) → `∅`.
2. **Operator precedence — over the state the fold produced.** The fold's state for a
   lineage is: the **standing exemption** (at most one, with its sequence number), the set
   of **active** pairings (each with author, sequence number, fingerprint), the set of
   **`conflicted`** candidates, and the `rejected` history. The fold's transition list —
   applied one entry at a time in sequence order, at commit and at merge alike — is:

   | Entry (by sequence) | Transition |
   |---|---|
   | operator `pair P` | any standing exemption with a lower sequence is cleared — a derived effect, **no entry is written**; P is activated (occupancy check: if P's invoice is active on another lineage, P → `conflicted` instead, recorded); every other active **or `conflicted`** pairing → `conflicted`, recorded |
   | machine `pair`/`propose M` | if an exemption stands → M → `rejected`, recorded (at write time the tool refuses instead; in a merge fold this is what "machine never overrides operator" means); else if an operator pairing is current → M → `conflicted`, recorded; else M joins the machine candidate set and **step 4's normalization runs inside this transition**, recording what it retires (round-19 finding: normalizing after the whole fold instead of inside each entry gave a different ledger) |
   | operator `unpair X` | X → `rejected`, whatever its state; nothing else changes, and nothing already retired becomes current |
   | operator `exempt E` | every active **and `conflicted`** pairing → `rejected`, recorded; E stands, superseding any earlier standing exemption (round-18 finding: two exemptions meeting a pairing between them had no rule — the fold settles E10, P15, E20 as: exempt, P clears E10, E20 rejects P and stands) |
   | operator `lift` | clears the standing exemption; restores nothing; a `lift` folded from a lineage where it was valid but meeting no exemption in the merged fold is a no-op |
   | specialist `propose M, resolves=[…]` | every named id → `rejected`, **unconditionally and whatever its state at replay** (round-19 finding: at write time the list was validated against the conflicted set, but a re-fold can meet the same entry with those ids in other states; the specialist's judgment about them stands), then as `propose M` |
   | store `retire X@a → conflicted \| rejected` | X's **activation** *a* — the sequence of the writer entry that made X active — is retired to that state; nothing else. The entry is ignored when X's current activation is later than *a* (round-20 finding: a retirement bound to the match id alone, generated while re-folding an older activation, retired the operator's later confirmation of the same pairing), and it never moves `rejected` back to `conflicted`. This binding is what lets a committed retirement survive every re-fold without outliving the thing it retired |
   | fingerprint change on the live row | not an entry — validity is judged in steps 3 and 5 against the live row at reduction time |

   Then, over that state:
   - an exemption stands → `{acct::no-document-expected}`, then **step 8**. Nothing is
     active while it stands, by the transitions above.
   - an operator-authored pairing is active → it is current (the transitions leave at most
     one); go to step 3.
   - otherwise → step 4 with the machine candidate set.

   Why the earlier statements of this step failed, so the shape is not re-litigated: rounds
   14–16 *selected* the latest entry instead of folding, so a later `unpair` or `lift` was
   never consumed; round 17 folded per lineage but combined two folded states pairwise at
   merge, which is not associative. A fold of the sorted union is both.

   **The targeted kinds have write-time preconditions**, refused by the server with the
   current facts rather than absorbed: `unpair X` requires X to be active or `conflicted`
   on the lineage (a `conflicted` candidate is shown in the residue and the reply grammar
   promises a negative verdict can remove it); `lift` requires an exemption to stand.
   Every operator pair-type write — `record_match` and `confirm_match` alike — on an
   exempt lineage is one transaction that appends `lift` first and `pair` second (round-18
   finding: only `record_match` said so, and a `confirm_match` of a shown `conflicted`
   candidate on an exempt lineage produced a current pairing under a standing exemption).

3. **Validity of the current operator pairing.** Compare P's fingerprint — the row facts
   and the expectation kind it was made against — with the live row and the currently
   derived expectation. Equal, and the document's kind still equals the expected kind —
   or the expectation is currently unknown and the pairing was kind-valid against the
   last known expectation (§Match records, "An expectation that becomes unknown") — →
   `{acct::matched}`. Different → `{acct::proposed}` with a residue line, P
   still the current pairing (never dropped, never replaced by an older entry); when the
   operator confirms against the new facts, or the facts revert, it is `matched` again —
   **except for a kind mismatch, which no confirmation cures** (round-27 finding): a
   payslip does not become an invoice because the operator says so twice. `confirm_match`
   and `record_match` refuse, with the facts, a document whose kind is not the lineage's
   current expectation kind; the operator has two ways to a match and one way to close the item without one
   (round-28 finding, both reviewers: the earlier list mixed them and skipped a step):
   confirm the right-kind candidate shown `conflicted` beside P; or correct the
   document's kind (`update_document_metadata`) or the expectation (`set_expectation`) —
   **either correction changes the proposition and bumps the item's revision, so the
   correction flow re-renders and applies nothing until the operator has seen the item
   again** (§"Descriptions choose the target; the revision the operator was SHOWN is what
   binds") — and then confirm P against what was shown. `set_exemption` is the third
   thing, not a route to a match: it rejects P and derives `none`, and its receipt says
   so (`Exempted 4 Adobe; unpaired payslip P.`). Then step 8.
4. **Machine candidates — a set, judged as a set, and `conflicted` is sticky.** This
   normalization runs **inside every transition that adds a machine candidate** (step 2's
   table), and what it retires is recorded; the reducer then reads the settled state. The
   lineage's machine candidate set is every machine pairing that is **active or
   `conflicted`** on it. Exactly one member **and it is active** → it is current; go to
   step 5. More than one →
   **all** of them are `conflicted` with a residue line, including any that arrived active
   and any already `conflicted` — an unresolved collision is a property of the set, and a
   newcomer joins it rather than surviving it. Exactly one member and it is **`conflicted`**
   → it stays `conflicted` and the lineage falls through to step 6 (round-18 finding: after
   A/B collided, the operator confirmed B and later unpaired B, and a singleton rule
   reactivated A, which the operator had never chosen — a retired pairing never returns on
   its own). Round 17 reproduced the set rule's necessity on bank-feed's real three-way
   supersession: with "collide the pair that met, keep the rest", merging A+B first left C as
   the survivor and merging A+C first left B, so traversal order chose the invoice that
   entered the package. A collision is resolved only by explicit reassignment: the operator
   confirming one candidate (§Tool surface, `confirm_match` on a `conflicted` id), the
   operator rejecting candidates (`unpair`), or the specialist re-proposing with
   `resolves=` naming the `conflicted` set exactly.

5. **Validity of the current machine pairing.** One active `matched` whose fingerprint
   holds — row facts and expectation kind alike, the document still of the expected
   kind (while the expectation is unknown: the last known kind verdict) → `{acct::matched}`; a machine pairing whose document kind no longer equals the
   expectation kind is not here at all — the sweep retired it `rejected` (§Match records,
   the fingerprint), so the lineage falls through to step 6; a `matched` whose fingerprint
   differs in row facts, or an active
   `proposed` (whether or not its fingerprint holds — an invalidated proposal stays a
   proposal, round-14 finding) → `{acct::proposed}` with a residue line for the change.
   Then step 8.
6. **Expectation** (§"Document expectation"), for a lineage with no current pairing:
   `none` → `{acct::no-document-expected}`; `optional` → `∅` (no tag: a nice-to-have that is
   missing is not an errand); `required` or unknown → step 7. Weaker than any pairing on
   purpose: a document that matches a specific payment beats a default about its
   counterparty or its class.
7. **Otherwise** `{acct::open}` — never paired, everything rejected or unpaired,
   `conflicted` only, accepted-missing after "stop chasing", not yet classified.
8. **Portal.** If the counterparty's source is `portal` and the row is eligible, add
   `acct::portal` to whatever the steps above produced.

Four properties the order is built to have, and the tests pin: **the state of any lineage,
merged or not, is the fold of its recorded history — writer entries and store retirements —
so it is reproducible from the log, and a re-fold never undoes a retirement**; **the reducer
selects only among pairings that are active now, and a retired pairing never returns on its
own**
(so a fingerprint change can invalidate the current pairing but cannot resurrect a replaced
or rejected one, and retiring a later entry does not revive an earlier one); **a targeted
operator entry (`unpair`, `lift`) touches only its target** (so a correction about one
candidate never cancels another); and **invalidation changes a pairing's status, never its
existence** (an invalid `matched` and an invalid `proposed` are both shown as `proposed`,
and both stay the lineage's current candidate). Every pairing that leaves "active" does so
into one of the four match states, at the commit or merge that retires it, and the reducer
never decides a retirement.

**Operator authorship is checked, not declared** (round-12 S1). An `author=operator` write —
`set_exemption`, `record_match`, `confirm_match`, `reject_match` from a correction — must
carry the `render_id` of a delivered rendering that showed that item, and the server accepts
the author only when the render log has it. A specialist working a pass holds no render ids,
so it cannot lift an exemption or launder an auto pick into an operator decision by passing a
word. Stated honestly: this is a guard against a mistaken caller, not a security boundary —
both callers are trusted models on the operator's own box, and a model that called
`build_review` first could forge the binding. It closes the reachable error, which is the
specialist doing on its own what only a correction may do.

**Cardinality invariants (server-enforced, not convention):**

- At most one **active** (`matched` or `proposed`) match per transaction row, and at
  most one active match per invoice, by default. The server rejects a second
  `record_match`/`propose_match` that would violate either.
- **No allocation groups in v1** (operator decision, 2026-09-21). One invoice paid in
  installments, one payment covering several invoices, partial settlements, fees, and a
  credit note *netted against an invoice inside one payment* are **not modelled** (a
  refund that is its own transaction matches its credit note one to one and is modelled
  — §"Document expectation", row 7): the documents are retained, the transaction stays
  unresolved, and `notes.md` explains the relationship for the accountant. This cuts
  group total arithmetic, group cardinality, group confirmation, group repair and group
  packaging semantics — machinery for exceptions v1 was never asked to automate. The
  round-2 finding that motivated the group design (€40 + €60 unrelated invoices passing
  total validation against a €100 payment) is closed by not having the mechanism.
  Re-add only if a real quarter produces these.

**Row-id lifecycle (bank-feed supersession).** Bank-feed rows are not immortal: a
sync can supersede a row (`state='superseded'`, `superseded_by` → new row; e.g.
pending → booked), and bank-feed itself migrates tags and notes to the successor
(verified: bank-feed `store.py` — apply_plan's supersede migration rewrites
`row_id` on annotations). Match records must follow: the repair sweep's **step 0**
re-resolves every active match's `row_id` via `get_transaction`; if the row is
superseded, the match is atomically retargeted to the successor (snapshot facts
re-verified against the new row — a booked amount correction that breaks the match
demotes it to `proposed` with a residue line); if the row has **ended** — `vanished`, or
erased outright — the lineage ends as the next paragraph says. `list_transactions`
filters to active rows, so without this step a superseded match would silently leave
every worklist.

**A lineage can end, and there are exactly two ways** (added 2026-09-24, after bank-feed
0.12.0 shipped `purge` with `user_work`). An earlier revision said a vanished row's match
"reopens as `unmatched`", which is not a match state and has no transition in the fold;
and it said nothing at all about a row that is simply *gone*, which bank-feed can now do
on the operator's word. Both are the same event for this plugin: **the lineage's
destination row will never again be an active row, and if the payment comes back it
comes back as a different row with no link to this one.** Verified against bank-feed
component 0.13.2 (bank-feed 0.12.2):

- **`vanished`** — bank-feed tombstones a row that is absent from a proven interval
  (`ingest.reconcile`, rule 3). It is terminal for that `row_id`: the matcher takes only
  `state='active'` rows as candidates (`live = [s for s in stored if s["state"] ==
  "active"]`), so a payment that reappears is a fresh insert with a new occurrence, never
  the old row revived, and no `superseded_by` link joins them. The row itself stays in the
  ledger, with whatever tags it carried.
- **erased** — `purge` (by `booking_date`, or `all`), `forget_local_account` and
  `delete_all_data` delete rows outright, their tags and notes with them. Such a row is
  absent from the bank snapshot and `get_transaction` answers `no transaction #N`.
  **An erased `row_id` is never reallocated**: `transactions.row_id` is `AUTOINCREMENT`
  and no erasure touches `sqlite_sequence`, so a later insert can never take its id and
  make a stored alias point at a different payment. The one path on which ids *are*
  reallocated is a restore, and that is already refused under a populated store
  (§Setup, "Test install"); erasures do not advance the restore generation and do not
  need to. **An erased row proves that the payment ended only if erasures never cut a
  supersession chain** (round-41 finding, Astra, reproduced on bank-feed's real
  `reconcile`, `apply_plan` and `purge`). The date form of `purge` selects rows by
  `booking_date < cutoff` whatever their state, so a pending row #1 (30 Jun), superseded by
  booked #2 (1 Jul), is deleted by `purge(before_date=2026-07-01)` while #2 survives. The
  one link between them, #1's `superseded_by`, goes with #1. Every test above then passes,
  and the operator's pairing on a payment that still exists would be retired. The
  reverse cut leaves a pending predecessor pointing at an erased successor. **Operator
  ruling, 2026-09-25: fixed upstream, not guessed around here.**
  [casa-specialist-finance#56](https://github.com/bonzanni/casa-specialist-finance/issues/56)
  makes `purge` lineage-closed: a row is deleted only when its whole chain is (a NULL
  `booking_date` counts as not before the cutoff), and `forget_local_account` and
  `delete_all_data` already remove whole accounts. **The bank-feed floor rises to the
  release that closes #56** (§Casa baseline). On a ledger at that floor, an erased
  destination is an erased lineage: no surviving row of the chain can exist, and no
  surviving `superseded_by` names a missing row. A lineage walk that nevertheless meets
  one — a surviving row whose `superseded_by` names an absent id — is a broken floor, not
  an end. The pass reports it and ends nothing, and the lineage keeps its decisions and
  its documents. **Chain closure concerns committed rows** (round-42 finding, Terra;
  dismissed after a refutation-defense run, Astra, which reproduced both branches on
  bank-feed's real `reconcile`, `purge_rows` and `apply_plan`). If a `purge` erases a
  predecessor after a `sync` built its plan but before the plan applies, `apply_plan`
  inserts the booked replacement and skips the missing predecessor's supersede by design
  (its "honest counts" rule), so the replacement is active and unlinked. That is the
  returning-payment case, even within one pass: the old lineage ends, its document is
  freed before triage, the residue line is written, and no operator decision carries
  across. Refusing the stale plan would not help: the next fetch inserts the same
  payment afresh, just as unlinked, since its only predecessor is gone.

**An ended lineage** is judged ended only on positive evidence from the bound ledger,
never from a failed read: `vanished` from the row's own `state` in the snapshot; erased
only when, in the same pass, the self-check found the bound account in the bound ledger
instance, the snapshot import succeeded, the destination row id is absent from it, and
`get_transaction` answers `no transaction #N` for it (the specialist reports that answer
through `record_observation`, as the not-found observation it is). A pass whose snapshot
import failed, or whose self-check did not find the bound account, judges nothing ended
— "not checked", as everywhere else. **The end is judged at the import, not in the
repair sweep** (round-41 finding): the sweep runs at the top of the weekly pass, before
`sync` and before that pass's snapshot exists, so a rule that judged ends in the sweep
would always be judging against the previous pass's snapshot — and the returning row,
admitted by the later import, would find its document still held by the dead lineage
until another pass. So `import_ledger_export` returns the erase candidates (every
managed destination absent from the snapshot) and the tombstoned destinations, the
specialist confirms the candidates with `get_transaction` **before triage** (§Weekly
pass, step 2), and the ends commit there — the freed documents are candidates in the
same pass's triage, against the same snapshot that admitted the returning row. The
import that precedes packaging applies the same rule before the build freezes its
snapshot. The end is recorded on the projection (`ended: vanished | erased`, with the
import that observed it) and is terminal. What follows from it, in one transaction:

- every active **and `conflicted`** pairing on the lineage is retired `rejected` by a
  **store retirement** (step 2's `retire` entry, cause `row-ended`), operator pairings
  included. This is not a machine decision overriding an operator's: the operator's
  decision was about a payment row, and the kind-mismatch rule keeps an operator pairing
  because the classifier may be the one that is wrong — here there is nothing left that
  could be wrong except bank-feed's own ledger, whose rules make the end terminal. A
  standing exemption stays in the log and is inert;
- **every retirement recomputes document availability** (below), so the documents the
  lineage held return to `unmatched` and the next triage can match them to the row the
  payment came back as, if it comes back. That row is a new lineage, admitted like any
  other, and **nothing carries across by resemblance**: an operator confirmation on the
  ended row is not a confirmation of the new one, whose pairing is judged afresh and
  shown like any machine pick. The residue says both halves once:
  `Adobe €59.99 · 3 Jul left the bank ledger (erased) — its invoice is free again`;
- the desired set is `∅` (reducer step 0). A vanished row still exists, so the sweep
  removes its owned tags like any ineligible row's — a stale writer's later `acct::open`
  on it is repaired the same way. An erased row has nothing to remove and can take no
  write: a stale `tag_transaction` on it fails at bank-feed.

An ended lineage stays enumerated, like every projection, but an erased one is never read
again (its id cannot come back) and a vanished one is read only to repair its tags. Ended
lineages are **outside every view's membership and coverage** (§"The sweep"): they have
no row to classify, so their observation timestamp would otherwise drag the
classification date back for ever. They are listed once in the residue of the pass that
ends them, and a package or view never lists them as open.

**A row can also change without being superseded, and that is the case v1 missed.**
bank-feed's update path rewrites `booking_date`, `value_date`, `amount_minor`,
`currency`, `direction`, `status`, `counterparty` and `remittance` **under the same
`row_id`** (verified 2026-09-21 against `apply.py`'s hand-written UPDATE column list).
Supersession-only revalidation therefore leaves an accepted €100 match standing after
the amount is corrected — the sweep sees neither `superseded` nor `vanished` and moves
on. So: every pass, and again immediately before packaging, every active match
re-compares its snapshot fingerprint (account, direction, currency, `amount_minor`,
status, booking date, counterparty, remittance), **bank-feed's review flags, and the
expectation kind derived from the row's classification** (§"Document expectation")
against the live row, superseded or not. The classification is the one input that is
**not in the bank snapshot** — tags live outside bank-feed's `transactions` table and
`export_history` does not carry them (round 25, both reviewers) — so it is observed per
row by the sweep's `get_transaction` read (§"The sweep", step 3) and recorded on the
projection as its **classification observation**, refreshed every cycle for every managed
lineage, matched and delivered ones included. **Since bank-feed 0.20.0 (#86; issue #1) the
export carries each row's tags**, so the import itself records the classification
observation for every row it carries; the per-row read remains for the rows the plugin
writes to or must read back. A changed material fact **invalidates** the acceptance (reducer step 3, or step 5 for a
machine pairing). A changed **expectation kind** (the classifier re-tags a salary payment
as fuel; a matched purchase gains `internal-transfer`; the operator sets a counterparty to
`none`) is stronger than an invalidation, because the held document *cannot* satisfy the
new expectation — a payslip is never a fuel invoice, whatever the amounts: a **machine**
pairing of the wrong kind is retired `rejected` by the sweep (a recorded retirement, with
a residue line naming the reason), and the lineage is searched for a document of the new
kind like any unpaired one. **Every retirement, this one included, recomputes the
document's availability**: a document held by no active pairing returns to `unmatched`,
so `list_unmatched_documents` and the next triage see it again — if the classifier
reverts its tags, the same document is a candidate once more, never hidden behind a
`matched` status nothing holds (round-27 finding); an **operator** pairing of the wrong kind is shown `proposed`
with the reason — the operator chose that document knowing what it was, and the classifier
may be the one that is wrong — and a candidate of the right kind that the search finds
lands `conflicted` beside it for the operator to pick, never replacing it silently
(round-26 finding: the earlier text invalidated both alike and then excluded the lineage
from the replacement search it promised). **An expectation that becomes unknown is not a
kind change** (added 2026-09-24): a row that returns to the classifier's queue, or whose
tags conflict (§"Document expectation", rows 4–5), has no kind to compare against, and
reading "unknown ≠ invoice" as a mismatch would retire every machine pairing on it. That
was reachable before — the classifier's tags can be removed by hand — and bank-feed
0.12.0 makes it reachable for the whole ledger at once: `purge(user_work=erase)` deletes
every tag on every surviving row, classification included, and the next sweep would
otherwise reject every machine pairing in the store. So **while the expectation is
unknown, the pairing keeps the kind verdict it had against the last known expectation**
(round-42 finding: "suspend the comparison" read as "the kind check passes", which
promoted an operator pairing already shown `proposed` for a kind mismatch back to
`matched` the moment its tags were erased). A pairing whose kind matched stays
kind-valid. One whose kind already mismatched stays mismatched: an operator pairing is
still shown `proposed` with that reason, and a machine pairing had already been retired.
The row is shown under "not yet classified" (where a paired one says which document it
holds), and comparison against the live expectation resumes at the first known kind the
classifier gives it: a match, nothing changes; a different kind, the rule above applies.
The row facts are compared as always. The last known kind is recorded on the projection
with each classification observation, so the verdict is read from the store, never
re-derived from tags that are gone. For a plain invalidation: the pairing stays the
lineage's current candidate and is shown `proposed` with a residue line until the operator
confirms against the new facts, or the facts revert. Migrated tags and notes on a successor row are not fresh approval
either.

### Document expectation — what a transaction needs, and how much it matters

**The operator's ruling (2026-09-22) that reshaped the scope.** Not every outgoing payment
has an invoice (interest, salaries), some have another document worth keeping (a payslip),
and incoming payments are the same story (a client payment settles a sales invoice; a tax
refund settles nothing). So the question this plugin answers for every transaction is not
"where is the invoice" but **"what document does this need, does one exist yet, and how
much does it matter"** — and the first half of that is decided by what the transaction
*is*, which is tx-classifier's judgment, never this plugin's.

An **expectation** is a pair: a document **kind** (`invoice`, `sales-invoice`,
`credit-note`, `payslip`, `statement`, `receipt`) or `none`, and a **tier**, `required` or
`optional` — the same kind domain the document store uses and `set_expectation` validates
against (round-27 finding: row 7 derived a kind the domain did not list). It is derived
per lineage by **one decision procedure, direction-aware, first rule that applies** (round
25 found a precedence table and a defaults paragraph that disagreed on `income, refund`
for a DBIT, and left a parked row with a counterparty override undecided; there is now one
table and it is the test oracle):

| # | Condition (checked in this order) | Expectation |
|---|---|---|
| 1 | the operator exempted this payment (`set_exemption`) | `none` |
| 2 | the counterparty KB carries an override (operator or specialist, `set_expectation`) | the override — it beats the row's workflow state too: a parked Belastingdienst row is `none`, not unknown |
| 3 | `unclassifiable` — terminal, checked **before** the queue state, in bank-feed's own `classification_state` order (terminal > parked > classified > workable; round-26 finding: a terminal row also has no classification tag, and a queue check placed first swallowed it into "unknown" forever, since the classifier never drains a terminal row) | DBIT → `invoice, required`; CRDT → `sales-invoice, required` (the operator declined to say; the safe reading is that a document matters) |
| 4 | the row is in the classifier's queue: `awaiting-operator`, or no classification tag at all (a parked row's content tags are not trusted) | **unknown** — `required` for the ledger tag (operator ruling: "nothing held yet"), searched for nothing until classified |
| 5 | **classification conflict**: the tags carry more than one flow correction, or chain roots that would select different rows below (`salary` with `fees`; `income, consulting` with `internal-transfer` is *not* a conflict — a flow correction beats a chain by rows 6–7) | **unknown**, and the row is shown under "classification conflict" with its tags, so the operator or the classifier can clean it up (round-26 finding: additive tags can select two rows with different answers, and "first rule" would have picked one silently) |
| 6 | flow correction `internal-transfer` or `cash-withdrawal` | `none` |
| 7 | the tag `refund` is present — either direction: a vendor's credit note behind money received, the B.V.'s own credit note behind money paid back. **Tags are tags**: bank-feed keeps no record of whether `refund` arrived as a flow correction or as the tail of the chain `income, refund`, so this one row covers both spellings (round-27 finding: a separate CRDT row for the chain form was unreachable, and a chain override for `income, refund` could be bypassed) | `credit-note, required` |
| 8 | flow correction `reimbursement` — either direction | `receipt, optional` |
| 9 | DBIT, chain under `salary` / `payroll` | `payslip, optional` |
| 10 | DBIT, chain under `fees` / `interest` / `tax` | `statement, optional` |
| 11 | DBIT, anything else (any chain, or a mapping override for that chain) | `invoice, required` |
| 12 | CRDT, chain under `income, interest` / `income, dividend` | `none` |
| 13 | CRDT, anything else — `income, *` or an unknown chain | `sales-invoice, required` |

Rows 6–13 are **the shipped mapping**; `set_expectation(scope=<chain>, …)` overrides the
outcome for a chain **at whichever row the row's tags actually select** — an override
for `income, refund` applies at row 7, because that is where a row carrying `refund` is
decided — and never depends on how a tag got there. **The mapping ships with defaults and is edited by asking**, never at install:
"payslips don't matter" moves `payslip` to `none`; "Belastingdienst never has a document"
writes a counterparty override (row 2). The defaults err toward *required*, because the
failure mode of `optional` is a VAT-relevant document nobody looked for, and the failure
mode of `required` is one line too many that the operator turns off in a sentence. The
administrative tag `invoice-missing` from the classifier's catalog is read as evidence
for nothing here: `acct::open` is this plugin's statement and the two may disagree
without either being wrong.

**The tier is what the ledger tag and the views run on** (operator, 2026-09-22):

| Expectation | No document held | Document matched |
|---|---|---|
| `required` (or unknown) | `acct::open`; leads every view; `MISSING` in the ledger | `acct::matched` |
| `optional` | **no tag**; not in "what am I missing"; shown on "show the rest"; still searched, cheaply, and filed when found; `notes.md` lists it under "nice to have, not found" | `acct::matched` |
| `none` | `acct::no-document-expected` | — (a document that turns up anyway is filed as `irrelevant` to this transaction, never matched) |
| unknown (not yet classified) | `acct::open`, and the row is shown under **"not yet classified"**, apart from "missing" | nothing new is matched to it; a pairing made while it had a kind keeps its last known kind verdict (`acct::matched` if it was kind-valid; an operator pairing already mismatched stays `acct::proposed`) and is shown under "not yet classified" with its document, until a kind returns (§Match records) |

Filtering the operator's own ledger by `acct::open` therefore answers exactly the question
the VAT return asks: which transactions still lack a document that matters.

**Sequencing with the classifier.** tx-classifier drains its queue in the same specialist
session, on `sync`'s own trailer, before this plugin's triage (§Weekly pass step 2). This
plugin reads the classification from the row's tags — the chain, the flow corrections
(`internal-transfer`, `refund`, `reimbursement`, `fees`), the parked and terminal markers —
and never writes one. A row still in the classifier's queue (workable, or parked
`awaiting-operator`) has expectation *unknown* unless an exemption or a counterparty
override says otherwise (rows 1, 2 and 4 of the table); `unclassifiable` is row 3, and a
classification conflict is row 5. **The
expectation is a standing dependency, not a one-time lookup** (round 25, both reviewers):
the classifier's rules are additive, `apply_rules` adds repaired-rule tags to old rows,
and a chain can be corrected later, so every managed lineage's classification is
re-observed every sweep cycle and a changed expectation *kind* invalidates a current
pairing (§Match records, the fingerprint). A payslip matched to a payment the classifier
later calls fuel is shown `proposed` with the reason, and the invoice the row now needs
enters the search; a tier change alone (`required` ↔ `optional`) invalidates nothing. On a quiet
week this ordering is invisible. On the first pass over a full quarter the classifier's
own 25-rows-per-pass budget means the first passes are mostly classification and the
document hunt follows; the coverage line says how many rows are not yet classified.

**Counterparty KB** — lean, one record per counterparty, now the second source above:

- canonical name; counterparty patterns as bank-feed shows them (`BCK*ZAPIER` → Zapier)
- expectation override: kind + tier, or `none` — set by the operator ("no invoices ever
  for X") or by the specialist when a vendor's documents turn out to be a kind the
  mapping did not predict
- source: `email` / `portal` — where the document comes from
- **document_link** — the researched deep link, as close to "the page listing your
  invoices" as the vendor allows. This is the KB's primary asset: found once with
  real effort (WebSearch), cached forever, re-researched only when the operator says
  it broke.
- search_hint (for email vendors), free-text notes ("invoices post ~3 days after
  charge")
- one source note per link (where it was found, when) — **not** per-field provenance,
  and no scheduled re-verification. A link is re-researched when a retrieval actually
  fails; an aging policy turns a deep-link notebook into standing upkeep for links that
  still work. A `[Wrong]` tap records the fact; it does not mandate a follow-up turn.

A counterparty entry of `none` is load-bearing: it is what stops bank fees, taxes, and
receipt-less charges from polluting the residue list forever, where the classification
mapping alone would not (a tax authority's rows may carry several different chains).

### Quarter workbook

Per-quarter record of pass runs, per-transaction state, and open residue. Makes
multi-round delegations stateless-safe (each delegation is a fresh ephemeral session;
state carries in the store, not in return values alone) and crash-safe: a casa
restart mid-pass loses only the in-flight turn.

## Tool surface (server, 36 tools)

**Erratum (operator, 2026-09-27; implementation plan D1):** the server registers 33 tools.
The flows below need writes this section never named: `begin_pass` / `end_pass` (the pass
marker), `record_probe`, `record_search`, `stop_chasing`, `set_watermark`, `relabel_match`,
`record_delivery`, and `apply_reply` (the executable reply grammar). The plan's §D1 has the full list.

**Erratum (issue #8, v0.3.3):** `read_document` sends a PDF with a short prefix before its header from the header, and refuses a PDF with no header in its first 1024 bytes (§Placement, "Which files an agent may `Read`").

**Erratum (issue #6, v0.3.2):** 36 tools. `read_document(doc_id)` hands an agent a filed document to read (§Placement, "Which files an agent may `Read`").

**Erratum (issue #2, v0.2.0):** 35 tools. `record_step` records a pass's delegated steps
(start, before the delegation; finish, the specialist's last action), and `continue_pass`
is the one claim that continues a pass or a package request across turns and rotates its
token (§"Running the pass on demand"). `build_quarterly_package`, `stage_for_delivery` and
`record_delivery` take the `package_token` that `end_pass` or `continue_pass` handed over
(required for a request-bound package; a resend and a single document stay token-free).
`begin_pass` takes `reply` (`silent` for the cron), and `list_quarter_state` takes an
optional `pass_token` for the clock.

Ingest & curation: `ingest_document(source_path, kind, counterparty, document_date,
document_number, amount, currency, recipient, source_ref)` — **the server takes `source_path` from
Casa's handoff folder with `casa_handoff.capture`, and copies those bytes into its own
store, hashes them and indexes them, in that order**. It returns the content hash.
`source_path` is the path gmail's `download_attachment` returned, or the path
`share_inbound_file` returned for a document the operator sent. Any other path is refused.
This is the only way bytes enter custody. It is a copy rather than a reference on
purpose: a handoff file is removed after 7 days, so a record pointing at it would be
custody in name only. The metadata
arguments are the agent's provisional reading, for filing; the bytes are the fact.
`update_document_metadata` (the specialist corrects the provisional `kind` here when it
judges the document), `mark_irrelevant`.
Query: `list_unmatched_documents`, `read_document`, `list_quarter_state`, `get_counterparty`.
Expectation: `set_expectation(scope, kind, tier)` — `scope` is a counterparty or a
classification chain; the operator's "no invoices ever for X" and "payslips don't matter"
land here, with the render binding when the operator is the author (§"Document
expectation"). The per-payment case stays `set_exemption`.
Reset: `reset_store()` — wipes `$CLAUDE_PLUGIN_DATA` to the fresh-install state. It is
argument-free and protected (one Casa tap), and it is the plugin's `casa.eraseTool`,
answering `{"erasure": "complete" | "incomplete", "report"}` (revised 2026-09-27, operator
ruling). It has no precondition, because the server could not check one; the guard is that the
next pass refuses every bank-feed write while bank-feed still reports `acct@<version>`
registered (§Setup, "Test install"). Every bank-feed write the skill prescribes carries the
workflow string `acct@<version>`; the restore point is minted by bank-feed on the first
such write, not by any tool here.
Matching: `record_match`, `propose_match`, `confirm_match`, `reject_match` — every
mutating match tool takes `expected_revision` (CAS; see the match-record state machine).
Two contracts round 17 found missing: **`confirm_match` accepts a `proposed` or a
`conflicted` id whose document kind equals the lineage's current expectation kind**
(refused with the facts otherwise — §Match records step 3). With the operator's `render_id`, confirming a `conflicted` candidate
makes it the operator's pairing — current, and every other active or `conflicted` candidate
on the lineage goes `conflicted` (step 2's operator-pair rule) — unless the candidate's
invoice has meanwhile been paired on another lineage, in which case the write is refused
with those facts (cardinality: a `conflicted` record holds no slot, so the slot may be
gone). And **`propose_match`/`record_match` by the specialist on a lineage with `conflicted`
candidates requires `resolves=[match_ids]` equal to the lineage's current `conflicted` set,
checked inside the transaction**: each named one goes `rejected`, and a list that names
fewer, names an id that is no longer `conflicted` (the operator confirmed it meanwhile —
round-18 finding, where "all of them" let a superset retire the operator's pairing), or
names an id from another lineage is refused whole, with nothing mutated. A `confirm_match`
on an exempt lineage appends `lift` then `pair`, exactly as `record_match` does — and
**the kind guard is evaluated against the expectation the lineage will have after the
lift**, in the same transaction, rolling back whole on refusal (round-28 finding: checked
before the lift, every operator match on an exempt lineage would refuse, since an
exemption's expectation is `none`).
Exemption: `set_exemption(projection_id, exempt, expected_revision)` — the operator's
per-payment "needs no invoice" / "does need one after all", a lineage-scoped decision
stored on the projection with the projection's revision as its CAS (round-11 finding: the
precedence table named this fact and no tool stored it). Committing `exempt=true` moves any
active pairing on that lineage to `rejected` in the same transaction, and while it stands
the server refuses `record_match` and `propose_match` on the lineage — so an exemption and
an active pairing never coexist. Lifting it is `exempt=false` — refused unless an exemption stands — or an
operator-authored `record_match`, which appends `lift` then `pair` in one transaction. "Operator-authored" is a
`render_id` binding the server checks against the render log, not a word the caller
passes (§Match records, "Operator authorship is checked"). Counterparty-level and class-level expectations live in `set_expectation`, outside the
lineage's log; the reducer reads them at step 6.
Projection: `list_projections()` — every transaction lineage with its desired tag set and
snapshot, which is what the specialist reconciles against bank-feed;
`record_observation(projection_id, observed_tags, observed_snapshot, error)` — what it
saw, recorded without exempting the projection from later sweeps.
KB: `upsert_counterparty`.
Setup: `check_setup()` — what the pass can actually reach (bank-feed tools, bound
account, gmail tools, last sync), whether bank-feed reports `acct@<this version>` registered, which install backup its
registration minted, and the ledger's restore generation (§Setup, "Test install"), one
branch at the top of every pass;
`bind_account(account_id)` — records the business account and its ledger instance on
first run; `set_package_name(name)` — changes the zip filename prefix, which otherwise
defaults and is never asked about.
Review: `build_review(scope)` — `scope` is a quarter (default: the current one), a list of
quarters, or one lineage; it fixes the projection set the rendering covers and the set
the coverage dates are computed over, before any filtering or capping (§"The sweep"). It
renders the current view (missing first, then guessed) from store state and persists it
as an **unshown rendering**: an id, the moment, and the
revision of every item in it. `mark_rendering_delivered(render_id)` — called by Ellen
**after** the send succeeds — is what promotes it to shown and advances the
shown-revision pointers. Without that second call the rendering stays unshown, because
`send_message` returns its outcome to Ellen and cannot write to this store (round-7
finding): rendering alone could otherwise advance a pointer for a view that never
arrived, and a design that only advanced on delivery with no way to record delivery
would leave corrections re-rendering forever. Corrections go through the ordinary match tools with `expected_revision`;
Ellen resolves the operator's description to an item by reading `list_quarter_state`,
and an ambiguous description is a question, never a pick.
Ledger input: `import_ledger_export(path)` — takes (via `casa_handoff.capture`) the
file bank-feed's `export_history` published to the handoff folder. **It runs every pass,
not only at packaging** (round-12 finding, both reviewers): the imported file is the pass's
**bank snapshot**, the one complete read of the bound account this design has — admission,
fingerprint revalidation of every active match and every delivered ledger row, and the
package ledger all work from it. `export_history` writes every ledger column except the
raw provider payload (`state`, `superseded_by`, `booking_date`, `value_date`, `direction`,
`status`, `needs_review`, `review_reason` included) for every account and every state, as a
file rather than model context, so nothing is truncated; the specialist calls it (it is a
finance-role tool) and passes the path. At roughly half a kilobyte a row the handoff cap of
25 MB is decades of one account's history. The package's ledger lists the full quarter from
it (unmatched DBIT and CRDT rows included); the match records alone cannot produce it. **The import retains every row of the
bound account in every state; only the package ledger and the admission candidates
select ACTIVE rows** (round-13 finding: an earlier sentence had the import itself filter
to active, which discarded exactly the superseded and vanished rows that lineage
resolution and the delivered-row fingerprint sweep need). `export_history` runs `SELECT …
FROM transactions ORDER BY …` with no state predicate (verified 2026-09-21,
`tools_refresh.py:914`), so it returns superseded predecessors beside their successors:
selecting "every transaction booked in the quarter" from it without the active filter
turns one €99 payment that went pending → booked into €198 (round-5 finding). Superseded
and vanished observations are kept as history and disclosed in `notes.md` where they
explain something, never summed into the ledger.
Packaging: `build_quarterly_package(quarter)`, `stage_for_delivery(target)` (copies
an invoice PDF or the built package into casa's plugin outbox for `send_media`, or
publishes it to the handoff folder so gmail's `send_email` can attach it **to the
operator's own mailbox**). **Operator ruling, 2026-09-22: emailing the package or an
invoice to themselves is on the free rung** — it never leaves their hands, so it is as
retractable as the Telegram copy — and "email me the Q3 package" is a supported ask.
Mailing anyone else stays on the gated rung and out of v1 (§"The reversibility ladder").
**Who checks the recipient: Casa, through the operator's own tap** (round 23, both
reviewers, verified against gmail 0.9.0 and Casa 0.328.0). gmail declares `send_email` a
protected tool, so every call needs a single-use approval that Casa hashes over the exact
arguments — the recipient included — and shows as *"Send email to <address>: <subject>"*;
an approval cannot be reused for a different recipient, and an unapproved call has no
grant. So the recipient rule is not skill guidance after all: this plugin's server still
cannot see who an email goes to, but the operator sees it on every send and nothing sends
without them. Two consequences, stated: **the cost is one tap per email**, which is Casa's
gate and not this plugin's, so the ruling's "free rung" is exactly true of the plugin and
one tap short of true end to end; and a tap-free self-mail would need a recipient
exemption in gmail or Casa, which is not asked for and not assumed.

House disciplines copied from bank-feed: explicit loud failures, numeric caps and
truncation notices on reads, provider text fenced as untrusted on output, three-way
tool-list agreement (server, `provides_tools`, role allow-lists) with a CI check.

## Mirroring decisions into bank-feed — one projection per transaction lineage

**This mechanism was rebuilt in round 8 after producing four reproduced failures across
three rounds.** Both reviewers had recommended cutting it; the deep reviewer withdrew
that recommendation once asked to generalize rather than to sharpen or remove. What
follows is the generalization, and it is smaller than the patchwork it replaces.

### Why the four failures were one failure

A stale writer reasserting a rejected pairing; a sweep whose selection excluded clean
`rejected` records; the same records escaping successor re-resolution; and per-match
desired values fighting over one transaction's tags. One sentence covers all four:

> **The old design treated annotation as a completed side effect of a match, when the
> thing being maintained is shared transaction state whose writers and whose bank-row
> identity both outlive that match.**

Four dimensions of one ownership mismatch — time, coverage, identity, and competing
claims — which is why patching a selection rule fixed one and exposed the next.

### The missing property

**One authoritative desired value per external mutation domain, with responsibility
retained for that domain's whole lifetime.** That gives a unique repair target and total
coverage. It does **not** make stale external writes impossible, and the distinction is
the part this document previously got wrong:

| Claim | Status |
|---|---|
| Unique, complete reconciliation of what we assert | **Achievable locally**, and this design achieves it. |
| The bank ledger never shows an obsolete assertion | **Impossible on this platform.** `tag_transaction` has no fingerprint, revision or idempotency precondition; the writer is an ephemeral specialist session that can pause indefinitely; two passes can overlap. No placement of a local check closes the window. |

Claiming the first as a solution to the second is exactly the error of rounds 5 and 6.
What is offered instead, stated as the guarantee with the qualifiers round 9 proved are
required: **once decisions, row identity and stale writes stop changing, the next
complete successful reconciliation restores the current projection — provided the row has
`acct::` capacity and the traversal completes.** Each qualifier is a reproduced failure,
not a hedge:

- **Capacity.** `tag_transaction` is all-or-nothing against per-row budgets, and two of
  them bind an `acct::` write (bank-feed 0.9.0, `rules.cap_problem`): the namespace's own
  16 per row, which classification tags cannot consume, **and a shared cap of 64
  namespaced tags per row across every owner, which other workflows can exhaust** — four
  namespaces at 16 each refuse the first `acct::open` with "would carry 65 namespaced
  tags, past the cap of 64" (round-10, reproduced by both reviewers). Our whole
  vocabulary is five names, so the first budget is only filled by hand-written `acct::`
  tags outside it (foreign, never removed); the second is out of our hands. Either
  refusal makes the row **reported as unprojectable**, not retried forever.
- **Renames — closed by bank-feed 0.9.0.** `rename_tag` renamed globally with no record
  of origin, so renaming an accounting tag moved the assertion outside our vocabulary
  and it survived every later reconciliation, including across a rejection. bank-feed
  now refuses any rename with a namespaced tag on either side, so an `acct::` assertion
  can only be written, removed (`untag_transaction`, `delete_tag`) or carried by
  supersession — each of which the reducer's fixed point already absorbs. An operator's
  `delete_tag` of an owned tag is repaired by the next sweep, like any removal.
- **Completeness.** A complete traversal is defined over a full **cycle**, not one
  session. `get_transaction` reads one row per call and the specialist's ceiling is 70
  turns, so with a few hundred retained projections one session cannot finish. The
  traversal therefore carries a **durable cursor** and resumes across passes; "the next
  complete reconciliation" means the next completed cycle.

### The projection

One object, owned by the plugin server, per **transaction lineage**:

- **Identity is the lineage, not the row.** Logically `(bank-feed instance, account,
  supersession lineage)`, implemented as a local projection id plus retained row-id
  aliases. **The live row id is an address, not the identity** — which is what the old
  design had backwards, and why a superseded row took its record out of every worklist.
  Lineage is established only by bank-feed's explicit `superseded_by` links, never by
  resemblance.
- **It holds** its row aliases and current destination; **one** desired owned-tag set;
  **one** self-contained annotation snapshot (accounting status, current document
  reference, projection revision); and its last observation plus last delivery error as
  diagnostics.
- **The server computes it from every current decision and classification fact about
  that transaction**, in the same local transaction as the decision change. Not per
  match: a rejected match contributes no accepted relationship, and issues no
  instruction to erase an accepted replacement's tags. That is finding 4, closed by
  construction.
- **Admission and eligibility are two different things** (round-11 finding, both
  reviewers). **Eligibility** is a predicate on a row's *current* facts: on the bound
  account — either direction (operator, 2026-09-22; round 10 had admitted DBIT only) —
  and dated on or after the **watermark** — by `booking_date`, or by `value_date` while
  the row is still pending and has none. **Admission** is the
  moment a lineage gets a projection: every pass takes the **bank snapshot** (§Tool
  surface, `import_ledger_export` — bank-feed's `export_history` through the handoff
  folder, every row of every state with `value_date`, `state` and `superseded_by`) and
  admits **every eligible row in it that has no projection yet**. Round 12 reproduced why
  the snapshot is the only read that works: `list_transactions` truncates at 200 rows,
  has no cursor outside the classifier-queue mode, and silently omits a pending row whose
  `booking_date` is NULL — so "read the rows in bulk" through it would skip exactly the
  pending payments that need admitting on their `value_date`. A pass whose snapshot import
  failed or is absent reports admission and revalidation as **not checked**, never as
  complete — never "every row we have not seen", because bank-feed corrects
  rows in place under the same `row_id` (a June 30 booking becomes July 1), and a row
  observed once and skipped must be admitted the pass it becomes eligible. Once admitted, a projection **persists for the lineage** through
  supersession, vanishing, rejection and every later correction — retention is what
  keeps a stale write repairable. But **the desired set is computed against current
  eligibility**: a managed lineage whose destination row is no longer eligible — a booking date
  corrected to before the watermark — desires the **empty set**, so the next sweep
  removes its owned tags and the row is reported as `ineligible`, still enumerated. (Round
  11 reproduced this against bank-feed's real `apply_plan` with a DBIT→CRDT correction
  keeping its `row_id` and its `acct::open`, back when incoming rows were out of scope; a
  direction flip is now an ordinary fingerprint change, and the expectation is re-derived
  from the row's classification like any other fact.)

  | Transition (real bank-feed behaviour) | Projection | Desired set |
  |---|---|---|
  | eligible row first seen, no projection | admitted | per the table below |
  | ineligible row first seen (before watermark, other account) | none | — |
  | pending row books: supersession to a booked successor | follows the lineage (alias) | recomputed on the successor's facts |
  | in-place correction makes an unmanaged row eligible | admitted this pass | per the table |
  | in-place correction makes a managed row ineligible | retained, marked `ineligible` | `∅` — owned tags removed |
  | correction makes it eligible again | same projection | per the table |
  | watermark moved earlier | previously ineligible rows admitted next pass | per the table |
  | watermark moved later | **not offered** in v1 | — |
  | destination row tombstoned (`state='vanished'`) | retained, marked `ended: vanished`; pairings retired, documents freed | `∅` — owned tags removed |
  | destination row erased (`purge`, `forget_local_account`, `delete_all_data`) | retained, marked `ended: erased`; pairings retired, documents freed; never read again | `∅` — nothing left to write |
  | the payment comes back (bank re-sync after an erasure, or a reappearance after a tombstone) | a **new** lineage, admitted like any other; nothing carried across | per the table |
  | `purge(user_work=erase)` strips the tags of a row it keeps | same projection; classification unknown until the classifier re-tags it | per the table — the sweep re-adds owned tags and restates the accounting note |

  Both directions are eligible (operator, 2026-09-22). The watermark is stored with the binding and
  defaults to **the first day of the quarter in which the account was bound** (operator
  ruling, 2026-09-22) — the quarter the operator installed the plugin to get done, and
  nothing older, which is what §Non-goals' "no bootstrap over historical quarters" means
  in row terms. The first view
  says so in one line (`Starting from Q3 2026 — say "start from Q2" to go further back`).
  Without a boundary at all, "whatever bank-feed has that we have not seen" admitted two
  years of history and wrote `acct::open` across it on install day (round-10 finding).
- **"Managed" includes transactions with no match at all** — one still awaiting an
  invoice has a projection, because coverage must not depend on a match existing.
  **Its desired EXTERNAL tag set was empty in v1** (round-9 finding, reproduced
  independently by both reviewers): writing `acct-open` on an unclassified row removed
  it from bank-feed's only classifier queue, because `classification_state` and the
  queue predicate treated every non-workflow tag as content classification. Ordering
  classification first is **not** an adequate fix, since it does not cover overlapping
  passes or deferred rows.
  [casa-specialist-finance#31](https://github.com/bonzanni/casa-specialist-finance/issues/31)
  closed that in bank-feed 0.9.0 (see the bank-feed floor above): `acct::open` leaves the
  row in the queue. **Restored (operator, 2026-09-22):** a managed transaction with no
  accepted or proposed pairing — and no exemption — desires `{acct::open}`. That is the
  concrete goal the mirroring exists for — the operator's own ledger shows which payments
  still lack an invoice, filterable by tag, without asking the plugin anything.
- **The desired set is one table, with precedence**, computed per lineage from its
  current decisions and facts. Exactly one *status* tag is ever desired; `acct::portal`
  is a channel fact and rides alongside whichever status applies. Round 10 found that
  the one-line rule above, read literally, overwrote an operator's "needs no invoice"
  with `acct::open` — an exempt payment has no pairing either — so precedence is
  written down:

  This table is **derived from the reducer in §Match records** ("The reducer, as one total
  function") and is illustrative; where they could ever differ, the reducer's order wins.

  | Lineage state (first row that applies) | Desired set |
  |---|---|
  | lineage ended — destination row `vanished` or erased | `∅` |
  | destination row not currently eligible (corrected to before the watermark) | `∅` |
  | exempt by the operator (`set_exemption`) — structurally, no active pairing exists while it stands | `{acct::no-document-expected}` |
  | an active `matched` pairing whose fingerprint still holds | `{acct::matched}` |
  | an active `proposed` pairing, or a `matched` one whose fingerprint no longer holds | `{acct::proposed}` |
  | nothing paired and the expectation is `none` | `{acct::no-document-expected}` |
  | nothing paired and the expectation is `optional` | `∅` |
  | otherwise — expectation `required` or unknown: never paired, every pairing rejected, `conflicted` only, accepted-missing after "stop chasing", not yet classified | `{acct::open}` |
  | plus, whenever the counterparty's source is `portal` and the row is eligible | `∪ {acct::portal}` |

  Round 11 found the first version of this table let an active `proposed` — an
  unresolved machine proposition — outrank the operator's exemption, and an existing
  proposal survive the operator exempting that payment. So the operator's exemption is
  not merely first in the table: committing it rejects any active pairing in the same
  transaction and the server refuses new auto pairings while it stands (§Tool surface,
  `set_exemption`), which makes the row above it unreachable rather than merely ordered.
  An expectation of `none` is weaker on purpose — it is a default about a counterparty
  or a class, and a document that does match a specific payment beats it; the operator's word about one
  payment beats everything. Accepted-missing stays `acct::open` on purpose: "stop
  chasing" rations search effort, it does not change the fact that no invoice exists. A
  match landing swaps one status for another in a single reduction, so `acct::open` and
  `acct::matched` can never be desired together.
- **`owned_tags` is a fixed, reserved vocabulary inside the `acct::` namespace**, not a
  prefix rule: exactly `acct::matched`, `acct::proposed`, `acct::portal`,
  `acct::no-document-expected`, `acct::open`.
  The namespace is what tells bank-feed these are not classifications; the fixed list is
  what tells the sweep what it may remove. Anything else — an `acct::`-namespaced tag the
  operator added by hand, or an un-namespaced `acct-matched` — is foreign and is never
  removed. A prefix reading would have the sweep silently deleting the operator's own
  tags, which is the failure this whole mechanism exists to prevent.
- **Registered before its first external write**, retained after rejection, and retained
  after its tags are observed absent.
- **Fan-in merges.** Two projections that resolve to one successor merge their aliases
  and their logs — writer entries and the store's recorded retirements alike — and the
  merged match-record state is **recomputed by folding that union in sequence order from
  the empty state** (§Match records, step 2's transition table), in one transaction with
  the projection recompute; retirements the merged fold newly causes are appended in the
  same transaction. Because recorded retirements are part of the fold, nothing a previous
  merge retired comes back (round-19 S1), and because the fold is over the whole recorded
  history, the result is reproducible from the log. It is **not** independent of the order
  in which bank-feed revealed the lineage — that is history, and the design says so in
  §Match records rather than promising otherwise.

**Invariant:** every managed lineage has exactly one current desired annotation, and
every row its annotations can migrate to is reachable from that projection.

### The sweep

Unconditional enumeration replaces every selection rule:

1. Enumerate **all** projections — no filter on match state, annotation state or
   delivery state.
2. Resolve destinations, merge collisions, revalidate transaction facts. **The sweep
   does not judge ends** — that happens at the snapshot import (§Match records, "A
   lineage can end") — it only carries out an ended lineage's `∅`: owned tags removed
   from a vanished row, nothing read or written for an erased one.
3. Read the current tags and notes; record the row's **classification observation**
   and re-derive its expectation (§"Document expectation") — for every projection, paired
   or not, since a changed expectation kind invalidates a pairing (§Match records); then
   take the server's freshly computed desired value. **Since issue #1 the import does this
   for every row the export carries** (its tags are in the export), and it settles, without
   a read, every lineage whose exported tags already equal its desired set and whose
   accounting note is known visible: a read confirms a note, and the confirmation stands
   while the row's `tag_revision` is unchanged and the read's import came more than a
   delegation's ceiling after the last `add_note` the plugin handed out (rounds D1–D2).
   Residuals (operator ruling 2026-09-28, round C1): a note change moves no tag revision,
   so (1) an erasure that strips a note from a row carrying no tags at all, and (2) twenty
   or more newer notes appended to one row (`get_transaction` shows the newest 20), leave
   the accounting note unseen until the lineage's note next changes. Both close when
   bank-feed exports a per-row note revision
   ([casa-specialist-finance#89](https://github.com/bonzanni/casa-specialist-finance/issues/89)).
4. Remove owned tags outside the desired set; add missing desired tags. The fixed point
   is `actual := (actual − owned_tags) ∪ desired`, reached from any starting state.
5. Append a current snapshot when the visible accounting note is missing or differs.
6. Read back and record what was observed. **An observation never exempts a projection
   from future sweeps** — that exemption is what let a stale writer escape in round 7. (Since
   issue #1 every import re-judges every exported row from its tags; only the read is
   skipped where nothing is owed.)

The enumeration is **resumable**: it advances a durable cursor and picks up where the
last session stopped, so a cycle spans as many passes as it needs. The cursor orders
projections by id, not by date, and says nothing about coverage on its own (round-27,
both reviewers): **the classification coverage of a view is the oldest successful
classification-observation timestamp among the lineages in that view's scope** — the
observation is stamped on each projection when the sweep reads its row — so a scope whose
every row was observed this cycle reads as fresh, and one row the cycle has not reached
yet drags the date back to when it was last seen, which is the honest number. Every view
prints it beside the bank coverage (`bank checked through 20 Sep · classification through
13 Sep`); the second date is the one that says how stale the expectation kinds behind
"what am I missing" may be. **Membership is defined per view, and fixed before any
expectation filtering or display cap** (round-28 finding, both reviewers: a stale
`none` lineage is exactly the one a missing-list filter would drop, and it is exactly
the one whose staleness may be hiding an errand): the status view, "what am I missing",
"anything I should check" and a quarter view take **every managed lineage from the
watermark through the latest quarter the view shows, whatever its expectation, tier or
state, printed or not** — except an **ended** lineage, which has no row left to classify
and would otherwise hold the classification date at its last observation for ever; it is
reported once, by the pass that ends it (§Match records, "A lineage can end") — by effective date, which is `booking_date`, or `value_date`
while the row is pending and has none, the same date admission uses (round-29 finding,
both reviewers: "booked in the quarter" dropped every pending row from every scope, and
"the older open items the view lists" let the display decide membership, so an older
lineage cached as `none` that the classifier had since re-tagged to `invoice, required`
sat outside the scope precisely while its staleness hid an errand). Those views reach
every earlier quarter — they list older open items from any of them — so their coverage
must answer for every earlier lineage too. `build_review(scope)` resolves that
membership set first, uncapped and unfiltered, persists it with the rendering, and only
then filters and caps what it prints; a one-item question takes that lineage; a package
takes its quarter's lineages by the same effective date. A member admitted and never observed has no
timestamp: it does not move the date, and the line says so in a count (`classification
through 13 Sep · 2 never checked`), so a fresh admission can never read as fresh
classification; a scope with no lineages at all prints `no transactions yet` and no
dates. Otherwise both dates are always printed; neither is ever printed alone. A projection whose
repair is refused — tag cap reached, a persistent API failure — is recorded with the
refusal and surfaced, never silently retried into an infinite loop.

### Notes are versioned assertions, not a field

An append-only store cannot converge to a value, so notes are not treated as one. Each
snapshot reads *"Accounting revision N: …"*, carries the document reference, and
explicitly supersedes earlier accounting assertions. Revisions come from one store-wide
sequence so they compare across merges; **a lower revision appended late is historical,
not current**, and a later sweep restates the current snapshot.

### What this replaces

| Current piece | Disposition |
|---|---|
| Per-match `annotation_state` (`pending`/`clean`/`repair_owed`) | **Removed.** A match cannot attest that shared bank state is clean. |
| Two-phase protocol and `mark_annotated` | **Removed as a protocol.** Desired state then reconciliation; an acknowledgement is only an observation. |
| The concrete repair queue | **Removed.** Differences are recomputed from current desired versus actual; stored cleanup instructions are never replayed. |
| Sweep selection rules | **Replaced** by unconditional enumeration. |
| Reconciliation-set membership rules | **Absorbed** into projection existence. |
| Successor re-resolution | **Survives**, once per lineage, independent of match state. |
| The generation marker | **Survives** for local writes and duplicate-work avoidance. It fences nothing external, and no longer claims to. |
| Match CAS and fingerprint revalidation | **Survive**, as accounting-correctness rules, separate from annotation delivery. |

Several overlapping state machines and worklists become one registry, one reducer and
one loop.

### What it still does not close, stated plainly

- **A stale writer still wins temporarily.** It can land after resolution, after
  cleanup, or after a successful readback. The guarantee above is the honest one.
- **Wrong note text is permanent.** Revisions make duplicates interpretable, not
  preventable: two specialists can both observe a missing snapshot and append it, and a
  crash after an append leaves the same ambiguity.
- **Absence from a note read is not proof of non-delivery.** `get_transaction` still
  returns only the newest 20 notes (`ORDER BY note_id DESC LIMIT 20`). Restating the
  current snapshot preserves visibility at the cost of more duplicates. Since bank-feed
  0.10.1 the reader is at least told how to interpret what it sees: the journal header
  reads *"where they conflict, the latest reflects the outcome"* and survives the cap,
  and a note-search hit says how many newer notes follow it on that row. That matches
  this design's own convention — tags carry current state, notes carry the story — and
  it helps a human reading the ledger; it does not change what this plugin may conclude
  from a read, so it is not part of the floor.
- **Projections accumulate.** Every sweep revisits every one, and safe retirement is
  unavailable without evidence that old writers cannot return. Tag caps or a persistently
  failing API can block repair, which is then reported rather than absorbed.
- **Identity tracking is real work.** Successor chains, merges, vanished rows and changed
  fingerprints still need handling; this centralises those obligations rather than
  erasing them.
- **A payment that leaves the ledger and comes back is two lineages.** bank-feed links a
  tombstoned or erased row to nothing, and this design never links by resemblance, so the
  operator's confirmation on the old row does not carry to the new one: the freed document
  is matched afresh and shown like any machine pick. The cost is one more line on a sheet
  after a rare, operator-initiated event; the alternative — re-attaching by content — is
  the resemblance rule this design refuses everywhere else, and bank-feed's own attempt at
  re-attaching annotations across an erasure was cut after the same finding twice
  (component 0.13.0).
- ~~**A lineage can be broken upstream.**~~ **Closed upstream, bank-feed 0.8.1**
  ([casa-specialist-finance#30](https://github.com/bonzanni/casa-specialist-finance/issues/30)).
  Two overlapping syncs could supersede one predecessor in turn, the second overwriting
  `superseded_by` and stranding the first successor's migrated tags and notes where no
  lineage walk reached them — not closable from this side, since the projection cannot
  hold an alias for a successor nobody observed. bank-feed now supersedes only a row
  that is still `state='active'` with `superseded_by IS NULL`; the losing run raises
  `StalePlan`, rolls back whole, and `sync` reports it `FAILED` with the ledger unchanged
  (verified 2026-09-22, `apply.py`). Lineage edges are now written once, which is the
  property the projection's alias model assumed.


## Setup (install day, once)

**The whole of it is one sentence to the configurator and one trigger. It asks the
operator nothing** (operator ruling, 2026-09-21: never block setup on a choice that has
a reasonable default; default it, say what was defaulted, and let it be changed later).
Everything else the plugin works out for itself. Verified against casa v0.323.0's own
install path (`recipes/plugin/add.md`, `plugin_add`, `agent.py:2664`).

**Prerequisites — things that must already be true, and are not this plugin's job:**
bank-feed installed on the finance specialist with the business account linked and
synced; the gmail plugin on Ellen; the finance specialist wired as Ellen's delegate.
If they are not, the plugin says so on its first pass rather than producing an empty
sheet (see the self-check below).

**Step 1 — one sentence.** "Install the quarterly accounting plugin from
`<owner>/casa-plugin-quarterly-accounting`, for Ellen and the finance specialist." The
configurator calls
`plugin_add(name="quarterly-accounting", repo=…, ref="latest", targets=["resident:<ellen>", "specialist:finance"])`,
which publishes the artifact, assigns it to both targets, reloads and verifies in one
call. **Its tools are granted by that assignment** — "installed ⇒ granted, by
construction" (`agent.py:2664-2668`: every server-level plugin grant is appended to the
agent's allowed tools). No role file is edited, by anyone.

**Step 2 — one trigger on Ellen**, created through the trigger recipe. This spec ships
the exact prompt text so nobody composes it at install time, because the closing clause
is what stops a pass delivering twice (ha-casa-app#960/#932):

```
name:     quarterly_accounting_pass
type:     cron        schedule: 0 9 * * 1        channel: telegram
prompt:   Run the quarterly-accounting background pass. It covers every
          open item, not just the current quarter. If it reports
          something that needs me, send me that
          and nothing else; then output the sentinel `<silent/>`. If it
          reports nothing, output `<silent/>` and nothing else.
```

**One trigger, not two.** There is no quarter-end trigger: packaging happens only when
the operator asks (§Packaging). An install that creates a second cron reintroduces
automatic delivery of a package nobody reviewed, which is the behaviour the operator
removed.

The weekly day and time are the operator's preference; Monday 09:00 is the proposal.

**That is the end of install.** No questions, no account selection, no vendor list, no
KB seeding, no category setup, no historical import.

**The plugin declares no required environment variables at all.** The package-name
setting that v1 wired through `CASA_PLUGIN_QA_COMPANY_SLUG` is now stored in the data
dir with a default (below), which is both better UX — the operator can change it by
saying so, instead of needing a configurator edit — and the cleanest possible answer to
ha-casa-app#1024: a plugin with no required variables triggers no install-time vault
exploration whatsoever.

### What the plugin works out for itself

**Which bank account — defaulted when it can be, asked only when it genuinely cannot.**
The first pass asks bank-feed for its accounts. bank-feed categorises every account
`personal` or `company` (`rules.py:44`, written by `label_account`), so:

| What bank-feed shows | What happens |
|---|---|
| Exactly one `company` account | **Bound, silently.** The first sheet's scope line says which: `Bound to <account label> · first review`. No question. |
| Several `company` accounts | The first sheet's first line asks which, answered in the same free-text reply as anything else. |
| No `company` account | The sheet asks, listing what bank-feed does have, and mentions that `label_account` is how an account becomes a company one. |

The binding records both the account and the ledger instance, so a recreated bank
database cannot silently inherit the old row handles — and the **watermark**, the first
day of the quarter the account was bound in, before which no row is managed (§"The
projection", admission). The first view says which quarter it starts from and how to move
it; nothing is asked.

**What the zip files are called — defaulted, never asked.** The package name defaults to
a slug of the bound account's label, or to `books` when that yields nothing usable, and
it is stored (changeable) rather than configured. The first package says so in one line:
`Files are named "books-2026-Q3-partial-2026-08-14.zip" — say "call the zips <name>" to change that.`
Said once, on the first package only. A default nobody minds costs one line; a question
at install costs a decision at the worst possible moment, when the operator wants the
thing installed and has no opinion yet.

**No `casa.setupTool` is declared, deliberately.** Casa would run it automatically after
a consent round, which sounds like the house pattern — but the only thing this plugin
needs at first use is a choice between accounts, and a setup tool cannot ask the
operator anything. Declaring one would buy nothing and would make three open casa
defects reachable (#1012 setup obligations consumed on tool availability, #1005 consent
re-arming, #1014 lost retirement notes), none of which can touch a plugin that declares
no setup tool and no credentials. The first-run binding above covers the same ground in
the surface the operator is already reading.

**The uninstall eraser (added 2026-09-27, operator ruling).** From Casa v0.329.0 a plugin
may declare `casa.eraseTool`. Uninstalling it then asks Keep data / Erase everything /
Cancel, and "Erase everything" runs the eraser and removes the plugin only on `complete`.
This plugin declares `reset_store` as its eraser; it declares no data-only eraser, because
it holds no sign-ins.
- **What it erases:** the whole store, the documents and the packages, then the freed pages
  (VACUUM, WAL truncate).
- **What its report names as not erased:** the `acct::` tags and accounting notes in
  bank-feed's ledger (restoring bank-feed's install backup removes them), Home Assistant
  backups, and the handoff and outbox copies, which Casa removes on its own schedule.
- An older Casa ignores the declaration, so the casa floor does not move.

**No trigger or callback consent round.** The plugin declares no triggers of its own —
the one above lives on Ellen's `triggers.yaml` — and no callbacks, so there is no consent
verdict for its setup to wait on.

### Health is observed, never inferred

**The server cannot check this on its own** (round-5 finding). It is deterministic local
custody: it cannot call bank-feed, cannot see whether Gmail is still authorised, and
cannot know which tools are reachable from Ellen or from the specialist. A
`check_setup()` that "reads state it already has" would answer today's question with
yesterday's inputs — after Gmail loses authorisation, a local-only check has exactly the
inputs it had while everything worked, and would report health.

So **each pass performs the real probes** — a bank-feed read, a Gmail read, the bound
account's existence — and records each result with the time it was observed.
`check_setup()` then reports observations **as observations**, timestamped, and
distinguishes "healthy when last checked, 9 days ago" from "healthy now". The operator's
coverage line carries the same distinction, which is what makes it a usable health
signal rather than a reassuring one.

### The self-check, so a mis-wired install is never silent

**The failure this closes:** a skipped or half-finished install produces a pass that
finds nothing, and "nothing to report" is indistinguishable from "everything is fine"
(UX round finding). So every pass begins by checking what it can actually reach, and a
pass that cannot work says so in place of a sheet:

```
Not set up yet.
No bank account is bound, and I can't
see bank-feed's tools from here.
Check that bank-feed is installed on
the finance specialist.
Nothing else to do until then.
```

The conditions it distinguishes, each with its own sentence: bank-feed tools not
reachable · no account bound and none offered · the bound account gone from bank-feed ·
gmail tools not reachable (matching still runs on documents already held; the sheet says
searching is off) · bank-feed reachable but never synced. **Nothing in that list is a
missing setting**, because there are no settings to miss: everything the plugin needs is
either defaulted or asked in the sheet, so the self-check only ever reports things
outside the plugin that are genuinely broken.

This is one `check_setup` tool reading state the server already has, and one branch at
the top of the pass. It is the difference between an operator who fixes a wiring mistake
in week one and an operator who concludes after a month that the plugin does nothing.

### Test install, reset, and the install backup

**Operator ruling, 2026-09-22.** The first versions of this plugin will need debugging end
to end — a real mailbox, a real, populated, classified ledger — and that means running on
production, because the test deployment holds no real banking data. What a run changes in
bank-feed is annotations, not the bank's records: the `acct::` tags come off (bank-feed's
`untag_transaction` and `delete_tag` remove namespaced tags) but **the note journal is
append-only by bank-feed's design and has no delete**, so a test run's accounting notes
would otherwise stay in the ledger forever. And this plugin's own store survives an
uninstall (Casa keeps `$CLAUDE_PLUGIN_DATA` on purpose), so reinstalling is not a fresh
start. The operator's answer is to make **the ledger itself the thing that resets**, with a
backup facility bank-feed should have anyway.

**Where the restore point is minted decides everything** (round 31, both reviewers). A
first draft had this plugin take the backup and refuse to write until it had; both reviewers
broke it the same way, because the plugin's server never makes the bank-feed writes — the
specialist does, with bank-feed's own tools — so the "gate" was skill guidance, and two
overlapping passes could each take a backup in good faith and leave a note between them.
So **the store being written mints its own restore point, on the first write, inside the
same lock as that write.** Every `acct::` tag write and every accounting note this plugin
causes carries a **workflow string**, `acct@<plugin version>`; bank-feed, on the first
write carrying a workflow string it has not registered, takes the backup **before applying
that write, under the write lock the write itself holds** — the copy is made through a
separate read connection while the writer holds `BEGIN IMMEDIATE`, since a same-connection
`VACUUM INTO` or backup call fails inside an open transaction (round 32, reproduced on
bank-feed's real `open_db`) — registers the string in the ledger, and only then commits.
**A backup failure aborts the write**: disk full during the copy rolls back the
registration and the annotation together, and the tool reports it; a write never lands
without its restore point. Two passes serialize on that lock; the loser finds the string
registered and writes without a second backup. The restore point therefore precedes the
first write of every plugin version by construction, and nothing in this plugin has to
remember to do anything. The fence is mechanical for tags (a namespaced tag without a
workflow string is refused, since a namespaced tag is by definition another workflow's) and
for every note written as the skill says; **a note the specialist writes without the
workflow string is indistinguishable from any other agent note and is the one residual**,
stated as such — its cost is one note surviving a restore.

**Bank-feed gains backups** (§"Changes in other repos"; upstream, not this plugin's code):
`backup(reason)`, `list_backups`, `restore_backup(id)`, and the first-write mint above. A
backup is a consistent copy of the whole ledger taken under `BEGIN IMMEDIATE` — bank-feed
runs in write-ahead mode, and its existing pre-migration snapshot (`VACUUM INTO`) shows the
consistent copy is available, but this is a **new subsystem**, not that routine: it adds a
reason and a timestamp, retention, a durable event index kept **beside** the database
rather than in it (so a restore cannot erase the record of itself, and the restore
generation stays monotonic across rollbacks), and a restore protocol. **The index and the
in-ledger registration need a crash protocol between them** (round-32 finding): the index
is an append-only file, each line fsynced, and every operation writes a `pending` line
before it acts and a `committed` line after. A **mint** copies to a temporary file and
fsyncs it, appends `mint <id> pending`, renames the copy into place **and fsyncs the containing
directory** (round-34 finding: fsyncing the file does not persist the rename, and a power
loss after the ledger commit would leave a registration with no restore point), then
registers and commits the write in the ledger transaction, then appends `mint <id>
committed`. A
**restore** — every restore is its own operation with its own id, distinct from the id of
the backup it restores, since the same backup can be restored twice (round-34 finding: a
marker holding the backup id was compared with the operation id and a committed restore
was recorded as aborted) — appends `restore <op> of <backup> pending`, performs the
transactional replacement, which also writes **the restore operation id** into a
`ledger_meta` row **inside that same transaction, after the rows**, then appends
`restore <op> committed`. Recovery runs at every `open_db`, **at the start of every mint, every restore and every
annotation write that carries a workflow string, before the operation touches anything,
and before `list_backups` answers** (round-39 finding: with settlement required before
mints, restores and listings but not before an ordinary write, a restore that committed
and died left an already-open process checking its write against the stale generation) — the
generation and the registration state that answer carries are what this plugin's
freshness and restore checks read, and round 36 reproduced an already-open process
answering with the pre-restore generation while the ledger already held the restored
rows, so a pass with a populated store went on annotating against stale row ids — an already-open process has no other
occasion to learn of a restore another process left unsettled, and round 35 reproduced a
second restore committing over a first one's marker before the first was settled, so the
first was recorded aborted and the generation advanced by one for two restores. It runs
**under the ledger's writer lock and an exclusive lock on the index file**; a mint or
restore keeps the writer lock from that settlement through its own commit, and
`list_backups` keeps both locks from settlement **through capturing the generation and
registration state it returns** (round-37 finding: releasing them in between let a restore
land and die after commit before the answer was assembled, and the answer carried the old
generation) (round-33
finding: two processes recovering at once each appended `committed` for one restore and
counted it twice), and **each operation id gets exactly one terminal record** — a second
process finding the terminal line already present appends nothing. It walks the index: a
`mint pending` whose final backup file is present but has no matching registration in the
ledger is an orphan backup — kept, since it is consistent, relabelled `orphan`; a `mint
pending` whose final file is absent (the crash came before the rename) has its temporary
file removed and `aborted` appended, and the next write mints afresh; a `mint pending`
whose final file is present **and** whose registration is in the ledger (the crash came
after the commit, before the terminal line) gets its `committed` line appended and
nothing else (round-34 finding: this third state was unlisted); a registration whose
backup file is missing fails that workflow's writes closed until the operator restores or
re-mints; a `restore pending` with no terminal line is settled by the ledger's own marker —
marker equal to that restore's **operation id** means the replacement committed, and
recovery appends `committed`; otherwise it appends `aborted`. **The restore generation is the number of distinct restore
ids with a `committed` line**, so it is monotonic by construction and never read from the
ledger. Reasons: `weekly`
(taken from the finance pass, since neither plugin can schedule), `install:<workflow
string>` (the mint above), `manual`. **Every `backup(reason)`, weekly or manual, follows
the mint's index protocol** — pending line, copy, rename, directory fsync, terminal line —
and holds both locks from settlement through its commit (round-38 finding: only the mint
and the restore had been given the protocol). Retention: a bounded number of weekly ones, every
install one until the operator deletes it. `restore_backup` is a protected tool — Casa
asks the operator for one tap naming the backup — performed **in place, as ordinary SQL
inside one `BEGIN IMMEDIATE`**: the backup file is attached, every **ordinary** table's rows are
replaced from it, then `sqlite_sequence` last, so row ids resume from the backup's counter
and are not reallocated (round 33) — but **not the full-text index**: bank-feed's
`notes_fts` and its shadow tables cannot take a row-by-row replacement (round-34 finding:
"database disk image is malformed" on the real schema), so they are excluded and the
index is rebuilt from the restored notes inside the same transaction — and the transaction
commits; the consent preflight below runs inside the
same transaction, so no other specialist's write can land between the check and the
replacement, and other specialist server processes on the same file (Casa spawns one per
session) see the restored rows at their next transaction rather than writing into a
replaced file. Round 32 reproduced why it is not SQLite's backup API: that API refuses a
destination that holds an open transaction ("destination database is in use"), and
releasing the lock to use it would separate the preflight from the replacement. **Consent bindings are never taken from a backup** (rounds 31–32): a consent is a fact
about the bank, not about the ledger's history, and a consent that was revoked at the bank
cannot be brought back by restoring its rows. So the replacement keeps the live `sessions`
rows and every live account's current `session_id` and provider `uid`, and restores
everything else — transactions, tags, notes, rules, registrations — from the backup. An
account present in the backup but no longer linked live is restored with its rows and
flagged `needs-relink`; an account linked live but absent from the backup keeps its live
binding and an empty history the next sync refills. The protected approval shows the
backup's id, time and reason and the line "consent bindings: kept live (N accounts)". A restore also unregisters every workflow string whose install
backup is at or after the restored point, since those writes are gone.

**"Fresh" is a fact bank-feed reports, not a claim this plugin makes.** `check_setup`
reports, from the specialist's read of `list_backups`, whether `acct@<this version>` is
registered and which backup its registration minted. A **fresh** install is one where it is
not registered: the first write will mint. A pass that finds the string registered while
the plugin's store is empty (the operator reset the store without restoring, or restored a
backup taken after the first write) **refuses every bank-feed write and says so**: "the
ledger still carries writes from acct@1.2.0 after its restore point — restore backup
<id> first". Round 31 showed why a restore *count* is not enough: a weekly backup taken
after the test's first note, restored, advances the count and keeps the note; the
registration travels with the writes, so it is present in exactly the backups that are not
clean.

**The reset loop, in the order that works.** The plugin stays installed throughout — an
uninstalled plugin's tools are gone (round-31 finding), and Casa keeps its store anyway.
(1) Quiesce: no pass running, `/new` on both agents. (2) Ask the finance specialist to
restore the install backup; the tap confirms which; bank-feed reports the restore and the
unregistered workflow. (3) `reset_store` (one Casa tap) wipes `$CLAUDE_PLUGIN_DATA` to the fresh-install
state; it has no precondition it could not check, and it is safe to call at the wrong time
only because the next pass will refuse to write until the ledger is clean. (4) Upgrade the
plugin if the fix needs it (`plugin_update`), otherwise just run the pass: the first write
mints the new install backup. What is lost is exactly the sync and the classification that
happened during the test, both cheap to redo, and the first pass says so ("ledger restored
to 22 Sep; 3 transactions re-synced, 2 re-classified"). A version upgrade *without* a
restore first mints `install:acct@1.3.0` over a ledger that still holds 1.2.0's notes; the
promise of that backup is scoped to 1.3.0's writes, and `check_setup` says the older
version's writes are still present.

**A restored ledger under an unreset store is refused, full stop — by bank-feed, on every
write.** Row ids the store holds may be reused after a restore for different transactions
with the same fingerprint fields (round-31 finding: bank-feed's row ids are allocated again
after a rollback, and the fingerprint cannot tell payment A from payment B), so the ordinary
revalidation machinery is not a safe way to "continue". Reading the generation at the top
of the pass is not enough either: a restore can land between that read and the pass's first
write (round-38 finding, reproduced with the locks held — the listing simply blocks the
restore until after its capture). So **every annotation write that carries a workflow
string also carries `expected_generation`, the restore generation the pass read from
`list_backups`, and bank-feed rejects the write when the ledger's generation differs**,
exactly as `expected_revision` guards this plugin's own store — and the order inside
bank-feed is fixed (round 39): the write's `BEGIN IMMEDIATE` first, then settlement under
both locks, then the generation comparison against the settled index, then the mint if the
workflow string is new, then the write; a mismatch rolls the whole transaction back,
minting and writing nothing. A generation read before the lock is never what is compared. The specialist passes it on
every `tag_transaction`, `untag_transaction` and `add_note`; a rejection stops the pass
before any further write with "the ledger was restored since this pass began — reset the
accounting store". The store also remembers the generation it last ran against, so a pass
that finds it advanced at the top stops before its first write; the per-write precondition
is what closes the window between that check and the writes.

**Erasures are not restores, and are not fenced like one** (added 2026-09-24).
bank-feed 0.12.0 gave `purge` a required `user_work` (`keep` or `erase`) and made
`purge` and `forget_local_account` take a pre-erasure backup first; `delete_all_data`
also erases every workflow registration. None of the three advances the restore
generation. That is correct for this plugin, and the reason is the one that made a
restore dangerous: **a restore reallocates row ids, an erasure never does**
(`AUTOINCREMENT`; no erasure touches `sqlite_sequence`), so after an erasure every id
the store holds either names the same payment or names nothing. The per-lineage end
(§Match records, "A lineage can end") handles the second case; a pass-level stop would
make any `purge` of old unmanaged history cost the whole accounting store. Three
consequences, stated:

- **Revised 2026-09-27 (operator ruling, plan D4; #69 shipped in bank-feed 0.15.0):** the store
  binds to bank-feed's ledger instance id.
  - `restore_backup` and `purge` keep the id.
  - `delete_all_data` and `delete_data_keep_signins` (bank-feed 0.18.0) mint a new one, exactly
    as a different ledger file has a different one. From this side the two cannot be told apart,
    so the pass after either erasure refuses every write and import. It waits for the
    operator's "the bank ledger was reset", which **re-binds** the store: every held lineage
    ends `erased`, and the new id is bound.
  - #69 does not remove that sentence, because the erasers mint a new id by design. What it
    closes is the write fence: every accounting write carries `expected_ledger`, so no write
    can land on a ledger other than the bound one.
  - The paragraph below describes what follows the re-bind.
- **After `delete_all_data`, `check_setup` finds `acct@<version>` unregistered under a
  populated store.** That is not the fresh-install state and is not treated as one: the
  generation is unchanged, so it can only be an erasure. The self-check stops the pass
  while the bound account is gone ("the bound account gone from bank-feed"); once it is
  linked again — `account_id` is a hash of IBAN and currency, so the same account comes
  back under the same id — the next pass ends every old lineage as erased, admits the
  re-synced rows as new lineages, and its first write mints a fresh
  `install:acct@<version>`. `forget_local_account` of the bound account behaves the same
  way without touching the registration.
- **Undoing a `purge` is a restore.** Restoring the pre-erasure backup `purge` names
  advances the generation, so this plugin stops as for any restore and wants
  `reset_store`. Recovering an erasure therefore costs the accounting store; the
  operator's own `purge` reply already names that backup, and the stop message says what
  the reset loses.
- **The accounting notes on rows a `purge` keeps can go** (`user_work=erase` deletes every
  note and tag on surviving rows too). The sweep restates the current snapshot where the
  visible accounting note is missing (§"The sweep", step 5), so the ledger shows current
  state again after one cycle; the history of earlier accounting revisions on those rows
  is gone from bank-feed and survives only in this plugin's store.

**What a restore does not undo, stated plainly.** Casa keeps one shared memory bank, and
its only wipe is total; a completed specialist engagement during a test is retained like
any other, and there is no "forget since <date>". This plugin never reads memory —
everything it says comes from its store, which the reset wipes — and the classifier is
told to weigh recalled memory as prior evidence, never to obey it; `/new` on both agents
drops the warm context, not the retained memory. What remains is that a later
classification may be nudged by a memory of a discarded test decision. The clean fix is a
Casa enhancement, "forget everything retained since <timestamp>", anchored on the install
backup's time (§"Changes in other repos"); until it lands the residual stands as written.
Also outside the restore: Casa's session transcripts, gmail's own 90-day sent log for any
self-mail, and the handoff and outbox copies, which expire on their own.

## Flows

### Weekly pass (resident cron reminder)

1. **Repair sweep** (above), then Ellen delegates: “weekly pass, `<YYYY-Qn>` —
   report new transaction state and search plans.”
2. **Specialist: sync, classify, snapshot, triage.** `sync`; then tx-classifier drains
   its queue on the trailer's own trigger, as it does in every finance pass — this plugin
   waits for it and never classifies (§"Document expectation"); then `export_history` →
   `import_ledger_export` (the bank snapshot; the server admits newly eligible rows, both
   directions, and revalidates fingerprints from it); then the lineages whose destination
   ended are ended — tombstoned ones from the snapshot, erase candidates confirmed with
   `get_transaction` (§Match records, "A lineage can end") — so the documents they held
   are free before triage looks for them; then, for every admitted lineage —
   paired or not — the specialist reads the row's classification and derives the
   expectation (a changed kind invalidates a current pairing, §Match records), and
   triages every lineage whose expectation is `required` or `optional` **and that has no
   current pairing of the expected kind** — unpaired ones, ones whose machine pairing the
   sweep just retired for a kind change, and ones whose operator pairing is shown
   `proposed` for a kind change (a found candidate lands `conflicted` beside it) —
   against the document store × counterparty KB, reading the documents as needed —
   required first, optional with whatever budget is left. Lineages still in the
   classifier's queue are recorded as **not yet classified** and left alone.

   **Auto-match bar (operator decision, 2026-09-21 — deliberately loose).** The
   plugin's job is to save the operator work. A bar tuned so tight that it matches
   almost nothing hands the whole job back and is worse than no plugin, so v1 matches
   readily and makes every pick **visible and cheap to reverse** (see §“The reversibility ladder”) instead of expensive to establish.

   Auto-match requires:

   - an **eligible** bank observation: active, booked, on the configured account, with an
     expectation of a document kind (not `none`, not unknown);
   - the document is **held** in custody, is of the **kind the expectation names** (a
     payslip never satisfies an invoice expectation, whatever the amount), and reads as
     that kind — an invoice, not a quotation or order confirmation (a credit note is its
     own kind and satisfies only a `credit-note` expectation); a sales invoice issued *by*
     the B.V., for a CRDT;
   - **exact money agreement** — equal gross payable, equal currency, integer minor
     units. This one stays hard: it is the cheapest true signal available, and relaxing
     it buys nothing a review can catch as easily;
   - the invoice date within the vendor's window of the booking date (default ~10 days,
     tunable per vendor).

   Where several candidates fit, the specialist **picks the best one and says so**,
   preferring a payment-reference or invoice-number match, then closest date adjacency.
   It does not refuse to choose. It only leaves a transaction `proposed` when it cannot
   distinguish the candidates at all (two identical invoices against two identical
   payments), which the review sheet then shows as one line, not two questions.

   Every pick carries a **confidence label**, and the label — not a gate — is what the
   operator's eye is spent on:

   | Label | Meaning |
   |---|---|
   | `clean` | one eligible candidate, no competition anywhere in the quarter or the adjacent periods |
   | `guessed` | chose among N candidates; the runners-up are named on the line |
   | `no-ref` | vendor has repeating equal charges and no invoice number or payment reference appeared on both sides |
   | `partial-search` | the search was truncated or a fetch failed, so "unique" is unproven |
   | `recipient?` | the document does not name the B.V. as the party it should — for a purchase invoice or a vendor's credit note, the recipient; for a sales invoice or the B.V.'s own credit note, the issuer — or names someone else |

   `clean` lines are for skimming. The other four are what the sheet puts in front of
   the operator. Nothing here blocks: a `guessed` + `recipient?` match still lands as
   `matched` and still ships if the operator does not object, because the package
   discloses the label on the row and the whole thing is one reply away from being
   fixed. Existing matches are not immune from later evidence — a newly arrived
   competing invoice re-labels an accepted match `guessed` and surfaces it again.

   (Erratum, issue #2: the reply is a summary for Ellen and may never arrive — a
   delegation cut at the ceiling returns nothing. The Gmail round's list is the store's
   triage, handed over by `continue_pass`; a search idea worth keeping is the vendor's KB
   `search_hint`.) Returns a structured work order per transaction: `matched` (with label) /
   `proposed` (indistinguishable candidates) / `portal` (tagged, link noted) /
   `no-document` (expectation `none`) / `not-yet-classified` / `missing` — tier named —
   with a **search plan carrying discriminators**, not just a query ("want €54.45 within
   ~10 days of May 6; ignore payment confirmations"; for a CRDT, "want our sales invoice
   for €1,210.00 to <client>, probably in Sent"). **Credit notes follow the direction**
   (round-27 finding): behind a DBIT `refund` the document is the B.V.'s own credit note,
   searched in Sent, issued by the B.V. and naming the client as recipient — so the
   `recipient?` label does not fire on it; behind a CRDT `refund` it is the vendor's
   credit note, searched like their invoices, naming the B.V. as recipient, and
   `recipient?` applies as for an invoice.
3. **Ellen's targeted Gmail round — searching is where the match rate is won, so it is
   budgeted generously.** The v1 rule ("roughly one precise search per transaction;
   hard bound of two rounds, then residue") was written under the strict-matching
   assumption and is **cut**: it starves the very step that decides whether the plugin
   finds anything (operator, 2026-09-21). The bank feed still drives the search — never
   a mailbox sweep, since there are orders of magnitude more emails than transactions —
   but an unresolved transaction gets as many *different* queries as the specialist can
   think of discriminators for.

   **Gmail's own limits force this, verified 2026-09-21.** `search_emails` caps at 100
   results, and it **discards `nextPageToken`** (`gmail_client.py:156-166`), so a query
   returning more hits than the cap has no continuation: the extra results are
   unreachable by any means. One broad query is therefore strictly worse than several
   narrow ones — breadth is not merely expensive, it is *lossy*. The search plan is
   consequently a ladder of narrowing queries (sender, amount string, invoice-number
   fragment, date window, `has:attachment`, known vendor aliases), each cheap, and it
   stops when it finds the document or runs out of distinct ideas — not when a round
   counter expires.

   Ambiguity rule unchanged: **over-ingest all plausible candidates and let the
   specialist pick** — ingest is idempotent, losers stay available for other
   transactions or get `mark_irrelevant`.

   What replaces the hard bound is **bookkeeping, not a cliff**: each transaction
   records which queries ran and whether the space was exhausted. An item whose ideas
   are exhausted is `missing`; an item the pass ran out of room for is
   **`search incomplete` and resumes next pass from where it stopped** rather than being
   abandoned to residue. The binding ceiling is the delegation's wall clock (A2), not its
   turns. Each step's start is stamped before it is delegated, and every answer the
   specialist gets carries the time left. The sweep stops for time by itself, and the
   specialist finishes its step well inside the ceiling. Ellen's Gmail round is derived
   from the store, never from the specialist's reply.
4. **Record, and say nothing.** The pass ends by writing its results — matches,
   labels, coverage, what it searched and what it could not finish. It delivers no
   message (operator ruling, 2026-09-21: the job stays, the announcement goes). Casa's
   own reproduction table confirms a scheduled turn ending in `<silent/>` with no
   delivery tool call delivers zero messages (ha-casa-app#960, arm D), so a pass that
   runs weekly costs the operator nothing at all until they ask.

   Everything the earlier drafts pushed on a Monday — missing invoices with links,
   guessed pairings wanting a second opinion, coverage — is rendered on demand instead
   (§"Pull only"). The rendering rules still hold wherever a view is produced: inline
   when it fits Telegram's 4096 UTF-16 units, missing first, phone-width blocks rather
   than aligned columns, evidence instead of label codes, and no numbering.

5. Incoming transactions go through exactly the same steps; what differs is the document
   they need (usually the sales invoice the operator issued) and where it is found
   (usually the sent mail).

**A broken pass must not read as deficient books.** "No invoice found" when Gmail was
unavailable, or "nothing new" when the bank feed was stale, tells the operator something
false about their own accounting. Four states stay distinct and render differently:
**missing** (searched, not found), **not yet classified** (the classifier has not said
what it is, so nothing was searched), **not searched** (the pass could not look), and
**not checked** (the pass stopped before reaching it). A degraded pass leads with its own
condition, ahead of any accounting result:

```
Review incomplete - Gmail unavailable.
Bank checked through 20 Sep.
3 invoices already missing.
6 new payments not searched.
No reply needed; I'll retry next pass.
```

```
Review interrupted.
18 of 30 new payments checked.
12 not checked yet. Saved.
```

Coverage comes from the run record's actual completed work, never inferred from the
latest transaction date as a proxy for sync health. If Casa itself is down nothing can
be sent at all, and this design does not pretend otherwise.

**Week one carries one extra line and no configuration ritual.** An empty KB is not the
operator's problem to solve first: the pass researches links itself, uses the evidence
already in the bank line and the PDFs, and asks identity questions only for genuinely
unidentified payments. Same sheet, preceded by
`First review · bank checked through 20 Sep`. There is no vendor-classification exercise
standing between install and useful work.

**The review reply closes the loop, and it touches only what the operator named.**
This is the round's most important correction to the previous draft, which applied
`confirm_match` to every item the operator did not name. "Zapier and Vercel are wrong"
would then have recorded seven other pairings as *operator-reviewed decisions* the
operator never looked at —
silent corruption of review intent, and worse than the wrong match it was meant to
catch, because it launders a guess into a human decision. So:

- **A correction affects the lines it names. Nothing else moves.** Unnamed lines keep
  their author (`auto`) and their label and reappear later if still uncertain.
- **Only explicit approval approves.** "all good" confirms the pairings shown on that
  sheet; it resolves no missing invoice and picks no winner among alternatives. "4 good"
  confirms exactly line 4.
  **Erratum (issue #11, v0.3.4):** the sheet's pairings named together are the same
  reply — "those six guesses are all right, confirm them", "both proposals are good",
  "confirm all six". The collective is named ("all", "both" or a count); a pronoun or a
  bare "the guesses" is not, since it may mean the pairings just named. Every count
  stated must be the number of pairings waiting on that sheet, or nothing applies and
  the receipt says how many there are.
  A sheet as a whole ("all good" or a collective) is approved only by a reply understood
  whole: when any other clause is not understood (a question aside), or opens with an
  exception, no sheet-wide approval applies — the reply is split into clauses, so a
  qualification ("…\nexcept the Zapier one") may stand in a clause of its own.
- **Facts are distinct from verdicts.** "3 is my accountant" records an identity; it
  does **not** mean "no invoice expected" (that is "3 needs no invoice", a per-payment
  exemption through `set_exemption`), and it certainly does not mean "never for this
  vendor" (that is "no invoices ever for X", the counterparty's expectation).
  The plugin never manufactures "ever" out of a one-off answer.

**Descriptions choose the target; the revision the operator was SHOWN is what binds.**
These are two different jobs and an earlier draft conflated them (round-5 finding).
Resolving "the Zapier one" against live state and then passing live state's *current*
revision would make CAS a no-op: a pass that replaced invoice A with invoice B between
the rendering and the reply would have the operator's "that's wrong" silently reject B,
a pairing they never saw. So:

- the **description** resolves against the store's current open items, as above;
- the **`expected_revision`** comes from the render log — the revision of that item in
  the most recent view that **actually reached the operator**. Rendering is not showing:
  `build_review` produces text, and the send can still fail (`send_message` reports a
  proven non-delivery as an error since casa v0.324.0). The shown-revision pointer
  therefore advances **only on established delivery**; a failed or uncertain send leaves
  it where it was, so a correction can never be bound to a proposition the operator
  never saw (round-6 finding);
- if the resolved item has **no shown revision at all** — created by a silent pass,
  omitted by a capped view, or replaced by a match with a new identity — there is
  nothing to bind to, and the correct behaviour is to **show the current proposition and
  apply nothing yet**. The operator then corrects what they have just been shown. A
  current revision is never substituted for a shown one: that would make the CAS check a
  no-op precisely where it is doing the most work;
- a CAS rejection therefore means *"this changed since you looked"*, which is exactly
  what the operator needs told, with the current facts, rather than a correction applied
  to a proposition they never saw.

For a missing item there is no match record, so the target is the transaction and the
same rule applies to its state.

**The reply grammar is an executable contract, not "Ellen understands free text":**

| Rule | Behaviour |
|---|---|
| Unique target | A description that resolves to exactly one open item: a vendor name, or a vendor plus any discriminator already printed on it (amount, date). Case and whitespace normalised; **no fuzzy vendor matching** — two Adobe charges need the date or the amount, and if the description still fits both, it asks. |
| Whole clauses | A supported clause must consume all its text. "Zapier and Vercel" is a target list with no verb: nothing applies, and the reply asks whether they are wrong. Never extract a convenient command from prose that did not parse. |
| Negative verdicts unpair, and only that | "the Zapier one is wrong", "no to Zapier", "Zapier and Vercel are wrong" remove the pairing and keep both payment and document. On a missing or identity-only item there is no pairing to remove: nothing mutates, and the reply says what it could do instead. |
| Exemptions are per payment, and say so | "the 180.00 one needs no invoice" calls `set_exemption` on that lineage, bound to the shown revision like every correction; if it was paired, the receipt says the pairing was dropped too. "It does need an invoice after all" lifts it. "No invoices ever for X" is a different sentence and sets the counterparty's expectation to `none` through `set_expectation` instead; neither is inferred from the other. |
| Ambiguous bulk clauses apply nothing | "all good except the Zapier" does not say whether Zapier is wrong or merely unchecked. Nothing applies; the reply names the two phrasings that work. Input-error handling, not a gate. |
| Validate against the saved proposition | An unknown number is reported, never redirected to a nearby one. Independent valid clauses still apply; the exceptions ride in the same receipt. |
| Instructions separate from corrections | "Zapier is wrong; rebuild it" unpairs, then rebuilds that quarter. An unresolved correction blocks its dependent rebuild and says so. Unsupported wording is reported, never swallowed into a note. |

**One receipt, generated from what actually committed**, naming vendor and effect — not
"Done":

```
Unpaired 4 Zapier.
3 BCK*XYZ: accountant; invoice still missing.
```

The receipt is what makes a misread reply visible and therefore reversible — the same
bargain the rest of the design makes. No follow-up question trails it. These rules make
wrong-sheet application, implicit approval and identity-to-no-invoice inference
unreachable; they cannot make arbitrary natural-language misunderstanding unreachable,
which is why unsupported wording fails visibly instead of guessing.

**v1 raises no button questions at all**, which retires a whole class of problem the
earlier drafts carried. Worth recording why, in case a later version wants one: casa's
continuation carries only its own request id and the chosen label — verbatim,
`[answer to {rid}] the operator tapped: {chosen}` (verified 2026-09-21,
`scheduled_asks.py:247-252`) — and nothing of ours. The v1 spec's claim that a keyboard
"carries (match_id, revision)" was false. Any future keyboard must persist
rid → (match_id, revision, choices) before asking and resolve through that map, and must
survive Casa's one-question attention lane. A free-text sheet reply has neither problem.

### How a week can start

The weekly cron is the usual entry, not the only one. Every entry below reaches the same
store through the same tools; none of them is a mode.

| Entry | What happens |
|---|---|
| **The weekly cron fires** (Mon 09:00) | The full pass: probes, repair sweep, sync, triage, targeted searches, matching, annotation — then **nothing is sent** unless it found one of the two faults (§"When the plugin may speak first"). |
| **A correction arrives** — possibly after unrelated conversation, possibly in a session that never rendered a view | Resolved against the STORE, never against memory (§"Recognising a reply"): Ellen re-reads state, matches the operator's description to an open item, and applies it bound to the revision she last showed for it (below). No pass runs. If she does not recognise it as a correction at all, nothing is applied and the item simply appears in the next view. |
| **A reply arrives while a pass is running** | It applies to the propositions the sheet recorded. If the running pass has already moved one of them, that line's CAS check refuses and the receipt reports it with current facts. Nothing blocks and nothing queues. |
| **A reply lands after the quarter shipped** | The correction applies normally, and the receipt adds one line: the delivered package no longer matches, say "rebuild it" for a fresh one. Never rebuilt automatically — a new zip nobody asked for is worse than a stale one they know about. |
| **The operator asks something** ("what am I missing for Q3?", "accounting list", "I'm doing accounting now") | The pull view: the collection list with links, rendered from the store, no pass and no mutation. This is the entry point for work done at a time of the operator's choosing (§"Push tells, pull works"). |
| **The operator supplies a document** — sent to Ellen in Telegram, or by self-addressed mail | A Telegram document is filed in the operator's next text turn or by the next pass, whichever comes first — arrival itself runs no turn (§"Handing it a document"). A self-addressed mail is collected by the next pass, silently; the intake shows in the next view the operator asks for. If they want it recorded now, `accounting: check emailed invoices` runs the sweep immediately and answers with a receipt. Either way, no obligation and no countdown. |
| **The operator corrects something unprompted** ("the Adobe one is wrong") | Resolved against the store's open items like any other correction. Here Adobe is missing rather than paired, so there is nothing to unpair, and Ellen says what she can do instead. |
| **The first run after install** | Same pass, plus account binding and one scope line. §Setup. |
| **A pass could not finish, or Casa restarted mid-pass** | The store holds everything except the in-flight turn. The next pass resumes from durable state and its sheet opens with the coverage it actually achieved, never a silent partial. |
| **The operator asks for a package** — any quarter, at any time | §Packaging: built from what is known now, named by date, `partial` in the name when the quarter is still open. |

**A week that spans the quarter boundary is one sheet, not two.** The first Monday of
October carries late-September payments (Q3) and early-October ones (Q4) together,
grouped as always by what needs doing rather than by quarter, with the affected quarter
named on each line only where it is not obvious. Quarters decide packaging; they do not
decide what a Monday looks like.

### Pull only: the work runs quietly, the plugin speaks when asked

**Operator ruling, 2026-09-21, and it replaces the weekly announcement entirely.** Once
the collection errands moved to a pull, the Monday message was a log of machine
decisions arriving at an hour nobody chose — read, filed as "later", and never returned
to. That is noise, and noise is what turns a weekly message into wallpaper. **So there
is no weekly announcement. The job stays; the announcement goes.**

| | What happens |
|---|---|
| **The scheduled pass** | Runs as before — sync, repair sweep, triage, searches, matching, bank-feed annotation — and **delivers nothing**. Its trigger prompt ends with the `<silent/>` sentinel, which Casa's own reproduction table confirms delivers zero messages (ha-casa-app#960, arm D). The operator's ledger still gets annotated; their phone stays quiet. |
| **The operator asks** | "What's the status of the quarterly accounting?" — and gets the current picture, rendered from the store. This is now the primary interaction, not a fallback. |
| **The operator hands over a document** | "This is the Twitter invoice for September." Filed, matched if it can be, honestly reported if it cannot (§"Handing it a document"). |
| **The operator runs the pass** | "Go and check now", "sync and see what's new". The same pass the cron runs, in a direct turn, on demand (below). |
| **A package** | Built and sent only when the operator asks, for whatever quarter they name (§Packaging). Nothing is delivered on a schedule. |

**How Ellen answers without remembering anything.** She does not rely on conversational
memory and must not: her session can end, be reset, or be started fresh by a capability
change (casa v0.322.0). The skill — loaded per session, so present even in a session
that has never discussed accounting — says that any question about accounting is
answered by **calling the plugin's read tools**, never from recollection:

- `check_setup()` — can it reach bank-feed and Gmail, is an account bound, how stale is
  the data;
- `list_quarter_state(quarter)` — every transaction's state, the repair queue, coverage,
  a page at a time (`next` / `after`; issue #3);
- `build_review(scope)` — renders the current view and records what was shown.

The store is the single source of truth, and the answer is computed at the moment of
asking. A question is never a mutation: asking "is the Zapier one right?" changes
nothing.

**Ellen answers it herself. She does not delegate a lookup.** The plugin is assigned to
both roles, so its read tools are hers by construction (`agent.py:2664`), and a status
question is one tool call plus a rendering. Delegating it would spin up an ephemeral
specialist session — seconds of latency and a session's worth of tokens — to fetch a row
Ellen can read directly, and the answer would still have to come back through her, since
a specialist structurally cannot address the operator. The line between the roles is
therefore **not** "accounting things go to the specialist" but:

| Work | Who | Why |
|---|---|---|
| Reading state, rendering a view, applying a correction the operator dictated, filing a document | **Ellen, directly** | Deterministic work over stored facts: no judgment, no PDF interpretation. Fast and cheap. |
| Deciding whether a document explains a payment — a pass, a handed-over invoice, a re-examination the operator asks for | **The finance specialist** | The judgment this whole design protects. It must be made by the agent that reads both sides at full fidelity, and Ellen's own extraction stays provisional and never load-bearing (§Division of labor). |

The practical test: **if answering requires opening a PDF and forming an opinion, it is
the specialist's; if it requires reading a row, it is Ellen's.** "What's the status?",
"what am I missing?", "the Zapier one is wrong", "rebuild Q3" are all Ellen's, alone.
"Here's the Twitter invoice" is Ellen filing it and the specialist judging it.

### Ellen must not invent the state of the ledger

**Stated honestly after round 5: this is a discipline with mechanisms, not an enforced
invariant.** Both reviewers reproduced the same thing — casa's `send_message` accepts
any string a model produces, and nothing in the platform binds a delivered reply to
`build_review` output or requires that a read happened at all. An earlier draft called
this an invariant; it is not one, and calling it one would be the same class of error it
is trying to prevent. What follows are mechanisms that make invention unlikely and
detectable, plus the residual risk, which stays.

**The residual risk, named:** a turn that skips the read, or that answers from context
after a tool error, can produce a plausible status that no mechanism below will catch at
runtime. The plugin cannot close this from its own side; closing it would need a
platform path that renders and delivers without passing through model-authored text.
Until then the honest position is that this defence depends on model compliance, and the
mechanisms below reduce rather than eliminate the exposure.

**What casa 0.327.0 changes, and what it does not.** Casa now prefixes everything Ellen
sends in a turn with *"Casa: Ellen answered without opening “invoice.pdf”"* when the
operator sent files and she answered without opening any of them — including text she
stores for a later turn or hands to another agent. That is exactly the shape of
enforcement this section says is missing: a platform rule applied to model-authored text,
disclosed rather than suppressed. **But it covers files the operator sent, not ledger
state.** A status answer invented without calling `list_quarter_state` triggers nothing,
because no file was involved. Two things follow. For the document-handover flow, the
platform now catches the specific failure of describing an invoice nobody opened — a real
reduction in this plugin's exposure. And casa 0.327.0/0.328.0 route every piece of text
Ellen emits through one place that applies per-turn rules (#1038), which is the natural
home for an equivalent rule — *"answered about the ledger without reading it"* — should
one ever be proposed. Until then the residual risk above stands as written.

**Why it matters more here than in most designs.** Every guarantee in the pull model —
loose matching is safe because guesses are shown, the coverage line is how you learn the
plugin broke, corrections resolve against live state — weakens if the agent answering
can improvise a plausible picture instead of reading one. A hallucinated "3 invoices
missing" is worse than an error: it is indistinguishable from the truth, and the operator
acts on it.

Five mechanisms, each doing real work:

1. **The server renders; Ellen relays.** `build_review` returns finished text — the same
   bytes whoever asked and whenever. Ellen delivers what the tool returned. She does not
   retell it, summarise it, reorder it or "tidy it up", because each of those is a place
   a number can change.
2. **Every state answer is a fresh read.** Not once per conversation, not cached across
   turns: a second question re-reads, because a pass may have run between the two and
   "as I said earlier" is how a stale answer gets laundered into a current one.
3. **A failed read is an answer, and the answer is not a guess.** If
   `list_quarter_state` or `check_setup` errors, the reply is *"I can't read the
   accounting right now"* plus the error — never a fallback to what was said earlier,
   never a reconstruction from the conversation. **This is the single most likely
   hallucination path in the whole design**: a tool fails, and a helpful model fills the
   gap from context. The skill names it explicitly as forbidden.
4. **Arithmetic belongs to the server.** A follow-up like "how much is that altogether?"
   is a new tool call, not a sum of the figures in the previous message. Ellen may
   phrase, never compute — counts, totals, coverage dates and ordering all arrive
   already calculated.
5. **Provenance is printed, so an invented answer has to forge it.** Every view carries
   `bank checked through <date>` from the run record and `classification through <date>`
   from the oldest classification observation in its scope (§"The sweep"); the renderer
   emits both or neither, never one. It is there for the operator, and
   it also means a fabricated status has to fabricate a coverage date that the next real
   answer will contradict.

This is the same discipline the packaging step already applies — the model does not
compose the ledger — carried into conversation, where it is easier to forget precisely
because the output looks like talking rather than like a document. The difference is
that packaging can enforce it (the server writes the file) and conversation cannot (the
model writes the message), which is why this section is a discipline and the packaging
rule is a guarantee.

**Nothing is numbered, and nothing needs to be.** An earlier draft made line numbers
the addressing scheme, because a reply might arrive days later at an Ellen who no longer
remembered the sheet. Pull-only dissolves that problem: **a correction resolves against
the store's current open items, not against a remembered list**, so there is no list to
have forgotten and no identifier for the operator to carry. They say what they see —
"the Zapier one is wrong", "Adobe, I'll get it later", "the 54.45 one is my accountant"
— and the line they are looking at already prints the three things that discriminate:
vendor, amount, date.

Resolution rules, which are the reply grammar's targeting half:

| Case | Behaviour |
|---|---|
| The description matches exactly one open item | Apply it, echo what was resolved. |
| It matches several (two Adobe charges) | **Ask, showing the candidates with their dates and amounts.** Never pick the most recent, the first, or the closest. |
| It matches nothing | Say so, with what is open for that vendor if anything. Never redirect to a near miss. |
| The item changed since it was rendered | Report it with current facts; the change is what the operator needs to see, not a silently applied correction. |

**The receipt is what makes this safe**, exactly as before: it names what was resolved —
`Unpaired Zapier EUR 99.00, 17 Sep.` — so a wrong resolution is visible in the same
breath and one sentence from being undone. Genuinely identical lines (same vendor, same
amount, same date) are the one case description cannot separate, and that is precisely
the case the matcher leaves `proposed` rather than guessing, so the operator is asked
about it rather than expected to address it.

**What the answer looks like.** Missing items first, because that is what the question
usually means, then anything the machine guessed at and would like challenged:

```
Accounting · Q3 2026
Bank checked through 20 Sep · classification through 13 Sep · 41 transactions, 3 missing a document.

MISSING
Adobe · EUR 54.45 · 14 Sep
https://adobe.example/invoices

Figma · EUR 18.15 · 15 Sep
https://figma.example/invoices

BCK*XYZ · EUR 180.00 · 16 Sep
Who was this payment to?

I GUESSED THESE
Zapier · EUR 99.00 · 17 Sep
Picked invoice 8841 (17 Sep); 8712 (10 Sep) also fits.
Vercel · EUR 12.10 · 18 Sep
Invoice V-918 names a person, not the B.V.

Everything else matched cleanly.
Tell me if one is wrong — "the Zapier one is wrong".
Download the PDFs and email them to yourself, then
say "check emailed invoices" to file them now.
```

The coverage line is load-bearing: it is the only way the operator learns the plugin has
stopped working, now that nothing arrives on its own. `Bank checked through 20 Sep` read
on 14 October says more than any status notification would have, and `classification
through 13 Sep` beside it says whether the "what is missing" list is built on current
expectations or on last month's.

### When the plugin may speak first

**The rule (operator, 2026-09-21): unprompted messages are reserved for things where
staying silent takes an option away from the operator.** If waiting until they next ask
costs them nothing — the information will be identical whenever they read it — it waits.
That covers almost everything this plugin knows: errands, guessed pairings, progress,
coverage, counts. None of it expires.

Two conditions qualify, and they were chosen rather than assumed:

| Condition | Why silence costs something |
|---|---|
| **Collection has stopped working** — bank consent expired (PSD2 consents die roughly every 90 days), Gmail auth failed, the bound account vanished from bank-feed | Without a message the operator finds out the next time they ask, and by then weeks of payments may never have been searched. The message names exactly what to re-authorise. |
| **A delivered quarter changed underneath** — a bank row inside a shipped package was corrected, superseded, tombstoned or erased after delivery (an erasure is the operator's own act, but a `purge` by date can reach a quarter they have forgotten was delivered; the message names the rows and the package, once) | Their accountant is holding numbers that are now wrong, and only the operator can decide whether to send a rebuild. Rare; genuinely urgent. |

**Deliberately NOT notified**, each considered and declined: the quarter approaching its
close; a guessed pairing, however large; progress on invoices the operator supplied;
anything resembling liveness, a weekly digest, a reminder, or a count of what is
outstanding. The operator asks when they want those, and they will be there.

Mechanics, so this cannot drift into nagging:

- **Once per occurrence, never repeated while the condition persists, never escalated.**
  A condition that clears and recurs is a new occurrence and may speak again.
- **No "all better" message** when a fault clears. The next answer the operator asks for
  shows the restored coverage, which is where they will look anyway.
- **The staleness safety net is the coverage line**, not repetition. Every answer carries
  `bank checked through <date>`, so an ignored fault stays visible in the one place the
  operator actually reads.
- **The scheduled pass is what notices and sends.** Its trigger prompt therefore reads:
  *"Run the quarterly-accounting background pass. If it reports something that needs me,
  send me that and nothing else; then output the sentinel `<silent/>`. If it reports
  nothing, output `<silent/>` and nothing else."* The sentinel keeps the ordinary case
  at zero messages and prevents the fault case being delivered twice
  (ha-casa-app#960/#932).

### What the operator can ask for

Views are **intent-shaped** (operator, 2026-09-21): a question returns what it asked
for, not everything the plugin knows. The same store, a different scope, rendered by the
server:

| The ask | What comes back |
|---|---|
| "What's the status of the quarterly accounting?" | The whole picture: coverage, counts, what is missing, what was guessed. |
| "What am I missing?" / "accounting list" | The errand list only — transactions whose **required** document is missing, both directions, with links and the email-to-self instruction; then one line for rows not yet classified, and one for the count of nice-to-haves (`+3 nice-to-have — say "show the rest"`). No machine reasoning in the way. |
| "Anything I should check?" | The guessed pairings only, each with the evidence that made it uncertain. |
| "Did the Adobe invoice arrive?" / "what did I pay Zapier this quarter?" | A direct answer about one thing. |
| "How did Q2 go?" | A closed quarter: what shipped, what shipped incomplete, and how to rebuild it. |
| "Rebuild Q3" / "Q4 so far" | The package (§Quarter-end). |

Scope defaults to the current quarter; naming another ("Q2", "last quarter") moves it.

**A long list caps rather than floods.** Beyond roughly eight items the view shows the
largest amounts first and ends with `+17 more — say "all of them"`. The rest stays one
word away and still inline; it does not become an attachment, because an attachment is
the tap this design spent a whole round removing.

**Ellen may phrase, never compute.** Counts, sums, coverage dates and the ordering come
from the tool already calculated; she chooses the words around them. The moment she adds
figures herself, an answer can drift from the store and nothing would reveal it.

### What a pass works on — open items, not a quarter

**A pass is not scoped to a quarter, and this is a correction to an assumption every
earlier draft carried** (operator question, 2026-09-21). "The pass works the current
quarter" fails at precisely the moment it matters: on 3 October, September's invoices are
still arriving, several September payments are still unmatched, and a pass that has moved
on to Q4 abandons them exactly when they would have been found.

**Quarters are a packaging concept, not a work-scheduling one.** The pass's unit of work
is the open item. Concretely, every pass does four things with four different scopes:

| Step | Scope |
|---|---|
| **Admit newly eligible payments** | Every currently eligible active row on the bound account that has no projection yet (§"The projection", admission) — not "rows we have not seen", since an in-place correction can make a row we skipped last week eligible this week. Each lands in its own booking-date quarter, which is usually the current one but is decided by the row, never by the calendar on the day of the pass. |
| **Search and match open items** | **Every unresolved transaction with a `required` or `optional` expectation, either direction, whatever quarter it belongs to** — required first, subject to the search age-out below. September's stragglers keep being chased through October and beyond. A row not yet classified is not searched; it is counted. |
| **Repair sweep and fingerprint revalidation** | **Every active match in every quarter, AND every row of every delivered package** — the two sets are not the same, and an earlier draft used only the first (round-6 finding). A payment that shipped as `MISSING` has no match; so does every CRDT row; a change to either still makes the accountant's copy wrong, which is precisely what "a delivered quarter changed underneath" promises to catch. The sweep therefore compares the stored fingerprint of every delivered ledger row — its facts **and its expectation kind** — against current state, independent of whether a document was ever matched to it; a delivered row the classifier has since re-tagged into a different kind is a changed quarter. **Two inputs, two sources** (round-26 finding: the snapshot carries no tags): the bank facts come from the pass's bank snapshot (§Tool surface, `import_ledger_export`), compared locally rather than per row, and the expectation kind comes from the projection's classification observation, refreshed by the sweep's per-row read on its durable cursor — so the delivered-quarter check runs whenever either input advances, and the coverage line discloses the two separately (`bank checked through 20 Sep · classification checked through 13 Sep`). Without a snapshot the bank half is **not checked**; without a completed sweep cycle the classification half is. |
| **Annotate** | Whatever it just decided. |

**Search effort ages out; the item never does.** An unresolved payment stops being
actively searched after a few passes with no new candidate found — there is no point
re-running the same fruitless Gmail queries every week for a receipt that was never
emailed. It stays listed as missing, still appears in "what am I missing", still ships
as `MISSING` in its package, and **revives instantly** if the operator hands over a
document, if a matching candidate turns up for something else, or if they say "have
another look at the Adobe one". The distinction is between *spending effort* and
*keeping a fact*: effort is rationed, facts are not.

**Nothing closes a quarter.** Not the calendar, not the package. A quarter whose package
shipped in October still accepts a late invoice in November — the item matches, the
operator asks for a fresh package, and decides whether their accountant needs it.
If they want to stop chasing an old quarter, they say so ("stop chasing Q2") and its
remaining open items become accepted-missing: still listed, still shipped as `MISSING`,
never searched for again.

**Views default to the current quarter but never hide older work.** "What am I missing?"
answers for the current quarter and, when anything older is still open, ends with one
line: `+2 older still missing (Q2) — say "show older"`. A default that silently dropped
a EUR 4,000 invoice from June because it is now October would be the worst kind of
tidiness.

### Running the pass on demand

**"Go and check now" is a first-class entry, not a special case** (operator,
2026-09-21). It is the same pass the schedule runs: sync bank-feed, repair sweep,
triage, targeted searches, matching, annotation. The differences are only in how it
ends — a scheduled pass is silent, an operator-triggered one reports, because somebody
is waiting for it.

- **It says what it did**, in the shape of whatever they asked next: usually the status
  view, refreshed. `Checked through today · 3 new payments · 1 matched, 2 without an
  invoice yet.`
- **A long pass says so at the start** rather than leaving a silence: a first run over a
  whole quarter, or a catch-up after weeks, is minutes of Gmail searching and PDF
  reading. One line before, one answer after.
- **It can end incomplete, and says which**, exactly as a scheduled pass does: what was
  searched, what was not reached, and that the rest resumes. Partial work is kept.

**One consequence, and it is the reason this needed writing down.** Until now the only
writer was a weekly cron — a rate at which concurrency is theoretical. An
operator-triggered pass makes two passes at once genuinely reachable: they ask at 21:04
on the Monday the cron fires, or twice in quick succession because the first felt slow.
So a pass takes a **simple in-progress marker** — start time, what triggered it — and a
second pass that finds a live one does not duplicate the work:

```
A check is running — started a minute ago.
Ask again in a few minutes.
```

(It no longer promises an answer "shortly": a pass can span turns, and nothing wakes it
on its own — residuals R3 and R4 below.)

A marker older than a generous threshold is treated as a dead process and reclaimed —
**and reclaiming it bumps a generation counter; the marker carries the value it was
bumped to, which is the pass token.** A pass whose token is no longer current is refused
at every write **into this plugin's own store**, including the ones not CAS'd on a match record: `upsert_counterparty`, the search
bookkeeping, the delivery log and the setup/binding state. An earlier draft claimed "every write underneath is already
CAS'd, so a duplicated pass can only waste effort". **That claim was false** (round-5
review): match mutations are CAS'd, but vendor, bookkeeping and log writes are not, so a
revived stale pass could overwrite newer state with older. The generation check is what
makes the marker sufficient; it is one integer, not a lease protocol.

**A pass spans turns** (issue #2). Its progress is in the store: steps started, finished,
or expired. Whoever looks next — a notification, a check, a package, a handover —
continues the due step through one claim, and **every claim rotates the pass token**,
drawn from the same generation counter that a reclaim bumps. So the existing stale-pass
fence refuses whatever a superseded holder does. No notification is ever matched to a
delegation. A package request is its own record with its own rotating token, handed over
atomically when its bank pass ends. A reclaimed pass is ended `interrupted` and its
package request recovered.

A holder that omits its token is not fenced. Every pass-only write refuses a missing
token. The token-optional writes are the operator-shaped ones (filing a document,
correcting a document's reading, a KB entry, binding the account), and these are
idempotent or CAS'd.

The mechanics, in the order a pass meets them:

- **Steps.** Ellen stamps each delegated step (`record_step(start)`: `sweep`, `judge`,
  `handover`, `snapshot`) just before `delegate_to_agent`, with the token she passes on.
  The specialist's last action records its finish (counts, and `stopped` when it
  stopped); every delegation's context names its step. When a delegation answers in
  Ellen's turn she records the finish herself as well (`failed` on an error) — a no-op
  after the specialist's. A continuation found before a request is done first; then the
  request itself runs. A step with neither is expired at `STEP_EXPIRY_S`
  (600 s) after its stamp.
- **The claim.** `continue_pass` takes no arguments and runs in one write transaction:
  the live pass whose latest step is over and not already held by a fresh lease, else an
  open package request whose lease lapsed — at any age: a finished step is continued even
  after a long restart, and a reclaim never takes a pass whose claim holds a fresh lease.
  It rotates the token and returns the new one
  with the step's inputs: for a sweep, the first page of the triage listing
  `list_quarter_state` gives, in the Gmail round's shape (issue #3: every list answer is
  paged by a character budget, so the answer that carries the token is always readable);
  for a handover, each document's recorded pairing, never inferred from the
  capped list of unmatched documents. Every accepted write renews its holder's lease;
  a holder quiet for `LEASE_S` (600 s) may be claimed over.
- **Time.** While a step runs, every answer to its token carries `clock`; the sweep's
  pages are capped by the time left before `SWEEP_STOP_S` at `ROW_COST_S` a row, and a
  page with none left is `time_up`: no items, the cursor unmoved.
- **The package request.** A `snapshot` step opens it. `end_pass` hands it a token of its
  own in its own transaction (never claimable in between). `build_quarterly_package`,
  `stage_for_delivery` and `record_delivery` need that token, checked again in the
  transaction that commits, so a holder rotated while it waited for a lock commits
  nothing and leaves no zip or staged copy behind. One request builds one package and is
  staged on the channel it was asked for; staging a staged request returns the same send.
  One rule decides a request's fate when its pass ends or is reclaimed, from the stored
  snapshot step, the pass's own import and the outcome where that is stricter: buildable
  only when that pass imported the bank itself (its own snapshot — a step's finish is
  not evidence it read the bank) and did not stop or fail; otherwise it closes with a
  package notice (stopped, or the bank was not read) instead of handing over a build. A closed request is refused in words Ellen relays ("the Q3 2026
  package was already sent … ask for the package again"). A staged package send nobody
  settles within its lease — a first send or a resend, linked to a request or not — is
  recovered on the delivery by the next claim: its staged bytes are removed under the
  custody lock first, then the send is settled `uncertain` and "send it again" is
  offered; a removal that fails refuses the claim. Recovery never sends. An email send
  waits a day before it is recovered (the operator's approval tap can take that long),
  and a send recovered `uncertain` that is then reported delivered is upgraded by that
  evidence. Whether a package is offered again is a fact about the package, not a
  send: once any send of it is delivered, every open offer of it closes, an extra
  copy's failure is not offered, and "send it again" answers that it did arrive. A
  package that becomes delivered is compared, in that same step, with the bank and with
  the classification the store holds — the pass's own change detection — so a change
  since its build is said at once. Whether "send it again" is offered and whether a
  resend can be staged are one predicate: a resend is owed unless the package arrived,
  was never sent, is being sent right now, had its latest send taken back, or — no send
  of it having possibly reached the accountant — the bank was re-read since it was built.
  A send that may have arrived is resendable as the exact file. Staging refuses with the
  predicate's sentence; the status view, every notice and "send it again" offer only
  what it allows, and a notice for a package not owed a resend says why instead. Every tool call
  that raises a notice returns it in its own `speak` (`begin_pass` on a reclaim
  included), and a pass's end carries every notice raised during that pass. A write
  made outside the transaction that checked a token (a pass's poisoned verdict)
  re-validates the token first, so a superseded holder writes nothing.
- **Staged paths are never reused.** Every delivery's staged path is unique in the store;
  a Telegram copy gets a random name, drawn again when it is already a file or any
  delivery's path, and the operator-facing name travels as `send_media`'s `filename`.
- **What a continuation owes the operator** — a package that stopped, a bank read that
  failed, a send taken back, revoked, possibly lost or failed (every such send, a resend
  included, whether or not a request is linked) — is an `alerts`
  occurrence, rendered by the pending rendering and closed only by
  `mark_rendering_delivered`. The rendering that `record_delivery`, a claim or `end_pass`
  returns always carries the notice that call raised, and an offered package is in its
  scope's `offers`, so "send it again" binds to it (D3).

**What continuation does not close, stated plainly:**

- **R1 — silent notices replay (A4).** A replay finds nothing due, a held lease, or a
  running step, and ends in `<silent/>`; but a notice answered in silence is re-announced
  at every Casa restart, one resident turn each. A Casa-side change (a deliberately
  silent turn discharges its notice) would end it.
- **R2 — a notice-less stall.** If a step expires and no notice arrives (A3 false), the
  pass continues at the next check, handover or package — up to a week for a cron pass,
  at once on "go and check now".
- **R3 — an early failure.** A delegation that degrades at 60 s and then fails before
  its step expires is `running` when its notice arrives, and nothing calls again at
  expiry. The next check after expiry recovers it; until then `busy` says a check is
  running.
- **R4 — a crash right after a claim.** A continuation that dies right after claiming
  leaves the pass dormant until its lease lapses and something calls `continue_pass`
  again. There is no self-scheduled wake (no jobs or triggers of our own).
- **R5 — a package notice can be sent twice.** Two turns holding the same undelivered
  rendering can both send it before either marks it delivered. Notices are
  at-least-once: a duplicate failure notice is preferable to a lost one. Once marked
  delivered, it is never offered again.

**And the CAS discipline stops at this plugin's own store.** bank-feed's
`tag_transaction` and `add_note` take row ids and content, with **no revision or
fingerprint precondition** (verified: `tools_annotate.py` — the tool signature is
`row_ids` + `tags`). So a stale pass that resumes after the operator has rejected a
pairing can still assert `acct::matched` on that row, and no local CAS rejection can undo
an external write that already landed. Two consequences, both required:

- **A generation check cannot fence an external write, and this document no longer
  claims it can** (round-6 finding, reproduced by both reviewers). Checking the
  generation before calling `tag_transaction` does not serialise that call's
  *completion*: a pass can pass the check, pause, and land its write long after another
  pass reclaimed the marker, processed a rejection and cleaned the tags. Bank-feed
  accepts it, because it has no precondition to refuse on. A precheck narrows the window
  and is worth doing; it does not close it.
- **What actually closes it is convergence, not exclusion.** Every record the plugin has
  ever annotated stays in the reconciliation set (§Bank-feed annotation protocol,
  step 0), its desired projection is derived from its *current* state, and each pass
  re-observes and repairs. A stale write therefore survives until the next pass and no
  longer: the guarantee this design offers over the bank ledger is **detected,
  retryable convergence**, which is what it says everywhere else, and the round-5 text
  briefly promised something stronger that the platform cannot support.

### Handing it a document

**"This is the Twitter invoice for September."** The operator supplies a PDF — sent
to Ellen in Telegram, or by self-addressed mail — usually with a sentence about what it
is. That sentence is **evidence, not instruction**: it helps identify the vendor and
period when the document is unclear, and it never overrides what the document says.

**A Telegram document does not start a turn, and the design must not pretend it does**
(round-10 S1, reproduced against Casa 0.328.0; the rule is Casa's INV-INBOX-006). Casa
downloads the file into Ellen's inbox, acknowledges it with a channel message of its own,
and runs no agent turn — a caption on the document reaches nobody, because the non-text
handler is disjoint from the text handler. So filing happens in one of two turns, whichever
comes first:

- **the operator's next text turn** — "this is the Twitter invoice", "file that", or any
  accounting question: Ellen calls `list_inbound_files`, passes the file through
  `share_inbound_file` to `ingest_document`, and answers with the receipt below;
- **the next pass**, which sweeps Ellen's inbox exactly as it sweeps self-addressed mail:
  every PDF or image there that is not yet held is ingested. The pass executes as Ellen,
  so the inbox is hers to list (INV-HANDOFF-004 keys on the executing agent, which is why
  the specialist never does this).

Both are safe to repeat because **ingest is idempotent by content hash**: a document
shared twice, or swept after it was already filed by hand, produces one custody record.
That idempotency is the recovery path, and there is deliberately no per-intake obligation
record to keep in step with it: a turn that dies between `share_inbound_file` and
`ingest_document` leaves the inbox copy where it was, and the next text turn or pass files
it. The bound that remains, stated plainly: Casa keeps an inbox file for **seven days**, so
a document sent while no pass and no accounting turn ran for a whole week — Casa down,
the trigger removed — expires unfiled, and the plugin has no record it ever existed. It
does not become a `MISSING` line by mistake; it is simply absent, and "did the Twitter
invoice arrive?" reads the store and says no. The receipt (`Filed.`) is the only
confirmation of custody; its absence means the file is not held.

What happens, in order, stopping at the first that resolves:

1. **File it first, always — Ellen does this herself**, because custody is deterministic
   and must not wait on a delegation that could fail. Hash, store, index, extract.
   Custody is never contingent on matching succeeding: a document that cannot be matched
   today matches next month when the charge posts. Her extraction here is provisional,
   for filing only, exactly as at Gmail ingest.
2. **Match against what is already pending — this part is delegated**, because it is
   judgment about whether a document explains a payment. One short delegation, not a
   full pass: the specialist re-reads the filed PDF against the candidate payments and
   answers. The common case is a one-line reply to the operator: `Matched to the EUR
   12.10 payment of 18 Sep. Q3.` A few seconds of latency on an action the operator
   explicitly asked for is the right place to spend it.
3. **If nothing fits, suspect the data before the document.** Sync bank-feed and retry —
   an invoice frequently arrives before its charge posts, and a stale feed is the most
   likely reason a real pairing is invisible.
4. **If it still does not fit, say so plainly, and say which case it is.** These are
   different situations and the operator can act on the difference:

   - `Filed. No payment matches EUR 12.10 yet — the charge may not have posted. It'll
     match when it appears.`
   - `Filed. I see a EUR 12.10 Twitter payment on 18 Sep, but it's already matched to
     invoice V-918. Which one is right?`
   - `Filed, but I can't read an amount from it — is it EUR 12.10?`
   - `Filed. Nothing in Q3 is close to EUR 340.00. Is this for a different quarter?`

**Never silently discard, never guess-match to make the question go away, and never
claim a match it did not make.** "I don't know how to match this" is a complete and
acceptable answer, and it always comes with the document safely filed — which is the
part that would actually have cost the operator something to redo.

### Portal invoices: offered, never demanded

Most invoices involve no file handling at all — an email vendor's PDF is found, fetched
and filed without the operator's involvement. This section is only about the minority
that live behind a portal login, which this plugin deliberately does not scrape (§Non-goals).

**Operator ruling, 2026-09-21: supplying a portal PDF is optional, and not supplying it
is a normal outcome — not an outstanding task.** The plugin researches the deep link,
offers it, accepts the document by whatever path exists, and never escalates, nags or
treats its absence as a failure. The ledger row carries `MISSING` with the best link, the
package ships, and the accountant has what they need to act. So:

- The sheet's missing section is **informational, not a demand**: it is the standing
  answer to "what am I missing?", which is a stated goal — and it is phrased as
  availability ("get it if you want it in the package"), never as a chore list.
- A portal item that keeps being offered and never supplied does not escalate. It stays
  a line, at the same weight, for as long as it is true.

**If the operator does want the PDF in the package, two paths exist, and the portal line
names both.** Sending the PDF to Ellen in Telegram is the obvious gesture, and it works
since Casa 0.325.0 ([ha-casa-app#1036](https://github.com/bonzanni/ha-casa-app/issues/1036)
receives the document) and 0.326.0
([#486](https://github.com/bonzanni/ha-casa-app/issues/486): `share_inbound_file` hands it
to this plugin) — it is filed in the operator's next text turn or by the next pass, since
arrival itself runs no turn (§"Handing it a document"). Email to self is
the other: the pass runs one targeted search for recent self-addressed mail carrying
attachments whenever anything is in the portal/missing state, so a forwarded PDF is
found even when its subject matches nothing about the transaction. Such documents carry
`source=manual-email`, which is recorded and changes nothing about how they are read or
judged. An earlier revision (2026-09-21) verified that Telegram documents were silently
dropped at the channel layer; that stopped being true at 0.325.0, and the floor is above
it.

### Recognising a reply, when nothing guarantees the context survived

**This is the weakest joint in the design and it is stated as such.** The earlier draft
assumed a reply arrives with the sheet still in mind. It often does not:

- days can pass, and a dozen unrelated conversations with Ellen can happen in between;
- Ellen's session can end, be reset with `/new`, be recycled by a reload, or be started
  fresh by a capability change (casa v0.322.0 deliberately starts a NEW session when an
  agent's delegate/job/executor set changes) — so her conversational memory of the sheet
  may simply not exist;
- Telegram's own reply-to gesture *looks* like it disambiguates, and does not: casa's
  inbound context carries the incoming message's id and not the message it replied to
  (`telegram.py:1647`), so that metadata never reaches Ellen at all.

**Therefore recognition may never depend on the conversation.** Three things make it
work anyway:

1. **The sheet lives in the store, not the chat.** An open sheet row survives session
   loss, `/new`, restarts and a month of other conversation. `list_quarter_state` answers
   what is open, and the render log answers what the operator was last shown for each of
   those items — both from disk, neither from memory.
2. **The skill carries a standing trigger rule**, and a skill is loaded per session — so
   it is present even in a session that has never seen a sheet. It says: before treating
   a message as ordinary conversation, if it contains a bare small integer, or reads as
   an approval/correction/rebuild ("all good", "wrong", "rebuild it", a vendor name with
   a verdict), **read the store first**. It answers; memory is not consulted. A
   description that matches no open item is not a correction, and the message is handled
   as whatever else it is.
3. **Missing a recognition is cheap, and that is what makes this acceptable.** If Ellen
   fails to spot a sheet reply, nothing is applied, nothing is corrupted and nothing is
   lost: the line keeps its existing state and reappears on the next sheet. The failure
   mode is a repeated question, not a wrong decision. Compare the alternative — an
   eager rule that claims ambiguous messages — whose failure mode is silently mutating
   the books from a sentence about something else.

**The escape hatch is one word.** Saying "accounting" anywhere in the message always
binds it to this plugin — "accounting: the Zapier one is wrong" — which matters when the
conversation has moved on since the view was rendered. In practice it is rarely needed,
because pull-only means the correction usually follows the operator's own question by
seconds.

**What still cannot be promised.** Recognition is model judgment, and model judgment is
not a guarantee. The design bounds the damage (nothing mutates unless a line resolves,
and every applied change is echoed in a receipt) rather than claiming the judgment is
reliable.

**Filed upstream, not depended on:**
[ha-casa-app#1037](https://github.com/bonzanni/ha-casa-app/issues/1037) asks casa to
propagate the inbound `reply_to_message_id`. If it lands, a reply made with Telegram's
own reply gesture binds exactly — the operator's gesture already said which message they
meant, and today it is discarded — and the trigger rule above becomes a fallback rather
than the primary path. Nothing here waits for it, and nothing changes if it never
arrives.

### A quiet week, and coming back after a gap

**A quiet week is simply quiet**, because every week is: the pass never speaks. What
would have been the liveness signal now rides the answer to the operator's own question,
as the coverage line (`Bank checked through 20 Sep`). That is strictly better —
it arrives when they are actually reading, and it tells them how stale the picture is
rather than merely that something ran.

**A sheet is always current state, never a replay of missed weeks.** After six ignored
sheets the seventh is not six sheets long: it shows what is missing *now* and what is
still unreviewed *now*, oldest first, capped at a readable length with the remainder
counted (`+14 older uncertain pairings — say "show older"`). Nothing accumulates into a
wall, nothing is lost by having been skipped, and the sheet never remarks on the gap.
The operator who returns after a month gets the same page they would have got anyway,
which is the entire reason state lives in the store rather than in the conversation.

### Asking between passes

The plugin is also a thing to ask, not only a thing that reports. "What am I missing for
Q3?", "did the Adobe invoice ever arrive?", "how much software spend this quarter?" are
answered from the store through the read tools Ellen already holds — no new mechanism,
no new tool, nothing to build beyond saying so in the skill. Two rules: the answer comes
from the store and never from the last sheet's text, and a question is never treated as
a correction (asking "is 4 right?" changes nothing about line 4).

### New portal vendor

First classification of a vendor as portal-only triggers the one-time link research
(specialist, WebSearch): prefer the authenticated deep URL
(`console.vendor.tld/invoices`) over the marketing domain. Filed via `upsert_counterparty`
with provenance. Every later quarter resolves instantly from the KB.

### Packaging (there is no quarter-end event)

**Operator ruling, 2026-09-21: packaging is a thing the operator asks for, and nothing
else.** There is no quarter-end pass, no quarter-end trigger and no automatic delivery.
This follows from §"What a pass works on": if passes already chase open items across
quarter boundaries, already revalidate delivered quarters, and a late invoice already
rebuilds a package, then "quarter end" was never a state transition in this system. It
was only ever the moment somebody wanted a zip.

**One cron remains in the whole design** — the weekly pass. The `quarterly_accounting_
quarter_end` trigger in §Setup is deleted.

**"Give me the accounting for Q1" packages Q1 as currently known and sends it.** Any
quarter, at any time, as many times as the operator likes:

- Asking in October for **Q1** builds Q1 from what is known today — including every late
  invoice that arrived since the last time they asked.
- Asking in August for **Q3**, the quarter they are standing in, builds what is known so
  far. That is a legitimate thing to want; it is simply incomplete, and says so.
- Asking twice in a row builds twice. The second build is cheap and nobody has to decide
  whether it was "necessary".

**What this deletes, and it is the point.** The per-quarter revision counter, the atomic
reservation of a revision number, the `supersedes rN-1` vocabulary, and the input-digest
rule that existed only to stop revision inflation — all gone. They were machinery
serving a numbering scheme, and the numbering scheme existed because packages arrived
unasked and had to be told apart. Packages the operator asked for, by date, need none of
it.

**What survives the deletion, because it was never about revisions:**

- **The build still freezes a snapshot in one transaction and renders from it.** This is
  what stops a correction landing mid-build from producing a zip that is half
  pre-correction and half post (the round-3 finding). Same discipline, no numbering.
- **A delivery log** — what was sent, when, and what it contained. Not version
  semantics: it is how the next package can say what changed, and how the operator can
  ask "what did I send my accountant in October?".

**Naming carries what revisions used to.** A date is better than a revision number for
the one person who has to act on it — the accountant — because they can order two files
without knowing anything about the scheme:

```
books-2026-Q1-2026-10-14.zip          a closed quarter, built 14 Oct
books-2026-Q3-partial-2026-08-14.zip  a quarter still open, built 14 Aug
```

`partial` is in the name because it is a fact about the **period**, not a version: a Q3
package built in August is not a draft of the final one, it is a picture of an
unfinished quarter, and an accountant must never mistake it for a filing set. A same-day
rebuild appends a time rather than inventing a counter — **and the build reserves its
filename by exclusive create**, widening to seconds and then to a short suffix until the
name is unused. Minute precision alone is not enough: two rebuilds at 14:12:10 and
14:12:45 with a correction between them would otherwise write different contents under
one name (round-5 finding). Every delivered build is retained under the exact name it
was delivered as, and the delivery log points at that file rather than at a recomputed
name.

**The caption says what changed**, computed by diffing against the delivery log rather
than asserted by a numbering rule:

```
Accounting Q1 2026 · 47 payments · 44 with invoices
2 invoices added since the package from 14 Oct.
3 still missing — listed in notes.md.
```

If nothing has changed since the last one, it says that too, and sends the zip anyway.
The operator asked; they get it.

**Accepted consequence, stated rather than discovered later: nothing will remind the
operator to send their accountant anything.** Quarter-close notifications were considered
and declined (§"When the plugin may speak first"), so the filing rhythm is entirely
theirs. This is the right trade for someone who knows their own deadlines, and it is a
deliberate risk rather than an oversight.

**Package membership is defined by the transaction's booking-date quarter, never by
where an invoice file happens to be stored.** The invoice-date storage path
(`invoices/<YYYY-Qn>/…`) is custodial only. The builder selects every transaction
booked in the quarter and pulls each matched invoice PDF into the package regardless
of its storage directory — so an invoice dated June 30 that pays a July 1 charge
ships in the Q3 package (the cross-quarter case both reviewers flagged). An
unmatched document appears in no package folder; it is listed in `notes.md` of the
quarter it was ingested in.

**The build is one transaction and deterministic.** `build_package(quarter)` freezes
the match snapshot and the imported ledger rows before writing any file, so the zip is
internally consistent even if a correction lands while it renders, and the same frozen
inputs produce the same bytes (fixed ordering, fixed timestamps, stable serialisation).
`notes.md` opens with what is missing and closes with `built <date>, covers <period>,
bank data through <date>, digest <hash>` for diagnostics.

```
<slug>-<YYYY-Qn>[-partial]-<YYYY-MM-DD>.zip
├── invoices/            # SnelStart bulk upload set: matched documents of kind
│                        # `invoice` whose expectation is REQUIRED —
│                        # YYYY-MM-DD_vendor_amount[_hash8].pdf; a `proposed` line
│                        # never lands here
├── sales-invoices/      # matched documents of kind `sales-invoice`, required tier
├── credit-notes/        # matched documents of kind `credit-note`, required tier —
│                        # a vendor's behind a refund received, the B.V.'s own behind
│                        # a refund paid
├── documents/           # every other matched document: any kind at OPTIONAL tier,
│                        # and `payslip` / `statement` / `receipt` / `other` at either
│                        # tier — one destination per document, never two
├── ledger.csv           # full quarter, both directions: date, amount, currency,
│                        # direction, counterparty, vendor, status, confidence label,
│                        # expectation (kind, tier), document filename OR portal
│                        # deep-link, notes
├── ledger.xlsx          # same rows, for the accountant's tooling (operator decision,
│                        # 2026-09-21: ship both)
├── unresolved/          # only when non-empty: retained candidate PDFs for lines that
│                        # did NOT match, so the accountant has the evidence without
│                        # another mailbox hunt — deliberately NOT in invoices/, which
│                        # is the bulk-upload set
└── notes.md             # missing REQUIRED documents first (purchase invoices, then
                         # sales invoices, with best links), then rows not yet
                         # classified, then nice-to-haves not found, then anomalies,
                         # unsupported relationships (split/aggregate payments, credit
                         # notes), KB changes, commentary
```

**Both ledger formats ship, and the XLSX costs something — name it.** The server is
stdlib-only, so `ledger.xlsx` is a minimal hand-written SpreadsheetML workbook
(`zipfile` + a fixed `xl/worksheets/sheet1.xml` template, inline strings, no styles
beyond a header row). That is perhaps 100 lines and a pinning test asserting the file
opens and its cell values equal `ledger.csv`'s. `ledger.csv` is `csv.writer` and is the
authoritative one: if the two ever disagree, the CSV is right and the XLSX is a bug.

**The confidence label travels with the row.** A `guessed` or `no-ref` match is marked
as such in both ledgers, so the accountant sees which pairings the machine chose under
competition rather than being handed a uniform-looking set of assertions.

Missing documents never block shipping — and neither does a row the classifier has not
reached (operator question, 2026-09-22: "what happens if a package is requested and this
information is still missing?" — it ships). Ledger rows read `MISSING` for a required
document with the best available link, `UNCLASSIFIED` for a row whose expectation is not
yet known, `NO-DOCUMENT` for expectation `none`, and `OPTIONAL-MISSING` for a nice-to-have
that was not found; `notes.md` opens with the required ones and lists the unclassified
ones apart, and the caption counts both ("3 still missing, 2 not yet classified"). **Routing is total
and exclusive over (kind, tier)** (round-25 finding: a required receipt had no folder and
an optional invoice had two): `invoice` + `required` → `invoices/`; `sales-invoice` +
`required` → `sales-invoices/`; `credit-note` + `required` → `credit-notes/`; every other
matched (kind, tier) → `documents/`; an expectation of `none` never has a matched document,
so nothing routes for it. The build is deterministic — the same
frozen inputs produce the same bytes (fixed ordering, fixed timestamps, stable
serialisation) — which is what lets a caption say "identical to the package from 14 Oct"
as a computed fact rather than a judgement. Supply stragglers and ask again: the next
build is a new file under a new name, the earlier one is retained, and resending any
built file is always free.
Delivery: atomic write to the plugin outbox → `send_media(kind="zip")` (shipped,
#482) → operator's Telegram, from the direct turn in which they asked. The outbox copy
is consumed on send (or reaped at 2 h); the canonical package stays in the data dir. Two
edges the shipped tool imposes: a transport timeout is reported as
`delivery_uncertain` — the send may have landed — so a retry resends **that exact built
file** rather than building a new one, and the date in the filename makes a duplicate
arrival self-evident; and the `zip` kind
caps at **20 MB**, which a quarter of PDF invoices can approach, so the build
**preflights the actual zip size** and an oversize package fails visibly, with the
canonical zip retained and `notes.md` naming the offenders. v1 does not silently split
the requested single zip, drop invoices or re-compress the operator's PDFs; if a real
quarter crosses the cap, the split rule is decided then, with evidence.

After a `delivery_uncertain` the package is NOT re-sent automatically: the send may have
landed, and a second zip in the accountant's hands is worse than a question. The next
pass offers to resend that exact file, in words, like everything else.

**Email delivery is in the same delivery log and under the same uncertainty rule** (round
23, both reviewers). When the operator asks for the package by email (§Tool surface,
`stage_for_delivery`; §"The reversibility ladder"), the delivery-log entry records the
channel, the exact built file, and the `request_id` passed to gmail's `send_email`, and is
marked delivered only when `send_email` returns a message id. gmail's own sent-log records
a `request_id` **only after a successful send**, so a transport timeout leaves no record
on either side and a retry with the same `request_id` sends a second email (Astra
reproduced it against gmail 0.9.0's real send path). Therefore an ambiguous email outcome
— timeout, an error after the request was accepted — is recorded as `delivery_uncertain`
against the retained build exactly as a Telegram one is, is never retried automatically,
and is resent only when the operator asks, as that exact file.

## Privacy

- The repo ships **zero** personal or company data: no IBANs (bank-feed owns
  accounts), no company name, no vendor list, no operator identity. This spec
  deliberately says “the operator's B.V.”.
- The zip filename prefix is a **stored setting in the data dir**, defaulted from the
  bound account's label (or `books`) and changeable by asking. It is not an environment
  variable, not a vault item and not an `op://` reference — the v1 text called it
  1Password-referenced and a plain setting in the same breath, and it is now neither.
  A company name is not a credential, and the plugin declares no required environment
  variables at all.
- Everything identifying lives only in the data dir.
- House publication guards anyway (pre-commit deny-patterns, gitleaks) as
  belt-and-braces for a private repo.

## Error handling

- Tools fail explicit and loud; no silent fallbacks.
- Ingest idempotent by content hash; over-ingestion harmless by design.
- All match mutations are CAS on the match `revision`; stale operator taps and
  crossed writes are rejected, never absorbed. Ellen re-asks with current facts on
  a CAS rejection of a review-sheet answer.
- Bank-feed mirroring is one projection per transaction lineage, reconciled by an
  unconditional sweep: no per-match annotation state, no repair queue, no selection
  rules to get wrong. Its guarantee is stated as what the platform can actually support —
  once decisions, row identity and stale writes settle, the next complete reconciliation
  restores the current projection — and not as "the ledger never shows an obsolete
  assertion", which bank-feed's unconditional write API cannot deliver.
- Server-enforced cardinality: no second active match per invoice or transaction.
  Split and aggregate payments are not modelled at all in v1 (§Cardinality); they are
  retained as documents and explained in `notes.md`.
- **Silence is a supported answer everywhere.** No review reply leaves every line as
  the pass left it; the quarter's package ships regardless and can be rebuilt on
  request. Nothing decays, nothing is lost, and nothing is inferred from silence.
- With no button questions, the whole durable-ask failure surface (lost outcomes,
  `operator_busy`, trigger-rewrite cancellation, restart replay) is **out of this
  plugin's failure model**. That is the main practical dividend of answering by sheet.
- A review reply that names a line whose revision moved underneath is reported on that
  line and re-offered on the next sheet, never applied to the new proposition.
- Casa restart mid-pass: workbook + store hold everything except the in-flight turn.
- Packaging with open residue ships `MISSING` rows rather than blocking.
- **No clean ledger, no external write.** A pass that finds `acct@<version>` already
  registered in bank-feed while its own store is fresh performs no tag or note write and
  names the backup to restore; a ledger restored under an unreset store stops the pass
  before any write (§Setup, "Test install").

## Testing

- Pure-Python unit tests over store, index, naming, KB, workbook, package build —
  no casa runtime required (bank-feed's model).
- A **fixture quarter** (synthetic transactions + synthetic PDFs) driving an
  end-to-end `build_quarterly_package` assertion: exact file set, ledger rows,
  MISSING handling, zip layout — including the red cases from spec review rounds
  1–3: a cross-quarter match (June invoice, July booking → ships in Q3's package);
  a superseded `row_id` retargeted by sweep step 0; an ambiguous same-vendor
  same-amount pair that must stay `proposed` **including when the pair arrives
  across two passes**; a stale-revision `confirm_match` that must be rejected by
  CAS; a demotion after which the transaction's recomputed projection no longer asserts
  a match (never a stale
  `acct::matched`); a retarget colliding with a successor-row match (two machine contenders both
  moved to non-active `conflicted` in one transition — residue line emitted,
  cardinality intact — and, the round-13 case, an operator's later pairing on the
  successor **kept** while the machine contender alone goes `conflicted`); a
  rejected match with `repair_owed` whose row is superseded before cleanup (repair
  rewritten to the live successor, stale tags corrected); and concurrent package builds
  that must yield
  distinct reserved filenames with snapshot-consistent contents.
- The review sheet is store state, so it is unit-testable and must be tested: line
  a correction is bound to the revision the operator was SHOWN, so a pass that moved
  the proposition between rendering and reply refuses on that item and applies the
  others (a partial reply is normal, not an error); a correction naming an item that was
  never shown re-renders and mutates nothing;
  two concurrent builds of one quarter do not interleave into one zip (each renders from
  its own frozen snapshot); a build of an open quarter is named `partial` and says so on
  the first line of `notes.md`; **two rebuilds within one minute both survive under
  distinct names** (exclusive create, widening precision), and neither overwrites the
  other or an earlier build; a rebuild with nothing changed still produces a file and
  says so in the caption, reserving no revision because there are none; the caption's
  "what changed since" line is computed from the delivery log rather than asserted; an
  email delivery is logged with its channel, exact file and `request_id`, marked
  delivered only on a returned message id, and a simulated timeout leaves it
  `delivery_uncertain` with no automatic retry (a test that retries the same
  `request_id` after a timeout must observe a second send against gmail's real server
  function, which is why the rule exists); and
  the ledger of a quarter containing a superseded predecessor and its active successor
  totals the payment ONCE.
- The round-5 red cases, from the independent design and its comparison: a material
  change to an ACTIVE row (amount corrected under an unchanged `row_id`) must
  invalidate an accepted match — the case supersession-only revalidation missed; two
  different PDFs carrying one issuer + invoice number must refuse automatic acceptance
  on both sides; an order confirmation and a net-vs-gross amount must not read as an
  invoice; an invoice naming a person rather than the B.V. must land `recipient?`; a
  truncated search must label `partial-search` and must never be rendered as `clean`;
  an operator's `acct::` tag removal on a clean record must be detected by the next
  pass; an accounting tag on an unclassified row must leave it in tx-classifier's
  untagged queue (pinned against bank-feed 0.9.0's real `list_transactions(untagged_only=true)`
  and `queue_totals`, not a double); and every tag the plugin writes must be in
  `owned_tags` and satisfy bank-feed's grammar
  `^(?:[a-z][a-z0-9-]{0,15}::)?[a-z0-9][a-z0-9-]{0,31}$` with the `acct::` prefix present
  (a pinning test, since the v1 spec's whole tag vocabulary was invalid and the round-9
  one was un-namespaced).
- **The rendered view and the reply grammar are testable and must be pinned**: a view
  goes inline, and one that would exceed the message limit **caps** — largest amounts
  first, with a counted remainder the operator can ask for — rather than becoming an
  attachment or being split across messages; no rendered line exceeds the phone-width
  budget; nothing rendered carries a line number; a correction naming two items leaves
  every other item's author untouched (implicit approval gets its own red case); "all
  good" confirms only what was shown; "the BCK*XYZ one is my accountant" does not set
  an exemption; "Zapier and Vercel" and "all good except the Zapier" apply
  nothing and answer with the phrasing that works; a description matching two open items
  asks rather than picking; a description matching none is reported and never redirected
  to a near miss; **an item with no shown revision produces a re-render and no
  mutation**; **a view that was rendered but whose delivery failed does not advance the
  shown-revision pointer**; and the receipt is generated from committed results, so a
  test that stubs the commit sees the receipt change.
- **The no-invention mechanisms are testable at the fixture level and must be pinned —
  while noting that fixture tests cannot establish runtime enforcement over model
  output**: given a tool returning
  a known rendering, the delivered message contains it verbatim rather than a paraphrase;
  a tool ERROR produces an "I can't read it right now" reply and never a state claim,
  including when the conversation already contains an earlier successful answer; a second
  question in the same conversation issues a second read rather than reusing the first;
  and a "how much altogether" follow-up calls the tool rather than summing the numbers
  printed in the previous message.
- **The projection sweep gets the four reproduced failures as pinned red cases**, since
  each one is now supposed to be unreachable rather than handled: a stale writer that
  lands `acct::matched` after a rejection has been cleaned up (next sweep restores the
  desired `{acct::open}` — not the empty set the pre-restoration text expected, round-10
  finding); a rejected projection whose row is superseded between the stale
  write and the sweep (the lineage, not the row id, keeps it enumerable, and the tags are
  found on the successor); a rejected match and an accepted replacement on ONE
  transaction (one desired set computed from both, reaching the same fixed point from
  either write order — the oscillation case); and a transaction with no match at all
  (still enumerated, desired `{acct::open}`, and — the regression this round exists to
  pin — **the row stays in bank-feed's untagged classifier queue while carrying it**).
  Plus the reducer itself:
  `actual := (actual − owned) ∪ desired` must reach the same result from an arbitrary
  starting tag set, including one containing foreign tags it must not touch. **And the
  desired-set table's precedence** (round-10 S1): a payment the operator exempted, and a
  `none-expected` vendor's payment, desire `{acct::no-document-expected}` and never
  `{acct::open}`; a portal vendor's unpaired payment desires both `acct::open` and
  `acct::portal`; a row with 64 namespaced tags from other owners is reported
  unprojectable rather than retried; a row on another account and a row
  booked before the watermark get no projection and no `acct::` write, and a CRDT row on
  the bound account gets one (operator, 2026-09-22); and moving the
  watermark earlier admits exactly the rows it newly covers on the next pass. **And the
  round-11 transitions**, each pinned against bank-feed's real `apply_plan` rather than
  a double: exempting a payment that carries an active `proposed` (or `matched`) rejects
  the pairing in the same transaction and the next sweep shows `acct::no-document-expected`
  alone; `propose_match` on an exempt lineage is refused; an operator `record_match` on an
  exempt lineage clears the exemption; a managed row corrected in place to before the watermark
  desires `∅`, its `acct::open` is removed by the next sweep, and the projection is still
  enumerated as `ineligible`; a June 30 row corrected to July 1 across the watermark is
  admitted on the next pass although it was observed and skipped before; a DBIT corrected
  in place to CRDT keeps its projection and re-derives its expectation from its
  classification (a `sales-invoice` expectation replacing an `invoice` one); and a pending row admitted on its `value_date`
  follows its supersession to the booked successor. **And the round-12 decision log**: a
  pending row with `booking_date NULL` is admitted from the snapshot (pinned against the
  real `export_history` output, since `list_transactions` omits it); a pass with no
  snapshot reports admission and revalidation as not checked; the same four decisions
  applied in each of the 24 orders to two lineages that then merge reach the desired state
  the two rules predict, and the merged log has one lift for one lift appended; an
  `author=operator` write without a valid `render_id` is refused, and one carrying a
  render id for a different item is refused; and a `propose_match` on an exempt lineage is
  refused and appears in the residue. **And the round-13 reducer order**, pinned against
  bank-feed's real `apply_plan`: an operator-accepted €100 pairing whose row is corrected
  in place to €90 is invalidated, not overridden — the log keeps the entry, the lineage
  shows `acct::proposed` with a residue line, and an operator confirmation against the
  new facts restores `acct::matched`; an exemption survives the same correction; an auto
  proposal followed by the vendor becoming `none-expected` is retired `rejected` as a kind
  change (erratum 2026-09-27, plan D8: §Match records' later ruling wins); a
  delivered ledger row that bank-feed then supersedes or vanishes is detected from a
  snapshot that retained non-active rows (a test that imports active rows only must
  fail); and the reducer, given the same merged log and row in any entry order, is a
  pure function of them. **And the round-14 traces**, each run through the numbered steps
  as written against bank-feed's real `apply_plan`: operator pairs A at €100, correction to
  €90, operator pairs B at €90, correction back to €100 — B is the current pairing, shown
  `proposed`, and A is never selected again; `[operator pair P, operator unpair P]` ends
  `acct::open` (or the vendor default), never `matched`; a lineage with machine pairing A@5
  merged with one whose log reads `[operator pair B@10, unpair B@20]` ends with A
  `conflicted` and `acct::open`: the fold meets B@10 while A is active, so A is retired
  `conflicted` (recorded) and stays so when B is unpaired — surfaced as residue for the
  operator to confirm (round 19 settled this trace's expectation, which rounds 14 and 15
  had each stated differently; the sequences are now explicit so it cannot drift again); `unpair Q` of a
  freshly shown `conflicted` candidate beside accepted P moves Q alone to `rejected` and
  leaves P `matched` (both pinned, since a refusal would pass the second assertion alone);
  `[exempt, lift]` ends `acct::open` (or the vendor default), never the exemption's set;
  **the round-17 merges**, against bank-feed's real multi-predecessor supersession:
  exemption at sequence 10 on one lineage merged with a valid operator pairing P at 20 on
  another ends `acct::matched` with **no** `lift` entry written (the clear is derived), so
  merging a third lineage carrying an exemption at 21 folds `E10, P20, E21` to
  `acct::no-document-expected` with P `rejected`; the reverse sequence, E at 20 and P at 10,
  ends P `rejected` and `acct::no-document-expected`; three predecessors carrying
  machine A `matched`, B `matched`, C `proposed` reach the **same** state — all three
  `conflicted`, `acct::open` — under every one of the six merge orders; `confirm_match` on
  one of those `conflicted` ids makes it `acct::matched` and leaves the other two
  `conflicted`, and is refused when its invoice was paired elsewhere in the meantime; and a
  specialist `propose_match` on that lineage without `resolves=` naming all three is
  refused; **the round-18 folds**: A/B `conflicted`, operator confirms B, operator unpairs B
  — A stays `conflicted` and the lineage is `acct::open`, never `matched`; machine A,
  exemption E@10 and operator P@20 across three predecessors reach one state under every
  parenthesisation of the merge (the fold gives one answer: A@5 is active until E@10 rejects it,
  P@20 clears E and is current — so A `rejected`, P `matched`, whatever the walk order); E@10,
  P@15, E@20 end exempt with P `rejected`; an operator `record_match` on an exempt
  lineage appends `lift` then `pair` and ends `acct::matched` (the earlier fixture —
  confirming a `conflicted` candidate under an exemption — is unreachable, since an
  exemption rejects every conflicted candidate; round-19 finding); and
  `resolves=[A, B]` after the operator confirmed B is refused whole, B untouched; **the
  round-19 folds**: machine A@10 and B@30 on separate predecessors merge and are
  `conflicted` (recorded); the operator pairs B's invoice to payment D@40; a third
  predecessor carrying `[exempt@20, lift@25]` then merges — B stays `conflicted`, D keeps
  the invoice, and the occupancy check would have refused B's activation even without the
  recorded retirement (both pinned); `[machine A@10]` merged with `[machine B@20, unpair
  B@30]` ends A `conflicted`, `acct::open`, because normalization ran inside B's
  transition; a `propose C@40, resolves=[A, B]` re-folded after a merge that meanwhile
  rejected A and activated B still rejects both and leaves C the lone candidate; and a
  `lift` folded from another lineage that meets no exemption is a no-op; **the round-20
  activations**: `[propose P@10, confirm P@30]` merged with `[exempt@20]` ends
  `acct::matched` — E@20 retires the activation P@10, the confirmation P@30 is a new
  activation the retirement does not touch — and re-folding it again does not change it;
  predecessors `[machine A@10]`, `[exempt@20, lift@25]` and `[propose C@30, confirm C@62]`
  end C `acct::matched` under every merge order, because the A/C collision retired C's
  activation at 30 and not its confirmation at 62; a recorded retirement never moves a
  `rejected` activation to `conflicted`; and two connections — and separately two processes — activating one invoice on two
  lineages in the same instant leave exactly one active and one recorded `conflicted`,
  with the later-serialized sequence the loser, and forced lock contention within the
  bound still applies both writes in order; `lift` with
  no exemption standing is refused and appends nothing; a merge that brings machine
  pairing A onto an exempt lineage leaves A `rejected` and the lineage
  `acct::no-document-expected`; an exemption on a portal vendor's row keeps `acct::portal`; an auto proposal at €90
  whose row is corrected to €100 stays `acct::proposed`, not `open`; and a correction that
  is reverted restores `acct::matched` with both changes in the residue.
- **Document expectation** (operator ruling, 2026-09-22), pinned against tx-classifier's
  real tag vocabulary and bank-feed's real `classification_state`: a row in the
  classifier's queue derives *unknown*, desires `{acct::open}`, is rendered under "not
  yet classified" and is searched for nothing; the same row, once tagged
  `internal-transfer`, flips to `{acct::no-document-expected}` in the next reduction, and
  once tagged `income, salary` on a DBIT flips to `∅` (optional) and leaves "what am I
  missing"; a CRDT tagged `income, consulting` derives `sales-invoice, required` and a
  held payslip does not satisfy it while a held sales invoice does; `set_expectation` on
  a counterparty beats the mapping and loses to `set_exemption` on one payment; the
  decision table in §"Document expectation" is the oracle for a fixture of twenty
  representative tag sets in both directions, pinning in particular `income, refund` on a
  DBIT → `credit-note, required` (erratum 2026-09-27, plan D9: decision-table row 7 is the oracle), a parked row with a counterparty override → the override, a parked row
  without one → unknown, and a chain-level `set_expectation` beating the shipped row;
  an `unclassifiable` row is `required`; a package built with two unclassified rows
  ships, lists them apart from `MISSING`, and its caption counts them; every (kind, tier)
  pair `set_expectation` accepts routes to exactly one package folder, a required receipt
  to `documents/` and an optional invoice to `documents/` and never `invoices/`; **and
  reclassification invalidates** (round-25 S1, both reviewers), pinned against
  bank-feed's real tag tools: a payslip matched to a DBIT tagged `income, salary` becomes
  `proposed` with a residue line when the row is re-tagged `transport, fuel`, and the
  lineage's `invoice, required` expectation enters the next search; a machine-matched purchase
  invoice is retired `rejected` (recorded, residue line) when the row gains
  `internal-transfer`, while an operator-matched one becomes `proposed` with the reason
  and a right-kind candidate found later lands `conflicted` beside it; a lineage whose
  machine pairing was retired for a kind change is in the next pass's search plan; a
  delivered ledger row re-tagged into a different kind is reported as a changed quarter
  from the classification observation, with the snapshot unchanged (a test that reads
  kind from the export must fail); and a tier-only change (`required` → `optional`)
  leaves a matched pairing matched. **And the round-26 rows**: a CRDT tagged `refund`
  or `income, refund` derives `credit-note, required`, a DBIT tagged `refund` the same,
  and a held credit note satisfies it while an invoice does not; a row tagged only
  `unclassifiable` derives `required` (row 3) and never `unknown`; `salary, fees` on a
  DBIT derives unknown, is tagged `acct::open`, and is shown under "classification
  conflict"; `transport, fuel, refund` derives `credit-note, required` and is not a
  conflict; and a required credit note routes to `credit-notes/`. **And the round-27 rows**: a CRDT
  tagged `income, refund` and one tagged `refund` alone both derive `credit-note, required`
  at row 7, and a chain override for `income, refund` applies to both; `confirm_match` of
  a payslip on a lineage whose expectation is `invoice` is refused with the facts, and
  goes through after `update_document_metadata` corrects the kind or `set_expectation`
  corrects the expectation; a document whose machine pairing was retired for a kind change
  is listed by `list_unmatched_documents` and proposed again when the tags revert; a
  DBIT `refund`'s search plan targets Sent and its credit note naming the client is not
  labelled `recipient?`; and a view over a scope with one lineage last observed 13 Sep and
  the rest today prints `classification through 13 Sep`, while a renderer that prints the
  bank date without the classification date fails; a "what am I missing" view over a
  quarter holding a required lineage observed 22 Sep and a `none` lineage observed 13 Sep
  prints `classification through 13 Sep`, because membership precedes the filter; a
  pending row admitted on its `value_date` is a member of that quarter's views; a Q2
  lineage cached `none` and last observed 13 Sep, re-tagged since to `transport, fuel`,
  is a member of a Q3 view's coverage although no view prints it, and the persisted
  membership set of the rendering contains it; a
  scope holding a lineage admitted this pass and never observed prints `N never checked`
  and keeps the date of the observed ones; an empty scope prints `no transactions yet`
  and no dates; correcting a wrong-kind document's `kind` bumps the item's revision, a
  `confirm_match` bound to the old shown revision is refused, and the correction flow
  re-renders before applying; an operator `record_match(invoice)` on an exempt DBIT
  tagged `transport, fuel` succeeds, the guard being evaluated after the lift, while the
  same call on a lineage whose counterparty override says `none` is refused, rolls the
  lift back, and its refusal names the override; and
  `set_exemption` on that item rejects the pairing and produces a receipt naming both
  effects.
- **Test install and reset** (operator, 2026-09-22), against bank-feed's real backup
  subsystem once it exists — and, for the mint and the restore protocol, against **real
  SQLite in WAL mode with two connections from the start**, never a double, since round 32
  showed a double would have concealed that the backup API cannot restore into a database
  with an open transaction; the rest against a double until the subsystem exists, stated as
  such: every tag and note
  write the skill prescribes carries `workflow="acct@<version>"`, pinned by a grammar test
  on the work orders; two specialist processes writing concurrently on a fresh ledger
  produce exactly one `install:acct@<version>` backup, taken before either write, and
  restoring it leaves zero `acct::` tags and zero accounting notes (the round-31 red case:
  two compliant passes each backing up and each leaving a note); a weekly backup taken
  after the first note, restored, still reports `acct@<version>` registered, so the next
  pass refuses to write and names the install backup; `reset_store` wipes the store with
  no precondition, and the pass after it refuses to write until the ledger is clean; a pass
  that finds the restore generation advanced under an unreset store stops before any write
  and never "continues"; a version upgrade without a restore mints a second install backup
  and `check_setup` reports the older version's writes still present; a restore across a
  renewed consent keeps the live `sessions` and account bindings and restores everything
  else, and the next sync succeeds against the live consent; a crash between `mint pending`
  and the rename leaves a temp file that recovery removes with an `aborted` line; a crash
  between the rename and the ledger commit leaves an `orphan` backup and no registration,
  and the next write mints again; a crash between the ledger commit and the terminal line
  is settled by recovery appending `committed` once; the same backup restored twice in a
  row yields two restore operations, two `committed` lines and a generation advanced by
  two; a restore on the real schema replaces the ordinary tables and rebuilds `notes_fts`,
  and `notes_match` finds a restored note afterwards; a rename without a directory fsync
  followed by power loss is the case the directory fsync exists for, pinned by a test that
  checks the final file's presence after a simulated loss; two processes recovering the same pending restore at
  once produce one `committed` line and a generation that advances by one; process A
  commits restore R1 and dies before its terminal line while process B is already open,
  then B restores R2 of the same backup — B's settlement records R1 `committed` before R2
  begins, and the generation advances by two; the same A-dies-after-commit case followed
  by already-open B answering `list_backups` reports the advanced generation and the
  unregistered workflow, because settlement ran before the answer, and a pass with a
  populated store then stops before any write; a `list_backups` racing a restore has exactly two
  serialized outcomes and the test accepts both and nothing else (round-38 correction of
  this line): the listing that acquires the locks first blocks the restore through its
  capture and returns the pre-restore pair (generation unchanged, workflow registered); a
  restore that commits first — and dies before its terminal line — is settled by the
  listing, which returns the advanced generation and the unregistered workflow; an answer
  of "generation unchanged, workflow unregistered, restored rows visible" is the one that
  releasing the locks between settlement and capture produced, and must never occur; a
  pass reads generation 0, a restore then lands, and the pass's first `tag_transaction`
  with `expected_generation=0` is rejected and the pass stops with no write landed
  (round-38 S1); a `tag_transaction` carrying a workflow string without
  `expected_generation` is refused; a restore commits and dies before its terminal line,
  then an already-open process issues a `tag_transaction` with the generation it read
  earlier — the write's own settlement records the restore, the comparison fails, and
  nothing is written or minted (round-39 S1) — including when the restored backup is a
  weekly one that still carries the workflow registration, so no mint would have run and
  nothing but the per-write settlement stands between the stale generation and a tag on
  a reused row id (Astra's reproduction); a `tag_transaction` whose caller read the
  generation, then let a restore commit, then acquired the lock, is rejected, since the
  comparison happens after `BEGIN IMMEDIATE`; and a weekly `backup(reason="weekly")` interrupted
  after its pending line is settled at the next open exactly as a mint is; a crash between the restore's commit and its `committed` line is settled by
  the in-ledger marker at the next open, and the generation counts it once; a restore performed as attach-and-replace inside
  `BEGIN IMMEDIATE` while a second process holds a read transaction leaves that process
  seeing the restored rows on its next write transaction; a disk-full failure during the
  mint's copy rolls back both the registration and the write; and after restore + reset the ledger carries no
  `acct::` tag and no accounting note, which is the property the whole loop exists for.
- **Intake and recognition**: a self-addressed mail carrying a PDF is ingested by the
  targeted sweep and matched like any other document; a message naming a number that is
  not a live line is NOT treated as a sheet reply; `all good` is a sheet reply only
  while a sheet is the most recent thing sent; and a question ("is 4 right?") never
  mutates line 4.
- **Entry points**: a reply arriving mid-pass applies against the sheet's recorded
  propositions and reports exactly the lines the pass moved; a reply after the package
  shipped applies and offers a rebuild without performing one; a week spanning the
  quarter boundary produces ONE view; a description matching two open items asks rather
  than picking, and a description matching none says so rather than redirecting; and a
  a stale pass whose generation was reclaimed is refused at every write, including
  `upsert_counterparty` and the delivery log, not merely at the CAS'd match writes.
- **Gap re-entry**: with six unanswered sheets behind it, the next sheet is bounded, is
  built from current state rather than replayed, and carries a count of what it did not
  print.
- **Coverage states render differently**: missing, not-searched and not-checked must be
  distinguishable in the rendered sheet, and a pass that failed must lead with its own
  condition rather than with accounting results.
- An **install smoke test**, because shared plugin storage is asserted rather than
  proven by casa's code: the resident ingests a synthetic document, the specialist
  reads that same record and those same bytes, and the resident stages it for delivery.
  It is a release check, not a reason to build a transfer service.
- **Ended lineages** (added 2026-09-24), each against bank-feed's real `purge`,
  `forget_local_account`, `delete_all_data` and `reconcile` — never a test double that
  deletes rows by hand, since what is being pinned is bank-feed's id and state behaviour:
  an operator-matched lineage whose row `purge` erases ends `erased`, its pairing is
  retired `rejected` by a recorded store retirement with cause `row-ended`, and its
  document returns to `unmatched` (count the documents held by active pairings before
  and after); a re-sync that brings the payment back admits a **new** lineage whose
  pairing with the freed document is a machine pick, never the operator's (assert the
  author) — and **in the same pass** (round-41 finding): with the `purge` landing before
  the pass and the payment re-synced by that pass's own `sync`, the end is committed after
  the import and before triage, so that pass's triage already sees the document as
  `unmatched` and a pairing of the returning row with it is never refused or
  `conflicted` by occupancy against the dead lineage (a design that judges ends in the
  repair sweep fails this); the same outcome when the `purge` lands **between a
  `sync`'s plan and its apply** (build the plan with `reconcile`, `purge_before`, then
  `apply_plan`: inserted 1, superseded 0, and the replacement is a new lineage); after
  `purge` and a re-sync, no new row's `row_id` equals any id the store holds
  (the `AUTOINCREMENT` claim, asserted, not assumed); a row tombstoned by `reconcile` ends
  `vanished` and the sweep removes its owned tags, and a stale `acct::open` written to it
  afterwards is removed at the next cycle; a pass whose snapshot import failed, and a pass
  whose self-check did not find the bound account, end **nothing** even when every
  `get_transaction` read answers `no transaction` (the wrong-ledger case — run it against
  bank-feed's other mode's ledger); an ended lineage is outside every view's membership,
  so a scope whose only stale lineage has ended prints a fresh classification date; a
  delivered row that `purge` erases raises the "delivered quarter changed" message once,
  and not again the next pass; `delete_all_data`, relink and re-sync leave `check_setup`
  reporting unregistered under a populated store, the pass proceeds, and the first write
  mints a new `install:acct@<version>`; restoring `purge`'s pre-erasure backup advances
  the generation and the pass stops for `reset_store`. **The cut chain** (round-41
  finding, Astra): on bank-feed at the floor, `reconcile` supersedes pending #1 (30 Jun)
  with booked #2 (1 Jul), then `purge(before_date=2026-07-01)` deletes **zero** rows of the
  chain; the next import ends nothing, and the operator's pairing follows #1 → #2 as
  ordinary supersession. Run against component 0.13.2 (below the floor), the same sequence
  must show why the floor exists: #1 is gone and #2 survives. A lineage walk that meets a
  surviving row whose `superseded_by` names an absent id ends nothing and reports a broken
  floor (build the ledger by hand for this one, since the floor makes it unreachable). **And the unknown-expectation
  rule**: `purge(user_work=erase)` on a ledger with machine-matched classified rows
  leaves every one of those pairings `matched` (count them) with the rows shown under
  "not yet classified", and once the classifier re-tags a row with a different kind, that
  pairing — and only that one — is retired `rejected`; a mutation that treats unknown as a
  kind change must fail the count. The round-42 trace: an operator pairing on one
  unchanged payment goes `invoice → payslip → unknown` (re-tag, then
  `purge(user_work=erase)` deleting zero rows) and must read `matched → proposed →
  proposed`. The `matched` count after the purge is 0, and a mutation that reads unknown
  as "kind passes" gives 1.
- Matching quality is LLM behavior, not unit-testable here, and the v1 expectation that
  the first quarter would run `proposed`-heavy is **obsolete** — it belonged to the
  strict bar that the loose-matching ruling replaced. The first quarter should match
  readily and be wrong sometimes, with the sheet catching it. What protects the books is
  not the bar's tightness but the loop: every pick is shown, labelled with its actual
  doubt, and one word away from being undone. A wrong match that reaches the accountant
  is a review-surface failure before it is a matching failure.
- A small labelled corpus of hard invoices (ambiguous period, order confirmation, net vs
  gross, personal recipient) can catch regressions in PDF reading between plugin
  versions. A passing corpus is not evidence that live matches are correct, and its
  score never becomes an automatic confidence threshold.

## Changes in other repos

| Repo | Change | Status |
|---|---|---|
| ~~casa-specialist-finance~~ | ~~Role bump and tool grants.~~ **Nothing is needed.** `max_turns` is already 70 (`role/role.yaml:13`), and plugin tools are granted by assignment — "installed ⇒ granted, by construction" (`agent.py:2664-2668`), so no allow-list is edited. Both halves of the v1 row were wrong. | **Struck 2026-09-21.** v1 needs no change in any other repo. |
| ha-casa-app | [#482](https://github.com/bonzanni/ha-casa-app/issues/482) zip media kind. | **Shipped** — closed 2026-08-14. |
| ha-casa-app | [#485](https://github.com/bonzanni/ha-casa-app/issues/485) scheduled-turn `send_media`. | **Shipped** — closed 2026-08-14. |
| ha-casa-app | [#573](https://github.com/bonzanni/ha-casa-app/issues/573) scheduled-turn `ask_user` — the half split out of #485. | **Shipped** — closed 2026-08-15. No longer needed by v1 (no button questions), kept here because the v1 spec was built on its absence. |
| ha-casa-app | [#486](https://github.com/bonzanni/ha-casa-app/issues/486) shared handoff area (shipped, Casa 0.326.0), [#487](https://github.com/bonzanni/ha-casa-app/issues/487) specialist→resident requests. | gmail→store custody goes through the handoff folder (gmail 0.9.0); specialist asks stay structured work orders (#487 still open, still not a dependency). |
| casa-specialist-finance (bank-feed) | **Backups and workflow restore points** (operator, 2026-09-22; §Setup, "Test install"): a new subsystem — `backup(reason)`, `list_backups` (timestamp, size, reason, restore events and registered workflow strings, from a durable index kept beside the database), `restore_backup(id)` as a protected tool restored in place as transactional SQL (attach the backup, replace every ordinary table's rows, `sqlite_sequence` last, the `notes_fts` index rebuilt rather than copied, a `ledger_meta` marker holding the restore operation id) inside one `BEGIN IMMEDIATE` with the consent-binding preflight in the same transaction — not the backup API, which refuses a destination with an open transaction; consistent copies under the write lock (WAL); reasons `weekly` / `install:<workflow>` / `manual`; bounded retention for `weekly`, unbounded for install backups; the weekly backup taken from the finance pass. **The first write carrying an unregistered workflow string mints `install:<workflow>` inside that write's lock, before the write**; a namespaced tag write without a workflow string is refused; a restore unregisters the workflows whose install backups are at or after the restored point. `tag_transaction`, `untag_transaction` and `add_note` gain optional `workflow` and `expected_generation` arguments; a write carrying a workflow string must carry `expected_generation`, and is rejected when it differs from the ledger's restore generation. | **Shipped** — filed 2026-09-22 as [casa-specialist-finance#39](https://github.com/bonzanni/casa-specialist-finance/issues/39), merged 2026-09-23 as component 0.12.0 (bank-feed 0.11.0, the floor). This plugin never writes to bank-feed without it: a bank-feed that refuses the `workflow` argument is below the floor, and the pass says so. |
| casa-specialist-finance (bank-feed) | **Lineage-closed `purge`**: a row is deleted only when its whole supersession chain is; a NULL `booking_date` counts as not before the cutoff; the reply counts the rows before the cutoff kept to keep chains whole (operator ruling 2026-09-25, round 41). | **Shipped 2026-09-25** as component 0.14.0 (bank-feed 0.13.0, the floor), [casa-specialist-finance#56](https://github.com/bonzanni/casa-specialist-finance/issues/56). |
| ha-casa-app | **"Forget everything retained since <timestamp>"** — a time-bounded memory wipe, so a test window's retained engagements can be dropped without wiping the bank. | **Filed 2026-09-22 as [ha-casa-app#1045](https://github.com/bonzanni/ha-casa-app/issues/1045)**. Not a dependency: the residual is stated in §Setup, "Test install". |
| Resident config | **One** weekly trigger (§Setup), plugin assignment to both roles. No quarter-end trigger: packaging happens only when the operator asks. | Operator/configurator action at install time. |

### Open casa issues this plugin designs around

None blocks v1. Each costs a plugin-side line rather than a wait (open as of
2026-09-20):

| Issue | Bite | Plugin-side answer |
|---|---|---|
| ~~[#990](https://github.com/bonzanni/ha-casa-app/issues/990) — `send_message` reports "sent" when the channel delivered nothing~~ | — | **Fixed** (verified 2026-09-21 in `tools.py`: a proven negative is now an error result). This is what lets the weekly sheet ride `send_message` inline instead of always being an attachment — the fix changed the UX, not just the failure mode. |
| [#960](https://github.com/bonzanni/ha-casa-app/issues/960) / [#932](https://github.com/bonzanni/ha-casa-app/issues/932) — a scheduled turn that delivers with a tool and then ends in prose delivers twice (bug, low) | Two DMs per pass. Nothing enforces the clause; only documentation asks for it. | Both trigger prompts this plugin ships carry the closing `<silent/>` clause verbatim, in the install notes. |
| [#975](https://github.com/bonzanni/ha-casa-app/issues/975) — bundle compensation writes an emptied tuple over a refused transaction's files (bug, high, `operator-decision`) | Hits the `casa-specialist-finance` role bump (`upgrade_specialist`), not the runtime: a refused upgrade can take the specialist's saved settings 1 → 0. | Capture the specialist's settings before the bump and verify after. The issue is blocked on an operator decision, so it will not clear on its own. |
| ~~[#1024](https://github.com/bonzanni/ha-casa-app/issues/1024) — install-time vault exploration searches variables no recipe may wire from a vault item~~ | — | **Not reachable.** The plugin declares no required environment variables (the package name is a defaulted stored setting), so no exploration runs for it. |
| [#1036](https://github.com/bonzanni/ha-casa-app/issues/1036) — casa cannot receive an inbound Telegram document (shipped, Casa 0.325.0; reaches this plugin via #486, Casa 0.326.0) | — | Resolved. Email-to-self still works; a document the operator never supplies is still a normal outcome. |
| [#1037](https://github.com/bonzanni/ha-casa-app/issues/1037) — the inbound `reply_to_message_id` is discarded (enhancement, filed by this work) | A reply made with Telegram's reply gesture cannot be bound to the message it answers. | Resolving descriptions against live store state removes the need entirely; the field would only be a convenience now. |
| [#1033](https://github.com/bonzanni/ha-casa-app/issues/1033) — a progress report made while answering the operator is credited to the previous batch (bug, medium) | Only if a pass becomes a `casa.jobs` job. | Settled by the jobs decision below; v1 does not declare a job. |
| [#480](https://github.com/bonzanni/ha-casa-app/issues/480) — apply the per-engagement uid and capability drop to in-process (`in_casa`) engagements too (enhancement) | Would change this plugin's file-access assumptions: the gmail→store custody hop (now through Casa's handoff folder, `0770` and root-owned) and the specialist's `Read` of the document store both rely today on plugins sharing the process user; a uid-dropped plugin could reach neither. INV-CONT-004 already requires a pinned plugin directory to be owned by the dropped uid or world-readable and traversable. | Watch it. If it lands, the store's directory modes and the ingest hop need a re-read. |

Checked and **not** reachable for this plugin as specified: the plugin-setup and consent
issues (#1012, #1005, #1014 — it declares no setup tool and no credentials) and #987
(it declares no `systemRequirements`, so it publishes no `verify_bin` that could shadow
an s6 name). If the implementation adds any of those, re-check them.

## Open items

- Exact auto-match thresholds (date window ~10 days per vendor) to be tuned during the
  first real quarter — **start loose and tighten only where the review sheet shows the
  machine guessing badly**, which is the opposite of the v1 instruction and follows
  from the reversibility ladder.
- **The match rate is the product metric.** After the first real quarter, the number to
  look at is what fraction of DBIT transactions landed `matched` without the operator
  touching them, and how many of those the operator reversed. A high reversal rate is a
  tuning problem; a low match rate means the plugin is not earning its keep, which is
  the failure mode that matters most.
- Ledger column set to be reviewed with the accountant after the first delivered
  package.
- Weekly reminder day/time: operator preference at install (proposal: Monday 09:00,
  now written into the trigger text in §Setup).
- **Background jobs: decided — not in v1.** A `casa.jobs` job buys progress lines, a
  topic and restart resume for a pass that handles 2–6 new transactions a week; against
  that it adds a second engagement, a batch protocol, and a dependency on machinery
  three weeks old with one open defect (#1033). The independent design proposed jobs and
  its own comparison round then cut them, on the same workload arithmetic. Revisit if an
  observed pass actually runs out of turns — that is the evidence that would change it.
  Evidence arrived 2026-09 (#2): the wall-clock ceiling cut two of three catch-up
  delegations. Fixed within delegations. Jobs stay out unless a budgeted pass still
  cannot converge.
- **The pass's time budget** (`SWEEP_STOP_S` 450, `RETURN_BY_S` 510, `STEP_EXPIRY_S` 600,
  `LEASE_S` 600, `ROW_COST_S` 10, from one measurement) is re-tuned from the first passes
  after v0.2.0.
- **Package size: decided — preflight and fail visibly.** The build checks the real zip
  against the 20 MB cap; oversize keeps the canonical package, names the offenders in
  `notes.md`, and tells the operator. No silent splitting, dropping or re-compressing.
  If a real quarter crosses the cap, the split rule gets decided then, against a real
  file list rather than a guess.
- **The weekly sheet's shape is a v1 experiment.** Grouping (`MISSING` / `LOOK` /
  `MATCHED`), line count and how much of each match is shown are tuned from the first
  few real sheets. The test is whether the operator can act on one in under a minute on
  a phone.
