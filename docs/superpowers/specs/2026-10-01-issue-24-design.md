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
pending.

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
> tool call per message, and a turn that dies anyway loses at most the item in flight.**

Two parts: the server cuts the chunk short by a count of calls it can see; the skill works
the chunk item by item and records each item when it is done.

### 1. The server counts the chunk turn's calls

Constants (`work.py`):

    ELLEN_TURNS   = 80   # Casa's assistant max_turns (ha-casa-app#1137); one message per call
    TURN_TAIL     = 4    # record_step(judge, start), delegate_to_agent,
                         # record_step(judge, delegated), the closing message
    HEAD          = 5    # continue_pass, a `speak` (send_message + mark_rendering_delivered),
                         # the Gmail probe (search_emails + record_probe)
    HEAD_FIRST    = 2    # the first chunk only: the self-addressed search, list_inbound_files
    FILINGS_FIRST = 8    # the first chunk only: at most this many files from those two (D1)
    ITEM_RESERVE  = 11   # the most one item may cost: 4 queries, 2 attachments × (list,
                         # download, ingest), its record_search
    GMAIL_CHUNK   = 10   # unchanged: the most items a chunk hands out

At every chunk hand-out (`steps._hand_chunk`, package round and check alike) the pass's
first step row's carry gets

    turn = {"budget": ELLEN_TURNS − HEAD − TURN_TAIL − (HEAD_FIRST if first chunk), "spent": 0}

replacing any earlier one (a re-claimed continuation, the next chunk: each is a new turn).
"First chunk" = the continuation of the pass's first step (`sweep` / `snapshot`).

The counter is armed by the hand-out: `turn` also keeps the token the hand-out minted.
Any `record_step(action="start")` of the pass disarms it (removes `turn`). A call is charged
only while `turn` is armed and the call's token is the armed one. That is the chunk turn:
from the hand-out to the judge step's start. The judge's own calls, a later claim's token,
a stale token, and a step that runs past its expiry are therefore never charged.

In the chunk turn:

- `record_search` charges `1 + len(queries)` (the queries as passed, before de-duplication:
  what Ellen says she ran);
- `ingest_document` charges 3 (a `list_attachments`, a `download_attachment`, the ingest —
  an overcount when the message id was already listed). Inbox filings at the first chunk
  are charged the same way (`share_inbound_file` + ingest: 2, charged 3). They carry the
  continuation's `pass_token` (D1, Astra S2). An operator's hand-over outside a pass stays
  tokenless and is never charged.

The charge is written in the call's own transaction. The answers of both calls then carry

    chunk = {"room": spent + ITEM_RESERVE <= budget, "calls_left": budget − spent}

(D1: on `ingest_document` too, so a filing is checked like an item. `ITEM_RESERVE` ≥ a
filing's cost, so one rule covers both: start another filing or item only while the last
answer said `room: true`.) `room` is read only between units (D2, Astra S1). An item once
started is finished, `record_search` included, whatever its own ingests answer: the
`room: true` that admitted it reserved `ITEM_RESERVE` for it.

Nothing is charged, and `chunk` is absent, while a step runs (the judge's own
`record_search` calls) or outside a pass.

At 80, a later chunk has 71 calls for items. A first chunk has 69 calls for filings and
items: at most 8 filings (24 charged) leave 45 or more, which is at least four heavy items.
A first chunk therefore always records items, and the loop's count falls. Ten items at the
measured ~4.5 calls each fit (~45). A run of heavy items is cut at the first answer past 58
on a first chunk, or 60 on a later one.

### 2. The skill: item by item, each recorded at once

Gmail round (the check's step 4 and the package round's `gmail-round`):

- The first chunk (the `sweep` / `snapshot` continuation): the probe, then the
  self-addressed search and the Telegram inbox sweep, BEFORE the items (today: after).
  Together they file at most `FILINGS_FIRST` = 8 files, newest first, each with the
  continuation's `pass_token` (D1). A file is filed once per pass: no second
  `share_inbound_file` or download of the same file.
- The cap holds for every filing sweep in a pass turn, not only in a Gmail chunk (D2, Terra
  S1). That includes the inbox sweep the skill prescribes before `end_pass` when the sweep
  stopped or failed: that turn has no armed counter, and its fixed cost (`continue_pass`,
  `list_inbound_files`, 8 × 2, `end_pass`, a `speak` 2, a status card 2–3, the closing
  message) is under 30.
- Then the chunk **one item at a time**: its queries, its downloads and ingests, then its
  `record_search` — before the next item. Several calls for the same item may go in one
  message; never start an item before the previous one is recorded.
- At most 4 queries and 2 attachments per item in one chunk. An item not found within that:
  `record_search(…, queries=[…], incomplete=true)` — `exhausted=true` only when the ideas
  ran out.
- Before starting a filing or an item, read the last `chunk` answer (from a `record_search`
  or an `ingest_document`). If it says `room: false`, start nothing more and go to the
  judge step. An item already started is finished first, its `record_search` included
  (D2). Items not reached need no record: they stay in the work and come in the next
  chunk. (The old "an item you never reached: `record_search(incomplete=true)` with no
  queries" is removed from the Gmail round: it cost a call per unreached item and changed
  nothing — an unrecorded item is not searched since the origin, `work.searched_since`.)

### The loop is unchanged

`_another_chunk` still requires `0 < left < handed`. A cut chunk recorded at least one item
(the budget always admits the first: `ITEM_RESERVE` ≤ every budget at 80), so `left` falls
and the next chunk is handed out; the report (`check_report`, `owed`) is untouched.

## Residuals (stated)

- Filings past the 8 (D1). In 0.6.0 such a turn died at the limit. Now the turn ends at its
  delegation, and the files left over wait. A Telegram document is filed in the operator's
  next text turn, which files the inbox first ("Ellen: a document the operator hands
  over", step 1). A self-addressed mail waits for the next pass's self-addressed search.
  The check's report is unchanged: it never counted filings.
- Calls the server cannot see are estimated: Ellen's off-script calls (a `set_watermark`,
  a status card in an operator's turn), more than one `speak`, an item past the caps. The
  margin at 80 (~25 calls over ten typical items) absorbs a few.
- While Casa's limit is below `ELLEN_TURNS` (before #1137 ships), a full chunk can still die
  at the limit; recording item by item limits the loss to the item in flight, and the next
  `continue_pass` (the operator's next message, or a stale reclaim) resumes it.
- Parallel calls make the count an overestimate: a chunk is cut earlier than it had to be,
  never later.
- A turn that dies is still silent until ha-casa-app#1121.

## Testing

- `_hand_chunk` sets the budget (first vs later chunk) and resets `spent` at every hand-out,
  package round and check.
- `record_search` during a chunk turn charges `1 + len(queries)` and answers `chunk`;
  `ingest_document` charges 3; a judge step's `record_search` charges nothing and carries no
  `chunk`.
- `room` turns false exactly when `spent + ITEM_RESERVE > budget` (boundary both sides), on
  `record_search` and on `ingest_document`.
- Astra's D2 trace: 8 filings, then items of 11 calls each. The item whose ingest answers
  `room: false` is finished and recorded. The report equals 0.6.0's, and the turn stays
  ≤ `ELLEN_TURNS` messages.
- A first chunk with 8 filings and 10 items of 11 calls each cuts the items but records at
  least four, and its message count stays ≤ `ELLEN_TURNS`.
- A simulated serial chunk turn (one message per call, items of 4–11 calls, heavy ones
  included) never exceeds `ELLEN_TURNS` messages from `continue_pass` to the closing message,
  with the skill's head and tail counted; and records ≥ 1 item per chunk.
- A check whose chunk is cut runs to `end-pass` with every item searched (the loop still
  falls); the package round likewise.
- Skill text: item-by-item order, record before the next item, the caps, `room`,
  the self-mail search and inbox sweep before the items, no `incomplete` record for an
  unreached item in the Gmail round.
- Mutation-check each new guard (in a worktree).
