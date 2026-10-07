# Quarterly accounting: what the operator says and sees (draft for approval, 0.11.2)

Ellen reads intent from whatever the operator says, in any wording or language. The
sentences below are **illustrations, not triggers**. There are two intents about a
quarter:

- **"Where does Q3 stand?"**: answer with its state. Nothing runs.
- **"Get Q3 done"**: make it happen (run it, or continue it). Confirm the period only
  when the books' start has to move earlier.

Every card's first line ends with the time it was composed, in seconds (local time). That
time is how a swipe-reply knows which card it answers (#53). Below, `⟦…⟧` is a button.
Text marked *new* is proposed; everything else is today's output, rendered from the code.

## 1. Where a quarter stands

**1a. Inside the books, with open items.** The operator says something like "how's Q3?",
"check Q3" or "where are we with Q3".
*new*: one card, and no run.
```
Q3 · 2 payments · 21:04:37
1 matched · 0 need no invoice · 1 to confirm · 1 missing
To confirm:
1. Zapier · 1 Sep · EUR 19.58 ↔ invoice ZAP-114 · EUR 19.58
Q4 so far: 1 to confirm
⟦Review 2⟧ ⟦Confirm all 1⟧ ⟦Get package⟧
```
- The card lists only Q3's items. Another quarter with open items gets one line.
- ⟦Get package⟧ appears only when the quarter has payments in the books.

**1b. Q3 is complete.**
`Q3 complete · 14 of 14 accounted for · 21:04:37` ⟦Get package⟧

**1c. Before the books' start** (the books start 1 Oct).
*new*: one line, no card, and nothing runs.
`Q3 2026 isn't in the books yet: they start 1 Oct 2026. Ask me to do Q3 and I'll start
the books from 1 Jul.`

## 2. Getting a quarter done

**2a. Inside the books.** The operator says something like "do the Q3 accounting" or
"run Q3".
1. Right away: `Checking the bank and your email — I'll post the result here.`
2. When the run ends, one card only. It is the 1a card, headed
   `Q3 checked · 2 payments · 21:09:12`. No separate relayed text repeats it.

If a run is already going, the operator sees its line instead
(`The accounting job was busy just now…`).

**2b. Before the books' start.** The operator says something like "do the whole Q3
accounting".
1. *new*: one confirmation. Casa's own approval prompt (the measured option, see the
   note) reads:
   ```
   🔐 Approval needed
   Finance (finance) wants to: start the books from 1 Jul 2026 and check Q3 2026
   Exact action (binding): {"quarter":"2026-Q3"}
   Tool id: mcp__plugin_quarterly-accounting_…__start_quarter
   ```
   ⟦Approve⟧ ⟦Deny⟧
2. On ⟦Approve⟧, with no further word from the operator: `Checking the bank and your
   email — I'll post the result here.`, then the 2a end card.
3. On ⟦Deny⟧: nothing changes.

## 3. A missing invoice

⟦Review⟧ walks the cards. A vendor with no invoice reads:
```
Card 2 of 2 · missing invoices · Twilio · 21:10:03
Twilio · EUR 20.00 · 14 Aug
```
⟦No invoice needed for these⟧ ⟦Never for Twilio⟧ ⟦Leave missing⟧

When the operator sends the PDF in the chat: `Filed. Checking it against the payments —
I'll post what I find.`, then one card with what changed.

## 4. Getting the package

The operator says something like "send me the Q3 package", or taps ⟦Get package⟧. The zip
arrives as a file, captioned with the quarter, and nothing else is posted.

## 5. A correction

The operator says something like "the Zapier one is wrong" (swiped on the card, or not):
```
I read this as:
· Unpair Zapier · EUR 19.58 · 1 Sep.
```
⟦Apply⟧ ⟦Cancel⟧. ⟦Apply⟧ → `Unpaired Zapier · EUR 19.58 · 1 Sep.`

The same holds for a request that implies an earlier start, whatever its words: it reads
as `Start from Q2 2026; …` with ⟦Apply⟧ ⟦Cancel⟧. Only "get Q3 done" (2b) folds the start
into the run.

---
**Notes for the approver.**
- **Why Casa's prompt in 2b.** Ellen's `ask_user` refuses on a Finance turn: Casa
  classifies the turn as delegated, measured at v0.344.46. Even when it is allowed, the
  answer arrives as a new turn, not inside the same one. A plugin ⟦Apply⟧ card cannot
  start the run: Casa runs a tap as one pinned tool call. Casa's approval for a protected
  tool does continue the same desk after ⟦Approve⟧, which gives "confirm, then it runs".
  The cost is Casa's fixed prompt lines ("Exact action", "Tool id").
- **The visible time** (#53, the operator's pick). The same card shown twice within one
  second can't be answered by a swipe until it is shown again. The reply is refused
  visibly, and nothing wrong is ever applied.
- **Open question (#52's shape on the Review card).** An invoice and its own receipt still
  appear as two candidates there, with two identical buttons (`CUWVSRB8-0007 · 1 Sep`
  twice). Should that card also show one line per purchase?
