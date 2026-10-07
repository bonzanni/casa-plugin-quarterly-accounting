# Quarterly accounting: what the operator says and sees (APPROVED 2026-10-08, 0.11.2)

Approved by the operator through BRAIN on 2026-10-08, with their changes folded in (incl. the
button-legend correction). This page is the reviewers' spec, alongside correctness.

Ellen reads intent from whatever the operator says, in any wording or language. The
sentences below are **illustrations, never triggers**. There are two intents about a
quarter:

- **Where does Q3 stand?** Answer with its state. Nothing runs.
- **Get Q3 done.** Make it happen: run it, or continue it. There is no question, even when
  the books' start has to move earlier.

General rules:
- Every card's first line ends with the time it was composed, in seconds (the operator's
  local time). That time is how a swipe-reply knows which card it answers (#53).
- Counts that are zero are not shown.
- Buttons keep short labels, with counts in parentheses: ⟦Review (2)⟧ ⟦Confirm all (1)⟧
  ⟦Get package⟧. A card that carries buttons ends with ONE short plain line saying what
  each button shown does. Only the buttons actually shown are described. `⟦…⟧` is a
  button.
- One answer per ask.

## 1. Where a quarter stands

**1a. Inside the books, with open items.** The operator says something like "how's Q3?",
"check Q3" or "where are we with Q3". They get one card, and nothing runs:
```
Q3 · 3 payments · 21:04:37
1 matched · 1 to confirm · 1 missing
To confirm:
1. Zapier · 1 Sep · EUR 19.58 ↔ invoice ZAP-114 · EUR 19.58
Q4 so far: 1 to confirm
Review: see each open item and decide · Confirm all: accept the proposed invoices · Get package: the Q3 zip for your accountant
⟦Review (2)⟧ ⟦Confirm all (1)⟧ ⟦Get package⟧
```
- The card lists only Q3's items. Another quarter with open items gets one line.
- The package button is there when the quarter has payments in the books.

**1b. Everything answered.**
```
Q3 · 14 payments · all accounted for · 21:04:37
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
2. When the run ends, one card: the 1a card, headed `Q3 checked · 3 payments · 21:09:12`.
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

⟦Review⟧ walks the cards. A vendor with no invoice reads:
```
Card 2 of 2 · missing invoices · Twilio · 21:10:03
Twilio · EUR 20.00 · 14 Aug
No invoice needed: these need none · Never for Twilio: Twilio never sends one · Leave missing: stop looking, keep them missing
⟦No invoice needed for these⟧ ⟦Never for Twilio⟧ ⟦Leave missing⟧
```

When the operator sends the PDF in the chat: `Filed. Checking it against the payments —
I'll post what I find.`, then one card with what changed.

**A proposal with an invoice and its own receipt is ONE line and one button** (#52). The
receipt is the same purchase; it is never offered as a second choice.

## 4. Getting the package

The operator says something like "send me the Q3 package", or taps ⟦Get package⟧.
The zip arrives as a file, captioned with the quarter. Nothing else is posted.

## 5. A correction

The operator says something like "the Zapier one is wrong" (swiped on the card, or not):
```
I read this as:
· Remove the match for Zapier · EUR 19.58 · 1 Sep.
Apply: make this change · Cancel: change nothing
⟦Apply⟧ ⟦Cancel⟧
```
⟦Apply⟧ → `Removed the match for Zapier · EUR 19.58 · 1 Sep.`

Every verb shown to the operator is a plain one. Internal verbs (pair, unpair, set aside)
are never shown.

---
**Residual (#53, the operator's pick).** The same card shown twice within one second
cannot be answered by a swipe until it is shown again. The reply is refused visibly, and
nothing wrong is ever applied.
