# casa-plugin-quarterly-accounting — design

Status: draft for operator review · 2026-08-10
Revised 2026-09-20 — re-verified against casa **v0.323.0**. Both scheduled-turn
dependencies landed; the contracts they landed with (one attention lane, durable
asks, background jobs) change the weekly pass. See “Casa baseline”.

## Purpose

Automate the quarterly accounting preparation for the operator's B.V.: match every
transaction on the business bank account (already ingested by bank-feed) against its
invoice, collect the invoice PDFs, and deliver — shortly after each quarter closes — a
single zip containing a SnelStart-ready invoice folder, a ledger, and a notes file.
Along the way, keep bank-feed tags and notes authoritative so the bank ledger itself is
progressively annotated.

## Goals

- Weekly, mostly-autonomous matching of DBIT transactions to invoice PDFs, with
  operator confirmation only where judgment is genuinely below the auto-match bar.
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
| Resident (Ellen) | Orchestrates passes; all Gmail work (targeted searches, attachment download, ingest); **all** operator conversation (summaries, PDF + keyboard confirmations, residue questions); sends the zip. |
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
confirmation keyboards in one turn.** It asks one, and asks the next when that settles.

**A scheduled ask is a durable obligation, not a message** (INV-JOB-013 / INV-JOB-007).
The record (`/data/scheduled_asks.json`) is written before the keyboard is posted,
survives a restart with its remaining timeout, and **every** terminal outcome — answered,
expired, cancelled, or settled `operator_busy` by the boot reconciler — is delivered back
to the asking session as a machine-authored scheduled turn that reports the tap in its
content, never as its speaker. The guarantee is **at-most-once**: the crash window
between "decided" and "dispatched" may lose an outcome, never duplicate it. Three
consequences:

- v1's "ask-keyboards die on casa restart" is **wrong now** — they survive.
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

- Files: `invoices/<YYYY-Qn>/<YYYY-MM-DD>_<vendor>_<amount>.pdf` — **invoice date**,
  lowercase vendor slug, amount with dot decimal (e.g.
  `invoices/2026-Q2/2026-05-06_adobe_54.45.pdf`).
- Index row: id, file path, content hash (dedup — ingest is idempotent, duplicates
  rejected), vendor, invoice date, invoice number, amount, currency, source (gmail
  message id / manual), extraction author, status
  (`unmatched` / `matched` / `irrelevant`).

### Match records — a revisioned state machine, all mutations CAS

invoice_id ↔ bank-feed `row_id`, state `matched` / `proposed` / `conflicted` /
`rejected`, decision author (`auto` / `operator`), rationale. `matched` and
`proposed` are the **active** states (they occupy cardinality slots and feed
packaging); `conflicted` is non-active — it holds matches that lost a consistency
race (see collision handling below), is always surfaced as residue, is excluded
from packaging, and leaves the cardinality slot free; it exits only by explicit
reassignment (specialist re-proposes or operator decides). Alongside the `row_id`, each match
snapshots the transaction's identifying facts (booking date, amount_minor, currency,
direction, counterparty) so a match is auditable even if the row it targeted changes.

**Every match carries a monotonic `revision`, bumped by every state change, and
every mutating tool call takes `expected_revision` — the server rejects a stale
mutation outright (compare-and-swap).** This is the single concurrency discipline;
there are no unversioned flags. Consequences, each closing a reviewed failure path:

- **Operator asks are bound to a revision.** A `[Confirm]` keyboard carries
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
- One-to-many cases (one invoice paid in installments, one payment covering several
  invoices) are **explicit allocation groups**: a match group whose member
  allocations must sum to the invoice amount (resp. the transaction amount); the
  server validates the total and rejects partial or over-allocated groups.
  Allocation groups are never created by auto-match, and **always require operator
  confirmation**: a specialist-created group is `proposed` until the operator
  confirms it — total arithmetic proves sums, not document relationship, so no
  rationale bypasses review. (Round-2 finding: €40+€60 unrelated invoices passing
  total validation against a €100 payment.)

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
- per-field provenance (learned-from-match / operator-said) + last-confirmed date;
  `notes.md` flags links unverified for more than two quarters.

`none-expected` is load-bearing: it is what stops bank fees, taxes, and receipt-less
charges from polluting the residue list forever.

### Quarter workbook

Per-quarter record of pass runs, per-transaction state, and open residue. Makes
multi-round delegations stateless-safe (each delegation is a fresh ephemeral session;
state carries in the store, not in return values alone) and crash-safe: a casa
restart mid-pass loses only the in-flight turn.

## Tool surface (server, 14 tools)

Ingest & curation: `ingest_invoice` (agent-extracted metadata as arguments),
`update_invoice_metadata`, `mark_irrelevant`.
Query: `list_unmatched_invoices`, `list_quarter_state` (includes repair-sweep view:
matches with `annotation_state` in `pending`/`repair_owed`), `get_vendor`.
Matching: `record_match`, `propose_match`, `confirm_match`, `reject_match`,
`mark_annotated` — every mutating match tool takes `expected_revision` (CAS; see
the match-record state machine).
KB: `upsert_vendor`.
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
2. The specialist writes the bank-feed side — `tag_transaction`
   (`acct:matched`, `acct:<YYYY-Qn>`; portal cases `acct:portal`; known no-invoice
   cases `acct:no-invoice-expected`; open cases `acct:pending`) and `add_note`
   (`invoice: <filename>` or the portal link) — then calls
   `mark_annotated(match_id, expected_revision)` → `annotation_state=clean`.
3. Every weekly pass **begins** with the repair sweep: `list_quarter_state`
   surfaces every match with `annotation_state` in (`pending`, `repair_owed`) —
   half-completed writes and demotion/retarget corrections alike — and the
   specialist completes them before new work.
4. `reject_match` / reassignment / demotion set `repair_owed` in the same CAS
   transition that changes the state, recording exactly which tags to
   remove/replace and the correction note to append (bank-feed notes are
   append-only — honest audit trail).

Bank-feed may lag by at most one pass; it can never drift silently.

## Flows

### Weekly pass (resident cron reminder)

1. **Repair sweep** (above), then Ellen delegates: “weekly pass, `<YYYY-Qn>` —
   report new transaction state and search plans.”
2. **Specialist triage** over new DBIT transactions × invoice store × KB, reading
   PDFs as needed. **Auto-match bar**: exact amount, invoice date within the window,
   vendor consistent via KB — **and globally unambiguous over the QUARTER's working
   set, not the pass's**: ambiguity is evaluated against all of the quarter's
   transactions and invoices seen so far, so two same-vendor same-amount pairs
   arriving in different weekly passes still count as ambiguous (round-2 finding:
   staggered arrival silently cross-matching). A repeated vendor+amount pair may
   only auto-match on a **transaction-specific discriminator** (invoice number in
   the remittance, unique date adjacency); otherwise all candidate pairings go
   `proposed` with the ambiguity stated. Existing confirmed matches are never
   reopened by later arrivals — a new look-alike invoice makes the *new* pairing
   proposed, flagged as a possible cross-match for the operator. Returns a
   structured work order per transaction:
   `matched` (recorded + annotated) / `proposed` / `portal` (tagged, link noted) /
   `none-expected` / `missing` with a **search plan carrying discriminators**, not
   just a query (“want €54.45 within ~10 days of May 6; ignore payment
   confirmations”).
3. **Ellen's targeted Gmail round** — roughly one precise search per unresolved
   transaction, never a mailbox sweep (there are far more emails than transactions;
   the bank feed drives the search, not the mailbox). Ambiguity rule: **over-ingest
   all plausible candidates and let the specialist pick** — ingest is idempotent and
   losers stay available for other transactions or get `mark_irrelevant`. Re-delegate
   for final picks. Hard bound: two search rounds per pass, then the item goes to
   residue.
4. **Operator report — one question at a time.** The cron turn delivers the summary
   and, for the first item needing judgment, the invoice PDF plus a
   `[Confirm] [Wrong] [Later]` keyboard (#485/#573). Casa's attention lane holds one
   scheduled question, so the remaining items are **queued in the workbook, not
   posted**: each terminal outcome returns as a machine-authored scheduled turn into
   the same session, and that turn posts the next question. An `operator_busy` refusal
   — the operator already has a live question or a consent challenge — is not an error:
   the item stays `proposed` and is re-offered next pass. A pass posts at most
   `ASK_BUDGET` questions (default 3, tunable); everything beyond that is residue by
   design. Unanswered asks decay to `proposed`; nothing is ever lost by silence, and
   nothing waits on an outcome that may never arrive.
5. CRDT transactions: classified and annotated only.

Confirmation taps close the loop: `[Confirm]` → `confirm_match` (+ annotation
phase 2, + KB learning); `[Wrong]` → `reject_match` + one follow-up question whose
answer lands in the KB; `[Later]` → stays proposed, resurfaces next pass and at
quarter end. Each tap arrives as the continuation turn's content carrying
(match_id, revision) — the CAS rejection path is the only thing between a stale tap
and a corrupted book, and the fixture suite exercises it.

### New portal vendor

First classification of a vendor as portal-only triggers the one-time link research
(specialist, WebSearch): prefer the authenticated deep URL
(`console.vendor.tld/invoices`) over the marketing domain. Filed via `upsert_vendor`
with provenance. Every later quarter resolves instantly from the KB.

### Quarter-end pass (cron: 10th of Jan / Apr / Jul / Oct)

Weekly-pass mechanics over the **full quarter**, one last residue conversation, then
`build_quarterly_package("<YYYY-Qn>")`.

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
├── invoices/          # SnelStart bulk upload: YYYY-MM-DD_vendor_amount.pdf
├── ledger.xlsx        # full quarter, both directions: date, amount, currency,
│                      # direction, counterparty, vendor, status,
│                      # invoice filename OR portal deep-link, notes
└── notes.md           # action list first (missing items + best links),
                       # then anomalies, KB changes, commentary
```

Missing invoices never block shipping: ledger rows read `MISSING` with the best
available link; `notes.md` opens with the action list (right after the revision
line). The package build is deterministic and idempotent — “hold it”, supply
stragglers, rebuild as `r<N+1>`, resend is free.
Delivery: atomic write to the plugin outbox → `send_media(kind="zip")` (shipped,
#482) → operator's Telegram, from the quarter-end cron turn itself. The outbox copy is
consumed on send (or reaped at 2 h); the canonical package stays in the data dir. Two
edges the shipped tool imposes: a transport timeout is reported as
`delivery_uncertain` — the send may have landed — so a retry is an operator-visible
**resend of the same revision**, never a silent rebuild (the revision line in
`notes.md` and the caption make a duplicate arrival self-evident); and the `zip` kind
caps at **20 MB**, which a quarter of PDF invoices can approach.

## Privacy

- The repo ships **zero** personal or company data: no IBANs (bank-feed owns
  accounts), no company name, no vendor list, no operator identity. This spec
  deliberately says “the operator's B.V.”.
- The zip filename prefix comes from `CASA_PLUGIN_QA_COMPANY_SLUG`, wired through
  casa's plugin-env (1Password-referenced), never committed.
- Everything identifying lives only in the data dir.
- House publication guards anyway (pre-commit deny-patterns, gitleaks) as
  belt-and-braces for a private repo.

## Error handling

- Tools fail explicit and loud; no silent fallbacks.
- Ingest idempotent by content hash; over-ingestion harmless by design.
- All match mutations are CAS on the match `revision`; stale operator taps and
  crossed writes are rejected, never absorbed. Ellen re-asks with current facts on
  a CAS rejection of a keyboard answer.
- Two-phase bank-feed annotation with start-of-pass repair sweep, whose step 0
  re-resolves superseded/vanished bank-feed rows before anything else; annotation
  is a tracked state (`pending`/`clean`/`repair_owed`), never a boolean that
  demotion could leave stale.
- Server-enforced cardinality: no second active match per invoice or transaction;
  allocation groups validated by totals and always operator-confirmed.
- Ask-keyboards are durable across a casa restart (#573), and every terminal outcome
  is delivered back to the asking session — at most once, so a lost outcome is a
  normal case, not an incident. Timeout, cancellation, trigger rewrite and
  `operator_busy` all read the same way downstream: the item is still `proposed`.
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
- The ask queue is store state, so it is unit-testable and must be tested: a pass with
  five items needing judgment posts ONE question and leaves four queued; an
  `operator_busy` refusal posts nothing and leaves the item `proposed`; a terminal
  outcome that never arrives (at-most-once loss) leaves the item `proposed` and the
  next pass re-offers it; and an answer arriving after the trigger was rewritten is
  rejected by CAS rather than applied.
- Matching quality is LLM behavior, not unit-testable here: first real quarter runs
  `proposed`-heavy by design until the KB warms up; the auto-match bar (exact
  amount + date window + vendor consistency via KB + global unambiguity) keeps wrong-match risk asymmetric
  in the safe direction (a missed match costs a residue line; a wrong match corrupts
  the books).

## Changes in other repos

| Repo | Change | Status |
|---|---|---|
| casa-specialist-finance | Role bump: `max_turns` 10 → ~40; allow the new plugin's tools in `role/role.yaml`. | Small release, needed for v1. |
| ha-casa-app | [#482](https://github.com/bonzanni/ha-casa-app/issues/482) zip media kind. | **Shipped** — closed 2026-08-14. |
| ha-casa-app | [#485](https://github.com/bonzanni/ha-casa-app/issues/485) scheduled-turn `send_media`. | **Shipped** — closed 2026-08-14. |
| ha-casa-app | [#573](https://github.com/bonzanni/ha-casa-app/issues/573) scheduled-turn `ask_user` — the half split out of #485, and the one the confirmation flow actually needs. | **Shipped** — closed 2026-08-15. |
| ha-casa-app | [#486](https://github.com/bonzanni/ha-casa-app/issues/486) shared handoff area, [#487](https://github.com/bonzanni/ha-casa-app/issues/487) specialist→resident requests. | Still open, still not dependencies: gmail→store custody stays an agent-passed path, specialist asks stay structured work orders. |
| Resident config | Two reminders (weekly; quarter-end on the 10th), plugin assignment to both roles. | Operator/configurator action at install time. |

### Open casa issues this plugin designs around

None blocks v1. Each costs a plugin-side line rather than a wait (open as of
2026-09-20):

| Issue | Bite | Plugin-side answer |
|---|---|---|
| [#990](https://github.com/bonzanni/ha-casa-app/issues/990) — `send_message` reports "sent" when the channel delivered nothing (bug, medium) | A pass's summary can vanish while the turn believes it delivered, and the closing-silence convention then suppresses the only other output. | A pass's operator-visible output is never a bare `send_message`: the report rides `send_media`/`ask_user`, whose failures are loud (`delivery_uncertain`, `operator_busy`). |
| [#960](https://github.com/bonzanni/ha-casa-app/issues/960) / [#932](https://github.com/bonzanni/ha-casa-app/issues/932) — a scheduled turn that delivers with a tool and then ends in prose delivers twice (bug, low) | Two DMs per pass. Nothing enforces the clause; only documentation asks for it. | Both trigger prompts this plugin ships carry the closing `<silent/>` clause verbatim, in the install notes. |
| [#975](https://github.com/bonzanni/ha-casa-app/issues/975) — bundle compensation writes an emptied tuple over a refused transaction's files (bug, high, `operator-decision`) | Hits the `casa-specialist-finance` role bump (`upgrade_specialist`), not the runtime: a refused upgrade can take the specialist's saved settings 1 → 0. | Capture the specialist's settings before the bump and verify after. The issue is blocked on an operator decision, so it will not clear on its own. |
| [#1024](https://github.com/bonzanni/ha-casa-app/issues/1024) — install-time vault exploration searches variables no recipe may wire from a vault item (bug, low) | `CASA_PLUGIN_QA_COMPANY_SLUG` is exactly that class; install will hunt 1Password for it and offer candidates the recipes forbid using. | Install notes state it plainly: a plain setting, typed at install, never a vault item. |
| [#1033](https://github.com/bonzanni/ha-casa-app/issues/1033) — a progress report made while answering the operator is credited to the previous batch (bug, medium) | Only if a pass becomes a `casa.jobs` job. | Settled by the jobs decision below; v1 does not declare a job. |
| [#480](https://github.com/bonzanni/ha-casa-app/issues/480) — apply the per-engagement uid and capability drop to in-process (`in_casa`) engagements too (enhancement) | Would change this plugin's file-access assumptions: the gmail→store custody hop and the specialist's `Read` of the invoice store both rely today on delegated turns sharing the process user. INV-CONT-004 already requires a pinned plugin directory to be owned by the dropped uid or world-readable and traversable. | Watch it. If it lands, the store's directory modes and the ingest hop need a re-read — and the case for [#486](https://github.com/bonzanni/ha-casa-app/issues/486) stops being a convenience argument. |

Checked and **not** reachable for this plugin as specified: the plugin-setup and consent
issues (#1012, #1005, #1014 — it declares no setup tool and no credentials) and #987
(it declares no `systemRequirements`, so it publishes no `verify_bin` that could shadow
an s6 name). If the implementation adds any of those, re-check them.

## Open items

- Exact auto-match thresholds (date window ~10 days, amount tolerance for FX/fees)
  to be tuned during the first real quarter; start strict.
- Ledger column set to be reviewed with the accountant after the first delivered
  package.
- Weekly reminder day/time: operator preference at install (proposal: Monday
  morning).
- **Should the matching pass run as a `casa.jobs` background job?** (New since v1.)
  For: a long pass stops blocking a resident turn, gets progress lines in its own
  topic, survives a restart, and is `/cancel`-able. Against: a job worker cannot ask
  the operator, so every confirmation returns to Ellen either way; it adds a second
  engagement to reason about; and the batch machinery is three weeks old with one open
  defect (#1033). Proposal: v1 ships without jobs, and the quarter-end pass — the long
  one — is the first candidate to move if a pass ever runs out of turns.
- **Package size against the 20 MB `zip` cap.** A quarter of PDF invoices can approach
  it. Decide the split rule (per-month parts, `invoices/` only + separate ledger) before
  the first real delivery rather than during it.
