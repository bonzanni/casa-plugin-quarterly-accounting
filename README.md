# casa-plugin-quarterly-accounting

A Casa plugin that prepares a B.V.'s quarterly accounting. It matches every transaction on the
business bank account (from bank-feed) to the document it needs, keeps `acct::` tags and
accounting notes current in the bank ledger, answers from its own store when asked, and builds
a quarter's zip (SnelStart-ready `invoices/`, `ledger.csv`, `ledger.xlsx`, `notes.md`) on
request. The server registers 40 tools. Design: `docs/superpowers/specs/2026-08-10-quarterly-accounting-design.md`.

The checking runs as one Casa job on the finance specialist, `quarterly-accounting:work`
("Accounting check", skill `skills/quarterly-job/SKILL.md`): one pass per run that reads the
bank, searches Gmail vendor by vendor, matches invoices to payments, keeps the bank ledger's
notes current (`record_mirror`) and ends with one message and its cards, in fresh sessions of
80 turns per batch. The job declares `quietWhenScheduled`: started by the scheduler it runs
silently and speaks only when there is something to say. The operator's taps (`verdict`) are recorded at once and each answer posts the next card; the
bank ledger's tags and notes follow at the next check. The
finance specialist's desk (skill `skills/quarterly-accounting/SKILL.md`) answers the operator
with posted views and their buttons, reads the operator's words into a reading to Apply
(`propose_reading`), files the documents the operator sends, and asks for the check
(`request_work`, then Casa's `start_job`). `get_package` builds the freshest quarter package
from the store on request and sends it as a file in Telegram, open items or not; nothing is
emailed. `job_status` answers, read-only, whether the job may end; `set_aside` closes a job
item no other write closes (an attachment that is no invoice, a row bank-feed still has).

## Requirements
- **Casa v0.344.39 or newer** (the release carrying #1301, #1302 and #1303, #1308 and #1312). #1301
  lets the job run silently when the scheduler starts it (`quietWhenScheduled`), #1302 lets a
  tap's receipt post the next card, #1303 lets a [Get package] button deliver the file, and
  #1308 (v0.344.38): a tap's pinned turn is the operator's tap, not a delegation (stored-call
  taps no longer refuse on the specialist's role scope). #1312 (v0.344.39): a message the
  job re-posts after Casa cut the batch before its delivery was marked is not sent twice
  (each deposit carries a `key`). An older Casa refuses this
  plugin's manifest (`casa.jobs invalid: entry 1 field quietWhenScheduled`): the plugin does
  not load. There is no Casa min-version field and no fallback.
- bank-feed **0.20.0** or newer (casa-specialist-finance component 0.21.0) installed on the
  finance specialist with the business account linked, labelled `company`, and synced.
- The gmail plugin (0.9.0 or newer) on the finance specialist (the job's searches; read-only).
  Without it, checks still run and say "Gmail isn't connected for the finance specialist —
  invoices aren't being searched." Packages are never emailed: they arrive as a file in
  Telegram.

## Install
1. Update the plugin on finance first (it is already assigned there in S2).
2. Assign the plugin to `specialist:finance` only. Unassign it from the main assistant; Casa's
   configurator states the consequence once (the assistant no longer answers accounting
   itself; it sends it to Finance).
3. Replace the old cron (if present) with the job trigger:
   `name: quarterly-check, type: cron, schedule: "0 9 * * 1", channel: telegram,`
   `job: "quarterly-accounting:work", task: "Weekly accounting check."`

Nothing is asked at install. The account binds itself when exactly one company account exists.
The package name and the start quarter are defaulted and changeable by asking.

## Upgrade notes
- **0.10 → 0.11:** schema 12. The first run writes one plain note per bank-ledger row (the
  notes are plain text now). Buttons on the S7 renderings already posted go stale once.
  Package requests still open at the upgrade are told so. Requires Casa v0.344.39 (see
  Requirements).
- **0.9 → 0.10: remove the old prompt cron.** The weekly prompt trigger
  `quarterly_accounting_pass` on the main assistant (the 0.9 install's
  `name: quarterly_accounting_pass, type: cron, schedule: 0 9 * * 1, prompt: Run the
  quarterly-accounting background pass…`) survives the plugin upgrade: Casa keeps a
  role's triggers whatever plugin is updated. Left in place it keeps firing every Monday
  at 09:00 and asks the main assistant to run the accounting, beside the new job trigger.
  Ask Casa's configurator to remove it ("remove the trigger quarterly_accounting_pass from
  the assistant"): it runs `config_trigger_delete(role="assistant",
  name="quarterly_accounting_pass")` and reloads that role's triggers. Then check that only
  `quarterly-check` (Install, step 3) remains.

## Uninstall
The plugin declares `reset_store` as its Casa `eraseTool`. On Casa 0.329.0+, uninstalling asks
Keep data / Erase everything / Cancel; "Erase everything" runs `reset_store()` and removes the
plugin only once it reports complete. `reset_store` takes no arguments and is protected (Casa
asks the operator for one tap); it may refuse while another session holds the documents lock
(filing or erasing), or answer `incomplete` while another session still reads the store — in
either case nothing is lost, and the same request can be made again. It erases this plugin's
own store, documents and packages; it does not remove the `acct::` tags and accounting notes
already written into bank-feed's ledger, nor Home Assistant backups.

## Development
- `python3 -m unittest discover -s tests -t .` — the whole suite, standard library only.
- `tests/upstream/` holds test-only copies of bank-feed (component v0.21.0, and v0.13.2 for the
  below-floor case) and gmail's sent log. Refresh with `scripts/vendor-bankfeed.sh <tag>`.
- `git config core.hooksPath .githooks` — tool-list agreement and the identifier scan.
- `scripts/check_tool_agreement.py` — the server's registry, `casa.provides_tools` and
  `casa.resultContract.tools` must name exactly the same 40 tools.
- `scripts/scan_identifiers.py .` — fails the build on an IBAN-shaped token anywhere outside
  `tests/upstream/`. No IBAN, company name, vendor list or operator identity belongs in this
  tree; when in doubt, run the script.
- CI (`.github/workflows/ci.yml`) runs the suite, both scripts, and a pinned gitleaks over the
  whole history. Accepted findings are declared by fingerprint in `.gitleaksignore`.

## Releasing
The version is `.claude-plugin/plugin.json`'s `version`. To release, bump it (MAJOR.MINOR.PATCH)
and merge to `main`. When `ci` passes on that push, `.github/workflows/release.yml` tags the
commit `v<version>`; a version that already has a tag is left alone. Tags are never pushed by
hand.

## Reset loop (debugging on production)
Quiesce first (no accounting job running, `/new` on the finance specialist), then, if the bank
ledger must go back too, restore bank-feed's install backup for `acct@<this version>`
(`list_backups`, then `restore_backup`, one operator tap — bank-feed's own tools). Then ask the
finance specialist to erase the accounting store: `reset_store()` (one operator tap; may refuse
while another session holds the documents lock, or answer `incomplete` while one still reads
the store — try again). Then upgrade, or say "test install" on the finance desk
(`skills/quarterly-accounting/SKILL.md`, "Test install": `check_setup`, then a check ask); the
check's first write mints the new install backup.
