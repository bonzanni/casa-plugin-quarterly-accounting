# Changelog

## 0.11.2

The card the operator reads, from the first prod "check Q3" on 0.11.1 (2026-10-08), and
the operator-approved conversation script (`docs/superpowers/specs/
2026-10-08-conversation-script.md`). Same Casa floor (v0.344.39) and store schema 15.

- **Two intents about a quarter (#55).** Where a quarter stands: `show_view(view="open",
  quarter=…)` posts one status card for that quarter: its payments, non-zero counts, its
  own proposals and missing invoices, one line per other quarter with open items, and
  [Get package] only when the quarter has a payment. Nothing runs. For a quarter before the
  books' start it posts nothing and answers one line ("Q3 2026 isn't in the books yet…"),
  as `{"view": null, "say": …}`, Casa's no-deposit statement. Getting a quarter done:
  `request_work(check, operator, quarter)`. For a quarter before the books' start it moves
  the start to the quarter's first day in the same transaction, with no question: "Starting
  the books from 1 Jul 2026 and checking Q3 — I'll post the result here." While a check is
  running, nothing moves and nothing is asked ("A check is running right now. When it has
  finished, ask me again to do Q3.": the running check read the bank from the old start).
  The "start from …" reading and its grant-only start setter are removed. The desk skill
  describes the two intents in prose, with no phrase lists. The run's end card is the
  quarter's card: its own items, every other quarter one line. After a delivered end card,
  the run's completion text is "The result card is posted in the chat; there is nothing to
  add." (Casa ha-casa-app#1332 owns the resident's restatement).
- **The render tag is a day and time (#53).** Every card's first line ends with
  " · 8 Oct 21:04:37", the moment it was composed, in CASA_TZ, then TZ, then UTC; it
  replaces the bare render number. Residual (operator ruling): the same card shown twice in
  the same second (within a year) is refused visibly when replied to, never bound wrongly.
- **One purchase is one line (#52), and a purchase is found by its email too (#48).** A
  purchase is everything linked, directly or through each other, by the same issuer and
  number OR the same email (any email the bytes were filed from) with the same read amount
  and currency (`documents.purchase`, the one definition; every write that can grow a held
  document's purchase is checked against the job's floor): prod read each receipt with its own
  number, so issuer + number alone left an invoice and its own receipt apart (an August card
  offered July's and October's receipts). The proposal line counts purchases. The Review
  card shows one line and one button per purchase: the current document, else the
  purchase's invoice. #48's floor uses the same definition.
- **Whole lines (#54).** No fixed-width hard breaks: `views._wrap` and `WIDTH` are gone, and
  every line is one item that the client wraps.
- **Plain words and buttons.** Zero counts are not shown. Buttons carry no counts
  (⟦Review⟧ ⟦Confirm all⟧ ⟦Get package⟧), and every card that carries buttons (and a
  reading, and the account choice) ends with one line saying what each button shown does:
  "Review: go through the 5 to confirm and the missing invoices of 6 vendors, one at a
  time". A run that took
  an operator's check gets the operator's card, whoever started it. Corrections
  read "Remove the match for …", "Rule out …", "Matched to …"; a proposal reads
  "Suggested: …".

## 0.11.1

Two floors at the job's decide, from Q2 re-run #3 on 0.11.0. Same Casa floor: v0.344.39.
Store schema 15 (the run's work queue admits an `email` item; the upgrade copies a live
run's rows).

- **One purchase backs at most one payment (#48).** A purchase is every document with the
  same issuer and the same document number (an invoice and its receipt filed from one email);
  a document with no number is a purchase of its own. The job's match or proposal of a
  document whose purchase already backs another payment is refused, and the refusal names
  that payment, so the job searches for this payment's own document. The candidates show such
  a twin as held by another payment, the exact fit skips it, a card's alternatives leave it
  out, and the job's `replace` answer to a handover refuses it (an open replace question
  whose purchase another payment took since is retired). The job's own reading
  (`update_document_metadata` with the pass token) is refused when its issuer or number would
  move a held document into a purchase another payment backs. The operator's taps and edits
  are not limited by it. No date window: Google's month shift and a March invoice paid in
  April stay legitimate. Re-run #3: refuses exactly the two wrong ElevenLabs proposals (July
  with May's receipt, September with August's); refuses nothing in run 1 or the bookkeeping
  key.
  *Accepted cost (operator ruling):* first claim wins within a run. If a later payment
  wrongly takes an earlier `missing` payment's purchase, the earlier payment stays missing
  until the next check; the operator rejects the wrong proposal, as with 0.11.0.
- **`missing` waits for the emails a payment's own reference search returned (#50).**
  `record_search` carries `emails`: on the search by the payment's own reference or order
  number, every vendor email it returned and whether its attachments were listed. `missing`
  is refused while one is unlisted, and the refusal names it; the hand-out carries them
  (`emails_to_list`). A listing is reported with no queries and is not a search. This is a
  reminder the model fills in: it catches a skipped listing on an honest report, never an
  email left out of the report or a query mislabelled — the server does not see Gmail.
  Re-run #3: refuses 2 of 31 `missing` decisions — Megekko 1 (its shipped email carries the
  invoice) and one Amazon payment whose outcome does not change.

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
