# Issue #2 — a pass that outlives its delegation: continuation design

Status: design, not implemented. Revision 2, after design round X1 on `8b055f5`.
Issue: bonzanni/casa-plugin-quarterly-accounting#2 (bug, severity:high).
Branch `fix/issue-2`, base `e9b4eff` (v0.1.0). Out of scope: #1 (freshness is ruled, and
blocked upstream on casa-specialist-finance#86).

**What changed from revision 1.** Revision 1 tied each Casa notification to our
delegation by the delegation's id. Round X1 found that single mechanism failing in six
ways:

- short-id collisions;
- a race between opening the record and attaching the id;
- a restart landing before the attach;
- inline errors that never attach;
- a package flow that lost its recovery authority once its pass ended;
- replays of silent notices.

The mechanism is **removed, not hardened** (controller ruling). Revision 2 stores no
Casa id and matches no notification. Progress lives in the store, per pass: each step is
recorded as *started* and *finished*, and a step with no "finished" expires after a set
time. One claim, made with a compare-and-set, continues whatever is due. A notification
is only a *reason to look*. It is never evidence of anything.

## 1. Root cause

The skill assumes that `delegate_to_agent(mode="sync")` returns the specialist's answer in
the same resident turn. Two facts break that assumption.

1. **The answer arrives on a later turn.** A sync delegation still running after about
   60 s returns `status: pending`. Its outcome then arrives as a new resident turn whose
   prompt is a system notification. That notification carries the result text (on ok)
   or the failure kind (on error), and nothing else. It has no `pass_token`, no step and
   no work order. `SKILL.md` gives no instruction for that turn, so Ellen either closed
   the pass as if the delegation were the whole pass (pass 2 on production) or had
   nothing to work from (passes 1 and 3).
2. **The Gmail round is fed by text that a cut delegation never produces.** Step 4
   searches "each item in the work order's `search` list", and that list exists only in
   the specialist's final reply. A delegation cut at the ceiling has no final reply. The
   sweep is bounded by "turn budget", but turns are not the binding limit. The binding
   limit is the wall clock, and nothing measured it.

The pass's *where-are-we* state lived in a message the platform does not guarantee. The
fix moves that state into the store. The work itself (observations, matches, search
bookkeeping, freshness) is already there.

## 2. Design assumptions (Casa behaviour this code is built on)

These are stated as assumptions: the repo is public, and they describe another
project's code as read at v0.331.0–v0.332.0. Each one says what this code does if it
does not hold. §8 carries them into the spec.

- **A1 — the 60 s degrade.** A sync delegation still running after about 60 s returns
  `{"status": "pending", …}`, and its outcome arrives on a later resident turn.
  - *If false* (sync waits to the end): the answer is inline, and Ellen calls
    `continue_pass` in the same turn. That is the same path.
  - *If the degrade is shorter:* nothing changes.
- **A2 — the 600 s ceiling.** A delegated turn is cancelled about 600 s after launch, and
  the resident receives an error of kind `timeout` with no result text.
  - The specialist's clock and a step's expiry are both measured from a stamp Ellen
    writes *before* she delegates, so they run ahead of Casa's clock.
  - *If the ceiling is longer:* a delegation does less than it could. A specialist still
    alive after its step expired can go on writing under the same pass for a while
    (see §6, "An expired step's specialist").
  - *If the ceiling is shorter than the budget:* the delegation is cut, and its step
    expires at `STEP_EXPIRY_S` anyway. The continuation then runs at the next
    `continue_pass` call made after expiry.
- **A3 — every outcome produces a resident turn** (ok, error, restart orphan, or "notice
  does not carry its answer"). This design reads nothing from that turn's content, not
  even its id.
  - *If false* (a notice is lost): nothing continues the pass until the next
    `continue_pass` call. That call comes at the next check, handover or package request
    (§3.4). The 3 h reclaim is the backstop.
- **A4 — a notice can arrive more than once, or late.** Casa re-announces an outcome
  after a restart until a turn *delivers* a reply. A turn that ends `<silent/>` delivers
  nothing (`agent.py` `_ack_delivery`). So a cron pass's notices come back at every
  restart.
  - *Here:* a replay calls `continue_pass`, and it can only continue what is due anyway
    (§3). Each replay costs one resident turn. That is residual R1 (§10), and §10 drafts
    the Casa issue.
- **A5 — a scheduled turn's notice can still deliver.** The scheduled-delivery marker
  travels with it, so the notice can send messages and media.
  - *If false:* on a cron pass, `speak` stays pending in `alerts` and goes out at the
    next `end_pass`, which is today's behaviour. A package continuation on such a turn
    fails its send, and it is recorded `uncertain` (§3.3).
- **A6 — Ellen's own turn is not bound by A2.** If her Gmail round dies, the claim
  expires after `CLAIM_TTL_S`, and the next `continue_pass` call claims the same step
  again. Every write in that step is idempotent (§6).

**Verified for round X1 (Terra S1).** Casa runs every turn under a per-session write gate
(`session_write_gate(channel_key)`, `agent.py` around line 1965). The key is
`(channel, role, chat_id)`, and a notice is built from the delegation's recorded origin
channel and chat id. So a notice normally lands on the delegating turn's own session key
and waits behind it. That includes a cron pass, whose origin is the scheduled turn's
channel and chat.

Revision 2 **does not rely on this**:

- There is no attach step left to race.
- Ellen writes the step's start *before* `delegate_to_agent`, in the turn that holds the
  token.
- Two turns that could both continue (a replay, a foreign notice, a second session)
  are separated by the compare-and-set claim, not by turn ordering.

## 3. The continuation mechanism

### 3.1 What the store records

**`pass_steps`**: one row per delegated step of a pass.

- **Steps:** `sweep` and `judge` (the pass), `handover`, and `snapshot` (the package's
  bank read).
- **Started:** Ellen writes the start with the token she holds, just before
  `delegate_to_agent`: `record_step(pass_token, step, action="start", …carry)`. The
  carry is what the successor needs and cannot derive: `doc_ids` for a handover, the
  Gmail round's `report` counts for a judge, and `quarter` and `channel` for a snapshot.
  A snapshot's carry goes into its package request, not into this row.
- **Finished:** the specialist writes this **as its last action**:
  `record_step(pass_token, step, action="finish", remaining_in_cycle=…, triage_remaining=…,
  stopped=<reason or omitted>)`.
- **Ellen finishes it instead** only when the delegation returned *to her, inline*: ok
  with no "finished" recorded, or an error. That is in-turn evidence, with no
  correlation involved. She records `action="finish", failed=true` on an error. A
  "finish" that finds the row already finished is a no-op.
- **Expired:** a step with no "finished" whose `started_at` is `STEP_EXPIRY_S` (600 s)
  old is treated as ended. By A2 its delegation is gone.

**`package_requests`**: one row per "give me the quarter" request. It holds `quarter`,
`channel`, the snapshot pass, its outcome, `package_id`, `delivery_id` and `state`. It
outlives its pass, so a restart after the bank read still finds it.

### 3.2 The claim — `continue_pass()`

`continue_pass()` takes no arguments and runs in one write transaction. It looks only at
the store:

1. **The live pass.** Take the live marker, if it is younger than `STALE_AFTER_S`, and
   its latest step. That step is **due** when it is finished or expired **and** its
   claim is free: never claimed, or claimed more than `CLAIM_TTL_S` ago without a later
   step or an `end_pass` superseding it.
   - A live pass with **no** step whose `started_at` is `STEP_EXPIRY_S` old is also due,
     with next step `end-pass`. The turn that began it died before it delegated.
2. **Otherwise, an open package request** whose pass has ended and whose claim is free
   (§3.3).
3. **Claiming** is a compare-and-set:

   ```sql
   UPDATE … SET claimed_at = now, claim_n = claim_n + 1
   WHERE <key> AND claim_n = <read value>
   ```

   Exactly one caller sees one changed row. It gets the continuation. Everyone else gets
   `{"continue": null, …}`.
4. **Nothing due.** The answer is `{"continue": null, "running": {step, trigger,
   started_at, due_in_s}}` when a step is still running, or `{"continue": null}`.

**What a claim returns.** `pass_token` is present only for a claimed live-pass step.

| Claimed | `next` | Inputs returned |
|---|---|---|
| `sweep` | `gmail-round`, or `end-pass` when `can_run` is false, the finish says `stopped`, or the step ended unfinished and the pass imported nothing | `pass_token`, `trigger`, `ended` (`finished`, `expired` or `errored`), the finish counts, `imported`, `throughput`, `can_run`, and `work` = exactly `list_quarter_state(triage=true)`'s answer |
| `judge` | `end-pass` | `pass_token`, `trigger`, `ended`, the finish counts, the carried `report`, `throughput` |
| `handover` | `end-pass-then-case` | `pass_token`, `ended`, and `documents`: for each carried `doc_id`, its **real pairing** from `match_state` (`matched` / `proposed` / `unpaired`; plus `irrelevant`), and for a pairing the payment's date, amount, currency and payee (`views.headline` fields). This is read from the pairing records, never inferred from absence |
| `snapshot` | `end-pass-then-build` | `pass_token`, `ended`, the finish counts, and `request` (id, quarter, channel) |
| (none; live pass older than `STEP_EXPIRY_S`) | `end-pass` | `pass_token`, `trigger`; outcome `failed` |
| a package request | `build`, `stage` or `record-uncertain` (§3.3) | `request` with `package_id` and `delivery_id`; never a pass token |

**The outcome rule for `end_pass`** can be evaluated from these fields in every case:

- **`stopped`**: `can_run` is false, or the finish says `stopped`.
- **`failed`**: the step ended unfinished (`expired` or `errored`) and `imported` is
  false.
- **`interrupted`**: any of the following:
  - `remaining_in_cycle` or `triage_remaining` is above 0;
  - `work.truncated` is true, or `work.not_fresh` is above 0;
  - an item was not searched;
  - the step ended unfinished after the import.
- **`complete`**: none of the above.

**Why a foreign or replayed notice is harmless.** It triggers only a claim, and a claim
only continues a step that is finished or expired. That step is due whatever triggered
the call, and at most one caller wins it.

- A replay after the step was continued finds the claim held, or superseded by the next
  step.
- A foreign notice during a running step finds `running`.

**The token.** The token is released only to the one winner of a claim on the live
pass's due step.

- A stale pass is never live, so it never releases a token.
- An ended pass's package continuation runs without one.
- An actor holding an old token is still refused by the generation fence.
- An unfinished step's specialist that is still alive keeps its token until the pass
  ends. The margin in §4.2 bounds that window, and §6 says what it can do in it.

### 3.3 The package request after its pass

`record_step(step="snapshot", action="start", quarter, channel)` creates the request
(`state='snapshot'`). A newer request for the same quarter supersedes an open one.

Progress is linked by the server, with no new arguments:

- `end_pass` of the request's pass stamps `pass_outcome` (`state='snapshot-done'`).
- `build_quarterly_package(quarter)` sets `package_id` on the open request for that
  quarter (`state='built'`).
- `stage_for_delivery(package_id=…)` sets `delivery_id` (`state='staged'`).
- `record_delivery` closes the request (`delivered` / `uncertain` / `failed`).

The continuation's `next` follows from that state:

| State | `next` |
|---|---|
| `snapshot-done` | `build`, or `tell-stopped` when `pass_outcome` is `stopped` (close without building) |
| `built` | `stage` |
| `staged`, unsettled | `record-uncertain` |

**It never returns "send".** A file that may already have gone out is never sent twice
on a recovery. `record_delivery(outcome="uncertain")` returns the existing `speak`
("may not have arrived"), and "send it again" stays the operator's.

Claims on a request use the same compare-and-set and `CLAIM_TTL_S` as steps.

### 3.4 Who calls `continue_pass`, and what Ellen says

Ellen calls it:

- (a) after every delegation returns inline, ok or error, once any inline "finish" is
  recorded;
- (b) on every system notification about a delegation to finance, of any kind;
- (c) at the start of every check (cron or "go and check now"), every handover and every
  package request, **before** `begin_pass`.

A continuation she gets in (c) is done *instead of* beginning a new pass, and is reported
the way that flow reports. A stranded pass is therefore finished by the next thing that
looks at it.

**On `pending`:** Ellen says the flow's one line (cron: `<silent/>`) and ends the turn.
She records nothing more, because the start was already recorded.

**On a notice with `continue: null`**, what she says depends only on the notice, and
changes no state:

- Stay quiet (`<silent/>`) when the notice is plainly an accounting one: its result
  begins `quarterly-accounting:` (the specialist's fixed first line), or the original
  request was the cron prompt, "go and check now", a package request or a handover. For
  an operator trigger with `running`, she says one line instead: "Still working on the
  check — I'll send the result here."
- Otherwise the notice is someone else's, and she answers it as Casa asks.

## 4. Server changes

- **Schema:** 3 → 4.
- **Tools:** 33 → **35**. Two new tools, `record_step` and `continue_pass`:
  - `record_step` is the store's only *progress* fact. Without it nothing says that a
    step started, when, or that it finished.
  - `continue_pass` is the single compare-and-set claim, which must be a write and
    must release a token. Folding it into `check_setup` would turn every setup read
    into a possible claim and token release. `begin_pass` cannot do it either, because
    notices must be able to continue without beginning.
- **Schema changes to existing tools:** `list_quarter_state` gains `pass_token`
  (optional, clock only). `list_projections` answers gain `time_up`.
- **Removed from revision 1:** `record_delegation` and `check_setup(delegation)`.

### 4.1 `server/db.py`

`SCHEMA_VERSION = 4`. The following goes into `DDL` and, as the same statements, into
`MIGRATIONS[3]`:

```sql
CREATE TABLE IF NOT EXISTS pass_steps (
  pass_id TEXT NOT NULL,
  step TEXT NOT NULL CHECK (step IN ('sweep', 'judge', 'handover', 'snapshot')),
  started_at TEXT NOT NULL,        -- Ellen, before delegate_to_agent: clock + expiry origin
  finished_at TEXT,
  finished_by TEXT CHECK (finished_by IN ('specialist', 'resident')),
  finish_json TEXT,                -- counts, stopped reason, failed flag: never free text
  carry_json TEXT NOT NULL DEFAULT '{}',   -- doc_ids / report: ids and counts only
  claimed_at TEXT, claim_n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (pass_id, step));
CREATE TABLE IF NOT EXISTS package_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  quarter TEXT NOT NULL, channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  pass_id TEXT NOT NULL, pass_outcome TEXT,
  package_id INTEGER, delivery_id INTEGER,
  state TEXT NOT NULL CHECK (state IN ('snapshot', 'snapshot-done', 'built', 'staged',
        'delivered', 'uncertain', 'failed', 'stopped', 'superseded')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  claimed_at TEXT, claim_n INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_package_requests_open ON package_requests(quarter, state);
```

`finish_json.stopped` is the server-side reason: the specialist passes the refusal text
it received, clipped to 300 characters. It is operator-relayable, as refusals already
are.

### 4.2 `server/passes.py`, with a new `server/steps.py`

**Constants:**

| Constant | Value | Meaning |
|---|---|---|
| `CEILING_ASSUMED_S` | 600 | A2 |
| `SWEEP_STOP_S` | 450 | the sweep stops here, at every site |
| `RETURN_BY_S` | 510 | `wrap_up`: 90 s to the ceiling |
| `STEP_EXPIRY_S` | 600 | an unfinished step is ended |
| `CLAIM_TTL_S` | 1800 | a claim with no progress may be claimed again |
| `ROW_COST_S` | 10 | measured; sizes a sweep page |

All six are re-tuned from the first real passes.

**Why `STEP_EXPIRY_S` equals the ceiling.** The stamp precedes Casa's launch, so when the
ceiling's `timeout` notice arrives (at or after 600 s on Casa's clock, plus up to 30 s of
teardown), the step is already expired by ours. That is what lets that very notice
continue the pass. The cost: a specialist can outlive its step by the gap between
Ellen's stamp and Casa's launch, plus teardown (tens of seconds) (§6).

**`steps.py` functions:**

- `start(conn, token, step, carry) -> dict`, in one `db.tx`.
  - The token is required and checked with `check_token`.
  - `step` must be valid for the pass's trigger: `sweep`/`judge` for `cron`/`operator`,
    `handover`, or `snapshot`.
  - The carry is validated per step: `handover` needs `doc_ids` that exist; `judge`
    takes an optional `report` of integer counts; `snapshot` needs `quarter` (canonical)
    and `channel`. Any other key is refused.
  - A step already started in this pass is refused ("already started this pass").
  - `snapshot` also inserts the `package_requests` row and supersedes an open one for
    the quarter.
  - Returns `{"step", "started_at", "return_by", "sweep_stop_at"}`.
- `finish(conn, token, step, *, by, counts, stopped=None, failed=False)`, in one `db.tx`.
  - `check_token`, then the row must exist.
  - Already finished: a no-op that returns the recorded finish.
  - Already claimed as expired: refused ("this step's time was up and the pass has moved
    on; your writes are kept — just return").
  - `by` is `specialist` unless `failed` is set, or Ellen passes `by="resident"`. The
    tool derives this from `failed`: `failed=true` implies `resident`. A specialist's
    finish never sets `failed`.
- `claim(conn) -> dict`: §3.2, in one `db.tx`. Its inputs:
  - `work` comes from `work.list_quarter_state(triage_only=True)`;
  - `documents` comes from `match_state WHERE doc_id=? AND state IN ('matched',
    'proposed')`, plus `documents.irrelevant`;
  - `throughput` comes from `passes.throughput`;
  - `imported` is true when the pass has a `snapshots` row.
- `clock(conn, token) -> dict | None`: read-only. It is `None` unless the token is live
  and the live pass's latest step is started and unfinished. Otherwise it returns
  `{"elapsed_s", "time_left_s": max(0, RETURN_BY_S - e), "wrap_up": e >= RETURN_BY_S}`.
- `sweep_allowance(conn) -> int | None`: `SWEEP_STOP_S` minus the elapsed time of the
  running step. It is `None` when no step is running, which keeps today's unbounded
  paging for the existing sims.

**Hooks into existing functions**, one line each:

- `passes.end_pass` stamps `package_requests.pass_outcome` for its pass, and supersedes
  any claim on the pass's steps.
- `package.build_quarterly_package` sets `package_id` on the open request for its
  quarter.
- `delivery.stage_for_delivery` sets `delivery_id` on the request holding `package_id`.
- `delivery.record_delivery` settles that request.

### 4.3 `server/sweep.py`

`list_projections` works as in revision 1, with the page capped by `sweep_allowance`:
`time_up` returns an empty page, `remaining_in_cycle` keeps its formula, and the cursor
is untouched on a `time_up` page.

### 4.4 `server/work.py`

`describe()` gains `search_hint` and `window_days` from the KB. These are what the
store-derived Gmail round is built from.

### 4.5 `server/tools.py` and the manifest

- **`record_step(pass_token, step, action, quarter?, channel?, doc_ids?, report?,
  remaining_in_cycle?, triage_remaining?, stopped?, failed?)`.** Description: "Ellen,
  just before delegating a step of a pass: action=start (with what the next step needs).
  The specialist, as its very last action: action=finish with its counts, and stopped=
  <the refusal> if it stopped. Ellen finishes it herself only when the delegation
  returned to her in the same turn without a finish (failed=true on an error)."
  Carry arguments are refused on `finish`, and finish arguments on `start`.
- **`continue_pass()`**, with an empty schema. Description: "Call after every delegation
  returns in your turn, on every system notification about a delegation to finance, and
  before starting any check, handover or package. If a pass or a package request is due
  to continue, this claims it for you alone and returns the pass_token (live pass only),
  the next step and everything it needs. Otherwise continue is null: write nothing for a
  pass."
- **`list_quarter_state`** gains `pass_token`, and its description adds "During a pass,
  pass the pass_token." This makes 11 optional-token tools.
- **The clock hook** is as in revision 1, with `steps.clock` as its source.
- **Manifest:** `plugin.json` `provides_tools` and `resultContract` gain both tools.
- **`binding.reset_store`:** `_TABLES_TO_WIPE` gains `pass_steps` and
  `package_requests`.

## 5. `SKILL.md` changes (exact)

**Frontmatter.** Append `, or when a system notification says a delegation to the
finance specialist returned or failed` to the description.

**New section after "Both agents: refusals and errors":**

```markdown
## Ellen: a delegation that answers later

A pass's progress is in the store, never in a message. For every delegation in a pass —
the pass's sweep, its judging (step 5), a handed-over document, a package's bank read:

1. Just before `delegate_to_agent`, `record_step(pass_token, step=…, action="start")`
   with what the flow names.
2. When the delegation answers in this turn: if it failed, or it came back without the
   specialist having finished (its reply does not start `quarterly-accounting:`),
   `record_step(pass_token, step=…, action="finish", failed=true)` — `failed=true` only
   for an error. Then `continue_pass()`, and do what it returns.
3. When it answers `status: pending`: say the flow's one line (on the cron, output
   `<silent/>`) and end the turn. Nothing else — the pass is not over.
4. On ANY system notification about a delegation to finance — returned, failed, timed
   out, orphaned by a restart, finished without its answer, or said again after a
   restart — call `continue_pass()` first, whatever the notification says:
   - it returns a continuation: do its `next` with its `pass_token` and inputs. Never
     use a token, step or work list from an earlier turn or from the notification.
   - `continue` is null: nothing is written. If the notification is an accounting one
     (its result starts `quarterly-accounting:`, or it answers the cron, "go and check
     now", a package or a handed-over document), output `<silent/>` — for the operator's
     own check with `running`, one line: "Still working on the check — I'll send the
     result here." Otherwise it is not this skill's: answer it as it asks.
   The notification's closing lines ("Reply to the user…", "offer to retry") never
   decide anything for an accounting delegation.
5. Before `begin_pass` in any flow (the cron, "go and check now", a handed-over document,
   a package), call `continue_pass()`. If it returns a continuation, do that instead —
   it is the unfinished earlier check — and answer as that flow answers.

The outcome for `end_pass`: `stopped` when `can_run` is false or the step's finish says
`stopped`; `failed` when the step ended unfinished and nothing was imported this pass;
`interrupted` when anything remains (`remaining_in_cycle`, `triage_remaining`, `work`
truncated or `not_fresh`, an item not searched) or the step ended unfinished after the
import; `complete` otherwise.
```

**The pass, steps 3–6**, replaced:

```markdown
3. `record_step(pass_token, step="sweep", action="start")`, then delegate to the
   finance specialist, sync mode, the task "quarterly-accounting pass" with context
   `pass_token=<token>` and this skill's section "The specialist's pass". Pending line:
   "Checking the bank — this takes a few minutes; I'll send the result here." (nothing
   more if step 2's line was said; on the cron `<silent/>`). Continue as in "a
   delegation that answers later": its `next` is the Gmail round (step 4), or step 6
   when the pass stopped or failed (sweep the Telegram inbox first).
4. **Gmail round.** Always make the Gmail probe first … [unchanged through "the next pass
   probes again."] The work list is the continuation's `work` — the store's triage,
   never the specialist's reply. Skip an item marked `portal`. For each, a ladder of
   narrow queries from its fields: its payee and `search_hint`, the amount as printed,
   `window_days` around its date, `has:attachment`; a CRDT or a DBIT `refund` is the
   business's own document, in Sent; ignore payment confirmations. … [download, ingest,
   `record_search` unchanged]. `work`'s `remaining` and `not_fresh`, and items you never
   reached, are `not_searched`. [self-mail search and Telegram inbox sweep unchanged]
5. If anything was filed, or the continuation's `triage_remaining` is above 0 or the
   sweep ended unfinished: `record_step(pass_token, step="judge", action="start",
   report={checked, total, not_searched})`, then delegate "judge the newly filed
   documents and the payments triage did not reach" with the same `pass_token` (the
   specialist's steps 6 and 7). Pending: `<silent/>`. Its continuation says `end-pass`
   and returns your `report`.
6. `end_pass(pass_token, outcome, report)` — the outcome rule above; `report` is
   `{checked, total, not_searched}` (the continuation's `report` after step 5). [speak /
   `<silent/>` / status handling unchanged]
```

**Handover, step 2.** After the `begin_pass` bullet, add
"`record_step(pass_token, step="handover", action="start", doc_ids=[<doc_id>])`, then".
The pending line is "Filed. Checking it against the payments — I'll tell you shortly."
Replace the outcome list with: "then `end_pass` with the outcome rule — always, whatever
came back, in this turn or on the continuation."

**Handover, step 3.** Add: "The case comes from the continuation's `documents`, the
pairing actually recorded, and the specialist's reply only adds which unpaired case it
is:

- `matched` → the matched line, with its payment's date and amount;
- `proposed` → "Filed. It could fit more than one payment — it's in 'anything I should
  check?'.";
- `unpaired` → the reply's case if there is one, otherwise "Filed. I'll match it at the
  next check."

Never infer a match from anything else."

**The specialist's pass.** Add a paragraph after the first one:

> "Every answer to a call that carries the `pass_token` may include `clock`. At
> `wrap_up`, finish the item in hand and finish the step. Your last action, always,
> is `record_step(pass_token, step=<the step you were given>, action="finish",
> remaining_in_cycle=…, triage_remaining=…)`, adding `stopped=<the refusal>` if you
> stopped. Your reply then starts `quarterly-accounting: <step> finished`. If Casa
> cuts you off first, everything you recorded is kept."

Further changes to "The specialist's pass":

- **Step 5:** "until `remaining_in_cycle` is 0 or it says `time_up`". In the batches
  paragraph, "When the budget runs out" becomes "When `time_up` or `wrap_up` comes".
- **Step 6:** `list_quarter_state(triage=true, pass_token=…)`.
- **Step 8** is replaced by: "**Finish** (as above) and reply briefly. Ellen searches
  from the store, never from your reply. A search idea the payment's own facts do not
  carry goes into the KB: `upsert_counterparty(name, search_hint=…, pass_token=…)`."
- **The handover and package delegations** gain "finish the step as your last action"
  with the step name.

**Packaging, step 1.** After `begin_pass`:
"`record_step(pass_token, step="snapshot", action="start", quarter=<the quarter>,
channel="telegram"|"email")`". The pending line is "Reading the bank first — the
<quarter> package follows in a few minutes." The continuation then drives the rest:

- `end-pass-then-build`: `end_pass` (outcome rule), then step 2 for `request.quarter`
  on `request.channel`.
- `build` or `stage`: resume step 2 at that point.
- `record-uncertain`: `record_delivery(delivery_id, outcome="uncertain")` and send its
  `speak`. Never send the file again yourself.
- `tell-stopped`: say why, and build nothing.

**Pinned tests.** `tests/test_skill.py` changes with the fix:

- `test_ellen_holds_…` pins the outcome-rule sentence instead of "`failed` if the
  delegation errored or ran out of turns".
- `test_the_sweep_is_batched…` pins "`time_up`".
- New pins:
  - "call `continue_pass()` first";
  - "Never infer a match";
  - "Never send the file again yourself";
  - the specialist's finish sentence.
- `OPERATOR_LINES` gains the pending lines, "Still working on the check — …" and the
  `proposed` handover line.

## 6. Invariants the design keeps

- **The stale-pass fence.** The generation check is unchanged, and `continue_pass` never
  releases the token of a pass that is not live. For the live pass, it releases the
  token to exactly one claimant of a due step. Every other caller, whether a replay, a
  foreign notice or a second session, gets `continue: null`. An ended pass's package
  work needs no token and gets none.
- **An expired step's specialist.** If A2 holds, it is gone within tens of seconds of
  expiry (§4.2). Until then its writes are ordinary writes of the live pass: the same
  gate and the same one-write-then-read discipline, each observation atomic. Its
  `finish` is refused and the pass has moved on. It cannot end the pass, and once Ellen
  ends it, every later write it makes is refused by the fence. If A2 is false, that
  window grows by however much longer the delegation lives, with the same bounds. This
  is stated, not closed.
- **Idempotent continuation.** A claim can be taken again after `CLAIM_TTL_S` (A6), so a
  step may run twice. That is safe:
  - ingest is idempotent by hash;
  - `record_search` counts a fruitless pass once per `pass_id` and de-duplicates queries;
  - `record_probe` replaces its row;
  - `end_pass` succeeds once;
  - a request never re-sends: a staged, unsettled delivery becomes `uncertain`.
- **The bank-write gate** is untouched. A `time_up` page asks for no write.
- **Freshness (#1 as it stands)** is unchanged. The time stop stamps nothing. The Gmail
  list is the `fresh_only` triage. The cursor rotates across passes, so search-and-match
  converges pass by pass. A complete-quarter package still needs #1.
- **D3 binding** is unchanged. Views on continuation turns are sent, then
  `mark_rendering_delivered`.
- **The 4096 fit.** There is no new server-produced operator text. `continue_pass`,
  `clock` and `time_up` are machine fields. `finish_json.stopped` is a refusal text of
  at most 300 characters, relayed as refusals already are.
- **Ellen holds every pass.** The specialist writes "finish", never `end_pass`.

## 7. Tests (through `qa_server.handle`; specialist side via `ToolFlow`)

The clock is `mock.patch.object(db, "_clock", …)`, advancing.

1. **Pending, then the notice.**
   - Setup: `begin_pass(operator)` and `record_step(sweep, start)`. The specialist's
     probes, import, sweep and `record_step(sweep, finish)` all run through the tools.
   - Check: `continue_pass()` returns the begun token, `next == "gmail-round"`,
     `ended == "finished"`, and `work` equal to `list_quarter_state(triage=true)`.
   - Then the Gmail round and `end_pass`. The `gmail` probe carries the pass id.
2. **The ceiling.**
   - Rows cost `ROW_COST_S` on the clock. The pages shrink, then `time_up` comes. No row
     is read after `SWEEP_STOP_S`, and `wrap_up` comes at `RETURN_BY_S`.
   - The specialist is cut with no finish. At 599 s, `continue_pass` gives `running`;
     at 600 s it claims, with `ended == "expired"` and `imported` true. The outcome
     rule gives `interrupted`, and `swept_this_pass > 0`.
   - The late `finish` is refused.
3. **Inline error before and after the import.** Ellen's `finish(failed=true)` makes the
   step due at once, with no wait for expiry. Before the import: `imported` is false,
   `next` is `end-pass`, and the outcome is `failed`. After the import: `gmail-round`.
4. **A foreign notice.** `continue_pass` during a running step answers `continue: null`
   with `running`, and writes nothing (the store digest is unchanged). With no pass
   live, it answers `continue: null`.
5. **A replay after a restart.** After a claim and `end_pass`, `continue_pass` again
   answers null. A replay while step 5 runs finds `judge` running and answers null.
6. **A restart between launch and the first write.** The step is started and the
   specialist writes nothing. At `STEP_EXPIRY_S` the next `continue_pass` (a new check,
   test (c)) claims it, and `next` is `end-pass` (outcome `failed`). There is also the
   variant with no step at all after `begin_pass`, which is also claimed at expiry.
7. **Two sessions racing the claim.** Two connections (two `db.open_store()`, as in
   `_procs.py` across processes) call `claim` on the same due step. Exactly one gets a
   token and the other gets null. After `CLAIM_TTL_S` with no progress, one re-claim
   succeeds.
8. **Package successor after a restart.**
   - The snapshot is finished and `continue_pass` gives `end-pass-then-build`. `end_pass`
     runs, then the "restart" (a new connection, no memory).
   - The next `continue_pass` gives `build` for the request's quarter and channel, with
     no pass token. `build_quarterly_package` links, and the next claim gives `stage`.
   - After `stage_for_delivery`, a restart gives `record-uncertain`, never a send.
     `record_delivery(uncertain)` closes it and returns `speak`.
   - A stopped snapshot gives `tell-stopped`.
9. **Handover pairing state.** The `documents` field reports `matched` with the payment's
   facts, `proposed`, `unpaired` and `irrelevant` from real records. There is also a
   case with more than 50 unmatched documents in which the handed-over one is still
   reported correctly, which pins that nothing is inferred from `list_unmatched_documents`.
10. **Cron.** Test 1 with `trigger="cron"`: `speak` is None, or fits in 4096.
11. **Two passes and a stale marker.** While A is live, `begin_pass` is `busy`. After the
    3 h reclaim, `continue_pass` never returns A's token, and A's `end_pass` is refused.
12. **Contract.** `record_step` refuses, each with a test:
    - a missing or stale token;
    - a wrong step for the trigger, or a second start of the same step;
    - carry arguments on a finish.

    `continue_pass` never carries `pass_token` except on a live-pass claim.
13. **Clock and back-compatibility.** No clock without a running step, or for a stale
    token. `list_projections` pages unbounded with no step recorded, so every existing
    sim test is untouched.
14. **Schema and surface.**
    - Freeze `DDL_V3` (schema 3, `server/db.py` at `e9b4eff`) and test v3 → v4.
    - `reset_store` wipes both new tables.
    - `EXPECTED` gains the 2 tools, and both counts become 35. The manifest agrees.
    - The skill's calls and arguments exist, with the pins as in §5.

## 8. Spec text to add (`2026-08-10-quarterly-accounting-design.md`)

**A new subsection under "## Casa baseline", "### Delegation timing — design
assumptions".** It is prefixed with the following, and then A1–A6 from §2, verbatim:

> "This plugin is built on the following behaviour of Casa's `delegate_to_agent`, as read
> in Casa v0.331.0–v0.332.0. They are assumptions about another project, and each says
> what this code does if it stops holding."

**§Weekly pass, step 3.** Replace "The only real ceiling is the specialist's `max_turns`
(70) and the pass's own wall-clock…" with:

> "The binding ceiling is the delegation's wall clock (A2), not its turns. Each step's
> start is stamped before it is delegated. Every answer the specialist gets carries the
> time left. The sweep stops for time by itself, and the specialist finishes its step
> well inside the ceiling. Ellen's Gmail round is derived from the store, never from the
> specialist's reply."

**§Running the pass on demand.** Add after the marker paragraph:

> "A pass spans turns. Its progress is recorded in the store as steps started, finished
> or expired. Whoever looks next, whether a notification, a check, a package or a
> handover, claims the due step with a compare-and-set and continues it. No
> notification is ever matched to a delegation. A late or repeated notification can only
> continue what is due anyway. A package request is its own record, and it outlives its
> bank pass."

**§Tool surface.** The heading becomes "(server, 35 tools)". Add an erratum line for
`record_step` and `continue_pass`, with §4's reason.

**§Open items, background jobs.** Append:

> "Evidence arrived 2026-09 (#2): the wall-clock ceiling cut two of three catch-up
> delegations. It is fixed within delegations (store-recorded steps plus a time budget).
> Jobs stay out unless a budgeted pass still cannot converge."

## 9. Failure modes considered and rejected

- **Correlating notifications to delegations** (revision 1: Casa id, prefix match,
  attach after `pending`). Rejected after round X1. The notice carries only 8
  characters, so an unrelated delegation can collide. Opening before attaching races.
  A restart before the attach strands the pass. An inline error never attaches. And
  every fix adds another case. Store-recorded progress plus a claim does not need to
  know which notice is which.
- **Parsing the work order from the notice text.** An error carries none, and a replay
  may carry "no answer". That was the bug.
- **Ellen's conversation memory.** A notice is a new turn, possibly after a restart. The
  goals exclude it.
- **`check_setup` exposing the live token.** Every reader would get it, with no
  exclusivity. The claim gives the token to one continuer, only while a step is due.
- **Inferring handover success from absence in `list_unmatched_documents`.** That list is
  capped at 50, and a `proposed` pairing also removes a document from it (Astra S1). So
  the handover reads real pairing records.
- **Ending the package pass and relying on memory for build and send.** An ended pass
  cannot release a write token, and must not. So the request is its own record,
  continued without one (Astra S2).
- **Auto-resending a staged package on recovery.** The file may already have arrived. So
  recovery records it `uncertain`, and "send it again" stays the operator's.
- **A row-count budget.** Casa enforces time, not rows. `ROW_COST_S` is used only to size
  pages.
- **A shorter pass reclaim.** No longer needed as a fix: step expiry and claim TTL
  recover a stranded pass at the next look, and the 3 h reclaim stays as the backstop.
- **`mode="async"`.** It is equivalent under this design and not load-bearing. Sync
  keeps a quick handover answered inline.
- **Casa background jobs.** A second engagement model, with per-batch topic messages,
  against a silent, pull-only cron. Not needed for #2 (§8).
- **The server ending a pass on a timer.** The server is a per-session stdio process with
  no scheduler. Expiry is evaluated when somebody looks.

## 10. Residuals and open questions

- **R1 — silent notices replay (A4).** Continuation makes a replay harmless: it finds
  nothing due, or a claim held or superseded, and ends `<silent/>`. But each retained
  notice costs one resident turn per Casa restart, indefinitely. That is every cron-pass
  delegation that outlived 60 s, and the judge's too. Draft Casa issue, for the
  controller to take to the operator before filing:

  > **Title:** A delegation outcome is re-announced after every restart when the resident
  > deliberately stays silent
  >
  > **What happens.** When a delegation finishes after the sync wait, its outcome is
  > announced to the delegating resident as a new turn. That announcement is a durable
  > obligation: it is discharged only when the turn's reply is delivered
  > (`agent.py` `_ack_delivery`, v0.332.0). A turn that ends in `<silent/>` delivers
  > nothing, so the obligation stays owed, and the boot reconciler re-announces it after
  > every restart (`replayed_after_restart`). A scheduled resident that delegates and
  > then correctly stays silent (a weekly background job whose doctrine is "say nothing
  > unless something needs the operator") therefore gets every such notice again after
  > each restart, indefinitely. Each costs a resident turn, and each invites the model to
  > narrate a stale result.
  >
  > **Expected.** A turn that handled the notice and chose silence discharges it, like a
  > delivered reply does. One way: the silence gate that already suppresses a
  > `<silent/>` turn also acknowledges a synthesized delegation turn when that turn ended
  > without an error kind. That keeps "never lose an announcement" for crashed or errored
  > turns, and ends the replay loop for deliberate silence.
  >
  > **Who is affected.** Any plugin whose cron flow delegates longer than the 60 s sync
  > wait (e.g. casa-plugin-quarterly-accounting #2).
- **R2 — a notice-less stall.** If a step expires and no notice arrives (A3 false), the
  pass continues at the next check, handover or package. A cron pass may then wait a
  week, and "go and check now" finishes it at once. That is accepted.
- **Q1 — release number.** Two tools plus a migration suggests v0.2.0. Any bump changes
  `WORKFLOW` to `acct@<new>`, and `check_setup` then reports `acct@0.1.0`'s writes as an
  older workflow still present. That is a report, not a stop. The test-install reset
  loop asks which backup to restore. Is that acceptable?
- **Q2 — budget numbers.** The values 450 / 510 / 600 / 1800 / 10 come from one
  measurement. Re-tune them from the first passes after the fix (`swept_this_pass`, the
  step's elapsed time).
