# casa-plugin-quarterly-accounting

A Casa plugin that prepares a B.V.'s quarterly accounting. It matches every transaction on the
business bank account (from bank-feed) to the document it needs, keeps `acct::` tags and
accounting notes current in the bank ledger, answers from its own store when asked, and builds
a quarter's zip (SnelStart-ready `invoices/`, `ledger.csv`, `ledger.xlsx`, `notes.md`) on
request. The server registers 38 tools. Design: `docs/superpowers/specs/2026-08-10-quarterly-accounting-design.md`.

The checking runs as one Casa job on the finance specialist, `quarterly-accounting:work`
("Accounting check", skill `skills/quarterly-job/SKILL.md`): the bank read, the ledger sweep,
the Gmail searches and the judging of documents, one unit at a time from `job_next`, in fresh
sessions of 80 turns per batch. Ellen only asks for work (`request_work`, `request_package`,
then Casa's `start_job`), relays what the job returns (`job_report`), and builds and sends
packages. `job_status` answers, read-only, whether the job may end; `record_filing` closes the
job's filing step. The 0.8.0 pass tools (`begin_pass`, `end_pass`, `continue_pass`,
`record_step`, `more_work`) are gone.

## Requirements
- Casa **0.337.0** or newer: the job needs `"session": "fresh"` and the brief's `Job id:` line
  (0.336.0) and `"host": "specialist"` (0.337.0). Below it the job would run on Ellen, without
  bank-feed or Gmail — do not install this version on an older Casa.
- bank-feed **0.20.0** or newer (casa-specialist-finance component 0.21.0) — unchanged —
  installed on the finance specialist with the business account linked, labelled `company`,
  and synced.
- The gmail plugin (0.9.0 or newer) on Ellen (emailing a package) and on the finance
  specialist (the job's searches). Without it on the finance specialist, checks still run and
  say "Gmail isn't connected for the finance specialist — invoices aren't being searched."

## Install
1. "Install the quarterly accounting plugin from `bonzanni/casa-plugin-quarterly-accounting`,
   for Ellen and the finance specialist."
2. The one trigger in `skills/quarterly-accounting/SKILL.md` ("Install"), on Ellen. Rewriting
   the trigger later cancels nothing this plugin relies on (it asks no button questions).

Nothing is asked at install. The account binds itself when exactly one company account exists.
The package name and the start quarter are defaulted and changeable by asking.

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
  `casa.resultContract.tools` must name exactly the same 38 tools.
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
Quiesce first (no accounting job running, `/new` on both agents), then: ask the finance specialist to
restore the install backup that `list_backups` registers for `acct@<this version>` (`restore_backup`,
one operator tap); call `reset_store()` (one operator tap; may refuse while another session
holds the documents lock, or answer `incomplete` while one still reads the store — try again);
then upgrade or just run the pass, whose first write mints the new install backup. Full steps,
including what to do when more than one `acct@` version is registered: see
`skills/quarterly-accounting/SKILL.md`, "Test install and reset".
