# Changelog

## 0.11.20

Same store schema (17) and Casa floor (v0.344.67).

- **The rest of the cards follow the house style (#102).** The "To check" list asks its
  question once ("Are these the right documents?") instead of "Not sure — say if it's wrong."
  under every item. The missing list has one title, not a second "Missing" heading.
  [Show matches to confirm] says how many are this quarter's and how many earlier, so the
  number matches the card it is on. Tapping a "Show …" button turns the card into that list
  instead of first sending a lone line. After "All good" the confirmation has a title and one
  line per payment, named as the card named it. A match in another currency names whose
  document it is. "Is Q3 ready for my accountant?" now opens with "Q3 is ready for your
  accountant." or "Not ready yet."

## 0.11.19

Same store schema (17) and Casa floor (v0.344.67).

- **"Check Q3" answers with the quarter's status (#103).** It is a question about where the
  quarter stands, as ruled on 8 October, also when the assistant passes it on as "run the
  check". Finance posts the status card and runs nothing. The accounting check starts when
  you ask for the work, for instance "do the Q3 accounting".

## 0.11.18

Same store schema (17) and Casa floor (v0.344.67).

- **A payment that was never classified is no longer called a missing invoice (#98).** When
  the bank sync says rows still await the classifier, the check classifies them first, the
  way the classifier itself does (it uses the payment's history, so a monthly wage-tax
  payment is recognised from the earlier ones). A payment it still cannot place shows as
  "not classified yet", never as a missing invoice, and it keeps the quarter open.
- **Tell Finance what a payment is (#98).** "That's wage tax" classifies it, and a fresh
  check takes it out of the missing list.
- **Money back from the tax authority is nice to have (#97).** The check judges, from the
  payment itself, when money is coming back from a tax authority, and lists its document as
  nice to have instead of a missing credit note. If the payment's details change later, it
  is judged again. A rule you set for the payer still wins.

## 0.11.17

Same store schema (17) and Casa floor (v0.344.67).

- **Cards share one light house style (#99).** Every card and list opens with a bold title
  that says what it is or what happened ("3 documents you sent", "Q3 checked · 6 payments").
  A question stands on its own line right above the list it asks about ("Confirm these
  matches?"). Items are one tight line each, with blank lines only between groups. The line
  explaining the buttons is gone: the buttons say it. Documents are named by issuer, date
  and amount, not by provider ids, and a section with nothing useful in it is left out.
- **Close sits beside a card's buttons, never alone (#93).** A card you can act on also
  offers Close, so you can dismiss it without acting. A card with nothing to act on, such as
  a list of missing invoices with nothing to confirm, now comes as a plain message with no
  buttons.
- **Get package only when you ask for the package (#94).** A check's end card and its
  completion notice no longer offer it. A quarter's status card offers it when your words
  make clear you want the package, for instance "is Q3 ready for the accountant?".
  "Send the Q3 package" works as before.
- **Each proposed match is stated once (#96).** A document you sent that needs your yes or
  no appears only in the numbered list that Review and Confirm all refer to.

## 0.11.16

Same store schema (17) and Casa floor (v0.344.67).

- **"Call Informatique by the name on its invoice" renames it, also when its cards already
  show that name (#92).** A vendor you have not named only borrows the name on its invoice,
  so it could change with a later invoice; Finance took the shown name as done and changed
  nothing. Now the rename happens and keeps the name.

## 0.11.15

Same store schema (17) and Casa floor (v0.344.67).

- **A rename shows its own outcome (#89).** Renaming one vendor now posts "Aws Emea is now
  called Amazon Web Services EMEA SARL." with the vendor's card, straight from the rename;
  Finance adds nothing. "Use the invoice names for all my vendors" renames them in one go and
  posts one short message: how many were renamed, which keep their bank names because another
  vendor has the invoice name, which keep the name you gave them, and which have no invoice
  yet.
- **"Call Informatique by the name on its invoice" renames it (#89).** Part of a name or a
  bank text is enough; when the words fit several vendors, Finance says which ones instead of
  guessing.
- **A customer is never renamed after your own company.** The name on a sales invoice is its
  recipient, not you; a payslip or a bank statement gives no vendor name.

## 0.11.14

Same store schema (17) and Casa floor (v0.344.67).

- **"Call Informatique by the name on its invoice" works (#86).** Finance looked for a vendor
  named exactly "Informatique", found none and gave up, although the vendor's invoice name was
  known. Finance now always starts from the full list of vendors, so part of a name or a bank
  text is enough, and it no longer repeats what an earlier conversation concluded.
- **Naming replies are short (#87).** Renaming one vendor gets one line ("Aws Emea is now called
  Amazon Web Services EMEA SARL.") and then the vendor's card. "Use the invoice names for all my
  vendors" gets one short message: how many were renamed, which keep their bank names and why,
  and which have no invoice yet.

## 0.11.13

Store schema 17 (a vendor records that you named it). Same Casa floor (v0.344.67).

- **You can rename any vendor (#84).** Tell Finance "call LINKEDIN 'LinkedIn'", "call Aws Emea
  by the name on its invoice" or "use the invoice names for all my vendors". A vendor Finance
  already knows is renamed in place: it keeps its search hints and your rules, and the bank's
  text still finds it. The name you give wins over an invoice's issuer, also when it differs
  only in capitals. A vendor with no matched invoice is named as such; Finance does not guess.
  A card posted before a rename answers once with the card again, under its new name.
- **Finance sees every vendor at once (#84),** with its current name and the issuer printed on
  its matched invoice, so "all my vendors" covers all of them in one go.
- **"Show me the Coolblue payment" and "anything to check?" get the card (#83),** not a
  description in Finance's own words.

## 0.11.12

Same store schema (16) and Casa floor (v0.344.67).

- **A payment's card says its invoice once (#79).** A matched payment's card read "Invoice … is
  filed with it." and then "Matched to invoice … (15 Sep)."; a suggested one read "Paired
  with …, not confirmed." and then "Suggested: …". Now only the second line stays.
- **"To check" no longer opens with "Not checked yet." (#79).** That line was one quarter's
  bank coverage; the list spans every quarter, so it is gone from there.
- **The last answer of a walk of cards is just its receipt (#80).** Before, the quarter's
  status card followed it unasked. Ask "how's Q3?" to see it.
- **Finance has a one-line description of each view (#80),** so two wordings of one question
  ("anything to check?", "what's to check?") should get the same view.

## 0.11.11

Same store schema (16) and Casa floor (v0.344.67).

- **A document whose date is corrected to another period lets go of its payment (#73).**
  Before, an invoice the check had matched or proposed while its reading said one date (say
  15 Apr) stayed on that payment after a later reading corrected the date to another period
  (15 Sep), and the payment it really belongs to could not take it. Now the check releases
  its own pairing as soon as the document's date no longer fits the payment's period, and
  the next check matches the document where it belongs.
  A pairing you confirmed always stays. A pairing across periods left over from before
  0.11.8 is released the same way.

## 0.11.10

Same store schema (16) and Casa floor (v0.344.67).

- **A handed-over invoice the check is sure of is matched for you (operator ruling).** Before,
  every handed-over invoice waited for your confirmation. Now the check reads it, looks at every
  payment it could fit, with the nearest date first, and matches it when it is sure. It still
  proposes it when it isn't sure, for example with several monthly charges of the same amount.
  The card says so plainly: "matched automatically to the 13 Jul EUR 131.17 payment (OpenAI)".
  A proposal reads "proposed for … — confirm?". To undo an automatic match, open that payment
  ("show me the OpenAI payment") and tap [Wrong].
- **A reissued invoice is recognised by its recipient (#77).** The check now records the
  "Bill to" name when it reads an invoice, so a reissue addressed to the company is no longer
  called "already filed": it reaches Keep current / Use new, which shows the recipient change.
- The "To check" list no longer names a quarter: it holds every quarter's matches to confirm
  (#77).

## 0.11.9

A long list arrives in full. Same store schema (16); **Casa v0.344.67 or newer is required**
(plain pages before a card, #1377; the Close button, #1375). On a Casa older than v0.344.64
a list's card is refused; on v0.344.64–66 a long list shows only its action card.

- **A long list is sent whole, with its actions on a card of their own (#66).** A list that
  does not fit one message (the payments still missing an invoice, the matches to confirm,
  one payment's possible documents) now comes as plain messages in a row, up to six, with
  no [More] button to tap. A separate card follows them, saying how many items are listed
  above. Its buttons act on the whole list: [All good] confirms every match on every page,
  and if any of them changed since it was shown, nothing is applied. A list that fits one
  message is still one card with its buttons. A list longer than six messages ends its card
  with 'say "more"' for the rest.
- **The next-step buttons say what they show.** "What's missing" and "Anything to check?"
  are now [Show missing invoices (N)] and [Show matches to confirm (N)], with the number of
  items each list holds. Each appears only when its list is not empty.
- **[Close] on every list card.** It removes the card's buttons when you are done with it,
  and changes nothing else.

## 0.11.8

Requires **Casa v0.344.64 or newer** (#1375, Casa's Close button). Same store schema (16).

- **A handed-over invoice goes to the payment it belongs to (#72).** For a charge that repeats
  every month at the same amount, a September invoice could be proposed for the April payment,
  which then blocked the September one. An invoice is now only offered to a payment whose period
  it can belong to (from two months before that payment's quarter to a month after it), and the
  check weighs its reference, billing period and date among the payments it could fit.
- **A payment still pending at the bank is named** ("not matched yet — a payment of EUR 0.46 to
  modal.com on 26 Sep is still pending at the bank") instead of "no payment of EUR 0.46".
- **Every hand-off result is posted.** A card with nothing to tap (an invoice already filed, one
  that could not be read) carries a Close button; before, Casa refused it and you saw only a
  generic line.
- Each proposal line names the payment's date and amount ("proposed for LINKEDIN · 15 Sep ·
  EUR 57.84"); the Keep current / Use new card says what differs ("Differs: recipient …"); the
  assistant's reply to a hand-off is one short line.

## 0.11.7

Store schema 16 (a run's handed-over documents; migrated on start). Same Casa floor (v0.344.39).

- **A handed-over invoice is read and matched to the payments it could fit (#67).** When you
  send invoices to the assistant, the check reads each one first (also when it was filed
  without a reading), then works only the payments those invoices could fit, and proposes
  each match for you to confirm. It no longer syncs the bank, searches your mail or works
  every open payment for a hand-off; ask for a check when you want that. Send several
  invoices at once and they are handled in one go ("Filed 3 documents. Checking them…").
- **The result names each invoice.** One line per invoice, with its issuer, number, date and
  amount as read, and what happened to it: matched to which payment, proposed for one, already
  filed as #N (the same invoice, read the same in every field), could not be read, or no payment
  of that amount in the books yet. [Get package] appears only for the quarter of a payment an
  invoice now backs, never for an unrelated quarter.
- A reissued invoice (same number, something read differently, such as the recipient) is not
  treated as a copy: on a payment that already has the earlier one, you are asked whether to
  keep the current one or use the new one.

## 0.11.6

Same store schema and Casa floor (v0.344.39; [See PDF] keeps its card on v0.344.58 or newer).

- **A running check no longer sends documents to the chat (#68).** While the accounting check
  ran, it could post a filed invoice into your chat, unrelated to anything you asked. While a
  check is running, the plugin now refuses to send a document unless you asked for it: the
  check reads documents without posting them. Your [See PDF] button still sends the file at
  any time. A [See PDF] button on a card posted before 0.11.6, tapped while a check is
  running, answers that a check is running; tap it again when the check has finished.
  "Show me that invoice" at the desk works the same way.

## 0.11.5

Same store schema and Casa floor (v0.344.39).

- **A vendor card's legend shows the vendor's name as written (#60).** When a vendor's bank
  text holds a character the chat reads as formatting, such as `*` or `_` ("PAYPAL *ACME"),
  the legend's "Never for …" entry could show part of the name in italics or with characters
  missing. It now reads exactly as the [Never for …] button does.

## 0.11.4

See the proposed document. Same store schema and Casa floor (v0.344.39); [See PDF] keeps its
card only on Casa v0.344.58 or newer (#1362).

- **[See PDF] on a to-confirm card (#56).** A card that proposes one document has a
  [See PDF] button ([See document] for an image). Tapping it sends the filed document into the
  chat, and the card stays as it was: [Confirm], [Wrong] and [Leave for now] still work, so
  you can look and then answer. At the desk, "show me that invoice" sends it the same way.
  Nothing is recorded: showing a document is not a delivery. On a Casa older than v0.344.58
  the tap still sends the document, but the card's buttons close as for [Get package].
- **Vendors named like a website (#62).** A reply that names a vendor as the card prints it,
  such as "no invoices ever for Twilio.com", is read with the name whole: a period inside a
  vendor name the plugin knows (an open payment's name, or a name in its knowledge base) no
  longer ends the sentence. Every other period splits sentences as before.
- **Credit-note card legend (#63).** On a vendor card whose missing documents are credit
  notes, the legend's first entry now names the button that is there ("No document needed:
  these need none").

## 0.11.3

Cards that read right, and the invoice links. Same store schema and Casa floor (v0.344.39);
the in-place updates need Casa v0.344.48 (#1339) and fall back to a receipt and a new card
on an older Casa.

- **[Invoice links] (#57).** The quarter's card ([Review] [Confirm all] [Get package]) has
  one more button while the quarter has missing invoices. Tapping it turns the same card
  into the card plus "Where to download the missing invoices:", one line per vendor with
  its download link and link note, or "no link known". The links are the ones the check
  learns for a vendor whose invoices sit behind a login. The card's other buttons stay.
- **One card, updated in place (Casa #1339).** [Next page], [Apply to all quarters],
  [Only this quarter] and [Invoice links] change only what the card shows, so the tapped
  card becomes the new one: no receipt, no second card. Taps that decide something keep
  their receipt and post the next card as before.
- **Readable vendor names (#59).** A card names a vendor by the name given in the
  knowledge base, else by the issuer printed on a document matched to another of its
  payments, else by the bank's text ("Google Cloud EMEA" rather than
  "Google*workspace Lesin"). A payment's own suggested or matched document never names
  it. Rules ([Never for …]) still apply to the bank's payee.
- **Plainer vendor cards (#59).** The download link reads "Where to download: …". A vendor
  whose missing documents are credit notes reads "missing credit notes", with [No document
  needed for these] and its legend and receipt naming credit notes. "Also in other
  quarters: 7 payments (Q2)" adds "· Never for X would also change them".
- **No narration after a card (#59).** The desk's reply after a reading or a view is
  `<silent/>` only, with no sentence saying that something was posted.
- **Not in this release: the proposed document's PDF (#56).** A file button's tap ends
  the card it sits on (Casa settles the whole keyboard and posts no next card after a
  file), so [See PDF] would leave the to-confirm card without [Confirm]. It waits on a
  Casa change.

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
- **One purchase is one line (#52).** The proposal line counts purchases (issuer +
  number, #48's unit), so an invoice and a receipt with the same number read as one
  document, and the Review card shows one line and one button per purchase. When one email
  carries an invoice and its own receipt, the job uses the invoice and never offers the
  receipt; when unsure which is the invoice it proposes, so the operator confirms (a skill
  judgment, operator ruling — no server rule; the #48 floor is 0.11.1's). A stored
  alternative set aside since it was stored is no longer offered.
- **A quarter's vendor card lists only that quarter.** The vendor's payments of other
  quarters are a frozen count on the card ("Also missing in other quarters: 2 (Q2, Q4)"),
  bound like a line; [Never] binds the shown and the counted. One switch, ⟦Apply to all
  quarters⟧, makes the card's next answer ([No invoice needed for these], [Leave missing])
  cover the other quarters' missing ones too: a short receipt ("All quarters on.") and the
  same card switched on (⟦✓ All quarters⟧, "· answers will cover them"). An answer refuses
  when a counted payment moved.
- **Reading fixes from PLAY's live check.**
  - One number per thing: the legend and the "Reviewing …" receipt print only the counts
    line's numbers.
  - A vendor card lists its missing payments, without the bank's payee text on each line,
    and puts the others [Never] would change on one line. It also has ⟦Leave for now⟧, and
    its receipts name the payments.
  - Words: "documents fit", the document's own kind in the Confirm legend, "Reject the
    suggested document for …" on a proposal, "waiting on the bank".
  - The switch-back button reads ⟦Only this quarter⟧.
  - The pick legend reads "A document button: use that document".
- **Whole lines (#54).** No fixed-width hard breaks: `views._wrap` and `WIDTH` are gone, and
  every line is one item that the client wraps.
- **Plain words and buttons.** Zero counts are not shown. Buttons carry no counts
  (⟦Review⟧ ⟦Confirm all⟧ ⟦Get package⟧), and every card that carries buttons (and a
  reading, and the account choice) ends with one line saying what each button shown does:
  "Review: go through the 5 to confirm and the 12 missing, one at a time". A run that took
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
