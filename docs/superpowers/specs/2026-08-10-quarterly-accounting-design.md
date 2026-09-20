# casa-plugin-quarterly-accounting — design

Status: draft for operator review · 2026-08-10
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
invoice, collect the invoice PDFs, and deliver — shortly after each quarter closes — a
single zip containing a SnelStart-ready invoice folder, a ledger, and a notes file.
Along the way, keep bank-feed tags and notes authoritative so the bank ledger itself is
progressively annotated.

## Goals

- Weekly, mostly-autonomous matching of DBIT transactions to invoice PDFs. The plugin
  **decides and shows** rather than asking: a loose match that is visible and reversible
  beats a strict one that hands the work back (see §“The reversibility ladder”).
- **It must be faster than doing it by hand.** The *review* of a week should cost one
  sheet read and at most one reply — modelled at roughly 55 seconds for a normal week:
  two incoming messages, one outgoing, two taps, six typed words, no attachment opened.
  If using the plugin becomes a chore it has failed, whatever its state machine
  guarantees.
- **Stated honestly, because the difference matters**: that budget covers reviewing the
  machine's work. It does not cover *collecting* the invoices it could not find — if
  three are missing, fetching them is still three errands, and no presentation trick
  makes twelve manual acquisitions fit in a minute. The plugin's claim is that it finds
  what it can, tells you exactly what it could not, and never makes you re-derive that
  list yourself. Deferred missing invoices are never counted as work saved.
- A standing answer to “what am I missing?” — the review sheet leads with it, every
  week, rather than saving it for quarter end.
- Quarter-end zip package delivered over Telegram: `invoices/` (bulk-uploadable to
  SnelStart), `ledger.xlsx`, `notes.md`.
- A lean vendor knowledge base whose primary asset is the **researched invoice
  deep-link** per portal vendor — found once, at real effort, reused every quarter.
- Bank-feed stays consistent: every match decision is mirrored into bank-feed tags and
  notes, with drift detected and repaired.

## Non-goals (v1)

- No vendor-portal scraping or credentialed browser automation. Portal invoices are
  link-only: the ledger carries the most precise link the agent could research.
- No invoice matching for CRDT (incoming) transactions — they are classified
  (revenue / transfer / interest / other) and annotated, nothing more. The ledger
  still lists the full quarter, both directions.
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

### Division of labor

| Actor | Responsibilities |
|---|---|
| Casa core | Fires the weekly / quarter-end triggers at the resident; enforces tool gates. |
| Resident (Ellen) | Orchestrates passes; all Gmail work (targeted searches, attachment download, ingest); **all** operator conversation (the review sheet, its free-text replies, the occasional one-line question folded into the sheet); sends the zip. |
| Finance specialist | All matching judgment, grounded in its own `Read` of the actual PDFs; all bank-feed tagging/notes; portal-link research (WebSearch); returns structured work orders. Never talks to the operator (structurally cannot: `ask_user` requires direct execution). |
| Plugin MCP server | Invoice store, vendor KB, match records, quarter workbook, filename normalization, package/zip build, outbox staging. |
| Operator | Taps confirmation buttons; answers occasional one-line residue questions; receives one zip per quarter. |

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
| **Free** | Everything v1 does: ingest, auto-match, demote, retarget, reject, re-label; bank-feed `acct-*` tags (an `untag_transaction` away from undone); notes (append-only, corrected by appending); **and the quarterly package itself** — the operator receives a zip, checks it, corrects what is wrong and asks for another (operator, 2026-09-21). A rebuild is `r<N+1>` and costs one message. | Act. No question, no confirmation, no ceremony. |
| **Gated** | Nothing, in v1. | — |

**v1 therefore has no routine gates and no routine button questions.** Every step is
either undoable in one word or cheap to redo, so a gate would only buy ceremony. Two
things would put something on the gated rung and are deliberately out of scope: sending
the package anywhere the operator cannot retract it from (mailing the accountant
directly, filing with the tax authority), and deleting retained documents — which v1
never does.

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
`expected_revision`, `annotation_state`, acceptance revisions, content hashes, the
confidence labels, quarter identifiers in tool form. None of it appears on a sheet, in a
caption or in a receipt. The operator's entire vocabulary is: a line number while a
sheet is in front of them, a vendor name, "wrong", "good", "needs no invoice", "rebuild
it", "send it again". Package revision numbers surface only when choosing between two
delivered files. Everything else is machinery, and machinery that leaks onto the sheet
is a defect.

## Casa baseline (re-verified 2026-09-20, casa v0.323.0)

v1 was written against v0.2xx with two scheduled-turn enhancements outstanding. Both
landed, and each brought contracts this plugin must design against, not merely enjoy.

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
`max_turns` is a `role.yaml` knob defaulting to 10 (`agent_loader.py:1215`); and there is
still no sanctioned cross-plugin file handoff (#486 open).

## Data model

All state lives exclusively in `$CLAUDE_PLUGIN_DATA` (survives plugin updates by
casa's design; a plugin update or reinstall may never touch it). SQLite index +
files, schema versioned so future plugin versions migrate rather than recreate.
Nothing identifying ships in the repo.

Canonical quarter identifier everywhere (paths, records, tool arguments, tags, zip
name): **`YYYY-Qn`** (e.g. `2026-Q2`). Never a bare `Qn`.

### Invoice store

- **Custody is by content hash, not by name.** Files live at
  `invoices/<sha256[:2]>/<sha256>.pdf`. The human-readable
  `<YYYY-MM-DD>_<vendor>_<amount>.pdf` is a package-time rendering, and carries a short
  hash suffix when two invoices would otherwise render the same name — two same-day,
  same-amount purchases from one vendor collide on the v1 scheme, which specified no
  overwrite, refusal or disambiguation rule.
- Index row: id, content hash, size, vendor, invoice date, invoice number, amount,
  currency, recipient-as-read, source (gmail message id / manual), acquisition
  coordinates for retry, extraction author, status
  (`unmatched` / `matched` / `irrelevant`).
- **Byte identity is not invoice identity.** A vendor that re-renders or re-sends the
  same invoice produces different bytes and a second index row; two equal payments
  could then each take one while every per-file cardinality check passes. The server
  flags an issuer + invoice-number collision across differing hashes and refuses
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
reassignment (specialist re-proposes or operator decides). Alongside the `row_id`, each match
snapshots the transaction's identifying facts (booking date, amount_minor, currency,
direction, counterparty) so a match is auditable even if the row it targeted changes.

**The acceptance revision moves only when the proposition moves.** A revision bump
means the thing the operator was asked about changed — the pairing, its evidence, or
the transaction facts under it. Annotation delivery progress is separate bookkeeping
(`annotation_state` and its own attempt counter) and never bumps the acceptance
revision, because a tap on an unchanged proposal must not be rejected by a CAS bump
that came from bank-feed write progress.

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
- **Annotation is a state, not a boolean:** `annotation_state:
  pending | clean | repair_owed`, part of the same revisioned record. Retarget,
  demotion, and rejection set `repair_owed` together with the concrete repair
  action (which tags to remove/replace, what correction note to append) in the
  same CAS transition. The repair sweep processes `pending` and `repair_owed`
  alike — migrated bank-feed tags can never silently keep asserting a match the
  store has demoted. (Round-2 finding: demotion leaving `acct:matched` standing.)
- **Match creation is preconditioned on a fresh row resolution.** The specialist
  re-resolves the target row (`get_transaction`, state active) immediately before
  `record_match`/`propose_match`, and passes the resolved snapshot; if a sweep
  retarget later collides with a match already created on the successor row, the
  server atomically moves **both** contenders to `conflicted` — a non-active
  state, so the one-active-match invariant is never violated by the collision
  itself — **and sets both to `annotation_state=repair_owed` with concrete
  cleanup actions in the same CAS transition** (a previously-`clean` contender's
  bank-feed tags no longer reflect any active match and must be corrected; a
  `conflicted`+`clean` record would otherwise be invisible to both worklists,
  round-4 finding) — with a residue line; the conflict is resolved only by
  explicit reassignment. Never a silent drop, never two active matches on one economic
  transaction. (Round-2 finding: retarget/new-match collision; round-3 finding:
  two active `proposed` would themselves have violated cardinality.)

**Cardinality invariants (server-enforced, not convention):**

- At most one **active** (`matched` or `proposed`) match per transaction row, and at
  most one active match per invoice, by default. The server rejects a second
  `record_match`/`propose_match` that would violate either.
- **No allocation groups in v1** (operator decision, 2026-09-21). One invoice paid in
  installments, one payment covering several invoices, partial settlements, fees and
  credit notes are **not modelled**: the documents are retained, the transaction stays
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
demotes it to `proposed` with a residue line); if the row has `vanished`, the match
reopens as `unmatched` and is reported. `list_transactions` filters to active rows,
so without this step a superseded match would silently leave every worklist.

**A row can also change without being superseded, and that is the case v1 missed.**
bank-feed's update path rewrites `booking_date`, `value_date`, `amount_minor`,
`currency`, `direction`, `status`, `counterparty` and `remittance` **under the same
`row_id`** (verified 2026-09-21 against `apply.py`'s hand-written UPDATE column list).
Supersession-only revalidation therefore leaves an accepted €100 match standing after
the amount is corrected — the sweep sees neither `superseded` nor `vanished` and moves
on. So: every pass, and again immediately before packaging, every active match
re-compares its snapshot fingerprint (account, direction, currency, `amount_minor`,
status, booking date, counterparty, remittance) **and bank-feed's review flags**
against the live row, superseded or not. A changed material fact invalidates
acceptance: the match demotes to `proposed` with `repair_owed` and a residue line.
Migrated tags and notes on a successor row are not fresh approval either.

### Vendor KB

Lean, one record per vendor:

- canonical name; counterparty patterns as bank-feed shows them (`BCK*ZAPIER` → Zapier)
- channel: `email` / `portal` / `receipt-only` / `none-expected`
- **invoice_link** — the researched deep link, as close to “the page listing your
  invoices” as the vendor allows. This is the KB's primary asset: found once with
  real effort (WebSearch), cached forever, re-researched only when the operator says
  it broke.
- search_hint (for email vendors), free-text notes (“invoices post ~3 days after
  charge”)
- one source note per link (where it was found, when) — **not** per-field provenance,
  and no scheduled re-verification. A link is re-researched when a retrieval actually
  fails; an aging policy turns a deep-link notebook into standing upkeep for links that
  still work. A `[Wrong]` tap records the fact; it does not mandate a follow-up turn.

`none-expected` is load-bearing: it is what stops bank fees, taxes, and receipt-less
charges from polluting the residue list forever.

### Quarter workbook

Per-quarter record of pass runs, per-transaction state, and open residue. Makes
multi-round delegations stateless-safe (each delegation is a fresh ephemeral session;
state carries in the store, not in return values alone) and crash-safe: a casa
restart mid-pass loses only the in-flight turn.

## Tool surface (server, 19 tools)

Ingest & curation: `ingest_invoice` (agent-extracted metadata as arguments),
`update_invoice_metadata`, `mark_irrelevant`.
Query: `list_unmatched_invoices`, `list_quarter_state` (includes repair-sweep view:
matches with `annotation_state` in `pending`/`repair_owed`), `get_vendor`.
Matching: `record_match`, `propose_match`, `confirm_match`, `reject_match`,
`mark_annotated` — every mutating match tool takes `expected_revision` (CAS; see
the match-record state machine).
KB: `upsert_vendor`.
Setup: `check_setup()` — what the pass can actually reach (bank-feed tools, bound
account, gmail tools, last sync), one branch at the top of every pass;
`bind_account(account_id)` — records the business account and its ledger instance on
first run; `set_package_name(name)` — changes the zip filename prefix, which otherwise
defaults and is never asked about.
Review: `build_review(scope)` — renders the sheet from store state, assigns stable
line numbers and persists line → `(match_id, revision)` so a reply two days late still
resolves; `resolve_review_line(sheet_id, line, verdict, note)` — what Ellen calls per
named line, CAS on that line's recorded revision.
Ledger input: `import_ledger_export(path)` — ingests bank-feed's `export_history`
artifact so the package's ledger can list the full quarter (unmatched DBIT and CRDT
rows included); the match records alone cannot produce it.
Packaging: `build_quarterly_package(quarter)`, `stage_for_delivery(target)` (copies
an invoice PDF or the built package into casa's plugin outbox for `send_media`).

House disciplines copied from bank-feed: explicit loud failures, numeric caps and
truncation notices on reads, provider text fenced as untrusted on output, three-way
tool-list agreement (server, `provides_tools`, role allow-lists) with a CI check.

## Bank-feed annotation protocol (two-phase, tracked)

The plugin server cannot call bank-feed's tools, so the specialist performs both
writes; the design makes the pair a tracked transaction rather than a convention:

0. **Row-id re-resolution first — scoped by annotation work, not match state**:
   every record whose `annotation_state` is `pending` or `repair_owed` — active,
   `conflicted`, or `rejected` alike — plus every active match, has its `row_id`
   re-resolved against bank-feed (`get_transaction`). Superseded rows retarget
   (CAS, revision bump), and any owed repair action is **rewritten to the live
   successor row in the same CAS transition** — bank-feed refuses annotation
   writes to a superseded row, so a repair left aimed at a dead row would fail
   forever while the migrated stale tags keep asserting a match on the live row
   (round-3 finding). Vanished rows reopen. Only then does annotation work run.
1. `record_match` / `confirm_match` store the match with `annotation_state=pending`.
   Tag and note are written together, at match time, including for an `auto` match:
   deferring the note until confirmation was considered and cut, because it buys only
   tidiness in the ledger's history and costs a second state dimension. A reversal
   appends one correction note; two lines on a row is an honest audit trail, which is
   what append-only notes are for.
2. The specialist writes the bank-feed side — `tag_transaction`
   (`acct-matched`, `acct-<yyyy-qn>`; portal cases `acct-portal`; known no-invoice
   cases `acct-no-invoice-expected`; open cases `acct-proposed`) and `add_note`
   (`invoice: <filename>` or the portal link, plus the decision id and revision) —
   then calls `mark_annotated(match_id, expected_revision)` → `annotation_state=clean`.
   **The tag grammar is `^[a-z0-9][a-z0-9-]{0,31}$`, which admits no colon** (verified
   2026-09-21 against bank-feed `tools_annotate.py`; reproduced: `acct:matched` is
   refused with "invalid tag … Nothing was changed"). Every `acct:…` tag the v1 spec
   named was invalid, and every pass would have retried a permanently failing write.
3. Every weekly pass **begins** with the repair sweep: `list_quarter_state`
   surfaces every match with `annotation_state` in (`pending`, `repair_owed`) —
   half-completed writes and demotion/retarget corrections alike — and the
   specialist completes them before new work.
4. `reject_match` / reassignment / demotion set `repair_owed` in the same CAS
   transition that changes the state, recording exactly which tags to
   remove/replace and the correction note to append (bank-feed notes are
   append-only — honest audit trail).

5. **Drift is detected on clean records too, not only on the repair queue.** A sweep
   that re-runs owed work alone cannot see the operator removing an `acct-` tag or
   adding a contradictory note directly in bank-feed: the local record still reads
   `clean`, and the package keeps endorsing a decision the operator already overturned.
   Every pass therefore reads the **actual** owned tags and any new notes on every
   accepted row and compares them against the desired projection. A discrepancy is
   recorded before it is repaired. A new operator note reopens the match only when the
   specialist reads it as contradicting or questioning the relationship — an ordinary
   administrative note ("accountant has a copy") is recorded as seen and changes
   nothing, because manufacturing questions out of ordinary bookkeeping spends the
   scarcest asset in the design.
6. **Classify before annotating.** bank-feed's untagged queue counts any tag outside
   its workflow set as content classification (verified 2026-09-21 against
   `tools_read.py`), so an `acct-` tag silently removes the row from tx-classifier's
   queue. Accounting annotation for a row therefore runs after its classification, and
   any accounting-tagged row whose classification is still unresolved is revisited
   explicitly rather than left invisible.

Bank-feed may lag by at most one pass. Note precisely what that buys: there is no
cross-store transaction and no promise of instantaneous equality. The guarantee is
**detected, retryable convergence** — desired projection held locally, applied against
the expected row fingerprint, and verified by readback every pass and again before
packaging. "It can never drift silently" is only true because of step 5; a dirty-work
queue alone would not have earned that sentence.

## Setup (install day, once)

**The whole of it is one sentence to the configurator and two triggers. It asks the
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

**Step 2 — two triggers on Ellen**, created through the trigger recipe. This spec ships
the exact prompt text so nobody composes it at install time, because the closing clause
is what stops every pass delivering twice (ha-casa-app#960/#932):

```
name:     quarterly_accounting_weekly
type:     cron        schedule: 0 9 * * 1        channel: telegram
prompt:   Run the quarterly-accounting weekly pass for the current quarter
          and send me the review sheet it produces.
          After the send, output the sentinel `<silent/>` and nothing else.

name:     quarterly_accounting_quarter_end
type:     cron        schedule: 0 9 10 1,4,7,10  channel: telegram
prompt:   Run the quarterly-accounting quarter-end pass for the quarter that
          just closed, build its package and send it to me.
          After the send, output the sentinel `<silent/>` and nothing else.
```

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
database cannot silently inherit the old row handles.

**What the zip files are called — defaulted, never asked.** The package name defaults to
a slug of the bound account's label, or to `books` when that yields nothing usable, and
it is stored (changeable) rather than configured. The first package says so in one line:
`Files are named "books-2026-Q3-r1.zip" — say "call the zips <name>" to change that.`
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

**No trigger or callback consent round.** The plugin declares no triggers of its own —
the two above live on Ellen's `triggers.yaml` — and no callbacks, so there is no consent
verdict for its setup to wait on.

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

## Flows

### Weekly pass (resident cron reminder)

1. **Repair sweep** (above), then Ellen delegates: “weekly pass, `<YYYY-Qn>` —
   report new transaction state and search plans.”
2. **Specialist triage** over new DBIT transactions × invoice store × KB, reading
   PDFs as needed.

   **Auto-match bar (operator decision, 2026-09-21 — deliberately loose).** The
   plugin's job is to save the operator work. A bar tuned so tight that it matches
   almost nothing hands the whole job back and is worse than no plugin, so v1 matches
   readily and makes every pick **visible and cheap to reverse** (see §“The reversibility ladder”) instead of expensive to establish.

   Auto-match requires:

   - an **eligible** bank observation: active, booked, DBIT, on the configured account;
   - the invoice is **held** in custody and reads as an invoice, not a quotation, order
     confirmation or credit note;
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
   | `recipient?` | the invoice does not name the B.V., or names someone else |

   `clean` lines are for skimming. The other four are what the sheet puts in front of
   the operator. Nothing here blocks: a `guessed` + `recipient?` match still lands as
   `matched` and still ships if the operator does not object, because the package
   discloses the label on the row and the whole thing is one reply away from being
   fixed. Existing matches are not immune from later evidence — a newly arrived
   competing invoice re-labels an accepted match `guessed` and surfaces it again.

   Returns a structured work order per transaction: `matched` (with label) /
   `proposed` (indistinguishable candidates) / `portal` (tagged, link noted) /
   `none-expected` / `missing` with a **search plan carrying discriminators**, not just
   a query (“want €54.45 within ~10 days of May 6; ignore payment confirmations”).
3. **Ellen's targeted Gmail round** — roughly one precise search per unresolved
   transaction, never a mailbox sweep (there are far more emails than transactions;
   the bank feed drives the search, not the mailbox). Ambiguity rule: **over-ingest
   all plausible candidates and let the specialist pick** — ingest is idempotent and
   losers stay available for other transactions or get `mark_irrelevant`. Re-delegate
   for final picks. Hard bound: two search rounds per pass, then the item goes to
   residue.
4. **Operator report — one sheet, delivered inline.** The pass ends by sending a
   **review sheet**. Every rule here is load-bearing (UX round, 2026-09-21):

   - **Inline whenever it fits.** A sheet under Telegram's 4096 UTF-16 units goes as an
     ordinary `send_message` — no tap, no download, nothing to open. Only a sheet that
     genuinely does not fit becomes a `.txt` via `send_media(kind="text")`, captioned
     with the missing count and as many names as fit. A forty-line sheet that fits still
     goes inline: scrolling is cheaper than opening. It is never split across numbered
     messages and never silently truncated. (`send_message` is trustworthy again now
     that ha-casa-app#990 reports proven non-delivery.)
   - **What is missing comes first, always**, before anything reassuring. A sheet whose
     first screen reads "9 matched" teaches the operator that opening it reveals nothing.
   - **Phone width is ~32 characters.** Short wrapping blocks, not aligned columns: a
     74-character row inside a fenced block is still 74 characters wide, and its
     continuation text begins off-screen.
   - **Evidence, not label codes.** The sheet never prints `clean`, `guessed`, `no-ref`
     or `partial-search`; it prints what they mean — "picked invoice 8841; invoice 8712
     also fits", "no shared reference", "invoice names a person, not the B.V.", "search
     incomplete". The labels stay internal, where they drive sorting.
   - **Diff-first.** Each sheet carries what is new or changed, every still-missing
     invoice, and every still-unreviewed uncertain pairing. An unchanged pairing the
     operator already reviewed does not come back — repetition is what turns a sheet
     into wallpaper.

   A normal week (illustrative; per §Privacy no real vendor list appears here):

   ```
   3 invoices missing · 2 pairings to check
   14-20 Sep · 9 new payments

   MISSING
   1 Adobe · EUR 54.45 · 14 Sep
   Get invoice:
   https://adobe.example/invoices

   2 Jansen BV · EUR 120.00 · 15 Sep
   No invoice found in email.

   3 BCK*XYZ · EUR 180.00 · 16 Sep
   Who was this payment to?

   CHECK THESE
   Included unless you correct them.
   4 Zapier · EUR 99.00 · 17 Sep
   Picked invoice 8841 · 17 Sep.
   Invoice 8712 · 10 Sep also fits.

   5 Vercel · EUR 12.10 · 18 Sep
   Invoice V-918 names a person,
   not the B.V.

   MATCHED
   6 Backblaze · EUR 7.99 · inv B5521
   7 Hetzner · EUR 24.20 · inv H9017
   ```

   **Line numbers are unique within a quarter and never reused.** Casa's inbound
   Telegram context carries the incoming message's own id and **not** the message it
   replied to (verified 2026-09-21, `telegram.py:1647`), so a native reply gesture
   cannot tell the plugin which sheet the operator meant. Unique numbers make that
   plumbing unnecessary: "4" resolves to exactly one proposition for the whole quarter,
   whichever sheet printed it. An unrecognised number is reported, never resolved
   against the newest sheet.

   **The pass raises no button questions.** A one-line identity question ("who is
   BCK*XYZ?") is a line on the sheet like any other, answered in the same reply.

5. CRDT transactions: classified and annotated only.

**A broken pass must not read as deficient books.** "No invoice found" when Gmail was
unavailable, or "nothing new" when the bank feed was stale, tells the operator something
false about their own accounting. Three states stay distinct and render differently:
**missing** (searched, not found), **not searched** (the pass could not look), and **not
checked** (the pass stopped before reaching it). A degraded pass leads with its own
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
`confirm_match` to every unnamed line. "4 and 9 are wrong" would then have recorded
seven other pairings as *operator-reviewed decisions* the operator never looked at —
silent corruption of review intent, and worse than the wrong match it was meant to
catch, because it launders a guess into a human decision. So:

- **A correction affects the lines it names. Nothing else moves.** Unnamed lines keep
  their author (`auto`) and their label and reappear later if still uncertain.
- **Only explicit approval approves.** "all good" confirms the pairings shown on that
  sheet; it resolves no missing invoice and picks no winner among alternatives. "4 good"
  confirms exactly line 4.
- **Facts are distinct from verdicts.** "3 is my accountant" records an identity; it
  does **not** mean "no invoice expected" (that is "3 needs no invoice"), and it
  certainly does not mean "never for this vendor" (that is "no invoices ever for X").
  The plugin never manufactures "ever" out of a one-off answer.

Each named line resolves to a `(match_id, revision)` recorded with the sheet — or, for a
missing item, to the transaction, since there is no match record to point at. A CAS
rejection means the line changed underneath, and Ellen reports that line with its
current facts instead of applying a stale correction.

**The reply grammar is an executable contract, not "Ellen understands free text":**

| Rule | Behaviour |
|---|---|
| Unique target | A displayed number, a list of them, or an exact displayed vendor name within the quarter. Case and whitespace normalised; **no fuzzy vendor matching** — two Adobe charges need a number. |
| Whole clauses | A supported clause must consume all its text. "4 and 9" is a target list with no verb: nothing applies, and the reply asks whether they are wrong. Never extract a convenient command from prose that did not parse. |
| Negative verdicts unpair, and only that | "4 wrong", "no to 4", "wrong: 4, 9" remove the pairing and keep both payment and document. On a missing or identity-only line there is no pairing to remove: nothing mutates, and the reply says what it could do instead. |
| Ambiguous bulk clauses apply nothing | "all good except the Zapier" does not say whether Zapier is wrong or merely unchecked. Nothing applies; the reply names the two phrasings that work. Input-error handling, not a gate. |
| Validate against the saved proposition | An unknown number is reported, never redirected to a nearby one. Independent valid clauses still apply; the exceptions ride in the same receipt. |
| Instructions separate from corrections | "4 wrong; rebuild it" unpairs, then rebuilds that sheet's quarter. An unresolved correction blocks its dependent rebuild and says so. Unsupported wording is reported, never swallowed into a note. |

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

### New portal vendor

First classification of a vendor as portal-only triggers the one-time link research
(specialist, WebSearch): prefer the authenticated deep URL
(`console.vendor.tld/invoices`) over the marketing domain. Filed via `upsert_vendor`
with provenance. Every later quarter resolves instantly from the KB.

### Quarter-end pass (cron: 10th of Jan / Apr / Jul / Oct)

Weekly-pass mechanics over the **full quarter**, then:

1. Final sync, repair sweep and fingerprint revalidation of every active match.
2. `build_quarterly_package("<YYYY-Qn>")`.
3. **Send it.** No gate, no keyboard, no confirmation (operator, 2026-09-21). The
   caption is short enough to read without opening anything and leads with what is
   missing, not with build metadata:

   ```
   3 invoices missing · 2 uncertain pairings
   2026 Q3 · revision 1 · 108 invoice PDFs
   Open notes.md first.
   Reply with corrections and "rebuild it".
   ```

   No digest, no internal build vocabulary, no request to acknowledge receipt — the
   digest lives at the END of `notes.md`, for diagnostics. `notes.md` opens with the
   actual missing items, numbered the same way the review sheets number them so a
   correction can be written against either, then the uncertain pairings that were
   included anyway, then unsupported relationships, and only then inventory and
   commentary. **The package is the
   quarter's review surface.** The operator checks it, says what is wrong, and asks for
   another; corrections apply and `r<N+1>` follows. That round trip is cheaper than any
   question the plugin could have asked beforehand.

**The replacement package's caption doubles as the receipt**, keeping a correction and
its result in one exchange. The operator replies `17 wrong; rebuild it` and the next zip
arrives captioned:

```
Unpaired 17 Zapier.
4 invoices missing · 1 uncertain pairing
2026 Q3 · revision 2 · 107 invoice PDFs
Replaces ...-Q3-r1.zip. Use ...-Q3-r2.zip.
```

"rebuild it" means the quarter of the package being replied to; "rebuild Q3" resolves
only when the year is unambiguous — the plugin never silently picks among years; "send
it again" resends the existing revision, labelled as a resend. What stays annoying is
real, and no gate fixes it: downloading, extracting, switching between `notes.md` and
the PDFs, and an obsolete zip still sitting in the phone's downloads. Naming the
replaced file reduces that confusion; it does not remove the burden.

**Two ways a package is produced, and one rule for repeats.**

- **Automatically**, by the quarter-end trigger, once per quarter.
- **On demand**, whenever the operator asks — "rebuild Q3", "send me Q3 again", "what
  does Q4 look like so far". This runs in an ordinary direct DM turn, which has full
  media rights without any scheduled-delivery marker, so it needs no trigger and no
  special path. Ellen builds and sends; the specialist may build but structurally
  cannot deliver.

**A repeat request does not mint a revision unless the inputs changed.** Every build
first computes an **input digest** over the frozen match snapshot, the imported ledger
rows and the package options. Then:

| Case | What happens |
|---|---|
| Input digest equals the current `r<N>`'s | **No new revision.** The existing `r<N>` is returned and re-sent if asked. The reply says so: “same as r3, sent 12 Oct — nothing has changed since”. |
| Inputs differ (a correction, a late invoice, a bank change) | A new revision `r<N+1>` is reserved and built, and `notes.md` and the caption say what changed since `r<N>`. |
| A second build arrives while one is in flight | The reservation transaction serialises them. A concurrent request with an equal input digest joins the in-flight build and receives its result rather than reserving a second number — two identical zips is a worse answer than one. |

This is what "deterministic and idempotent" has to mean to be worth saying: asking
three times in a row gets you one package three times, not `r2`, `r3` and `r4` each
claiming to supersede the last. Revision numbers stay scarce, so `supersedes rN-1`
keeps meaning something.

**A quarter that is still open can be packaged too, and is marked differently.** An
interim request ("Q4 so far") builds `<slug>-<YYYY-Qn>-interim-<YYYY-MM-DD>.zip`: same
contents, no revision number, not recorded as the quarter's package, and `notes.md`
opens with `INTERIM — quarter still open, n transactions so far, not for filing`.
Interim builds consume no revisions and can never be confused with the package that
eventually closes the quarter. That keeps “where am I?” cheap without polluting the
record that goes to the accountant.

**Package membership is defined by the transaction's booking-date quarter, never by
where an invoice file happens to be stored.** The invoice-date storage path
(`invoices/<YYYY-Qn>/…`) is custodial only. The builder selects every transaction
booked in the quarter and pulls each matched invoice PDF into the package regardless
of its storage directory — so an invoice dated June 30 that pays a July 1 charge
ships in the Q3 package (the cross-quarter case both reviewers flagged). An
unmatched invoice appears in no package's `invoices/`; it is listed in `notes.md` of
the quarter it was ingested in.

**Every built package carries revision identity, and revisions are atomically
reserved.** `build_quarterly_package` opens one store transaction that (a) reserves
the next per-quarter revision number and (b) freezes the immutable match snapshot
the build will render — before any file is written. The zip, its content digest,
and the delivery record are all bound to that reservation, so two overlapping
builds can never both claim `r2` and a correction landing mid-build can never leak
into a zip labeled with the pre-correction revision (round-3 finding). The zip is
named `<slug>-<YYYY-Qn>-r<N>.zip`, `notes.md` opens with `revision rN, supersedes
rN-1, built <date>, digest <hash>`, and the Telegram caption says the same — a
rebuilt-and-resent package is never confusable with the stale one it replaces at
upload time.

```
<slug>-<YYYY-Qn>-r<N>.zip
├── invoices/            # SnelStart bulk upload: YYYY-MM-DD_vendor_amount[_hash8].pdf
│                        # matched only — a `proposed` line never lands here
├── ledger.csv           # full quarter, both directions: date, amount, currency,
│                        # direction, counterparty, vendor, status, confidence label,
│                        # invoice filename OR portal deep-link, notes
├── ledger.xlsx          # same rows, for the accountant's tooling (operator decision,
│                        # 2026-09-21: ship both)
├── unresolved/          # only when non-empty: retained candidate PDFs for lines that
│                        # did NOT match, so the accountant has the evidence without
│                        # another mailbox hunt — deliberately NOT in invoices/, which
│                        # is the bulk-upload set
└── notes.md             # what is missing first (with best links), then anomalies,
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

Missing invoices never block shipping: ledger rows read `MISSING` with the best
available link; `notes.md` opens with the action list (right after the revision
line). The build is deterministic — the same inputs produce the same bytes, byte for
byte (fixed ordering, fixed timestamps, stable serialisation) — which is exactly what
makes the input-digest rule above safe: "nothing changed" is a computed fact, not a
judgement. Supply stragglers and the next build is `r<N+1>`; ask again with nothing
changed and you get `r<N>` back; resending any revision is always free.
Delivery: atomic write to the plugin outbox → `send_media(kind="zip")` (shipped,
#482) → operator's Telegram, from the quarter-end cron turn itself. The outbox copy is
consumed on send (or reaped at 2 h); the canonical package stays in the data dir. Two
edges the shipped tool imposes: a transport timeout is reported as
`delivery_uncertain` — the send may have landed — so a retry is an operator-visible
**resend of the same revision**, never a silent rebuild (the revision line in
`notes.md` and the caption make a duplicate arrival self-evident); and the `zip` kind
caps at **20 MB**, which a quarter of PDF invoices can approach, so the build
**preflights the actual zip size** and an oversize package fails visibly, with the
canonical zip retained and `notes.md` naming the offenders. v1 does not silently split
the requested single zip, drop invoices or re-compress the operator's PDFs; if a real
quarter crosses the cap, the split rule is decided then, with evidence.

After a `delivery_uncertain` the package is NOT re-sent automatically: the send may have
landed, and a second zip in the accountant's hands is worse than a question. The next
pass offers a resend of the same revision, in words, like everything else.

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
- Two-phase bank-feed annotation with start-of-pass repair sweep, whose step 0
  re-resolves superseded/vanished bank-feed rows before anything else; annotation
  is a tracked state (`pending`/`clean`/`repair_owed`), never a boolean that
  demotion could leave stale.
- Server-enforced cardinality: no second active match per invoice or transaction;
  allocation groups validated by totals and always operator-confirmed.
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
  CAS; a demotion that must leave `annotation_state=repair_owed` (never a stale
  `acct:matched`); a retarget colliding with a successor-row match (both moved to
  non-active `conflicted` and `repair_owed` in one transition — no
  `conflicted`+`clean` record — residue line emitted, cardinality intact); a
  rejected match with `repair_owed` whose row is superseded before cleanup (repair
  rewritten to the live successor, stale tags corrected); an allocation group whose
  totals must validate and which must stay `proposed` without operator
  confirmation; and concurrent `build_quarterly_package` calls that must yield
  distinct reserved revisions with snapshot-consistent contents.
- The review sheet is store state, so it is unit-testable and must be tested: line
  numbers stay bound to `(match_id, revision)` across a rebuild; a reply naming a line
  whose revision moved is refused on that line and applied on the others (a partial
  reply is normal, not an error); a reply arriving two sheets later resolves against
  the sheet it names, not the newest; a rebuild requested after a correction produces
  `r<N+1>` whose contents differ in exactly the corrected lines; **a rebuild requested
  with nothing changed returns `r<N>` and reserves no number**; two concurrent builds
  with equal input digests yield one revision and one zip, while two with differing
  digests yield distinct reserved revisions with snapshot-consistent contents; and an
  interim build of an open quarter consumes no revision and is never recorded as the
  quarter's package.
- The round-5 red cases, from the independent design and its comparison: a material
  change to an ACTIVE row (amount corrected under an unchanged `row_id`) must
  invalidate an accepted match — the case supersession-only revalidation missed; two
  different PDFs carrying one issuer + invoice number must refuse automatic acceptance
  on both sides; an order confirmation and a net-vs-gross amount must not read as an
  invoice; an invoice naming a person rather than the B.V. must land `recipient?`; a
  truncated search must label `partial-search` and must never be rendered as `clean`;
  an operator's `acct-` tag removal on a clean record must be detected by the next
  pass; a row must be classified before it is accounting-tagged, or it vanishes from
  tx-classifier's untagged queue; and every tag the plugin writes must satisfy
  `^[a-z0-9][a-z0-9-]{0,31}$` (a pinning test, since the v1 spec's whole tag vocabulary
  was invalid).
- **The sheet and the reply grammar are testable and must be pinned**: a sheet that
  fits goes inline and one that does not becomes exactly one attachment with a leading
  missing-count caption (never split, never truncated); no line of a rendered sheet
  exceeds the phone-width budget; a correction naming two lines leaves every other
  line's author untouched (the round-6 defect — implicit approval — gets its own red
  case); "all good" confirms only the pairings shown; "3 is my accountant" does not set
  no-invoice-expected; "4 and 9" and "all good except the Zapier" apply nothing and
  answer with the phrasing that works; an unknown number is reported and never
  redirected; a reply naming a line from an older sheet in the same quarter still
  resolves, because numbers are unique per quarter; and the receipt is generated from
  committed results, so a test that stubs the commit sees the receipt change.
- **Coverage states render differently**: missing, not-searched and not-checked must be
  distinguishable in the rendered sheet, and a pass that failed must lead with its own
  condition rather than with accounting results.
- An **install smoke test**, because shared plugin storage is asserted rather than
  proven by casa's code: the resident ingests a synthetic document, the specialist
  reads that same record and those same bytes, and the resident stages it for delivery.
  It is a release check, not a reason to build a transfer service.
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
| ha-casa-app | [#486](https://github.com/bonzanni/ha-casa-app/issues/486) shared handoff area, [#487](https://github.com/bonzanni/ha-casa-app/issues/487) specialist→resident requests. | Still open, still not dependencies: gmail→store custody stays an agent-passed path, specialist asks stay structured work orders. |
| Resident config | Two reminders (weekly; quarter-end on the 10th), plugin assignment to both roles. | Operator/configurator action at install time. |

### Open casa issues this plugin designs around

None blocks v1. Each costs a plugin-side line rather than a wait (open as of
2026-09-20):

| Issue | Bite | Plugin-side answer |
|---|---|---|
| ~~[#990](https://github.com/bonzanni/ha-casa-app/issues/990) — `send_message` reports "sent" when the channel delivered nothing~~ | — | **Fixed** (verified 2026-09-21 in `tools.py`: a proven negative is now an error result). This is what lets the weekly sheet ride `send_message` inline instead of always being an attachment — the fix changed the UX, not just the failure mode. |
| [#960](https://github.com/bonzanni/ha-casa-app/issues/960) / [#932](https://github.com/bonzanni/ha-casa-app/issues/932) — a scheduled turn that delivers with a tool and then ends in prose delivers twice (bug, low) | Two DMs per pass. Nothing enforces the clause; only documentation asks for it. | Both trigger prompts this plugin ships carry the closing `<silent/>` clause verbatim, in the install notes. |
| [#975](https://github.com/bonzanni/ha-casa-app/issues/975) — bundle compensation writes an emptied tuple over a refused transaction's files (bug, high, `operator-decision`) | Hits the `casa-specialist-finance` role bump (`upgrade_specialist`), not the runtime: a refused upgrade can take the specialist's saved settings 1 → 0. | Capture the specialist's settings before the bump and verify after. The issue is blocked on an operator decision, so it will not clear on its own. |
| ~~[#1024](https://github.com/bonzanni/ha-casa-app/issues/1024) — install-time vault exploration searches variables no recipe may wire from a vault item~~ | — | **Not reachable.** The plugin declares no required environment variables (the package name is a defaulted stored setting), so no exploration runs for it. |
| [#1033](https://github.com/bonzanni/ha-casa-app/issues/1033) — a progress report made while answering the operator is credited to the previous batch (bug, medium) | Only if a pass becomes a `casa.jobs` job. | Settled by the jobs decision below; v1 does not declare a job. |
| [#480](https://github.com/bonzanni/ha-casa-app/issues/480) — apply the per-engagement uid and capability drop to in-process (`in_casa`) engagements too (enhancement) | Would change this plugin's file-access assumptions: the gmail→store custody hop and the specialist's `Read` of the invoice store both rely today on delegated turns sharing the process user. INV-CONT-004 already requires a pinned plugin directory to be owned by the dropped uid or world-readable and traversable. | Watch it. If it lands, the store's directory modes and the ingest hop need a re-read — and the case for [#486](https://github.com/bonzanni/ha-casa-app/issues/486) stops being a convenience argument. |

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
- **Package size: decided — preflight and fail visibly.** The build checks the real zip
  against the 20 MB cap; oversize keeps the canonical package, names the offenders in
  `notes.md`, and tells the operator. No silent splitting, dropping or re-compressing.
  If a real quarter crosses the cap, the split rule gets decided then, against a real
  file list rather than a guess.
- **The weekly sheet's shape is a v1 experiment.** Grouping (`MISSING` / `LOOK` /
  `MATCHED`), line count and how much of each match is shown are tuned from the first
  few real sheets. The test is whether the operator can act on one in under a minute on
  a phone.
