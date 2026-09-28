# Issue #2 — a pass that outlives its delegation: continuation design

Status: design, not implemented. Revision 3, after design rounds X1 (on `8b055f5`) and
X2 (on `32a36c9`).

- Issue: bonzanni/casa-plugin-quarterly-accounting#2 (bug, severity:high).
- Branch: `fix/issue-2`, base `e9b4eff` (v0.1.0).
- Out of scope: #1 (freshness). It is ruled, and blocked upstream on
  casa-specialist-finance#86.

**History.**

- **Revision 1** matched Casa's notifications to our delegations by their id. Round X1
  found that one mechanism failing six ways, so it was removed rather than hardened.
- **Revision 2** kept progress in the store and added one compare-and-set claim. Round X2
  found three findings of one shape: **a claim carried no fencing generation**, so a
  superseded holder could still write. These were a stale claimant ending a pass under a
  running judge, a stale claimant staging a package twice, and ending the snapshot
  opening a second continuation.
- **Revision 3** generalises instead of patching (controller ruling). **Every successful
  claim rotates the token.** The token is the one integer the existing stale-pass fence
  already checks on every write. Whatever a superseded holder does is refused by the
  same check that refuses a reclaimed pass today. The package request gets its own
  token under the same rule. There is no separate claim-ownership check anywhere.

## 1. Root cause

The skill assumes that `delegate_to_agent(mode="sync")` returns the specialist's answer
in the same resident turn. Two facts break that.

1. **The answer can arrive on a later turn.** A sync delegation still running after
   about 60 s returns `status: pending`. Its outcome arrives later, as a new resident
   turn whose prompt is a system notification. That notification carries the result
   text on ok, or only the failure kind on error. It carries no `pass_token`, no step
   and no work order. `SKILL.md` has no instruction for that turn:
   - on pass 2 in production, Ellen closed the pass as if the delegation were the
     whole pass;
   - on passes 1 and 3 she had nothing to work from.
2. **The Gmail round is fed by text that a cut delegation never produces.** Step 4
   searches "each item in the work order's `search` list". That list exists only in the
   specialist's final reply, and a delegation cut at the ceiling has no final reply.
   The sweep is bounded by "turn budget", but turns are not what binds. The wall clock
   is, and nothing measured it.

The pass's *where-are-we* state lived in a message the platform does not guarantee. The
fix moves it into the store, next to the pass's work (observations, matches, search
bookkeeping, freshness), which is already there.

## 2. Design assumptions (Casa behaviour this code is built on)

These are stated as assumptions: the repo is public, and they describe another project's
code as read at v0.331.0–v0.332.0. Each says what this code does if it is false. §8
carries them into the spec.

- **A1 — the 60 s degrade.** A sync delegation still running after about 60 s returns
  `{"status": "pending", …}`, and its outcome arrives on a later resident turn.
  - *If false*, the answer is inline. Ellen calls `continue_pass` in the same turn, which
    is the same path.
  - *If the degrade is shorter*, nothing changes.
- **A2 — the 600 s ceiling.** A delegated turn is cancelled about 600 s after launch, and
  the resident receives an error of kind `timeout` with no result text. The specialist's
  clock and the step's expiry are measured from a stamp Ellen writes *before* she
  delegates, so our clock runs ahead of Casa's.
  - *If the ceiling is longer*, a delegation does less than it could. Once its step has
    been continued, the rotated token refuses its writes (§6).
  - *If the ceiling is shorter than the budget*, the delegation is cut, and the step
    expires at `STEP_EXPIRY_S` anyway.
- **A3 — every outcome produces a resident turn.** This holds for ok, error, restart
  orphan, and "notice does not carry its answer". This design reads nothing from that
  turn's content.
  - *If false* (the notice is lost), the pass continues at the next check, handover or
    package (§3.5). The reclaim after 3 h is the backstop.
- **A4 — a notice can arrive more than once, or late.** Casa re-announces an outcome
  after a restart until a turn *delivers* a reply. A turn that ends `<silent/>` delivers
  nothing (`agent.py` `_ack_delivery`), so a cron pass's notices come back at every
  restart.
  - Each replay calls `continue_pass`, which can only continue what is due anyway (§3).
    Each one still costs a resident turn: residual R1, and §10 drafts the Casa issue.
- **A5 — a scheduled turn's notice can still deliver.** The scheduled-delivery marker
  travels with the notice.
  - *If false*, on a cron pass `speak` stays pending in `alerts` and goes out at the next
    `end_pass`, which is today's behaviour. A package continuation on such a turn fails
    its send and records it `uncertain` (§3.4).
- **A6 — Ellen's own turn is not bound by A2.** If her continuation dies, its lease
  lapses after `LEASE_S`. The next `continue_pass` then claims again and rotates the
  token, and anything the dead holder's turn still attempts is refused (§3.2).

**Verified for round X1 (Terra S1).** Casa runs every turn under a per-session write gate
(`session_write_gate(channel_key)`, around line 1965 of `agent.py`), keyed by
`(channel, role, chat_id)`. A notice is built from the delegation's recorded origin
channel and chat id, so it normally lands on the delegating turn's own key and waits
behind it. This design does not rely on that. The step's start is written before
`delegate_to_agent`, and concurrent continuers are separated by the rotating claim, not
by turn order.

## 3. The mechanism

### 3.1 The token is the fence, and it now rotates

**Today:**

- `counters.pass_generation` is a monotonic integer. `begin_pass` bumps it.
- The marker stores the value it was bumped to (`pass_marker.generation`), and that
  value is the `pass_token`.
- `check_token(conn, token)` refuses unless `token == pass_marker.generation` and the
  marker is live.

**Revision 3 changes one thing. The same counter is also bumped by every successful
claim** (`continue_pass`, and the package authority transfer in `end_pass`, §3.4). The
new value becomes the live token, stored in `pass_marker.generation` for a pass and in
`package_requests.token` for a package request.

- **One integer, no composite.** The token is one integer drawn from the one monotonic
  counter. No two holders, of any pass, claim or request, ever hold the same value. So
  a token needs no pass part and no claim part: equality with the current holder's
  value *is* the whole check.
- **What stays the same.** `passes.generation` keeps the value from `begin_pass`. It
  identifies the pass and orders `last_pass`. `pass_id` is unchanged.
- **The one new function** is `check_package_token(conn, request, token)`: it refuses
  unless `token == request.token` and the request is open.

**What rotation fences.** After a claim, the specialist of the step being continued and
any earlier claimant hold stale values. Their `record_step`, observations, matches,
searches, probes, filings with a token, and `end_pass` are all refused by the existing
check, with the existing wording: "this pass is no longer the current one … stop —
nothing was written".

This also closes the revision 2 window in which an expired step's specialist wrote
alongside Ellen.

### 3.2 What the store records, and when a step is due

**`pass_steps`** holds one row per delegated step of a pass. The steps are `sweep`,
`judge`, `handover` and `snapshot`.

- **Started.** Ellen writes the start just before `delegate_to_agent`, with the token she
  holds: `record_step(pass_token, step, action="start", …carry)`. She passes that same
  token to the specialist.
- **Finished by the specialist.** As its last action, the specialist writes
  `action="finish"` with its counts (`remaining_in_cycle`, `triage_remaining`) and
  `stopped=<refusal>` if it stopped.
- **Finished by Ellen.** She writes `action="finish"` herself only when the delegation
  returned to her in the same turn without a finish. On an error she adds
  `failed=true`.
- **Expired.** A step with no finish whose `started_at` is `STEP_EXPIRY_S` (600 s) old.

**The claim lives on the marker:**

- `pass_marker.claimed_step` records which step the current token continues;
- `pass_marker.lease_at` records when the holder last made progress.

`check_token` refreshes `lease_at` whenever it accepts a token inside a write
transaction. So a holder that keeps writing keeps its lease, and a holder that has gone
quiet for `LEASE_S` (600 s) loses it. That is short on purpose (X3): a continuation that
crashes right after claiming is recovered by the next notice or "go and check now"
within ten minutes (R4).

**A live pass is due** when either:

- its latest step is finished or expired, and either `claimed_step` is not that step or
  `lease_at` is `LEASE_S` old; or
- it has no step at all, `started_at` is `STEP_EXPIRY_S` old, and there is no fresh
  lease. That is the turn that began the pass dying before it delegated.

### 3.3 `continue_pass()` — the single claim

`continue_pass()` takes no arguments and runs in one write transaction:

1. **The live pass**, if it is younger than `STALE_AFTER_S`, and due: bump the counter,
   then set `generation = <new>`, `claimed_step = <latest step or 'none'>` and
   `lease_at = now`, and return the continuation below.
2. **Otherwise, an open package request** whose lease has lapsed: bump the counter,
   set `request.token = <new>` and `lease_at = now`, and return its continuation (§3.4).
3. **Otherwise:**
   - `{"continue": null, "running": {step, trigger, started_at, due_in_s}}` while a step
     runs;
   - `{"continue": null, "held": true}` while a fresh lease is held;
   - `{"continue": null}` when there is nothing.

The transaction is `BEGIN IMMEDIATE`, so two callers serialize. The second one sees the
lease the first one wrote and gets null.

**What a claim returns.** Every pass continuation returns the **new** `pass_token`, the
`trigger` and the `reply` (§3.6).

| Claimed | `next` | Inputs returned |
|---|---|---|
| `sweep` | `gmail-round`, or `end-pass` when the pass stopped or nothing was imported | `ended` (`finished` / `expired` / `errored`), finish counts, `imported`, `throughput`, `can_run`, `work` = exactly `list_quarter_state(triage=true)`'s answer |
| `judge` | `end-pass` | `ended`, finish counts, the carried `report`, `throughput` |
| `handover` | `end-pass-then-case` | `ended`, `documents`: for each carried `doc_id`, its **real pairing** from `match_state` (`matched` / `proposed` / `unpaired`, plus `irrelevant`). A paired document also carries its payment's date, amount, currency and payee. Never inferred from absence |
| `snapshot` | `end-pass-then-build` | `ended`, finish counts, `request` (id, quarter, channel) |
| none (the pass began and never delegated) | `end-pass` | outcome `failed` |
| a package request | §3.4 | `package_token`, `request`, never a pass token |

**The `end_pass` outcome** is computable from these fields in every case:

| Outcome | When |
|---|---|
| `stopped` | `can_run` is false, or the finish says `stopped` |
| `failed` | the step ended unfinished (`expired` or `errored`) and `imported` is false |
| `interrupted` | anything remains (`remaining_in_cycle`, `triage_remaining`, `work.truncated`, `work.not_fresh`, an item not searched), or the step ended unfinished after the import |
| `complete` | otherwise |

**A foreign or replayed notice can only claim a continuation that is due anyway.** The
claim rotates the token, so whoever continues is the one live holder. Terra's X2
foreign-session finding (A's result reported in B's session) is **accepted by ruling**:
there is one operator, and the writes are the pass's own. The report goes to the channel
the pass recorded (§3.6).

### 3.4 The package request and its token

The flow of a package request:

1. **Creation.** `record_step(step="snapshot", action="start", quarter, channel)` creates
   the request with `state='snapshot'` and no token. A newer request for the same quarter
   supersedes an open one.
2. **Authority transfer, inside `end_pass`.** When `end_pass` ends a pass that owns an
   open request, in the same transaction that ends the pass it:
   - bumps the counter;
   - sets `request.token = <new>`, `lease_at = now` and `pass_outcome = <outcome>`;
   - sets `state` to `snapshot-done`, or to `stopped` when the outcome is `stopped`;
   - when the outcome is `stopped`, raises the request's **package notice**
     (`package-stopped`, §3.7) in the same transaction, instead of asking Ellen to tell
     it;
   - returns `{"package_token": <new>, "request": {…}, "next": "build" | null}` to the
     caller of `end_pass`, with `speak` as always.

   There is no window in which the request is claimable: its lease is fresh from the
   instant the pass ends.
3. **Token-bound writes.** Each accepted write refreshes the request's `lease_at`:
   - `build_quarterly_package(quarter, package_token)` needs the current token of the
     open request for that quarter. It links `package_id`, and `state` becomes `built`.
   - `stage_for_delivery(channel, package_id, package_token)` needs the token of the
     request that holds `package_id`. **It is idempotent:** if the request already has
     a staged, unsettled, unrevoked delivery, it returns that delivery (same
     `delivery_id`, path and `request_id`) instead of inserting a row. Otherwise it
     stages, and `state` becomes `staged`.
   - `record_delivery(delivery_id, outcome, package_token)` needs the token of the
     request that holds that delivery. It closes the request as `delivered`,
     `uncertain` or `failed`. When it records `uncertain` or `failed`, **the same
     transaction** enqueues the package notice (`package-uncertain` or
     `package-send-failed`, §3.7) and returns its rendering as `speak`. It no longer
     returns a separate offer rendering. A crash before that `speak` is sent loses
     nothing: the occurrence is offered again (X4).
   - **Token checks happen in the committing transaction** (X4 rule, general; §4.3). A
     check made before a lock wait (the custody lock, the SQLite write lock) is only an
     early refusal. The binding check is repeated inside the final transaction that
     registers or links the result. If it refuses there, whatever was prepared outside
     that transaction and is still unregistered (the built zip, the staged copy) is
     deleted before the refusal returns. So a holder rotated while it waited can never
     link a package, stage a delivery or settle one.
   - **Every new delivery gets a never-reused staged basename** (X4). For Telegram, the
     outbox file is `qa-<16 random hex>.zip`, or the document's extension, drawn afresh
     for every new delivery row: first sends, resends ("send it again") and single
     documents. It is never the package's display name, so a resend never recreates a
     path a superseded holder may still hold.
     - The operator-facing name travels as `send_media`'s `filename` argument, with the
       caption as before. That argument exists at Casa v0.332.0: the `send_media`
       schema has `path`, `kind`, `caption` and `filename`, and "an explicit arg else
       the path basename" (`tools.py`, around lines 558–720). The stage answer returns
       `filename` for Ellen to pass.
     - *Fallback, if a Casa release drops the argument:* the operator would receive the
       random name. The display name then goes first in the caption, and the send is
       otherwise unchanged. Correctness does not depend on the name.
     - Email is already unique per publish: `casa_handoff` creates a new `<id>/`
       directory for every publish, and the attachment keeps its human filename.
     - `_to_outbox`'s "same bytes at the same name are reused" branch becomes
       unreachable for new deliveries and is removed. Idempotent staging of the **same**
       delivery (§3.4) returns that delivery's recorded path; it does not look up a
       name.
4. **Token-free paths stay token-free.** A resend ("send it again": `resend=true`) and a
   single document (`doc_id`) are not request-bound.
5. **Reclaim.** `continue_pass` claims a request whose lease has lapsed, rotates its
   token and returns `next` by state:

   | State | `next` |
   |---|---|
   | `snapshot-done` | `build` |
   | `built` | `stage` |
   | `staged` (unsettled) | none: the claim itself settles it (below) |

   Terminal states are never claimed: `stopped`, `recovery-failed`, `revoked`,
   `withdrawn`, `delivered`, `uncertain`, `failed` and `superseded`. Whatever the
   operator is owed about them is a package notice (§3.7), not a step.

   - **A staged request is withdrawn before it is settled** (X3 ruling). A superseded
     holder may still have the staged path in hand, and `send_media` or `send_email`
     checks no token of ours. So a claim that finds `state='staged'` does all of the
     following:
     1. It takes `db.custody_lock()` **first**, then the write transaction (the store's
        lock order).
     2. It removes the unconsumed staged bytes: the Telegram outbox file, or this
        plugin's own handoff entry for email. This is the same removal the import's
        `withdraw_revoked` does, refactored into one helper,
        `delivery.withdraw(conn, rows)`, which both call.
     3. It stamps `deliveries.withdrawn_at` and settles the delivery `uncertain`: the
        stalled holder may already have sent it.
     4. It moves the request to `withdrawn` and raises its package notice
        (`package-uncertain`), whose text is the existing offer ("may not have arrived …
        say 'send it again'").
     5. It returns `next: null` with that `speak`.

     If the removal fails (an OSError other than "already gone"), the whole claim rolls
     back and is refused, with "could not take back a staged package — nothing
     changed", exactly as the import refuses. A superseded holder's later `send_media`
     fails because the file is gone, and its `record_delivery` is refused by the token.
     A later explicit "send it again" stages afresh from the package retained in
     `packages/` (`resend_target` → `stage_for_delivery(resend=true)`): a new copy and a
     new delivery row.
   - **`revoked`.** An import withdrew the staged send (fix E5). `revoke_superseded_first_sends`
     now also moves the request to `revoked` and raises `package-revoked`, whose line is
     the existing "The bank was re-read before I could send it — ask for it again and
     I'll rebuild it."
   - **Recovery never sends.** A file that may already have gone out is never sent twice
     by recovery, and "send it again" stays the operator's.

### 3.5 Reclaiming a stale pass

`begin_pass`, finding a live marker older than `STALE_AFTER_S`, does all of the following
in its existing transaction, before it bumps the generation for the new pass:

- **Terminalizes the displaced pass**: `ended_at = now`, `outcome = 'interrupted'`,
  `report_json` stamped `{"reclaimed": true}` plus `throughput`. The pass no longer looks
  unended, and `last_pass` shows it.
- **Recovers its package request**, if it has an open one:
  - if its `snapshot` step finished, the request becomes `snapshot-done` with no token
    and a lapsed lease, so the next `continue_pass` claims `build`;
  - otherwise it becomes `recovery-failed` and raises its package notice
    (`package-failed`: "I couldn't read the bank for the <quarter> package — ask for it
    again."), which is delivered once through §3.7.
- **Returns** `"reclaimed": true` as today, plus `"recovered": <request id or null>`.

### 3.6 Who calls it, what Ellen says, where

`begin_pass(trigger, reply)` records `reply` on the pass: `"silent"` for the cron, and
`"telegram"` for the operator, a handover or a package. `continue_pass` returns it. The
continuation reports on that channel when it can: `silent` means `<silent/>` except
`speak`. Otherwise it reports on the current one.

Ellen calls `continue_pass`:

- after every delegation that returns in her turn, once any inline "finish" is recorded;
- on every system notification about a delegation to finance, of any kind;
- **before** `begin_pass` in every check, handover and package request.

A continuation she gets that way is done *instead of* beginning a new pass.

- **`pending`.** She says the flow's one line and ends the turn. On the cron she outputs
  `<silent/>`.
- **Notice, `continue: null`.** When it is plainly accounting (its result starts
  `quarterly-accounting:`, or it answers the cron, a check, a package or a handover),
  she outputs `<silent/>`. For an operator trigger with `running`, she says one line
  instead: "A check is running — ask again in a few minutes." Otherwise the notice is
  not this skill's, and she answers it as it asks.
- **`busy`.** When `begin_pass` answers `busy` on a running, unexpired step, the text is
  "A check is running — started N minutes ago. Ask again in a few minutes."
  - This replaces "Already checking … I'll have the answer shortly." That answer
    promised a report the design cannot guarantee (residual R3).
  - An expired step never reaches `busy`, because `continue_pass` already claimed it.

### 3.7 What a continuation owes the operator: package notices in the render log

X3 ruling. Every failure or outcome message a continuation owes the operator becomes
an occurrence in the existing `alerts` table, and is rendered through the existing
`alerts.pending_rendering`. It is closed only by `mark_rendering_delivered`, which
stamps `sent_at`. This is the pattern the collection alerts already follow:

- an undelivered rendering is offered again;
- a delivered one never is.

**Kinds and keys.**

| Kind | `occurrence_key` | Raised by |
|---|---|---|
| `package-stopped` | `request:<id>:stopped` | the immediate `stopped` reply of `end_pass` |
| `package-failed` | `request:<id>:failed` | the stale-pass reclaim |
| `package-uncertain` | `delivery:<id>:uncertain` | the staged-request claim (it records the delivery `uncertain` itself) |
| `package-revoked` | `request:<id>:revoked` | the import's revocation |
| `package-uncertain` | `delivery:<id>:uncertain` | `record_delivery(uncertain)` for any package delivery, including a resend |
| `package-send-failed` | `delivery:<id>:failed` | `record_delivery(failed)` for any package delivery, including a resend |

The key is UNIQUE, so raising an occurrence twice (a replayed claim, a retried
`end_pass`) inserts once. `detail` holds `{quarter, reason, package_id}`. The reason is
the refusal text, clipped at `DETAIL_MAX` (300).

**Rendering.** `alerts._units` renders them after the collection alerts and before
package changes, one wrapped line each:

| Kind | Line |
|---|---|
| `package-stopped` | "I couldn't build the <quarter> package: <reason>." |
| `package-failed` | "I couldn't read the bank for the <quarter> package — ask for it again." |
| `package-revoked` | "The bank was re-read before I could send the <quarter> package — ask for it again and I'll rebuild it." |
| `package-uncertain` | the existing `delivery.offer_lines(<filename>)` |
| `package-send-failed` | "The <quarter> package didn't go out. Say "send it again" and I'll send it." |

A rendering that prints a `package-uncertain` or `package-send-failed` occurrence also
puts that package in its scope's `offers`, beside `alerts`. That way "send it again" binds to it exactly as it
binds to `record_delivery`'s own offer today (D3).

**Delivery.** The `speak` that `end_pass` already returns is that rendering. So is the
new `speak` on every `continue_pass` answer, but only when the caller claimed something
or nothing is live, held or running, so that a turn racing a live holder does not also
send it.

**Losing it and double-telling are both closed.**

- *Crash after the claim* (the X3 case "recovery-failed → told, 0 messages"): the
  occurrence stays unsent, and the next `end_pass` or `continue_pass` offers it again.
- *Two tells*: the second turn finds it delivered, or finds the same undelivered
  rendering.

What is left is the at-least-once window of the alerts pattern: two turns can hold the
same undelivered rendering at the same instant, and both can send it before either
marks it delivered. This is **accepted as design** (X4 ruling, residual R5): a duplicate
failure notice is preferable to a lost one. There is no send-lease.

`build_review`'s own receipts are unchanged. The skill's step "if it stopped, tell the
operator why" becomes "send `speak`": Ellen composes no failure line of her own for a
package.

## 4. Server changes

**Counts.** The schema goes from 3 to 4. The tool count goes from 33 to **35**, with two
new tools:

- `record_step` is the only record of progress.
- `continue_pass` is the one claim. It must write, rotate and release a token, so it is
  not folded into `check_setup` (a read) or `begin_pass`, because notices must be able to
  continue without beginning.

**Existing tools that gain an argument:**

| Tool | Argument | Required? |
|---|---|---|
| `begin_pass` | `reply` | optional, default `telegram` except `cron` → `silent` |
| `build_quarterly_package` | `package_token` | required |
| `stage_for_delivery` | `package_token` | required for a request-bound `package_id` |
| `record_delivery` | `package_token` | required for a request-bound delivery |
| `list_quarter_state` | `pass_token` | optional, clock only |

`list_projections` answers gain `time_up`.

### 4.1 `server/db.py`

`SCHEMA_VERSION = 4`. The following goes into `DDL`, and the same statements into
`MIGRATIONS[3]`:

```sql
ALTER TABLE pass_marker ADD COLUMN claimed_step TEXT;
ALTER TABLE pass_marker ADD COLUMN lease_at TEXT;
ALTER TABLE passes ADD COLUMN reply TEXT NOT NULL DEFAULT 'telegram';
CREATE TABLE IF NOT EXISTS pass_steps (
  pass_id TEXT NOT NULL,
  step TEXT NOT NULL CHECK (step IN ('sweep', 'judge', 'handover', 'snapshot')),
  started_at TEXT NOT NULL,            -- Ellen, before delegate_to_agent
  finished_at TEXT,
  finished_by TEXT CHECK (finished_by IN ('specialist', 'resident')),
  finish_json TEXT,                    -- counts, stopped (≤300 chars), failed
  carry_json TEXT NOT NULL DEFAULT '{}',   -- doc_ids / report: ids and counts only
  PRIMARY KEY (pass_id, step));
CREATE TABLE IF NOT EXISTS package_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  quarter TEXT NOT NULL, channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  pass_id TEXT NOT NULL, pass_outcome TEXT, reason TEXT,
  package_id INTEGER, delivery_id INTEGER,
  token INTEGER, lease_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('snapshot', 'snapshot-done', 'built', 'staged',
        'delivered', 'uncertain', 'failed', 'stopped', 'recovery-failed', 'revoked',
        'withdrawn', 'superseded')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_package_requests_open ON package_requests(quarter, state);
ALTER TABLE deliveries ADD COLUMN withdrawn_at TEXT;   -- staged bytes taken back on a reclaim
```

`alerts` needs no schema change. The four package-notice kinds (§3.7) are new values
of `alerts.kind`, which has no CHECK. The `occurrence_key` is already UNIQUE.

Two notes on the schema:

- The fresh-store `DDL` gets the four added columns inline, not as `ALTER`s.
- The `pass_marker` columns are cleared whenever `begin_pass` rewrites the marker.

### 4.2 `server/passes.py`, with a new `server/steps.py`

**Constants** (all re-tuned from the first real passes):

| Constant | Value | Meaning |
|---|---|---|
| `CEILING_ASSUMED_S` | 600 | A2 |
| `SWEEP_STOP_S` | 450 | the sweep stops |
| `RETURN_BY_S` | 510 | `wrap_up`: 90 s to the ceiling |
| `STEP_EXPIRY_S` | 600 | an unfinished step is ended; the stamp precedes Casa's launch, so the ceiling's own `timeout` notice already finds the step expired |
| `LEASE_S` | 600 | a claim with no progress may be claimed again. Every accepted write refreshes it; a Gmail round that goes 10 min without one loses the claim, its next write is refused, and the new claimant redoes the idempotent step |
| `ROW_COST_S` | 10 | measured; sizes a sweep page |

**`passes.py` changes:**

- **`rotate(conn) -> int`** runs inside the caller's transaction. It bumps
  `counters.pass_generation` and returns the new value. It is the only source of every
  token.
- **`check_token(conn, token)`** is unchanged in what it refuses. When it accepts a
  non-None token inside an open transaction, it also runs
  `UPDATE pass_marker SET lease_at=now`.
- **`check_package_token(conn, request_id, token)`** refuses unless
  `token == request.token` and the state is open. The wording: "this package request
  has been taken over by a later turn — stop, nothing was written". When it accepts, it
  refreshes `lease_at`.
- **`begin_pass(conn, trigger, reply)`** gains the reclaim terminalization and request
  recovery of §3.5, and the new `busy` text. For a live, not-stale marker with a
  running step, the text is
  `f"A check is running — started {when}.\nAsk again in a few minutes."`, and the same
  text is used when no step exists yet.
- **`end_pass`** gains the package authority transfer of §3.4, in its transaction. It
  returns `package_token`, `request` and `next` next to `ended`, `outcome` and `speak`.

**`steps.py` functions:**

- **`start(conn, token, step, carry)`**:
  - checks the token (required) and that the step is valid for the pass's trigger;
  - validates the carry per step;
  - refuses a step already started in this pass;
  - for `snapshot`, also creates the request;
  - returns `{"step", "started_at", "return_by", "sweep_stop_at"}`.
- **`finish(conn, token, step, *, by, counts, stopped=None, failed=False)`**:
  - checks the token, so a superseded specialist is refused here;
  - a second finish of a finished step is a no-op.
- **`claim(conn)`** is §3.3, all in one transaction:
  - `work` comes from `work.list_quarter_state(triage_only=True)`;
  - `documents` comes from `match_state WHERE doc_id=? AND state IN ('matched',
    'proposed')` and `documents.irrelevant`;
  - `throughput` comes from `passes.throughput`, and `imported` means the pass has a
    `snapshots` row.
- **`clock(conn, token)`** is `None` unless the token is live and the latest step is
  started and unfinished. Otherwise it returns
  `{"elapsed_s", "time_left_s": max(0, RETURN_BY_S - e), "wrap_up": e >= RETURN_BY_S}`.
- **`sweep_allowance(conn)`** is `SWEEP_STOP_S` minus the running step's elapsed time.
  It is `None` when no step is running, which keeps today's paging for the existing
  sims.

### 4.3 Every write path through the fence (audit at `e9b4eff`)

`check_token` is already called, inside the write transaction, by every write that
takes a pass token:

| Tool(s) | Call site(s) |
|---|---|
| `bind_account` | binding.py:35 |
| `import_ledger_export` | ledger.py:206 and :211 |
| `ingest_document`, `update_document_metadata`, `mark_irrelevant` | documents.py:118, :169, :182 |
| `stage_for_delivery`, `record_delivery` | delivery.py:73 (pre-check), :120 (in-tx), :219 |
| `end_pass`, `record_probe` | passes.py:100, :121 |
| `record_match` / `propose_match` (`_machine`), `relabel_match` | matches.py:93, :269 |
| `list_projections`, `record_observation` | sweep.py:111, :222 |
| `record_search` | work.py:39 |
| `upsert_counterparty`, `set_expectation` | kb.py:80, :161 |

The new writes join them:

- `record_step` (start and finish) and the successor's `start` go through `check_token`.
- `build_quarterly_package` (currently no check at all), `stage_for_delivery` and
  `record_delivery` on a request go through `check_package_token`. **Fixed here:**
  `build_quarterly_package` had no fence.

**Where the check binds (X4 rule).** Every token check that decides a write is made
**inside the transaction that commits it**. An earlier check outside it, such as
`stage_for_delivery`'s pre-check at delivery.py:73 or a check before
`db.custody_lock()`, is only an early refusal, never the authority. Applied to each
lock-waiting path:

- **`build_quarterly_package`.** `check_package_token` is repeated in the registering
  transaction (package.py around lines 357–368, beside the existing `latest_import`
  re-check), which also links `package_requests.package_id`. The existing
  `except: path.unlink()` already deletes the unregistered zip.
- **`stage_for_delivery`.** The check is repeated in the transaction that inserts, or
  finds, the delivery (delivery.py:120), together with the request's `staged` link. On a
  refusal, the freshly written outbox copy or handoff entry is removed; its `created`
  flag says it is this call's.
- **`record_delivery`.** Its single transaction checks the token, settles, closes the
  request and enqueues the notice.
- **`steps.claim` (the staged withdrawal).** Custody lock first, then the transaction.
  Inside it, it re-reads the request's state and lease before removing anything.
- **`end_pass`'s authority transfer.** It runs in `end_pass`'s own transaction.

**The gap, and what closes it.** `check_token(None)` passes. That is by design, for
operator-side calls: filing a Telegram document, a resend, a single-invoice send. So a
holder that *omits* its token is not fenced. Every write that exists only inside a pass
already refuses a missing token:

- `list_projections`, `record_observation`, `import_ledger_export`, `record_probe`,
  `end_pass`, `record_step`;
- `_machine` (a machine match);
- `propose_match` and `relabel_match` (required in the schema);
- a specialist-authored `ingest_document` or `set_expectation`;
- `record_search` other than a bare revive.

The remaining token-optional writes are operator-shaped and idempotent or CAS'd:
`ingest_document` (resident), `update_document_metadata`, `mark_irrelevant`,
`upsert_counterparty` and `bind_account`. This design leaves them as they are, and says
so in the spec.

### 4.4 `server/sweep.py`, `server/work.py`, `server/delivery.py`, `server/package.py`

- **`sweep.list_projections`**: the page is capped by `sweep_allowance`. A `time_up`
  page returns no items and does not move the cursor. `remaining_in_cycle` keeps its
  formula.
- **`work.describe()`** gains `search_hint` and `window_days` from the KB.
- **`delivery`** gains:
  - a `package_token` on `stage_for_delivery` and `record_delivery`, per §3.4;
  - idempotent staging for a request that is already staged;
  - staged basenames that are never reused (`qa-<random>`), with `filename` returned for
    `send_media` (§3.4);
  - `record_delivery` enqueueing `package-uncertain` / `package-send-failed`, and
    returning its rendering as `speak` in place of `_offer_again`;
  - `withdraw(conn, rows)`, the removal of staged bytes (outbox file, or the plugin's
    own handoff entry), extracted from `withdraw_revoked`. Both the import and the
    staged-request claim call it, under the custody lock, and a failed removal refuses
    the whole call;
  - `revoke_superseded_first_sends` moving that request to `revoked` and raising
    `package-revoked`;
  - `resendable` / `resend_target` treating a withdrawn `uncertain` delivery like any
    uncertain one: "send it again" re-stages a fresh copy.
- **`alerts`** gains the four package-notice kinds in `_units`, and an `offers` scope for
  `package-uncertain` in `pending_rendering`. `views.mark_rendering_delivered` already
  stamps `sent_at` for every alert in scope.
- **`steps.claim`** takes `db.custody_lock()` before its transaction whenever the
  candidate it will claim is a staged request. The candidate is chosen by a read first,
  and re-checked inside the transaction.
- **`package.build_quarterly_package(conn, quarter, package_token)`** checks the token
  before its custody lock, and links the request inside its freeze transaction.

### 4.5 `server/tools.py` and the manifest

- **`record_step`**: `pass_token`, `step`, `action`, `quarter`, `channel`, `doc_ids`,
  `report`, `remaining_in_cycle`, `triage_remaining`, `stopped`, `failed`. The carry is
  allowed only with `start`, and the finish fields only with `finish`.
- **`continue_pass()`**, with this description: "Call after every delegation returns in
  your turn, on every system notification about a delegation to finance, and before
  beginning any check, handover or package. When something is due, this claims it for
  you alone and returns a NEW pass_token (or package_token) — use only that one from
  now on — with the next step, where to report (`reply`) and everything the step
  needs. Otherwise continue is null: write nothing."
- **Schema changes**:
  - `begin_pass` gains `reply`;
  - the three package tools gain `package_token`, with the description "the
    package_token end_pass or continue_pass gave you";
  - `list_quarter_state` gains `pass_token`, as the 11th optional-token tool, with the
    standard sentence.
- **The clock hook** is as before, using `steps.clock`.
- **Manifest**: `provides_tools` and `resultContract` gain both new tools.
- **`reset_store`** wipes `pass_steps` and `package_requests`.

## 5. `SKILL.md` changes (exact)

**Frontmatter.** Append `, or when a system notification says a delegation to the
finance specialist returned or failed`.

**New section, after "Both agents: refusals and errors":**

```markdown
## Ellen: a delegation that answers later

A pass's progress is in the store, never in a message, and every continuation gets a NEW
token: the old one is refused from then on.

1. Before `begin_pass` in any flow (the cron, "go and check now", a handed-over document,
   a package), call `continue_pass()`. If it returns a continuation, do that instead — it
   is an unfinished earlier one.
2. Just before `delegate_to_agent`, `record_step(pass_token, step=…, action="start")`
   with what the flow names, and pass that same token to the specialist.
3. When the delegation answers in this turn: if it failed, or it came back without
   finishing (its reply does not start `quarterly-accounting:`),
   `record_step(pass_token, step=…, action="finish")` — with `failed=true` for an
   error. Then `continue_pass()`, and do what it returns with the token it returns.
4. When it answers `status: pending`: say the flow's one line (on the cron, output
   `<silent/>`) and end the turn. The pass is not over.
5. On ANY system notification about a delegation to finance — returned, failed, timed
   out, orphaned by a restart, finished without its answer, or said again after a
   restart — call `continue_pass()` first:
   - a continuation: do its `next` with ITS token and inputs; report where its `reply`
     says (`silent`: say nothing but `speak`), else here. Never use a token, step or
     work list from an earlier turn or from the notification.
   - `continue` is null: write nothing. If the notification is an accounting one (its
     result starts `quarterly-accounting:`, or it answers the cron, a check, a package or
     a handed-over document), output `<silent/>` — or, for the operator's own check with
     `running`, "A check is running — ask again in a few minutes." Otherwise it is not
     this skill's: answer it as it asks.
   Its closing lines ("Reply to the user…", "offer to retry") never decide anything here.
6. A refusal that "this pass is no longer the current one" or "this package request has
   been taken over" means another turn continued it: stop at once, say nothing more.

The outcome for `end_pass`: `stopped` when `can_run` is false or the step's finish says
`stopped`; `failed` when the step ended unfinished and nothing was imported this pass;
`interrupted` when anything remains (`remaining_in_cycle`, `triage_remaining`, `work`
truncated or `not_fresh`, an item not searched) or the step ended unfinished after the
import; `complete` otherwise.
```

**The pass, steps 1 and 3–6.**

- **Step 1** becomes: "`continue_pass()` (above); then
  `begin_pass(trigger="cron"|"operator", reply="silent"|"telegram")`. If it answers
  `busy`: on the cron, output `<silent/>`; for the operator, send its text."
- **Step 3** is `record_step(pass_token, step="sweep", action="start")`, then the
  delegation. The pending line is "Checking the bank — this takes a few minutes; I'll
  send the result here." When it answers, continue as above. `next` is the Gmail round,
  or step 6 when the pass stopped or failed; in that case, sweep the Telegram inbox
  first.
- **Step 4, the Gmail round**, is as in revision 2. The work list is the continuation's
  `work`. Skip `portal` items. The query ladder comes from the item's fields
  (`search_hint`, the printed amount, `window_days`, `has:attachment`). For CRDT and
  DBIT `refund`, search Sent. The not-searched set is `work.remaining`, `not_fresh`, and
  anything not reached.
- **Step 5.** If anything was filed, or `triage_remaining` is above 0, or the sweep ended
  unfinished: `record_step(pass_token, step="judge", action="start",
  report={checked, total, not_searched})`, then "judge the newly filed documents and the
  payments triage did not reach". The pending answer is `<silent/>`.
- **Step 6** is `end_pass(pass_token, outcome, report)` with the outcome rule above. The
  report is the continuation's `report` after step 5. The speak and `<silent/>` handling
  is unchanged.

**Handover.**

- **Step 2** gains `continue_pass()` before `begin_pass`, and
  `record_step(pass_token, step="handover", action="start", doc_ids=[<doc_id>])`. The
  pending line is "Filed. Checking it against the payments — I'll tell you shortly."
  The pass ends by the outcome rule, always, on this turn or on the continuation.
- **Step 3** reads the case from the continuation's `documents`, which is the recorded
  pairing:

  | Pairing | What Ellen says |
  |---|---|
  | `matched` | the matched line, with its payment |
  | `proposed` | "Filed. It could fit more than one payment — it's in 'anything I should check?'." |
  | `unpaired` | the reply's case if there is one, else "Filed. I'll match it at the next check." |

  Never infer a match from anything else.

**The specialist's pass** is as in revision 2:

- the `clock` / `wrap_up` paragraph;
- the finish as its last action, with `quarterly-accounting: <step> finished` as the
  first line of its reply;
- step 5 runs "until `remaining_in_cycle` is 0 or `time_up`";
- step 6 is `list_quarter_state(triage=true, pass_token=…)`;
- step 8 is "finish and reply briefly; search ideas go into
  `upsert_counterparty(search_hint=…)`".

Add: "If a write answers that this pass is no longer the current one, stop and return:
the pass has moved on and your recorded work is kept."

**Packaging.**

- **Step 1**: `continue_pass()`; then `begin_pass(trigger="package", reply="telegram")`;
  then `record_step(pass_token, step="snapshot", action="start", quarter=<the quarter>,
  channel="telegram"|"email")`, then the delegation. The pending line is "Reading the
  bank first — the <quarter> package follows in a few minutes." At `end-pass-then-build`:
  `end_pass(…)`. **Its answer carries `package_token` and `next`**: do `next` with that
  token.
- **Step 2**:
  - `build_quarterly_package(quarter, package_token)`;
  - `stage_for_delivery(channel=…, package_id=…, package_token=…)`;
  - the send;
  - `record_delivery(delivery_id, outcome, package_token=…)`.

  The resend and single-document paths are unchanged and token-free.
- **Continuation nexts**:
  - `build` and `stage` resume step 2 at that point;
  - `next: null` with a `speak`: send it verbatim, then `mark_rendering_delivered`.
    That covers a stopped package, a failed recovery, a withdrawn or revoked send.
    Never send the file again yourself, and never write a failure line of your own.

  Every `speak` that `end_pass` or `continue_pass` returns is sent and marked
  delivered, including on a cron turn. It is how anything owed about a package reaches
  the operator exactly once.

**Pinned tests (`tests/test_skill.py`).**

- Pin the outcome-rule sentence (it replaces "`failed` if the delegation errored or ran
  out of turns") and "`time_up`".
- New pins:
  - "call `continue_pass()` first";
  - "use only that one";
  - "Never infer a match";
  - "Never send the file again yourself";
  - "this pass is no longer the current one";
  - the specialist's finish sentence.
- `OPERATOR_LINES` gains the pending lines, "A check is running — ask again in a few
  minutes.", the `proposed` handover line, and the `busy` text's lines.

## 6. Invariants the design keeps

- **The stale-pass fence, now also a claim fence.** There is one integer per holder, from
  one monotonic counter, compared on every write (§4.3). A claim rotates it, so there is
  exactly one live holder of a pass and one of a package request. A superseded
  claimant, the specialist of a continued step, or the holder of a reclaimed pass is
  refused at every token write, including `end_pass`, `record_step`, `build`, `stage`
  and `record_delivery`. It never sees a new token, because only the claimant's own
  call returns one. An ended pass never releases a pass token.
- **No window between a pass and its package.** The authority transfer happens in
  `end_pass`'s own transaction, and the request's lease is fresh from that instant.
- **At most one staged send per request, and no sendable file in a superseded
  holder's hands.**
  - Staging is idempotent, recovery never sends, and a stale holder's stage is refused.
  - A reclaim of a staged request removes its staged bytes under the custody lock before
    it settles the delivery `uncertain`. Casa's send tools check no token of ours, so
    the file itself is what must be gone.
  - A failed removal refuses the claim, as it refuses an import.
- **No two deliveries share a staged path.** Every new delivery draws a fresh random
  basename, so a superseded holder's path never names a later copy.
- **Token checks bind in the committing transaction.** A holder rotated while it
  waited for a lock commits nothing and leaves no unregistered bytes behind.
- **What a continuation owes the operator is said at least once, and never after it
  was delivered.** Package
  failures and outcomes are `alerts` occurrences (§3.7), offered until
  `mark_rendering_delivered` and never after. A crash loses nothing, and a replay adds
  nothing.
- **Idempotent continuation.** A step can run again after a lease lapses (A6), under a
  new token. That is safe:
  - ingest is idempotent by hash;
  - `record_search` counts a fruitless pass once per `pass_id` and de-duplicates
    queries;
  - `record_probe` replaces its row;
  - `end_pass` succeeds once.
- **The bank-write gate** is untouched. A `time_up` page asks for no write.
- **Freshness (#1 as it stands)** is unchanged. The time stop stamps nothing, and the
  Gmail list is the `fresh_only` triage. The cursor rotates across passes, so searching
  and matching converge pass by pass. A complete-quarter package still needs #1.
- **D3 binding** is unchanged.
- **The 4096 fit.** No new server-produced operator text exceeds one short line: the
  `busy` text, `request.reason`, and `finish.stopped` at 300 characters or less, which
  is relayed as refusals already are. `_deliverable` still guards `end_pass`, which now
  also returns `package_token`, a non-text field.
- **Ellen holds every pass.** The specialist writes "finish", never `end_pass`.

## 7. Tests (through `qa_server.handle`; the specialist side via `ToolFlow`)

The clock is `mock.patch.object(db, "_clock", …)`, advancing. Concurrency uses two
connections via `db.open_store()`, as in `_procs.py`.

1. **Pending, then the notice.**
   - Setup: `begin_pass(operator)`, then `record_step(sweep, start)`.
   - The specialist runs its probes, import and sweep, then `finish`.
   - `continue_pass()` returns a **new** token `T2 ≠ T1`, `next == "gmail-round"` and
     `work == list_quarter_state(triage=true)`.
   - A write with `T1` (`record_search`, `end_pass`) is refused.
   - The Gmail round and `end_pass` succeed with `T2`.
2. **The ceiling.**
   - Pages shrink and then `time_up` comes. No row is read after `SWEEP_STOP_S`, and
     `wrap_up` comes at `RETURN_BY_S`.
   - Cut without a finish:
     - at 599 s the answer is `running`;
     - at 600 s the step is claimed, with `ended == "expired"`;
     - the outcome is `interrupted`, with `swept_this_pass > 0`.
   - The cut specialist's late `record_observation` and `finish` with `T1` are refused.
3. **Inline error before and after the import.** `finish(failed=true)` makes the step
   due at once. With no import, `next` is `end-pass` and the outcome is `failed`. After
   the import, `next` is `gmail-round`.
4. **A foreign notice.** During a running step, `continue_pass` gives `running` and the
   store digest is unchanged. With no pass, it is null.
5. **A replay after a restart.** After a claim and `end_pass`, the answer is null. During
   step 5 it is `running`. While a claim's lease is fresh it is `held`.
6. **A restart between launch and first write.** The step starts and nothing is written.
   At expiry, the next check's `continue_pass` claims it, and the outcome is `failed`.
   The variant with no step after `begin_pass` is claimed at expiry too.
7. **Two sessions race the claim.** Two connections call `continue_pass` on the same due
   step: exactly one gets a token, and the other gets `held`.
8. **A superseded claimant.**
   - A claims the sweep's continuation (`T2`) and goes quiet. After `LEASE_S`, B claims
     (`T3`).
   - **A's `end_pass(T2)`, `record_search(T2)` and `record_step(judge, start, T2)` are
     refused.** B's `end_pass(T3)` succeeds.
   - A variant: B starts the judge step with `T3`, and A wakes. A's writes are still
     refused, and a new `continue_pass` gives `running`, not due.
   - A lease that A's own writes keep refreshing is not reclaimed.
9. **Package token transfer with a racing claim.**
   - The snapshot is finished. The claim `end-pass-then-build` returns `T`.
   - `end_pass(T)` returns a `package_token` `P` and `next == "build"`.
   - A second connection's `continue_pass` in the same instant gets null (the lease is
     fresh).
   - `build_quarterly_package(P)` builds. `build` without a token, or with `T`, is
     refused.
10. **Idempotent staging.** With the current token, `stage_for_delivery` on a request
    that is already staged returns the **same** `delivery_id` and path, and the
    `deliveries` row count is unchanged.
11. **A stale holder cannot send (X3 S1).**
    - `P1` builds and stages. The staged outbox file exists. `P1`'s lease lapses.
    - The reclaim `continue_pass` gives `P2`, `next: null`, and `speak` with the offer.
      The staged file is **gone**. The delivery is `uncertain` with `withdrawn_at` set,
      and the request is `withdrawn`.
    - `stage_for_delivery(P1)` and `record_delivery(P1)` are refused. The staged path P1
      holds does not exist, so the send fails.
    - An import that lands next revokes nothing new, and still no file exists.
    - "Send it again" (`resend=true`) stages a new copy from `packages/` under a new
      delivery.
    - The email variant: the handoff entry directory is removed.
    - A removal that fails (a read-only directory) refuses the claim, and nothing
      changes.
12a. **Token check in the committing transaction (X4 Terra S1).**
    - `P1` calls `build`. While it waits (simulated by holding `db.custody_lock` in a
      second process, `_procs.py`), `P1`'s lease lapses and a reclaim rotates the
      token to `P2`.
    - `P1`'s build is refused *in the registering transaction*: no `packages` row, and
      no zip left in `packages/`.
    - The same shape for `stage_for_delivery`: refused, no delivery row, no outbox file.
12b. **Never-reused paths (X4 Astra S1).**
    - A first send is staged at path `X`. After withdrawal, "send it again" stages at
      `Y ≠ X`, and `X` stays absent.
    - Two resends of one package get two distinct paths.
    - The stage answer carries the package's display `filename`.
    - Idempotent re-staging of the same delivery returns `X` again.
12c. **`record_delivery` notices (X4 Astra S2).**
    - `record_delivery(uncertain)` creates one `package-uncertain` occurrence and
      returns its rendering, whose scope carries `offers`.
    - A "crash" before `mark_rendering_delivered` → the next `continue_pass` offers the
      same text.
    - "Send it again" after delivery binds that package.
    - `failed` gives `package-send-failed`, with its own wording.
12. **Revocation.** The next pass's import revokes an unsent first send. The request
    becomes `revoked`, and one `package-revoked` occurrence exists.
13. **Reclaim terminalizes and recovers.**
    - A snapshot pass with its step finished is left live for `STALE_AFTER_S`. Then
      `begin_pass` gives `reclaimed` and `recovered`, and the displaced pass has
      `ended_at` set and `outcome == "interrupted"`.
    - The next `continue_pass` gives `build` for its request, with a package token.
    - The variant with the snapshot unfinished raises one `package-failed` occurrence.
14. **Package notices: lost crash and double tell (X3 S2).**
    - **Lost crash.** A reclaim raises `package-failed`. The first turn gets `speak`
      and "crashes" before `mark_rendering_delivered`. The next `continue_pass` (or
      `end_pass`) offers the **same** text again. After `mark_rendering_delivered`, no
      later call offers it: `sent_at` is set, and there are 0 undelivered occurrences.
    - **Double tell.** A stopped snapshot: `end_pass` raises `package-stopped` and
      returns `speak`. A replayed `end_pass` (refused) and two later `continue_pass`
      calls insert no second occurrence (UNIQUE key). After delivery the notice is
      never offered again.
    - The `package-uncertain` rendering's scope carries `offers`, so "send it again"
      after it binds that package.
15. **`busy` wording.** While a step runs, `begin_pass` gives "A check is running —
    started N minutes ago.\nAsk again in a few minutes." It fits 4096, and names no
    machinery.
16. **Handover pairing.** `documents` shows `matched` with its payment, `proposed`,
    `unpaired` and `irrelevant` from the records. A handed-over document is reported
    correctly even with more than 50 unmatched documents.
17. **Lease of 600 s.** A claim is `held` at 599 s with no write, and reclaimable
    at 600 s. A write at 590 s refreshes it (still `held` at 900 s).
18. **Cron, contract, clock, back-compatibility, schema and surface.**
    - **Cron:** `reply == "silent"`.
    - **`record_step` refusals:** a stale or missing token, the wrong step for the
      trigger, a second start, carry arguments on `finish`.
    - **Clock:** absent without a running step.
    - **Back-compatibility:** `list_projections` pages without limit when no step
      exists, so the existing sims are unchanged.
    - **Schema:** `DDL_V3` frozen at `e9b4eff`, v3 → v4 migration, `reset_store` wipes
      both tables.
    - **Surface:** 35 tools, the manifest, the skill's calls and arguments, and the pins
      from §5.
    - **Existing tests:** e2e package tests that call `build_quarterly_package` directly
      get a token through `end_pass`.

## 8. Spec text to add (`2026-08-10-quarterly-accounting-design.md`)

**New subsection under "## Casa baseline": "### Delegation timing — design
assumptions".** It opens with the following, then gives A1–A6 verbatim:

> "This plugin is built on the following behaviour of Casa's `delegate_to_agent`, as read
> in Casa v0.331.0–v0.332.0. They are assumptions about another project, and each says
> what this code does if it stops holding."

**§Weekly pass, step 3.** Replace "The only real ceiling is the specialist's `max_turns`
(70) and the pass's own wall-clock…" with:

> "The binding ceiling is the delegation's wall clock (A2), not its turns. Each step's
> start is stamped before it is delegated, and every answer the specialist gets carries
> the time left. The sweep stops for time by itself, and the specialist finishes its
> step well inside the ceiling. Ellen's Gmail round is derived from the store, never
> from the specialist's reply."

**§Running the pass on demand.** Add after the marker paragraph. It also corrects the
"generation" wording:

> "A pass spans turns. Its progress is in the store: steps started, finished, or
> expired. Whoever looks next — a notification, a check, a package, a handover —
> continues the due step through one claim, and **every claim rotates the pass token**,
> drawn from the same generation counter that a reclaim bumps. So the existing
> stale-pass fence refuses whatever a superseded holder does. No notification is ever
> matched to a delegation. A package request is its own record with its own rotating
> token, handed over atomically when its bank pass ends. A reclaimed pass is ended
> `interrupted` and its package request recovered."
>
> "A holder that omits its token is not fenced. Every pass-only write refuses a missing
> token. The token-optional writes are the operator-shaped ones (filing a document,
> correcting a document's reading, a KB entry, binding the account), and these are
> idempotent or CAS'd."

**§Tool surface.** The heading becomes "(server, 35 tools)". Add an erratum for
`record_step` and `continue_pass`, and for `package_token` on the three package tools.

**§Open items, background jobs.** Append:

> "Evidence arrived 2026-09 (#2): the wall-clock ceiling cut two of three catch-up
> delegations. Fixed within delegations. Jobs stay out unless a budgeted pass still
> cannot converge."

## 9. Failure modes considered and rejected

- **Correlating notifications to delegations** (revision 1). Short-id collisions, an
  open/attach race, a restart before the attach, and inline errors that never attach.
  Removed.
- **A claim without a fencing generation** (revision 2). A superseded holder could still
  write, end the pass, or build and stage a second package. It is generalised into the
  rotating token, rather than a per-write claim-ownership check. That check would be a
  second fence, on every write path, beside the one that exists.
- **A composite token `(pass generation, claim generation)`**. It is unnecessary: one
  global monotonic counter already makes every holder's value unique, and it keeps
  `check_token` a single comparison.
- **Session ownership of a continuation** (Terra X2). Rejected by ruling: there is one
  operator, and the writes are the pass's own. The pass records where to report.
- **A self-scheduled wake at step expiry** (Astra X2 S2). Rejected by standing
  constraint: no Casa jobs or triggers of our own. It is residual R3.
- **Parsing the work order from the notice, and relying on Ellen's conversation
  memory.** An error carries no text, and a notice is a new turn. That was the bug.
- **`check_setup` exposing the live token.** There would be no exclusivity and no
  rotation.
- **Inferring handover success from `list_unmatched_documents`.** That list is capped at
  50, and a `proposed` pairing also removes the document.
- **Auto-resending on recovery.** The file may already have arrived.
- **A row-count budget, a shorter reclaim, `mode="async"`, Casa background jobs, a
  server timer.** As in revision 2: Casa enforces time, not rows; expiry, leases and the
  3 h reclaim already recover; async is equivalent; jobs are not needed; and a stdio
  server has no scheduler.

## 10. Residuals and open questions

**R1 — silent notices replay (A4).** A replay is harmless: it finds nothing due, a held
lease, or `running`, and ends in `<silent/>`. But each retained notice costs one
resident turn per Casa restart, indefinitely. Draft Casa issue, for the controller to
take to the operator before filing:

> **Title:** A delegation outcome is re-announced after every restart when the resident
> deliberately stays silent
>
> **What happens.** When a delegation finishes after the sync wait, its outcome is
> announced to the delegating resident as a new turn, and that announcement is a durable
> obligation. It is discharged only when the turn's reply is delivered (`agent.py`
> `_ack_delivery`, v0.332.0). A turn that ends in `<silent/>` delivers nothing, so the
> obligation stays owed, and the boot reconciler re-announces it after every restart
> (`replayed_after_restart`). A scheduled resident that delegates and then correctly
> stays silent therefore gets every such notice again after each restart, indefinitely.
> This applies, for example, to a weekly background job whose doctrine is "say nothing
> unless something needs the operator". Each replay costs a resident turn and invites
> the model to narrate a stale result.
>
> **Expected.** A turn that handled the notice and chose silence discharges it, as a
> delivered reply does. For example, the silence gate that already suppresses a
> `<silent/>` turn could also acknowledge a synthesized delegation turn that ended
> without an error kind. Crashed or errored turns keep "never lose an announcement";
> deliberate silence ends the loop.
>
> **Who is affected.** Any plugin whose cron flow delegates for longer than the 60 s sync
> wait (e.g. casa-plugin-quarterly-accounting #2).

**R2 — a notice-less stall.** If a step expires and no notice arrives (A3 false), the
pass continues at the next check, handover or package. That can be a week for a cron
pass, while "go and check now" finishes it at once.

**R3 — early async failure (accepted by ruling).** Consider a delegation that degrades
at 60 s and then crashes at 90 s. Its error notice arrives while the step is still
unexpired, so `continue_pass` answers `running`. Nothing calls again at 600 s. The pass
recovers at the next check after expiry (the cron, or "go and check now"). Until then
`busy` tells the operator a check is running and to ask again in a few minutes.

**R4 — a crash right after a claim (Terra X3, accepted by ruling).** A continuation
that dies right after claiming leaves the pass dormant until its lease (600 s) lapses
*and* something calls `continue_pass` again. That caller can be a notice, a check,
"go and check now", a handover or a package request. There is no self-scheduled wake
(standing constraint). An operator who asks again within ten minutes gets "A check is
running …". After that, the ask recovers it. This is the same shape as R3.

**R5 — a package notice can be sent twice (Terra X4, accepted by ruling).** Two turns
holding the same undelivered rendering can both send it before either marks it
delivered. Notices are at-least-once, because a duplicate failure notice is preferable
to a lost one. Once it is marked delivered, it is never offered again.

**Q1 — release number.** Two tools, three new tool arguments and a migration suggest
v0.2.0. Any bump changes `WORKFLOW` to `acct@<new>`, and `check_setup` then reports
`acct@0.1.0`'s writes as an older workflow still present. That is a report, not a stop.
The test-install reset loop then asks which backup to restore.

**Q2 — budget numbers.** 450 / 510 / 600 / 1800 / 10 come from one measurement. Re-tune
them from the first passes after the fix.
