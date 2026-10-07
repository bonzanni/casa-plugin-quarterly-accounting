# Changelog

## 0.11.0

The simple loop. Casa v0.344.37 carries the three features: #1301 `quietWhenScheduled`
(v0.344.36), #1302 a tap's receipt posts the next card (v0.344.35), #1303 a file-delivering
button (v0.344.37). v0.344.38 (#1308): a tap's pinned turn is the operator's tap, not a
delegation (stored-call taps no longer refuse on the specialist's role scope). v0.344.39
(#1312): a re-posted plugin message with a known `key` is not sent twice. There is no
Casa min-version field: an older Casa refuses the manifest (`casa.jobs invalid: entry 1 field
quietWhenScheduled`) and the plugin does not load.

Requires Casa v0.344.39 or newer (the release carrying #1301, #1302 and #1303, and #1312).

0.10.0 was released separately as the S7 work (quarterly accounting off Ellen); this release
builds on it.

- **The loop.** One pass per run: probes, a snapshot, the erase checks, filing, the payments
  one at a time in date order, the mirror and the post. The job entry gains
  `quietWhenScheduled` (a scheduler-started run is silent unless it has something to say) and a
  real batch cap, `"batches": 20`. Each payment comes with its candidate documents and their
  stored reading; when nothing fits, the job searches that vendor's mail (at most three
  searches a payment), files every invoice it finds, and decides the payment in one call.
  Progress is one definition, read by Casa's batch report and by the hand-outs alike. There
  is no call budget: work is handed until Casa ends the batch at its turn limit, and a
  standalone `report` step reports the work since the last report (a batch Casa cut is
  reported by the next one). Live Q2 run 1 showed a budget the model must count for is
  fragile.
- **Owed work survives a cut.** Everything a unit owes is a server row from the moment it is
  known: each erase candidate, the own-mail search and every attachment it found, every
  attachment a payment's search found (`record_search` carries `refs`, recorded right after
  the search). A payment is decided only once its found attachments are filed or set aside; a
  unit handed twice without progress gives its items up, visibly ("N attachments found but not
  filed", "Your own mail was not read", "N erased bank rows not confirmed"), and a run that gave
  anything up says its missing payments are "search incomplete". `set_aside` closes an
  attachment that is no invoice or a row bank-feed still has; the filing's own closing tool is
  gone (filing ends when its queue is empty). A payment decided missing is walked again in
  the same run when a later search files its invoice.
- **The floor and decide.** A match needs the same currency and the exact amount; another
  currency is only proposed; a document is held only by a match or a proposal's chosen
  document (a proposal's alternatives hold nothing); a no-op re-decision changes nothing.
  `record_missing` records a payment with no document. A document is filed first, then read
  through `read_document` (a download itself cannot be read) and its reading recorded with
  only what it prints — never the payment's amount; a document a cut left unread is handed
  `unread` and read before it is judged; two readings of the same
  file that disagree make its amount unknown for good, and such a document is only ever
  proposed (a machine match on it turns into a proposal).
- **Later documents and handovers.** A document found later by mail never reopens a match or a
  proposal: it is filed and listed in the package as unmatched. A document the operator hands
  over for a payment that already has one gets ONE card — "… already has an invoice. Current: …
  New: …" [Keep current] [Use new] — bound to what it showed; nothing is replaced without the
  tap.
- **The mirror.** The bank ledger's `acct::` tags and notes are written as a diff against what
  was last mirrored, in plain note text, in grouped calls (`record_mirror`, after each chunk
  of 8); rows that left scope lose their tags. A cut inside a chunk may repeat up to 8 calls:
  a repeated tag call changes nothing, a repeated note call adds an identical line.
- **One delivery per message.** Every view and results deposit carries a key (`view:<store
  id>:<render id>`, `results:<store id>:<render ids>`): with Casa #1312, a message re-posted after Casa cut the batch
  before its delivery was marked is not sent twice (Casa v0.344.39).
- **An empty check says why (#47).** A check with no payment in its quarter says so plainly
  — "Nothing to check for Q2 2026: the books start 1 Oct 2026. Say 'start from Q2 2026' to
  include it." — in the operator's end message and in the completion text an assistant
  relays (whoever started the run); a check that worked completes with "Q3 2026 checked: N
  matched, M to confirm, K missing".
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
