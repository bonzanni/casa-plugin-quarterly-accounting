# Changelog

## 0.11.0

The simple loop. Casa v0.344.37 carries the three features: #1301 `quietWhenScheduled`
(v0.344.36), #1302 a tap's receipt posts the next card (v0.344.35), #1303 a file-delivering
button (v0.344.37). v0.344.38 (#1308): a tap's pinned turn is the operator's tap, not a
delegation (stored-call taps no longer refuse on the specialist's role scope). There is no
Casa min-version field: an older Casa refuses the manifest (`casa.jobs invalid: entry 1 field
quietWhenScheduled`) and the plugin does not load.

Requires Casa v0.344.38 or newer (the release carrying #1301, #1302 and #1303).

0.10.0 was released separately as the S7 work (quarterly accounting off Ellen); this release
builds on it.

- **The loop.** One pass per run: probes, a snapshot, filing, the vendor units, the mirror and
  the post. The job entry gains `quietWhenScheduled`: a scheduler-started run is silent unless
  it has something to say. `job_next` hands out the work list; progress is counted at batch end
  (`calls_made`) and a stop is said per streak.
- **The floor and decide.** A match needs the same currency and the exact amount; the machine's
  own pairing is replaced by the operator's, a no-op re-decision changes nothing, and a
  decision applies per vendor group. `record_missing` records a payment with no document.
- **The mirror.** The bank ledger's `acct::` tags and notes are written as a diff against what
  was last mirrored, in plain note text, in grouped calls (`record_mirror`); rows that left
  scope lose their tags.
- **The end message and its cards.** One message ends a run, with a Review order, paged vendor
  cards and an open-items card; a tap's receipt posts the next card (#1302), and the ready
  notice announces a finished package.
- **`get_package`.** The package is built from the store at the request, always sent with one
  dated caption line, open items or not; the delivered file is the receipt (#1303).
- **The deletions.** The sweep, the chunk carry, the judge, credits, nested passes, package
  requests and the delegation protocol are gone, with their tables
  dropped by schema 12.

Upgrade notes:
- Schema 12 migrates the store on first start; open package requests are told at the upgrade.
- The first run writes one plain note per ledger row (D14).
- The S7 renderings' buttons go stale once; ask for the view again.

## 0.10.0

The S7 release: the quarterly accounting moved off Ellen onto the finance specialist, with the
typed reading and its bound rendering, the job's starter line and the desk/job split.
