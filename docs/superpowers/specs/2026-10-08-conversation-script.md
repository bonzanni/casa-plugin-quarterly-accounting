# Quarterly accounting: what the operator says and sees (APPROVED 2026-10-08, 0.11.2)

Approved by the operator through BRAIN on 2026-10-08, with their changes folded in (incl. the
button-legend correction, the no-count-on-buttons rule, the invoice-over-receipt
judgment, the quarter-only vendor card and its one switch). This page is the reviewers' spec, alongside correctness.

Ellen reads intent from whatever the operator says, in any wording or language. The
sentences below are **illustrations, never triggers**. There are two intents about a
quarter:

- **Where does Q3 stand?** Answer with its state. Nothing runs.
- **Get Q3 done.** Make it happen: run it, or continue it. There is no question, even when
  the books' start has to move earlier.

General rules:
- Every card's first line ends with the day and time it was composed, in seconds (the
  operator's local time), e.g. `8 Oct 21:04:37`. That time is how a swipe-reply knows which card it answers (#53).
- Counts that are zero are not shown.
- Buttons keep short labels: ⟦Review⟧ ⟦Confirm all⟧ ⟦Get package⟧. A count on a button
  must be a number the card itself shows, so buttons carry no counts; the legend line
  says what they cover. A card that carries buttons ends with ONE short plain line saying what
  each button shown does. Only the buttons actually shown are described. `⟦…⟧` is a
  button.
- One answer per ask.

## 1. Where a quarter stands

**1a. Inside the books, with open items.** The operator says something like "how's Q3?",
"check Q3" or "where are we with Q3". They get one card, and nothing runs:
```
Q3 · 3 payments · 8 Oct 21:04:37
1 matched · 1 to confirm · 1 missing
To confirm:
1. Zapier · 1 Sep · EUR 19.58 ↔ invoice ZAP-114 · EUR 19.58
Q4 so far: 1 to confirm
Review: go through the 1 to confirm and the missing invoices, one at a time · Confirm all: accept the suggested documents listed above · Get package: the Q3 zip for your accountant
⟦Review⟧ ⟦Confirm all⟧ ⟦Get package⟧
```
- The card lists only Q3's items. Another quarter with open items gets one line.
- The package button is there when the quarter has payments in the books.

**1b. Everything answered.**
```
Q3 · 14 payments · all accounted for · 8 Oct 21:04:37
Get package: the Q3 zip for your accountant
⟦Get package⟧
```

**1c. Before the books' start** (the books start 1 Oct). One line, no card, and nothing
runs:
`Q3 2026 isn't in the books yet: they start 1 Oct 2026. Ask me to do Q3 and I'll start
the books from 1 Jul.`

## 2. Getting a quarter done

**2a. Inside the books.** The operator says something like "do the Q3 accounting" or
"run Q3".
1. Right away: `Checking the bank and your email — I'll post the result here.`
2. When the run ends, one card: the 1a card, headed `Q3 checked · 3 payments · 8 Oct 21:09:12`.
   No separate text repeats it.

If a run is already going, the operator sees its line instead.

**2b. Before the books' start.** The operator says something like "do the whole Q3
accounting". Nothing is asked:
1. Right away: `Starting the books from 1 Jul 2026 and checking Q3 — I'll post the result
   here.`
2. Then the 2a end card.

A request that only implies an earlier start ("include Q2 as well") is the same intent
for that quarter: Q2 gets done.

## 3. A missing invoice

⟦Review⟧ walks the cards. A vendor's card lists only THAT quarter's payments (operator
ruling 2026-10-08). A vendor with no invoice reads:
```
Card 2 of 2 · missing invoices · Twilio · 8 Oct 21:10:03
EUR 20.00 · 14 Aug
No invoice needed: these need none · Never for Twilio: Twilio never sends one · Leave missing: stop looking, keep them missing · Leave for now: decide later
⟦No invoice needed for these⟧ ⟦Never for Twilio⟧ ⟦Leave missing⟧ ⟦Leave for now⟧
```
When the vendor also has missing payments in OTHER quarters, the card says how many and adds
one switch, ⟦Apply to all quarters⟧ (operator ruling 2026-10-08: one toggle):
```
Card 2 of 2 · missing invoices · Twilio · 8 Oct 21:10:03
EUR 20.00 · 14 Aug
Also missing in other quarters: 2 (Q2, Q4)
No invoice needed: these need none · Never for Twilio: Twilio never sends one, in any quarter · Leave missing: stop looking, keep them missing · Apply to all quarters: your next answer here also covers the 2 in other quarters · Leave for now: decide later
⟦No invoice needed for these⟧ ⟦Never for Twilio⟧ ⟦Leave missing⟧ ⟦Apply to all quarters⟧ ⟦Leave for now⟧
```
Tapping the switch, on screen (Casa v0.344.46, measured: a tap's answer is a receipt, and
the next card follows it, never in its place; Casa clears the tapped card's buttons and adds
"☑ <the button>"):
1. The old card loses its buttons and ends `☑ Apply to all quarters`.
2. One short line from Finance: `All quarters on.`
3. The same card again, switched on (the switch now reads ⟦Only this quarter⟧):
```
Card 2 of 2 · missing invoices · Twilio · 8 Oct 21:10:41
EUR 20.00 · 14 Aug
Also missing in other quarters: 2 (Q2, Q4) · answers will cover them
No invoice needed: these and the 2 in other quarters need none · … · Leave missing: these and the 2 in other quarters · Only this quarter: switch back · Leave for now: decide later
⟦No invoice needed for these⟧ ⟦Never for Twilio⟧ ⟦Leave missing⟧ ⟦Only this quarter⟧ ⟦Leave for now⟧
```
Switching back is the same sequence: `☑ Only this quarter`, then `This quarter only.`, then
the card switched off.

When the operator sends the PDF in the chat: `Filed. Checking it against the payments —
I'll post what I find.`, then one card with what changed.

**An invoice and its own receipt: the invoice is used** (#52, operator ruling 2026-10-08:
"is there an invoice? Use it. You're not really sure which is the invoice? Ask the
operator"). When one email carries both, the job decides with the invoice and never offers
the receipt. When it isn't sure which is the invoice, it doesn't guess: the payment comes
as a proposal to confirm, the ordinary Review card. A judgment, not a guarantee.

## 4. Getting the package

The operator says something like "send me the Q3 package", or taps ⟦Get package⟧.
The zip arrives as a file, captioned with the quarter. Nothing else is posted.

## 5. A correction

The operator says something like "the Zapier one is wrong" (swiped on the card, or not). On
a proposal (not confirmed yet):
```
I read this as:
· Reject the suggested document for Zapier · EUR 19.58 · 1 Sep.
Apply: make this change · Cancel: change nothing
⟦Apply⟧ ⟦Cancel⟧
```
⟦Apply⟧ → `Rejected the suggested document for Zapier · EUR 19.58 · 1 Sep.` (On a confirmed
match the words are "Remove the match for …".)

Every verb shown to the operator is a plain one. Internal verbs (pair, unpair, set aside)
are never shown.

**PLAY's reading rules (live check 2026-10-08).** One thing, one number: a card and its walk
never print two different numbers for the same thing (the legend and the "Reviewing …" line
print only the counts line's numbers). A vendor card's subject is its missing payments, the
bank's payee text is not repeated on each line, and the payments [Never] would also change
are one line ("Never for X would also change 6 more payments of this quarter."). Every card
has a way to move on without answering (⟦Leave for now⟧). Receipts name the payments they
acted on. Words say what a thing is: "documents fit", "this receipt is right", "pending at
the bank". No button label starts with a mark (Casa adds "☑ <label>").

---
**Residual (#53, the operator's pick).** The same card shown twice within one second
(the day is shown, so within a year) cannot be answered by a swipe until it is shown again. The reply is refused visibly, and
nothing wrong is ever applied.
