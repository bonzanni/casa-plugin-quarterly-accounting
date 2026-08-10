# casa-plugin-quarterly-accounting — design

Status: draft for operator review · 2026-08-10

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
  set for specialist and executor tiers (`agent_loader.py:83-97`). Ellen being the
  scheduled entry point is structural.
- A delegated specialist cannot use `ask_user` (gate requires
  `execution == "direct"`, `tools.py:635`), and the two-turn ask shape cannot outlive
  an ephemeral delegation anyway. Operator UX therefore belongs to the resident.
- `send_media` gates on **origin**, not executor (`tools.py:352` area): delegated
  turns inherit origin. Scheduled turns currently carry a synthetic chat id
  (`trigger_registry.py:249`), which blocks media/keyboards in the cron turn itself —
  see “casa dependencies” for the enhancement and the degradation path.
- The specialist reads PDFs itself rather than receiving curated extractions:
  matching judgment ("does this document explain this transaction?") must be made by
  the agent that sees both sides at full fidelity. Ellen's ingest-time extraction is
  provisional, for filing only, never load-bearing.

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

### Match records

invoice_id ↔ bank-feed `row_id`, state `matched` / `proposed` / `rejected`, decision
author (`auto` / `operator`), rationale, and **`bankfeed_annotated: bool`** (see
annotation protocol).

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
matches with `bankfeed_annotated=false`), `get_vendor`.
Matching: `record_match`, `propose_match`, `confirm_match`, `reject_match`,
`mark_annotated`.
KB: `upsert_vendor`.
Packaging: `build_quarterly_package(quarter)`, `stage_for_delivery(target)` (copies
an invoice PDF or the built package into casa's plugin outbox for `send_media`).

House disciplines copied from bank-feed: explicit loud failures, numeric caps and
truncation notices on reads, provider text fenced as untrusted on output, three-way
tool-list agreement (server, `provides_tools`, role allow-lists) with a CI check.

## Bank-feed annotation protocol (two-phase, tracked)

The plugin server cannot call bank-feed's tools, so the specialist performs both
writes; the design makes the pair a tracked transaction rather than a convention:

1. `record_match` / `confirm_match` store the match with `bankfeed_annotated: false`.
2. The specialist writes the bank-feed side — `tag_transaction`
   (`acct:matched`, `acct:<YYYY-Qn>`; portal cases `acct:portal`; known no-invoice
   cases `acct:no-invoice-expected`; open cases `acct:pending`) and `add_note`
   (`invoice: <filename>` or the portal link) — then calls `mark_annotated`.
3. Every weekly pass **begins** with a repair sweep: `list_quarter_state` surfaces
   any match still unannotated (e.g. a turn died between the writes) and the
   specialist completes it before new work.
4. `reject_match` / reassignment likewise: correcting tags plus an appended
   correction note (bank-feed notes are append-only — honest audit trail), tracked
   the same way.

Bank-feed may lag by at most one pass; it can never drift silently.

## Flows

### Weekly pass (resident cron reminder)

1. **Repair sweep** (above), then Ellen delegates: “weekly pass, `<YYYY-Qn>` —
   report new transaction state and search plans.”
2. **Specialist triage** over new DBIT transactions × invoice store × KB, reading
   PDFs as needed. Returns a structured work order per transaction:
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
4. **Operator report.** With ha-casa-app#485: PDF + `[Confirm] [Wrong] [Later]`
   ask-keyboards directly in the cron turn. Until then, two beats: text summary
   (“2 items need your eyes — say ‘review’”), and the media/keyboards flow in the
   DM turn the operator's reply creates. Unanswered asks decay to `proposed`;
   nothing is ever lost by silence.
5. CRDT transactions: classified and annotated only.

Confirmation taps close the loop: `[Confirm]` → `confirm_match` (+ annotation
phase 2, + KB learning); `[Wrong]` → `reject_match` + one follow-up question whose
answer lands in the KB; `[Later]` → stays proposed, resurfaces next pass and at
quarter end.

### New portal vendor

First classification of a vendor as portal-only triggers the one-time link research
(specialist, WebSearch): prefer the authenticated deep URL
(`console.vendor.tld/invoices`) over the marketing domain. Filed via `upsert_vendor`
with provenance. Every later quarter resolves instantly from the KB.

### Quarter-end pass (cron: 10th of Jan / Apr / Jul / Oct)

Weekly-pass mechanics over the **full quarter**, one last residue conversation, then
`build_quarterly_package("<YYYY-Qn>")`:

```
<slug>-<YYYY-Qn>.zip
├── invoices/          # SnelStart bulk upload: YYYY-MM-DD_vendor_amount.pdf
├── ledger.xlsx        # full quarter, both directions: date, amount, currency,
│                      # direction, counterparty, vendor, status,
│                      # invoice filename OR portal deep-link, notes
└── notes.md           # action list first (missing items + best links),
                       # then anomalies, KB changes, commentary
```

Missing invoices never block shipping: ledger rows read `MISSING` with the best
available link; `notes.md` opens with the action list. The package build is
deterministic and idempotent — “hold it”, supply stragglers, rebuild, resend is free.
Delivery: atomic write to the plugin outbox → `send_media(kind="zip")`
(ha-casa-app#482) → operator's Telegram. The outbox copy is consumed on send (or
reaped at 2 h); the canonical package stays in the data dir.

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
- Two-phase bank-feed annotation with start-of-pass repair sweep (above).
- Ask-keyboards die on casa restart / timeout → state decays to `proposed`, never
  lost.
- Casa restart mid-pass: workbook + store hold everything except the in-flight turn.
- Packaging with open residue ships `MISSING` rows rather than blocking.

## Testing

- Pure-Python unit tests over store, index, naming, KB, workbook, package build —
  no casa runtime required (bank-feed's model).
- A **fixture quarter** (synthetic transactions + synthetic PDFs) driving an
  end-to-end `build_quarterly_package` assertion: exact file set, ledger rows,
  MISSING handling, zip layout.
- Matching quality is LLM behavior, not unit-testable here: first real quarter runs
  `proposed`-heavy by design until the KB warms up; the auto-match bar (exact
  amount + date window + vendor consistency via KB) keeps wrong-match risk asymmetric
  in the safe direction (a missed match costs a residue line; a wrong match corrupts
  the books).

## Changes in other repos

| Repo | Change | Status |
|---|---|---|
| casa-specialist-finance | Role bump: `max_turns` 10 → ~40; allow the new plugin's tools in `role/role.yaml`. | Small release, needed for v1. |
| ha-casa-app | [#482](https://github.com/bonzanni/ha-casa-app/issues/482) zip media kind. | **Needed for delivery.** |
| ha-casa-app | [#485](https://github.com/bonzanni/ha-casa-app/issues/485) scheduled-turn operator interaction. | UX improvement; graceful two-beat degradation without it. |
| ha-casa-app | [#486](https://github.com/bonzanni/ha-casa-app/issues/486) shared handoff area, [#487](https://github.com/bonzanni/ha-casa-app/issues/487) specialist→resident requests. | Not dependencies; flow converts naturally if/when they land. |
| Resident config | Two reminders (weekly; quarter-end on the 10th), plugin assignment to both roles. | Operator/configurator action at install time. |

## Open items

- Exact auto-match thresholds (date window ~10 days, amount tolerance for FX/fees)
  to be tuned during the first real quarter; start strict.
- Ledger column set to be reviewed with the accountant after the first delivered
  package.
- Weekly reminder day/time: operator preference at install (proposal: Monday
  morning).
