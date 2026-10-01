# Issue #24 — a Gmail chunk fits Ellen's turn when she calls one tool at a time

Issue: bonzanni/casa-plugin-quarterly-accounting#24 (bug, severity:high). Plugin 0.6.0 →
0.6.1 (no schema change). Follows `2026-09-30-issues-21-22-design.md` (0.6.0) and
`2026-09-30-issues-17-18-19-design.md` (0.5.0), whose chunk mechanism this bounds.

Status: draft, design round D1 pending.

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
closing message needs a turn of its own (it is in `TURN_TAIL`).

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
    ITEM_RESERVE  = 11   # the most one item may cost: 4 queries, 2 attachments × (list,
                         # download, ingest), its record_search
    GMAIL_CHUNK   = 10   # unchanged: the most items a chunk hands out

At every chunk hand-out (`steps._hand_chunk`, package round and check alike) the pass's
first step row's carry gets

    turn = {"budget": ELLEN_TURNS − HEAD − TURN_TAIL − (HEAD_FIRST if first chunk), "spent": 0}

replacing any earlier one (a re-claimed continuation, the next chunk: each is a new turn).
"First chunk" = the continuation of the pass's first step (`sweep` / `snapshot`).

While the pass's latest step is over (the chunk turn: between a hand-out and the judge
step's start), with the pass's current token:

- `record_search` charges `1 + len(queries)` (the queries as passed, before de-duplication:
  what Ellen says she ran);
- `ingest_document` charges 3 (a `list_attachments`, a `download_attachment`, the ingest —
  an overcount when the message id was already listed). Inbox filings at the first chunk
  are charged the same way.

The charge is written in the call's own transaction. `record_search`'s answer then carries

    chunk = {"next_item": spent + ITEM_RESERVE <= budget, "calls_left": budget − spent}

Nothing is charged, and `chunk` is absent, while a step runs (the judge's own
`record_search` calls) or outside a pass.

At 80: a first chunk has 69 calls for items, a later one 71. Ten items at the measured
~4.5 fit (~45); a run of heavy items is cut at the first `record_search` past 58 (60).

### 2. The skill: item by item, each recorded at once

Gmail round (the check's step 4 and the package round's `gmail-round`):

- The first chunk (the `sweep` / `snapshot` continuation): the probe, then the
  self-addressed search and the Telegram inbox sweep, BEFORE the items (today: after).
- Then the chunk **one item at a time**: its queries, its downloads and ingests, then its
  `record_search` — before the next item. Several calls for the same item may go in one
  message; never start an item before the previous one is recorded.
- At most 4 queries and 2 attachments per item in one chunk. An item not found within that:
  `record_search(…, queries=[…], incomplete=true)` — `exhausted=true` only when the ideas
  ran out.
- When a `record_search` answers `chunk.next_item: false`, stop the items and go to the
  judge step. Items not reached need no record: they stay in the work and come in the next
  chunk. (The old "an item you never reached: `record_search(incomplete=true)` with no
  queries" is removed from the Gmail round: it cost a call per unreached item and changed
  nothing — an unrecorded item is not searched since the origin, `work.searched_since`.)

### The loop is unchanged

`_another_chunk` still requires `0 < left < handed`. A cut chunk recorded at least one item
(the budget always admits the first: `ITEM_RESERVE` ≤ every budget at 80), so `left` falls
and the next chunk is handed out; the report (`check_report`, `owed`) is untouched.

## Residuals (stated)

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
- `next_item` turns false exactly when `spent + ITEM_RESERVE > budget` (boundary both sides).
- A simulated serial chunk turn (one message per call, items of 4–11 calls, heavy ones
  included) never exceeds `ELLEN_TURNS` messages from `continue_pass` to the closing message,
  with the skill's head and tail counted; and records ≥ 1 item per chunk.
- A check whose chunk is cut runs to `end-pass` with every item searched (the loop still
  falls); the package round likewise.
- Skill text: item-by-item order, record before the next item, the caps, `next_item`,
  the self-mail search and inbox sweep before the items, no `incomplete` record for an
  unreached item in the Gmail round.
- Mutation-check each new guard (in a worktree).
