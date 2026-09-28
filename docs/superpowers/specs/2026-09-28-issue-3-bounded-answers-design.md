# Issue #3 — every list answer fits what the agent can read

Issue: bonzanni/casa-plugin-quarterly-accounting#3 (bug, severity:high). Plugin 0.3.0 → 0.3.1.

## What went wrong

On a real quarter `continue_pass`'s sweep continuation answered 62,8xx characters. Claude
Code hands an agent at most 25,000 tokens of one MCP answer (10,000 draws a warning,
`MAX_MCP_OUTPUT_TOKENS`, checked in the Claude Code MCP docs 2026-09-28); above that it
saves the answer to a file under the agent's private Claude home, which Casa's
`path_scope` does not let Ellen read (ha-casa-app#1082). The answer carried the only copy
of the rotated pass token, so the pass stayed held, and the Gmail round never ran.

The cause is not the item count (`TRIAGE_LIMIT = 50`) but that nothing bounds an item:
`describe()` carries accumulated search queries of any length, full candidate summaries
with runners-up and rationale, document fields read from PDFs, and the result is
pretty-printed.

## The property

**Every answer that lists records is bounded by a stated character budget, whatever the
records hold.** It is enforced where lists are made, not by capping counts:

- `server/budget.py` states the numbers: `RESULT_LIMIT = 20_000` characters for a whole
  answer (about 8 K tokens of this JSON, under the 10 K warning) and `PAGE_BUDGET =
  16_000` for the items of one page, leaving room for the answer's own fields.
- `budget.page(items, limit)` takes items in order while both the count `limit` and the
  page budget allow (at least one item, so a page always makes progress) and says what
  was left out. Each item is measured as it will be rendered.
- Every answer is rendered compactly (no indentation).

A list that is cut says so exactly as before (`truncated`, `remaining`), and a
`list_quarter_state` page names its `next` cursor.

## What each answer carries

**The sweep continuation's `work`** — the Gmail round's list — carries only what the
round uses: `pid`, `date`, `amount_minor`, `currency`, `direction`, `pending`,
`counterparty`, `expectation` (`kind`, `tier`, `row`: a DBIT refund is told from a DBIT
purchase by the row), `search_hint`, `window_days`, `portal`, `fresh`. The round never
matches, so it needs neither `row_snapshot` nor candidates. Free text is clipped
(counterparty name 80, search hint 200). It is one page of the same triage order as
before; what did not fit is `remaining`, already counted into the pass's `not_searched`.
An ordinary item renders to about 300 characters, so a page holds the full 50; with every
clip at its full length (about 520) it holds about 30.
The #2 design's "`work` = exactly `list_quarter_state(triage=true)`'s answer" becomes
"the same items, in the same order, in the Gmail round's shape".

**`list_quarter_state`** (both modes) carries each item's record with its unbounded
parts made bounded:

- `search`: `query_count`, the last 3 queries (each clipped to 120), `exhausted`,
  `incomplete`, `last_searched_at` — not every query ever run.
- `current` and each of `candidates`: the match's ids, revision, state, author and labels,
  and the document's identifying fields (clipped: issuer and recipient 80, number 40) —
  not the runners-up or the rationale.
- `row_snapshot`, `revision` and the bank counterparty stay verbatim: `record_match` /
  `propose_match` compare them exactly and `get_counterparty` looks the text up exactly.
  They are bank-supplied, bounded by the bank's formats in practice; the page budget, not
  a clip, keeps a page small whatever they hold.

Paging: `after` is the `next` of the previous page, passed back unchanged — the sort key
of the page's last item (triage: tier, date, pid; a quarter: date, pid). A key cursor,
not an offset, so an item leaving the list between pages (matched in between) never makes
the next page skip one. `total` and `counts` are over the whole list, not the page.

**`list_unmatched_documents`**: the same page budget, document text fields clipped.

## The token is never lost again

`continue_pass` measures its rendered answer inside the claiming transaction; above
`RESULT_LIMIT` it raises, the transaction rolls back and nothing is claimed — a loud
error, never a committed token rotation whose answer cannot be read. With the bounded
`work` this cannot happen; it is the backstop that makes "the token reaches the agent or
nothing was claimed" hold by construction.

## At the source

`record_search` stores each query clipped to 200 characters (it already keeps at most
50), so the stored record is bounded too.

## Skill

- Specialist step 6: triage a page at a time; while `next` is set and there is time,
  list again with `after=next`; finish with the last page's `remaining`.
- Ellen, finding a payment in a quarter: follow `next` until found.
- Gmail round: unchanged (the continuation's `work`, its `remaining` counted not searched).

## Test

`tests/test_bounded_answers.py` builds 60 worst-case items (long bank texts, 50 queries of
200+ characters, a long KB hint, pairings with long runners-up and rationale, documents
with long fields) and
asserts: the rendered `continue_pass` answer and every `list_quarter_state` /
`list_unmatched_documents` page are within `RESULT_LIMIT`; following `next` visits every
item exactly once, also when an item leaves the list between pages; an oversized claim
rolls back and leaves the pass unclaimed.

## Not in scope

- ha-casa-app#1082 (Ellen cannot read a saved result) — Casa's half.
- `apply_reply`'s `receipt_pages` are already Telegram-sized pages; how many a very long
  reply produces is not this issue.
- The pass token is a small integer; it is a fence against stale holders, not a secret.
