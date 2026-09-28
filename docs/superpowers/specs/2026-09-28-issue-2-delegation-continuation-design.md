# Issue #2 — a pass that outlives its delegation: continuation design

Status: design, not implemented. Issue: bonzanni/casa-plugin-quarterly-accounting#2
(bug, severity:high). Branch `fix/issue-2`, base `e9b4eff` (v0.1.0).
Out of scope: #1 (freshness; ruled, blocked upstream on casa-specialist-finance#86).

## 1. Root cause

The skill assumes one thing that is false: that a `delegate_to_agent(mode="sync")` call
returns the specialist's answer in the same resident turn. Two things break it.

1. **The answer arrives on a later turn.** A sync delegation that is still running after
   60 s returns `status: pending`, and the answer comes back later as a new resident
   turn. That turn's prompt is a system notification. It carries the result text (ok) or
   only the failure kind and message (error), plus the operator's original words. It does
   **not** carry the task, the `pass_token`, or which step of which flow is waiting.
   `SKILL.md` has no instruction for that turn. So Ellen closes the pass as though the
   delegation were the whole pass (pass 2 on production: `end_pass`, no Gmail round), or
   has nothing to work from (passes 1 and 3).
2. **The Gmail round depends on text that a cut delegation never produces.** Step 4
   searches "each item in the work order's `search` list". The work order exists only in
   the specialist's final reply. A delegation cut at the 600 s ceiling returns no reply,
   so there is nothing to search even when Ellen does continue (pass 3). The specialist's
   sweep stops "close to your turn budget", but turns do not bind. The wall clock does,
   and nothing in the plugin measures it.

So the pass's **state lives in a message**, and the platform does not guarantee that
message: it can arrive late, arrive without the result, or arrive twice. The fix moves
every fact the continuation needs into the store, keyed so that a hand-back can find it.
The pass's work already lives there (observations, matches, search bookkeeping,
freshness). What was missing is the *where-are-we* state:

- which pass is waiting;
- on which delegation;
- what comes next;
- the items to search.

## 2. Design assumptions (Casa behaviour this code is built on)

These are stated as assumptions: this repo is public, and they describe another
project's code at a version we do not control. Each one says what this design does if
the assumption is false. The same text goes into the spec (§8).

- **A1 — 60 s degrade.** A sync delegation still running after about 60 s returns
  `{"status": "pending", "delegation_id": <id>, …}` and continues in the background. Its
  outcome arrives on a later resident turn. *If false* (sync waits to the end): the
  answer comes back inline, and Ellen runs the same next step in the same turn. The
  hand-back path is simply not taken. *If the degrade is shorter:* nothing changes.
- **A2 — 600 s ceiling.** A delegated turn is cancelled about 600 s after it launches.
  The resident then receives an error hand-back of kind `timeout` with no result text.
  The specialist's budget (§4.4) is measured from a stamp Ellen writes *before* she
  delegates, so it runs ahead of Casa's own clock. *If the ceiling is longer:* each
  delegation does less than it could, and nothing breaks. *If it is shorter or lower
  than the budget:* the delegation is cut, the hand-back is an error, and the
  continuation still closes the pass from the store (§3). Only the work in flight at the
  cut is lost. Each row's observation is its own transaction, so a cut rolls back at
  most the call in progress.
- **A3 — the hand-back names the delegation.** The hand-back's prompt names the
  delegation by the first 8 characters of the `delegation_id` that the `pending` answer
  returned ("your delegation to finance (id 07bfeb0b)"). The ok, error, restart-orphan
  and "does not carry its answer" shapes all do this. *If false* (the id is missing or
  in another form): `check_setup(delegation=…)` answers `ours: false`, and the pass is
  not continued. It stays live until the existing 3 h reclaim (`STALE_AFTER_S`). That
  fails closed: no write lands under a pass that did not delegate.
- **A4 — a hand-back can arrive more than once, or late.** Casa re-announces a
  delegation outcome after a restart until a turn *delivers* a reply. A resident turn
  that ends in `<silent/>` delivers nothing, so the cron pass's hand-back can come back
  at every restart, possibly long after its pass ended (`replayed_after_restart`, or
  the "recovery notice does not carry its answer" shape). *If false* (at-most-once): the
  design is unchanged. It treats every hand-back as possibly a duplicate, and a
  continuation it cannot identify (A3) fails closed.
- **A5 — a scheduled turn's hand-back may still deliver.** The hand-back of a
  delegation made on a scheduled (cron) turn can still send messages and media, because
  the scheduled-delivery marker travels with it. *If false:* on a cron pass, a `speak`
  the hand-back turn cannot send stays pending in `alerts`. It is sent at the next
  operator-visible `end_pass`, which is today's behaviour for an unsent `speak`.
- **A6 — Ellen's own turn is not bound by A2.** The Gmail round runs on Ellen's
  hand-back turn, which is a resident turn and not a delegation. If that turn dies, A4
  re-announces the hand-back after a restart, and the continuation runs again. Every
  write it makes is idempotent (§6). Otherwise the pass is reclaimed after 3 h.

## 3. The continuation state machine

### 3.1 The record

A new table, `delegations`, holds one row per delegation a pass makes. Ellen writes it
with a new tool, `record_delegation`, in two calls:

1. **Open**, before `delegate_to_agent`: `record_delegation(pass_token, site, …carry)`.
   It stamps `started_at`, which is what the specialist's clock measures from. It also
   stores what the next step needs and cannot get from the store: `quarter`, `channel`,
   `doc_ids` and `report`.
2. **Attach**, only on a `pending` answer:
   `record_delegation(pass_token, site, delegation_id=<pending's delegation_id>)`. This
   stores Casa's id on the pass's latest delegation row.

The hand-back turn finds its pass with `check_setup(delegation="<the 8-char id>")`. The
answer includes a `handback` block. **The live pass's token is released only here, and
only to a caller who names a delegation that is the latest one of the live pass.** The
token returned is always that pass's own token.

### 3.2 Hand-back handling, common to every site

```
delegate_to_agent → ok / error inline (< 60 s) ─────────────┐
                  → pending → attach id → say ≤1 line, end turn
                                  … later turn …
hand-back (any kind) → check_setup(delegation=id8)
   ours=false                → not this plugin's: Casa's default reply
   ours, no pass_token       → stale (pass ended, reclaimed, or a later step
                               is running): <silent/>, write nothing
   ours, pass_token          → site's NEXT step (below) ◄───┘
```

Ellen handles all hand-back kinds (`ok`, `ok` + replay, error of any kind including
`timeout`, `restart_orphan`, and "this recovery notice does not carry its answer") the
same way. The **store** decides the next step and the outcome. The result text is used
only for two things:

- the reason to relay when the specialist says it stopped;
- the handover's case line.

A result that is absent never blocks the continuation. The notification's closing lines
("Reply to the user…", "offer to retry") do not apply to an accounting delegation.

The outcome is decided the same way at every site:

| Outcome | When |
|---|---|
| `stopped` | `check_setup().can_run` is false, or the specialist's reply says it stopped |
| `failed` | the delegation ended without a reply (an error of any kind, or a notice without its answer) **and** `handback.sweep.imported` is false: nothing of the bank was read this pass |
| `interrupted` | `remaining_in_cycle` > 0, or triage was `truncated` or had `not_fresh` > 0, or an item was not searched, or the delegation ended without a reply after the import |
| `complete` | otherwise |

### 3.3 Per site

| Site | Flow | Opened with | On `pending` Ellen says (operator) | Next step on return or hand-back |
|---|---|---|---|---|
| `sweep` | the pass, step 3 | `site="sweep"` | "Checking the bank — this takes a few minutes; I'll send the result here." (nothing more if step 2's line was already said); cron: `<silent/>` | `stopped` → step 6. `failed` → step 6, no Gmail round. Otherwise → **step 4, the Gmail round from the store** (`list_quarter_state(triage=true)`), then step 5 |
| `judge` | the pass, step 5 | `site="judge", report={checked,total,not_searched}` | `<silent/>` (the operator was told at the sweep) | step 6, with the `report` carried back in `handback.report` |
| `handover` | a document handed over | `site="handover", doc_ids=[…]` | "Filed. Checking it against the payments — I'll tell you shortly." | `end_pass` with the outcome, then the case line: from the reply when there is one. Without one: "Filed. I'll match it at the next check." when `list_unmatched_documents` still lists the document, else "Filed and matched." |
| `package` | Packaging, step 1 | `site="package", quarter=…, channel="telegram"\|"email"` | "Reading the bank first — the <quarter> package follows in a few minutes." | `end_pass`, then step 2 (build → stage → send → `record_delivery`) for `handback.quarter` on `handback.channel`. `stopped` → say why, build nothing |

`handback.next` names that step: `gmail-round`, `end-pass`, `end-pass-then-case` or
`end-pass-then-build`. It is the site's fixed successor. Ellen follows the site's row
above. The value is there so that she does not have to infer the step from memory.

**Cron pass.** Everything is the same, with two differences. Every point where the
operator row says a line, the cron pass outputs `<silent/>`. And step 6 on the hand-back
turn sends `speak` (A5) and then `<silent/>`. The scheduled trigger text is unchanged.

**Two passes.** `begin_pass` is unchanged. While a pass waits on a delegation, its
marker is live, so a second trigger gets `busy` ("Already checking — started N minutes
ago."). There are two ways the two passes could interfere, and both are closed:

- *A hand-back of the earlier pass arrives while a later pass is live.* This happens
  after a 3 h reclaim, or with an A4 replay. The earlier delegation is not the live
  pass's latest, so the answer carries no token and Ellen writes nothing.
- *A hand-back of step 3 arrives while step 5 of the same pass runs* (an A4 replay). The
  `sweep` row is no longer the pass's latest, so again there is no token. This keeps a
  replay from ending the pass under a running judge.

**Stale marker.** The 3 h reclaim is unchanged. A delegation cannot outlive A2, so a
marker older than 3 h is a pass whose hand-back was lost or not identified (A3). The
reclaim still bumps the generation. The reclaimed pass's hand-back, if it ever arrives,
gets no token, and its token is refused at every write, as today.

## 4. Server changes

The schema goes from 3 to 4. The tool count goes from 33 to **34**: one new tool,
`record_delegation`. **Why a new tool:** nothing in the store records that a pass is
waiting on a delegation, and Casa's id is the only key the hand-back carries (A3). That
fact has to be written, and no existing write fits it:

- `record_probe` is a health observation, and `check_setup` renders it as one.
- `begin_pass` happens before the id exists, and the judge delegation needs a second
  record.

Three schemas gain an optional argument each: `check_setup(delegation)`,
`list_quarter_state(pass_token)` and `list_projections` (response only).

### 4.1 `server/db.py`

- `SCHEMA_VERSION = 4`.
- In `DDL`, and as `MIGRATIONS[3]`, the same statements:

```sql
CREATE TABLE IF NOT EXISTS delegations (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  pass_id TEXT NOT NULL,
  site TEXT NOT NULL CHECK (site IN ('sweep', 'judge', 'handover', 'package')),
  started_at TEXT NOT NULL,       -- written BEFORE delegate_to_agent: the clock's origin
  casa_id TEXT,                   -- Casa's delegation_id, attached on a `pending` answer
  carry_json TEXT NOT NULL DEFAULT '{}');   -- quarter, channel, doc_ids, report: never text
CREATE UNIQUE INDEX IF NOT EXISTS ux_delegations_casa_id ON delegations(casa_id)
  WHERE casa_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_delegations_pass ON delegations(pass_id);
```

  `carry_json` holds only ids, a quarter, a channel name and counts. It never holds the
  operator's words or any delegation text (Privacy).

### 4.2 `server/passes.py`

New constants:

- `SITES = ("sweep", "judge", "handover", "package")`
- `CEILING_ASSUMED_S = 600`, which is A2.
- `RETURN_BY_S = 450`, the point at which the specialist is told to wrap up. That leaves
  150 s for the last item, the reply, and clock skew.
- `SWEEP_STOP_S = 300`, the point at which the sweep stops at sites where triage follows
  it in the same delegation (`sweep`, `handover`). At `package` and `judge`, the sweep
  stops at `RETURN_BY_S` instead.
- `ROW_COST_S = 10`, the measured cost of one row (about 50 rows in 480–600 s). It is
  used only to size a page.

New functions:

- `open_delegation(conn, token, site, carry) -> dict`, in one `db.tx`.
  - `check_token(token)`. The token is required: `None` is refused, unlike `check_token`'s
    pass-through.
  - `site` must be in `SITES`. `carry` is validated per site:
    - `package` needs `quarter` (canonical) and `channel` (`telegram` or `email`);
    - `handover` needs `doc_ids`, a non-empty list of existing doc ids;
    - `judge` takes an optional `report` of integer counts;
    - any other key is refused.
  - It inserts the row with `pass_id` = the live pass and `started_at = db.now()`.
  - It returns `{"site", "started_at", "return_by": <UTC>, "sweep_stop_at": <UTC>}`.
- `attach_delegation(conn, token, site, casa_id) -> dict`, in one `db.tx`.
  - `check_token`.
  - `casa_id` must match `^[0-9a-zA-Z-]{8,64}$` and is stored lowercased.
  - The target is the live pass's latest row (`MAX(seq)`). It must have the same `site`
    and a `casa_id` that is NULL or already equal to this one (idempotent). Otherwise the
    call is refused: "the delegation to attach is not this pass's latest `<site>` one —
    open it first".
  - It returns `{"attached": casa_id[:8]}`.
- `latest_delegation(conn, pass_id)` is a SELECT helper.
- `clock(conn, token) -> dict | None`, read-only. It returns `None` unless `token` is the
  live generation and the live pass has a delegation row. Otherwise, with
  `e = _age_s(started_at)`, it returns
  `{"elapsed_s": e, "time_left_s": max(0, RETURN_BY_S - e), "wrap_up": e >= RETURN_BY_S}`.
  A stale token gets `None` and no refusal. The refusal is the write's own business.
- `sweep_allowance(conn) -> int | None`, called inside `list_projections`' transaction.
  It returns the seconds until this site's sweep stop, or `None` when the live pass has
  no delegation row. `None` means today's unbounded behaviour, which is what the direct
  sim and the existing tests rely on.
- `handback(conn, prefix) -> dict`, read-only.
  - `prefix` must be at least 8 characters of `[0-9a-zA-Z-]`; otherwise the call is
    refused.
  - It matches rows with `lower(casa_id) LIKE lower(prefix) || '%'`. The prefix is
    validated first, so it contains no `%` or `_`.
  - No row: `{"ours": false}`.
  - More than one row: `{"ours": true, "ambiguous": true}`, with no token.
  - One row: `{"ours": true, "pass_id", "trigger", "site", "live", "current", "next",
    "quarter"?, "channel"?, "doc_ids"?, "report"?, "sweep": throughput(pass_id) +
    {"imported": <this pass has a snapshots row>}}`.
    - `live` is true when the marker is live on this `pass_id`.
    - `current` is true when this row is the pass's latest delegation.
    - `"pass_token"` is present **only when both `live` and `current` are true**.
    - `next` comes from `site`, as in §3.3.

### 4.3 `server/binding.py`

`check_setup(conn, delegation=None)`: when `delegation` is given, the answer gains
`"handback": passes.handback(conn, delegation)`. Without it, the answer is exactly as
today, and **the answer never carries a pass token** (pinned by a test).

### 4.4 `server/sweep.py`

In `list_projections`, after `order` is computed and still inside the transaction:

```python
allow = passes.sweep_allowance(conn)
time_up = allow is not None and allow <= 0
if time_up:
    page = []
else:
    cap = limit if allow is None else min(limit, max(1, allow // passes.ROW_COST_S))
    page = order[:max(1, int(cap))]
```

The answer gains `"time_up": time_up`. `remaining_in_cycle` keeps its formula, so it is
greater than 0 after a stop for time: 0 still means only "every lineage was read". The
cursor is not moved by a `time_up` page. The existing wrap and `cycle_started_at` updates
do not depend on the page.

### 4.5 `server/work.py`

`describe()` gains `"search_hint": cp["search_hint"]` and `"window_days":
cp["window_days"]` (None and 10 without a KB entry). Ellen's Gmail round builds its
queries from the triage item. The specialist's durable way to pass a search idea is the
KB (`upsert_counterparty(search_hint=…)`), never its reply.

### 4.6 `server/tools.py`

**New tool `record_delegation`.**

- Description: "Before every delegate_to_agent in a pass, record which step is
  delegating (site: sweep, judge, handover, package, plus what the next step needs:
  quarter and channel for package, doc_ids for handover, report for judge). If the
  delegation answers `pending`, call it again with the same site and its delegation_id.
  That is how the answer, when it comes back on a later turn, finds this pass."
- Schema: `{pass_token (req), site (req), delegation_id: S, quarter: Q, channel: S,
  doc_ids: AI, report: O}`.
- With `delegation_id`, it calls `attach_delegation` and refuses any carry argument.
  Without it, it calls `open_delegation`.

**`check_setup`.** The schema becomes `{"delegation": S}`, optional. The description adds:
"When a system notification says a delegation to the finance specialist returned or
failed, pass the 8-character id it names as `delegation`. `handback` says whether it is
this plugin's, which pass and step it belongs to, and — only while that pass is still
waiting on it — the pass_token."

**`list_quarter_state`.** It gains the optional `pass_token` (TOKEN), used only for
`clock`. The description gets the standard "During a pass, pass the pass_token." The
optional-token count in `test_every_optional_token_write_says_to_pass_it` goes from 10
to 11.

**`list_projections`.** The description adds: "`time_up`: this delegation's time for the
sweep is spent; stop sweeping, whatever remains."

**The clock hook.** At the end of the module, every registered tool whose schema has
`pass_token` is wrapped once. When the call carried an integer `pass_token` and returned
a dict, the answer gains `"clock": passes.clock(conn(), token)`, but only when that is
not None. A refusal (a string) is untouched. `_deliverable` is unaffected: `clock` is
not operator text.

**Manifest.** `.claude-plugin/plugin.json` lists the new tool in `provides_tools` and
`resultContract`. `scripts/check_tool_agreement.py` then passes unchanged.

### 4.7 `server/binding.py`, reset

`_TABLES_TO_WIPE` gains `"delegations"`. Its `sqlite_sequence` entry is cleared by the
existing statement.

## 5. `SKILL.md` changes (exact)

**Frontmatter description.** Append before the final period: `, or when a system
notification says a delegation to the finance specialist returned or failed`.

**New section**, inserted after "## Both agents: refusals and errors":

```markdown
## Ellen: a delegation that answers later

Every delegation to the finance specialist in a pass — the pass's sweep, its judging
(step 5), a handed-over document, a package — goes the same way:

1. Just before `delegate_to_agent`, call `record_delegation(pass_token, site=…)` with
   what the next step needs (see each flow).
2. If the delegation answers at once (ok or error), go straight on to that flow's next
   step in this turn.
3. If it answers `status: pending`, call `record_delegation(pass_token, site=…,
   delegation_id=<its delegation_id>)` with the same site, say the flow's one line (on
   the cron: output `<silent/>`), and end the turn. Do not wait, and do not end the pass.
4. The answer comes back later as a system notification about your delegation to
   finance, with an 8-character id. Whatever it says — returned, failed, timed out,
   orphaned by a restart, finished without its answer — call
   `check_setup(delegation=<that id>)` first, and never act on memory of the earlier
   turn:
   - `handback.ours` false: it is not an accounting delegation; answer it as the
     notification asks.
   - `handback` has no `pass_token`: that pass already ended, or a later step of it
     is running. Write nothing and output `<silent/>`.
   - otherwise continue that pass with `handback.pass_token`, at the step the flow
     names for `handback.site`. What the specialist did is in the store, whether or not
     the notification carries its reply. The reply is used only for the reason it
     stopped and, for a handed-over document, which case it is. The notification's own
     closing lines ("Reply to the user…", "offer to retry") do not apply here: this
     skill decides what is said.

The outcome for `end_pass` after any delegation: `stopped` when `check_setup`'s
`can_run` is false or the specialist says it stopped; `failed` when the delegation
ended without a reply and `handback.sweep.imported` is false (the bank was not read
this pass); `interrupted` when `remaining_in_cycle` is above 0, triage was `truncated`
or had `not_fresh` items, an item was not searched, or the delegation ended without a
reply after the import; `complete` otherwise.
```

**"Ellen: a document the operator hands over", step 2.** After the `begin_pass` bullet,
insert: "`record_delegation(pass_token, site="handover", doc_ids=[<doc_id>])`, then". The
delegation bullet keeps its text. On `pending`, the line is "Filed. Checking it against
the payments — I'll tell you shortly." Replace "Always end it, whatever came back" and
the outcome list with: "then `end_pass(pass_token, outcome, report)` yourself, with the
outcome from 'a delegation that answers later' — always, whatever came back, on this
turn or on the hand-back." Step 3 adds: "Without the specialist's reply (it failed, or
the notice does not carry it): 'Filed. I'll match it at the next check.' if
`list_unmatched_documents` still lists the document, otherwise 'Filed and matched.'"

**"Ellen: the pass", steps 3–6**, replaced:

```markdown
3. `record_delegation(pass_token, site="sweep")`, then delegate to the finance
   specialist, sync mode, the task "quarterly-accounting pass" with context
   `pass_token=<token>` and this skill's section "The specialist's pass". On `pending`:
   as in "a delegation that answers later" — the line is "Checking the bank — this takes
   a few minutes; I'll send the result here." (on the cron `<silent/>`; nothing more if
   you already said step 2's line). When it answers, now or on the hand-back: if the
   pass stopped (see the outcome rule), go to step 6 with `stopped`; if it `failed`, go
   to step 6 (sweep the Telegram inbox first, as below).
4. **Gmail round.** Always make the Gmail probe first … [unchanged through "the next
   pass probes again."] The work list is the store's, never the specialist's reply:
   `list_quarter_state(triage=true)` — the payments read since this import that need a
   document and have none. Skip an item marked `portal`. For each, run a ladder of
   narrow queries built from its fields … [the ladder, as today, now sourced from the
   item: counterparty and `search_hint`, the amount as printed, `window_days` around its
   date, `has:attachment`; a CRDT or a DBIT `refund` is the business's own document, in
   Sent; ignore payment confirmations] … [download/ingest/record_search unchanged]. Items
   left out (`remaining`, `not_fresh`) and items you never reached are `not_searched`.
   [self-mail search and Telegram inbox sweep unchanged]
5. If anything was filed, `record_delegation(pass_token, site="judge",
   report={checked, total, not_searched})`, then delegate "judge the newly filed
   documents" with the same `pass_token` (the specialist's steps 6 and 7). On `pending`,
   output `<silent/>` and end the turn; the hand-back continues at step 6 with
   `handback.report`.
6. `end_pass(pass_token, outcome, report)` — the outcome from "a delegation that
   answers later"; report `{checked, total, not_searched}` (on a hand-back of step 5,
   `handback.report`). [speak / `<silent/>` / status handling unchanged]
```

**"The specialist's pass".** Add a paragraph after the first one:

> "Every answer to a call that carries the `pass_token` may include `clock`. When it says
> `wrap_up`, finish the item in hand and return at once. Casa cuts a delegation off
> after about ten minutes, and whatever you have recorded is kept, but a reply that
> never comes is lost."

- Step 5: "Repeat `list_projections(pass_token)` until `remaining_in_cycle` is 0 or it
  says `time_up`". In the batches paragraph, "When the budget runs out" becomes "When
  `time_up` or `wrap_up` comes".
- Step 6: "`list_quarter_state(triage=true, pass_token=…)`".
- Step 8 is replaced:

> "8. **Return** a short report for Ellen: whether you stopped and why, the sweep's
> `remaining_in_cycle`, and triage's `remaining`. Ellen searches from the store, not
> from your reply. A search idea the payment's own facts do not carry (the address a
> vendor mails from, another name it invoices under) goes into the KB:
> `upsert_counterparty(name, search_hint=…, pass_token=…)`."

**Packaging, step 1.** After the `begin_pass` sentence, add: "`record_delegation(pass_token,
site="package", quarter=<the quarter>, channel="telegram"|"email")`". On `pending`, the
line is "Reading the bank first — the <quarter> package follows in a few minutes." On
the hand-back, the quarter and channel are `handback.quarter` and `handback.channel`.
The `failed` clause becomes the common outcome rule.

`tests/test_skill.py` pins that change with the fix. `test_ellen_holds_the_package_and_handover_passes`
now asserts the common rule's sentence instead of "`failed` if the delegation errored or
ran out of turns". `test_the_sweep_is_batched…` asserts "`time_up`" instead of "the
budget runs out". The skill introduces no new operator line that names machinery, and
`OPERATOR_LINES` gains the three `pending` lines and "Filed and matched.".

## 6. Invariants the design keeps

- **Stale-pass fence.** The generation check is unchanged. The only new way to get a
  token is `handback`, which returns the delegating pass's **own** token while it is
  live and this delegation is its latest. No actor gets another pass's token. Neither
  path lets a replay write: a replay of an ended pass gets no token, and a replay of an
  earlier step of the live pass gets no token either. A reclaimed pass's token is still
  refused at every write.
- **Idempotent continuation.** A resumed step can run twice (A4, A6) and do no harm:
  - ingest is idempotent by hash;
  - `record_search` counts a fruitless pass once per `pass_id`
    (`last_counted_pass`) and de-duplicates queries;
  - `record_probe` replaces its row;
  - `end_pass` succeeds once, and the second call is refused as not current.
- **Bank-write gate.** It is untouched. The clock and `time_up` never change a gate
  verdict or an instruction. A `time_up` page returns no items, so it asks for no write.
- **One write, then a read.** Unchanged. `time_up` is evaluated only in
  `list_projections`, between rows, never inside an observation.
- **Freshness (#1 as it stands).** Unchanged. The time stop stamps nothing, and the
  Gmail list is `triage` with `fresh_only`, exactly the set a machine match accepts. The
  cursor persists across passes, so each pass reads the next rows and searches and
  matches those in the same pass. The search-and-match work converges over passes. A
  complete-quarter package still needs #1.
- **D3 binding.** No reply path changes. Every view on a hand-back turn is sent, then
  `mark_rendering_delivered`, as before.
- **4096 fit.** The server produces no new operator text. `handback`, `clock` and
  `time_up` are machine fields. `_deliverable` still guards `end_pass` and every view.
- **Ellen holds every pass** (fix wave F). The specialist still never begins or ends a
  pass. The difference is that "Ellen ends it" may now happen on a later turn.

## 7. Tests (all through `qa_server.handle` unless noted)

Clock control: `mock.patch.object(db, "_clock", …)` with an advancing clock, the same
technique as `test_passes_binding.py`. The specialist's side is the `ToolFlow` harness
in `tests/test_e2e.py`, running against the real bank-feed.

1. **Pending, then hand-back (the production failure).**
   - Setup: `begin_pass(operator)`, `record_delegation(site="sweep")`,
     `record_delegation(site="sweep", delegation_id="07bfeb0b-…")`. The specialist's
     probes, import and a full sweep run through the tools.
   - Check: `check_setup(delegation="07bfeb0b")` returns `ours`, `live`, `current`, the
     `pass_token` equal to the begun token, `next == "gmail-round"`,
     `sweep.imported == True`.
   - Then Ellen's round runs on `list_quarter_state(triage=true)`: its items are exactly
     the fresh ones needing a document. It continues with `record_probe(gmail)`,
     `record_search` and `end_pass`.
   - Assert: the `gmail` probe row carries this `pass_id`, `record_search` counted, and
     the marker is not live.
2. **Error hand-back at the ceiling.**
   - Setup: the clock advances `ROW_COST_S` per row read. `list_projections` pages shrink
     and then answer `time_up: true` with `remaining_in_cycle > 0` and no items. Assert
     that no row was read after `SWEEP_STOP_S`.
   - The "cut": the specialist simply stops, before triage and without a reply.
   - Assert that `check_setup(delegation=…)` still gives the token, that the triage list
     is non-empty and fresh-only, and that `end_pass(outcome="interrupted")` stores
     `swept_this_pass` > 0.
   - Variant: the cut comes before the import. `sweep.imported` is false, and the outcome
     rule gives `failed`.
3. **Cron pass.** The same as test 1 with `trigger="cron"`, and `handback.trigger ==
   "cron"`. `end_pass` returns `speak` None when nothing is pending, which is the skill's
   `<silent/>` path. A variant with an alert pending returns `speak` that fits in 4096.
4. **Two passes.**
   - (a) While A waits, a second `begin_pass` answers `busy`.
   - (b) A's `judge` delegation is opened. A replayed hand-back of A's `sweep` id then
     answers `current: false` with no token.
   - (c) A ends and B begins. A's id answers `live: false` with no token, and B's id
     gives B's token.
   - (d) Marker reclaimed after `STALE_AFTER_S`. A's hand-back gets no token, and
     `end_pass` with A's token is refused.
5. **The token never leaks.**
   - `check_setup()` with no argument has no `pass_token` anywhere in its JSON, whether
     or not a pass is live.
   - An unknown id answers `ours: false`.
   - An id shorter than 8 characters, or with `%`, is refused.
   - Two rows sharing a prefix (inserted directly) answer `ambiguous` with no token.
6. **`record_delegation` contract.**
   - A missing or stale token is refused.
   - Rejected inputs: a `site` outside the set, `package` without a quarter or channel,
     `handover` with an unknown doc id, and carry arguments on an attach.
   - An attach to a site that is not the latest, or to a row holding a different id, is
     refused. An attach of the same id twice succeeds.
7. **Clock.** Absent without a delegation row, and absent for a stale token. Present on
   `record_observation`, `record_match` and `list_quarter_state(pass_token)`. `wrap_up`
   flips at `RETURN_BY_S`. At `package` and `judge` the sweep stops at `RETURN_BY_S`, not
   at `SWEEP_STOP_S`.
8. **Back-compatibility.** `list_projections` without a delegation row keeps today's
   paging, and every existing sim test passes untouched.
9. **Schema.** Freeze today's DDL as `DDL_V3` in `tests/schema_history.py` (schema 3,
   `server/db.py` at `e9b4eff`). A v3 store migrates to v4 with `delegations` present and
   its data intact, and `SCHEMA_VERSION == 4`. `reset_store` empties `delegations`.
10. **Surface.** `EXPECTED` gains `record_delegation`, and the count becomes 34 in both
    assertions. The manifest agrees. The skill's new backticked calls and arguments
    exist (`test_every_named_argument_exists_on_its_tool` covers `record_delegation(…)`
    and `check_setup(delegation=…)`). Pinned sentences are as listed in §5.
11. **Package hand-back.** `record_delegation(site="package", quarter="Q3",
    channel="telegram")`, then attach. `handback.quarter == "2026-Q3"`,
    `handback.channel == "telegram"`, and `next == "end-pass-then-build"`. After
    `end_pass`, `build_quarterly_package(handback.quarter)` builds.

## 8. Spec text to add (`2026-08-10-quarterly-accounting-design.md`)

**New subsection** under "## Casa baseline", titled "### Delegation timing — design
assumptions". Its text is §2, A1–A6, verbatim, prefixed with:

> "This plugin is built on the following behaviour of Casa's `delegate_to_agent`, as read
> in Casa v0.331.0–v0.332.0. They are assumptions about another project, and each says
> what this code does if it stops holding."

**§Weekly pass, step 3.** Replace "The only real ceiling is the specialist's `max_turns`
(70) and the pass's own wall-clock, and both are facts to report, never silently
absorbed." with:

> "The binding ceiling is the delegation's wall clock (A2), not its turns. The server
> stamps each delegation's start, and every tool answer the specialist gets carries the
> time left. The sweep stops for time on its own, and the specialist wraps up well
> inside the ceiling. A delegation cut off anyway loses only the call in flight. Ellen's
> Gmail round is derived from the store (`list_quarter_state(triage=true)`), never from
> the specialist's reply, so it runs even after an error hand-back."

**§Running the pass on demand.** After the marker paragraph, add:

> "A pass spans turns. Its delegations answer on later turns (A1), and each delegation
> is recorded in the store before it starts. The hand-back finds its pass by the
> delegation's id (A3) and gets that pass's token only while the pass is still waiting on
> that delegation. A duplicate or late hand-back (A4) finds nothing to continue and
> writes nothing."

**§Tool surface.** The heading becomes "(server, 34 tools)". Add an erratum line for
`record_delegation`, with the reason from §4. The Setup paragraph gains
`check_setup(delegation)`.

**§Open items, background jobs.** Append:

> "Evidence arrived 2026-09 (#2): two of three catch-up delegations reached the
> wall-clock ceiling. #2 is fixed within delegations (continuation plus a time budget).
> Jobs stay out unless a budgeted pass still cannot converge."

## 9. Failure modes considered and rejected

- **Parse the work order out of the hand-back text.** An error hand-back has none. A
  replay may carry "this notice does not carry its answer". That was the bug.
- **Rely on Ellen's conversation memory for the token and step.** The hand-back is a
  new turn, and for a cron pass it may run in another session than the one that
  delegated. The goals exclude it.
- **`check_setup` always shows the live pass's token.** It is simpler, but any
  hand-back, including a replay of an older pass (A4) or of step 3 while step 5 runs,
  would get the live token and could end a pass whose specialist is still writing. It
  would also hand the token to every Ellen turn that answers a question. Releasing the
  token by delegation id gives each hand-back only its own pass, and only while that
  pass waits on it.
- **Derive the site from store state instead of recording it.** The site itself could be
  derived: the trigger, plus whether this pass has a `gmail` probe. But the id key (A3)
  and the clock's origin both need a write, and the package's quarter and channel and
  the judge's report counts are not in the store. Once there is a record, it can carry
  all of these.
- **A row-count budget instead of time.** A row costs 2–8 calls, and the sync and
  classifier wait vary. Casa enforces time, so the budget is time. Rows only size a page
  (`ROW_COST_S`).
- **A shorter reclaim** (e.g. a pass stuck waiting more than 30 min). This was deferred.
  A pass legitimately spans a delegation, a Gmail round and a second delegation, and a
  lost hand-back is re-announced (A4). A shorter reclaim would add a second reclaim
  path to review for the fence. The 3 h backstop stays. If the first real passes show
  stranded markers, this is the change to make.
- **`mode="async"` everywhere.** The continuation handles sync and async identically, so
  the mode is not load-bearing. Sync keeps a quick handover answered inline.
- **The specialist ends the pass, or a durable work-order table.** The first undoes fix
  wave F (a dying delegation must not strand the marker). The second is a second copy
  of what `triage` already derives, which is more state to reconcile.
- **Split triage into its own delegation every pass.** It gives each part a full budget,
  but it adds a hand-back to every pass. Because the Gmail list comes from the store, the
  split is no longer needed for correctness. The clock gives triage its slot, and step 5
  catches up anything triage did not reach.
- **Casa background jobs** (`casa.jobs`). See the open-items text in §8: a second
  engagement model, per-batch topic messages against a pull-only, silent cron. It is not
  needed to fix #2.
- **The server ends a pass on a timer.** The server is a per-session stdio process with
  no scheduler, so it has nothing to fire the timer.

## 10. Open questions for the controller

1. **Silent hand-backs are re-announced (A4).** Per Casa v0.332.0 (`agent.py`
   `_ack_delivery`), a turn that ends `<silent/>` never discharges its delegation
   announcement. So every cron pass that delegates past 60 s is re-announced at every
   Casa restart. This design makes that harmless: the replay gets no token and ends in
   `<silent/>`. But it costs one resident turn per retained notice per restart,
   indefinitely. Should this be filed on ha-casa-app, since a chosen silence should
   probably discharge the obligation?
2. **Release number.** The new tool plus the migration suggests v0.2.0. Bumping the
   version changes `WORKFLOW` to `acct@0.2.0`. `check_setup` then reports `acct@0.1.0`'s
   writes as an older workflow still present: that is a report, not a stop, and existing
   behaviour. The test-install reset loop then asks which backup to restore. A patch
   bump (0.1.1) has the same effect. Is that wanted for the test install?
3. **Budget numbers.** `SWEEP_STOP_S` is 300 and `RETURN_BY_S` is 450. They are derived
   from one measurement (about 50 rows in 480–600 s). The first test-install pass after
   the fix should record `swept_this_pass` and the elapsed time, and the numbers get
   re-tuned from that. Is it acceptable to ship with these numbers?
