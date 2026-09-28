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

## Revision 2 (after code round C1 on 3ea4edc: Astra DNS, Terra DNS)

Round C1 found two holes, and both come from one bad assumption: that what the list holds
stays well-behaved. (1) Verbatim fields (`row_snapshot`, the bank's counterparty, a KB
link) are bounded only "in practice". A 24,000-character remittance or a long link made
one item bigger than the page, and `budget.page` let it in anyway. (2) The cursor was the
sort key of the last item shown, and that key can change. A `set_expectation` between
pages moved an unvisited payment from optional to required, which put it behind the
cursor. It was then never listed and never counted (`remaining` 0). Also,
`list_unmatched_documents` cut its list with no way to fetch the rest.

Revision 2 replaces both assumptions.

**A. Every item has a size bound by construction.**
- A listed item passes through `budget.bounded(item, 200)`, which clips every string in
  it. Named exceptions: `link` is clipped at 500, and `upsert_counterparty` refuses a
  `document_link` over 500 characters, so the stored value is bounded too.
- Every list inside an item has a bound:
  - `reasons` and `labels` come from fixed vocabularies.
  - `search.last_queries` holds at most 3.
  - `candidates` holds at most 3 summaries.
  - `candidate_ids` lists every candidate's id: it is the set `resolves` must name, and
    it is only integers.
- `row_snapshot` leaves the listing. In its place goes `row_digest`: 16 hex characters,
  the sha256 of the canonical `facts_of(live row)`. `record_match` and `propose_match`
  take `row_digest`, and a digest that differs from the live row's is refused with the
  message the snapshot mismatch gives today. `row_snapshot` is still accepted, for
  existing callers. Exactly one of the two is required. The payment reference stays
  readable next to the digest, as `remittance` clipped at 200 (code round C2, Astra): it
  is the tie-break between otherwise identical payments. The skill names only
  `row_digest`. The facts the specialist judges from (amount, currency, date, the bank
  texts clipped at 200) are all still in the item.
- `budget.page` never admits an item over the page budget. Such an item raises an error
  naming its pid. It is reachable only through more than about 1,000 candidates on one
  payment. The test builds the largest item the clips allow and asserts it is under
  `PAGE_BUDGET`.

**B. A traversal never skips an item.**
- `list_quarter_state(quarter=…)` pages in pid order, which never changes. Its cursor is
  `after=[pid]`.
- `list_unmatched_documents` pages in doc_id order and gains `after` / `next`.
- Triage keeps its priority order (required first, then oldest), and that order can
  change. So with a `pass_token` the traversal lives in the store:
  - A table `triage_listed(token, pid)` records every pid a page has shown to that
    token's holder.
  - Each call first checks the token (a stale holder is refused, as for every pass
    write). It then deletes the rows of other tokens and returns the next page, in the
    current priority order, of triage items this token has not been shown yet.
  - `remaining` counts the triage items not yet shown to this token.
  - So a payment whose tier or date changes, or one that joins triage partway through,
    is listed exactly once. A payment that leaves triage is simply not listed.
  - Every claim rotates the token, so the judge step's holder starts a fresh traversal.
  - Without a `pass_token`, triage answers only its first page (`remaining` is still
    counted).
- Schema 5 → 6 adds `triage_listed`. `tests/schema_history.py` freezes DDL_V5.
- The continuation's `work` is computed at the claim and records nothing: the Gmail
  round's list is not the specialist's traversal.

Skill: the specialist lists triage again while `remaining` is above 0 and there is time,
and finishes with the last page's `remaining`. It passes the item's `row_digest`. Ellen
follows `next` in a quarter listing.

## Revision 3 (after design round D2 on 967b75c: Astra DNS, Terra DNS)

Revision 2's store-backed triage traversal (`triage_listed`) is dropped. It recorded a
page as shown when the page was built, which is not the same as the agent receiving it.
Two sequences break it:
- The answer is lost after the commit. A retry with the same token then skips that page
  (Astra and Terra).
- A payment changes after it was shown. It stays marked as shown, so it is never listed
  again, and `remaining` is 0 (Astra).

The store cannot know what the agent received. So the traversal must be something the
agent can replay, over an order that cannot change.

**B (replaces revision 2's B).** Every paged list is a stateless cursor over an
immutable order: pids for `list_quarter_state` in both modes, doc_ids for
`list_unmatched_documents`. The cursor is `after=[id]`, and `next` is the last id shown
while more follow.
- A lost answer is replayed exactly by calling again with the same `after`.
- An item whose tier, date or facts change between pages keeps its place.
- An item already shown that must be judged again, because a match was refused as
  changed, is re-read with the new `list_quarter_state(pid=…)`. That returns the one item
  in the listed shape, or null if it has ended.
- The cost: triage no longer puts required payments first across pages. It pages in pid
  order, which roughly follows import order. Whatever a delegation does not reach is
  still counted in the last page's `remaining` and reached by a later step or pass.
  Order within the continuation's `work` is the same pid order.
- An item that joins a list mid-traversal with an id behind the cursor is not listed in
  this traversal: a payment entering triage, or a document that becomes unmatched when
  its pairing is retired (code round C2, Terra). That is the same snapshot-at-listing
  semantics as before issue #3, for all three lists. The next traversal lists it: the
  judge step or the next pass for triage, the next listing for documents.
- **But it is not free (code round C3, Astra's refutation defense, reproduced against
  both revisions).** Before #3, a quarter with more than 50 open payments left
  `triage_remaining` above 0, and that forced a judge step in the same pass. After #3,
  the last page says 0. Take a payment reopened behind the cursor, say by the operator
  rejecting a wrong pairing, whose correct invoice is already filed. Before #3 it was
  matched in that pass. After #3 it stayed open while the pass ended `complete`. So the
  sweep continuation now carries `judge_due`, taken at the claim. It counts the fresh,
  booked payments still in triage for which an unmatched document meets the necessary
  part of the auto-match bar (kind, currency, exact amount). Ellen runs the judge step
  when it is above 0.
  - It can over-count: a payment triage already declined, for example on its date
    window. The cost is at most one extra judge delegation. It never under-counts a
    payment that could be auto-matched.
  - A payment that joins with no filed document is the Gmail round's. If that round
    files something, the judge step runs anyway.
- The single-payment re-read `pid=` goes through the same page guard (code round C3,
  Astra: 4,200 candidates returned 21,728 characters there).
- No schema change.

**A, amended.**
- `row_digest` is the full sha256, 64 hex characters, of the canonical facts. It is not
  truncated.
- A listed item's `labels` are de-duplicated, and so are the stored labels written from
  now on.
- A listed document's `collisions` holds at most 5 ids, plus `collision_count`.

**"The token is never lost" is narrowed:** the token is never lost because of the
answer's size. A crash after the claim commits and before the answer is written is the
residual issue #2 already accepted (round X3). The lease lapses after 600 s, and the next
`continue_pass` claims the step again.

## The token is never lost to the answer's size (revision 1)

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
