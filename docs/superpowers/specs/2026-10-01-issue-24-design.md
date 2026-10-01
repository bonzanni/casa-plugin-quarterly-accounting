# Issue #24 — a Gmail chunk fits Ellen's turn when she calls one tool at a time

Issue: bonzanni/casa-plugin-quarterly-accounting#24 (bug, severity:high). Plugin 0.6.0 →
0.6.1 (no schema change). Follows `2026-09-30-issues-21-22-design.md` (0.6.0) and
`2026-09-30-issues-17-18-19-design.md` (0.5.0), whose chunk mechanism this bounds.

Status: draft. Design round D1 (4895d30): Astra DNS, Terra DNS. Both raised the same S1:
the self-addressed search and the Telegram inbox sweep file an unbounded number of documents
before the first `record_search`, so the first budget check can come too late. Astra also
raised an S2: Telegram filings carried no `pass_token`, so they could not be charged. Both
are folded in below (marked D1). Design round D2 (039ea59): Astra DNS, Terra DNS, with
different findings. Astra S1: stopping at an ingest's `room: false` in the middle of an item
left that item's search unrecorded, and the judge then paired it (the report regressed
against 0.6.0). Terra S1: a sweep that stopped goes straight to `end-pass` with an uncapped
inbox sweep, and no counter is armed there. Both folded in (marked D2). Design round D3
(c2e819d): Terra SHIP; Astra DNS, two S1s. (a) Failed downloads and listings without an
ingest cost messages but were never charged: 101 messages under the caps. That is the
third round with one shape: the server infers Ellen's message count from the calls it can
see, and an unseen call escapes. Generalized (marked D3): the chunk turn is bounded by
construction, from the caps, and the counter is gone. (b) Items handed out but not reached
were paired by the next judge before any search, so the check's report said `not_searched`
where 0.6.0 said complete. Generalized (marked D3): the check's work is its owed searches,
not current triage. Design round D4 (e4b6ac3): Astra DNS, Terra DNS. Astra S1: a package
ask that reclaims a died chunk turn spends two prescribed calls before `continue_pass`
(`begin_pass`, its line), which makes 81 messages. Terra S1: searching an owed item that a
judgment paired can age it out, which 0.6.0 never did. Terra S2: Telegram files stay in the
inbox, so newest-first with a cap of 8 starves the ninth. Folded in (marked D4). Design
round D5 pending.

---

## What happens today (0.6.0)

0.5.0/0.6.0 rely on one property:

> **Every Ellen turn does a bounded amount of work, and ends at a delegation whose
> notification starts the next one.**

The "bounded amount" is a chunk of `GMAIL_CHUNK` = 10 items, sized for parallel calls
(~3 items per SDK turn). In the live check behind #24 (Casa 0.332.18, Opus, a fresh
session), 107 of 107 of Ellen's messages carried exactly one tool call, and every turn
that worked a full chunk reached Ellen's 20-turn limit before the judge delegation:

- the cost per item, called one at a time, was ~4.5 calls (search 1–4, `list_attachments`
  0–2, `download_attachment` and `ingest_document` per candidate, `record_search` 1);
- the skill orders the chunk breadth-first ("every item's first `search_emails`, … then every
  `record_search`"): in serial order that puts every `record_search` last, so a turn that
  dies leaves all its searches unrecorded (turns of 14 and 19 calls recorded nothing);
- a dead turn ends silently (ha-casa-app#1121), and nothing calls `continue_pass` until the
  operator writes again.

Casa is raising the assistant's limit (ha-casa-app#1137, ~80 suggested; the value is the
operator's ruling there, not yet made). **Operator, 2026-10-01: size the plugin for 80 now**
(not for today's 20). `ELLEN_TURNS` follows #1137's ruling when it ships. Until then a full
chunk can still outrun 20; that is accepted. The limit counts messages: in the #24
transcript 20 tool-call messages ran, then `max_turns_reached` at turnCount 21, so the
closing message needs a turn of its own (it is in `TURN_TAIL`). Confirmed from Casa's code
and CLI 2.1.273 by the ha-casa-app session: one turn is one model call, so parallel calls in
one message cost one turn. A plugin cannot see the turn count, and no tool result ends the
turn. The limit is read from `defaults/agents/assistant/runtime.yaml`.

## The property this adds

> **A chunk turn ends at its delegation within `ELLEN_TURNS` messages when Ellen makes one
> tool call per message and follows the caps, and a turn that dies anyway loses at most the
> item in flight.**

### 1. The chunk turn is bounded by construction (D3)

Every unit of work in a chunk turn has a capped cost in messages, so the chunk size alone
bounds the turn. The server needs no count of Ellen's calls (D1–D3 tried to count them,
and each round found a call it could not see).

Constants (`work.py`):

    ELLEN_TURNS     = 80  # Casa's assistant max_turns (ha-casa-app#1137): model calls,
                          # the closing message included
    TURN_HEAD       = 8   # continue_pass, one `speak` (send_message + mark_rendering_delivered),
                          # the Gmail probe (search_emails + record_probe), and the entry
                          # prefix (D4): a package ask's begin_pass and its line before
                          # continue_pass, and one spare
    TURN_TAIL       = 4   # record_step(judge, start), delegate_to_agent,
                          # record_step(judge, delegated), the closing message
    FILING_HEAD     = 2   # first chunk: the self-addressed search, list_inbound_files
    FILINGS_FIRST   = 8   # first chunk: at most 8 files ATTEMPTED from those two
    FILING_COST     = 3   # per attempt: list + download + ingest (Telegram: share + ingest)
    ITEM_COST       = 11  # per item: ≤ 4 queries, ≤ 2 attachments ATTEMPTED × (list,
                          # download, ingest), its record_search
    CHUNK_LATER     = (ELLEN_TURNS − TURN_HEAD − TURN_TAIL) // ITEM_COST            = 6
    CHUNK_FIRST     = (ELLEN_TURNS − TURN_HEAD − TURN_TAIL − FILING_HEAD
                       − FILINGS_FIRST × FILING_COST) // ITEM_COST                   = 3

`GMAIL_CHUNK` (10) is replaced by the two sizes: the continuation of the pass's first step
(`sweep`, `snapshot`) hands out at most `CHUNK_FIRST` items, and a judge continuation hands
out at most `CHUNK_LATER`. Check: 8 + 4 + 2 + 24 + 3 × 11 = 71, and 8 + 4 + 6 × 11 = 78,
both ≤ 80.

The turns that work a chunk, and what precedes `continue_pass` in each (D4):

| Turn | Before `continue_pass` |
|---|---|
| a delegation's notification (rule 5) | nothing |
| the cron (rule 1) | nothing |
| the operator's "go and check now" (rule 1) | nothing |
| a package ask (Packaging step 1: `begin_pass` first) | `begin_pass`, its `text` sent |
| any operator turn that reclaims a chunk whose turn died | whatever that turn did first (residual) |

The caps count attempts, not successes. A failed download, or a listing that files nothing,
uses up an attachment of the item's 2, or a file of the 8 (D3).

### 2. The check's work is its owed searches (D3)

In 0.6.0, `check_work` is cut from current triage. An item that was handed out but not yet
searched, and that a judge then pairs, leaves the work: it is never searched, and the
report (over `owed`) counts it `not_searched`. Smaller chunks make that common: an item in
chunk 2 waits through judge 1. So the work becomes

    check_work = fresh, non-portal triage items not searched since since_seq
               ∪ owed pids (merged lineages resolved), not ended, fresh, non-portal,
                 not searched since since_seq

handed out in pid order, as every chunk is. A paired owed item is searched like any
other. Its search may find the paired document again; ingest is idempotent, and
`record_search` records the search. The loop (`0 < left < handed`) and the report read the
same set: `left` falls only by searches, and every owed item is either searched or still in
the work. `owed` is unchanged: it grows at every hand-out and never shrinks. A not-fresh
item is owed but never handed out (as in 0.6.0).

An owed search of an item that needs no search now (it left triage: paired, exempt, or no
longer expecting a document) is recorded for the report (`searched_seq`, `facts_fp`,
its queries). It never moves the item's age-out count or its `search_state`, because 0.6.0
never searched such an item (D4, Terra S1). A `revive` is unaffected.

Package rounds keep `package_work` (triage-based). A round's item paired by an earlier
chunk's judge needs no search for the package: it has its document, and round_fate reads
the quarter's work, as in 0.6.0.

### 3. The skill: item by item, each recorded at once, within the caps

Gmail round (the check's step 4 and the package round's `gmail-round`):

- The probe first, as today.
- The first chunk only (the `sweep` / `snapshot` continuation): the self-addressed search
  and the Telegram inbox sweep, BEFORE the items. Together they attempt at most 8 files,
  newest first, each once, every `ingest_document` with the continuation's `pass_token`.
  What is left waits for the next pass, or the operator's next message.
- Each such filing passes its `source_ref`: the message id for self-addressed mail, the
  path `list_inbound_files` shows for a Telegram file (also in an operator's turn). The
  first chunk's continuation carries `filed_refs`, the `source_ref`s of the
  `manual-email` / `manual-telegram` documents filed in the last 8 days (Casa keeps an
  inbox file 7 days), newest first, at most 60. Ellen skips any file whose ref is listed:
  that costs no attempt. A ninth file is therefore filed by the next pass, not starved
  (D4, Terra S2).
- The same cap holds wherever the skill sweeps the inbox in a pass turn, including the
  `end-pass` turn of a sweep that stopped or failed (D2, Terra S1).
- Then the chunk **one item at a time**: its queries, its downloads and ingests, then its
  `record_search`, before the next item.
- Per item: at most 4 queries and at most 2 attachments attempted. An item not found
  within that: `record_search(…, queries=[…], incomplete=true)`. Set `exhausted=true` only
  when the ideas ran out.
- Every item handed out is worked: the chunk is sized for it. An unreached item (a turn
  that died) needs no record. It stays in the work, and the old "`record_search(incomplete=true)`
  with no queries" is removed from the Gmail round.

### The loop

`_another_chunk` still requires `0 < left < handed`, with `left` and `handed` over the new
`check_work` for a check and `package_work` for a package.

## Residuals (stated)

- Files past the 8 (D1). In 0.6.0 such a turn died at the limit. Now the turn ends at its
  delegation, and the files left wait: a Telegram document is filed in the operator's next
  text turn (which files the inbox first), and a self-addressed mail by the next pass's
  self-addressed search.
- More judges per check: about 1 + ⌈(n − 3) / 6⌉ for n items, against ⌈n / 10⌉ in 0.6.0.
  Each judge is a specialist delegation bounded by its 510 s wrap-up.
- An operator turn that does other work (files Telegram documents, answers a question)
  before its `continue_pass` reclaims a chunk whose turn died: that prefix is outside
  `TURN_HEAD`. This needs a died turn first. 0.6.0's chunk of 10 did not fit even
  without the prefix.
- More than 60 operator-supplied files in 8 days: the oldest refs are not listed, and
  filing one again costs an attempt (ingest is idempotent).
- Ellen's off-script calls (a `set_watermark`, a status card in an operator's turn) are
  outside the caps. A typical item costs ~4.5 messages against the 11 budgeted, which
  leaves a wide margin.
- While Casa's limit is below `ELLEN_TURNS` (before #1137 ships), a chunk can still die at
  the limit. Recording item by item limits the loss to the item in flight.
- A turn that dies is still silent until ha-casa-app#1121.

## Testing

- The arithmetic: `CHUNK_FIRST` and `CHUNK_LATER` derived from the constants; the worst-case
  first and later turns ≤ `ELLEN_TURNS`. A test asserts the sum from the constants.
- Hand-outs: a check's first chunk ≤ 3 and later ≤ 6, and a package round's the same;
  portals never handed out; 25 items run 3 + 6 + 6 + 6 + 4.
- An owed-only search (paired item) with no candidate leaves `passes_without_candidate`
  and `search_state` unchanged; the same search of a triage item advances them as before.
- `filed_refs` on a first chunk lists exactly the recent manual documents' refs, newest
  first, bounded; later chunks carry none.
- Astra's D3 (b): ten items, a judge pairs unreached items 5–7 between chunks. They come in
  the next chunk, are searched, and the report is `{10, 10, 0}`. 0.6.0's `check_work`
  fails this test.
- `left` falls only by searches; a paired-but-unsearched owed item keeps the loop going.
- An ended owed lineage (or a portal, or a not-fresh one) is never handed out; a merged one
  is handed out once, under its surviving pid.
- Skill text: item-by-item order, record before the next item, the caps (counting
  attempts), filing first with the token and at most 8, the cap in the end-pass inbox sweep,
  no `incomplete` record for an unreached item in the Gmail round.
- Package rounds: their suite is unchanged except for the chunk sizes.
- Mutation-check each new guard (in a worktree).
