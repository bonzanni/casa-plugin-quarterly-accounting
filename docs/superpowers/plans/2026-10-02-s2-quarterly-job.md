# S2: the quarterly check as one finance job — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the quarterly check and package rounds out of Ellen's long-lived conversation
into one finance-hosted Casa job, `quarterly-accounting:work`. Its fresh turns ask the plugin
what to do next (`job_next`). The store carries all state, fences and budgets. Ellen keeps only
the thin interim: asking, relaying, and sending packages.

**Architecture:**
- A new module `server/job.py` is the cursor. It claims, fences, hands out units, sizes
  batches, measures progress and decides the end of a pass.
- A new module `server/asks.py` holds the requests Ellen records and the relay (`job_report`).
  The pass machinery (`passes.py`, `steps.py`, `work.py`, `sweep.py`) is reused through in-tx
  cores.
- Job passes carry `protocol='job'`, which switches off every wall clock and delegation rule.
  Delegation-protocol code paths stay, unreachable from the tool surface, because the existing
  suite pins them; S7 removes them.

**Tech Stack:** Python 3 standard library, sqlite3 (WAL), the repo's MCP server
(`server/qa_server.py`), `unittest`.

**Spec:** `~/Projects/ha-casa-app-docs/specs/2026-10-02-s2-quarterly-check-as-finance-job-design.md`
(revision 5, CONVERGED; the reviewed text is the frozen copy
`rounds-2026-10-02-s2-design/r5/design-frozen.md`, sha256 `0b6f2d3a3b7f08ab86334ed02db10874e675135211b505797bee5d8faf69f584`).
Section numbers below (§4, §5.2, …) are the spec's.

## Global Constraints

- **Stdlib only.** No new dependency, in the server or in tests.
- **Test command:** `python3 -m unittest discover -s tests -t .` must pass at the end of every
  task. A task that changes behaviour an existing test pins updates that test, in that task,
  and says why in the commit message.
- **Store schema:** 9 → 10, in one migration. A fresh store's DDL equals the migrated schema
  (`tests/schema_history.py` pins it).
- **Margins, verbatim from the spec:**
  - Z = 900 s (note confirmation, §4);
  - W = 30 min, counted from the import's sweep completion; at most 2 W-refreshes per pass
    (§5.2);
  - at most 2 adoptions per pass (§6.3);
  - `turnsPerBatch` = 80, with a 10-turn reserve (§5).
- **Job declaration, verbatim (§3):** `"jobs": [{"name": "work", "skill": "quarterly-job",
  "title": "Accounting check", "summary": "Checks the bank and Gmail, judges documents,
  prepares packages", "batches": "unlimited", "turnsPerBatch": 80, "session": "fresh",
  "host": "specialist"}]`.
- **Removed tools (§8):** `begin_pass`, `end_pass`, `continue_pass`, `record_step`,
  `more_work`.
- **Added tools (§8):** `job_next`, `job_report`, `request_work`, `request_package`,
  `record_filing`; and `job_status`, read-only (design delta for ha-casa-app#1180).
- **Changed tools:** `import_ledger_export` takes `acq`; `record_probe` takes `acq` and
  `absent`.
- **Bank-feed floor:** unchanged (0.20.0). The §4 chain was traced in 0.22.1.
- **Casa floor: ≥ 0.337.0** (S1 v0.336.0: `"session": "fresh"` and the `Job id:` line; S1b
  v0.337.0: `"host": "specialist"`). Below it, the job would run on Ellen without bank-feed or
  Gmail. README's install section says so.
- **Ellen-side scope guard (§1):** no guarantee may depend on a rule Ellen carries out
  carefully. Ellen's skill only asks, relays verbatim, and sends.
- **Operator-facing text:**
  - every text a tool returns is ≤ `views.TELEGRAM_LIMIT` (`tools._deliverable`);
  - no machinery words (`views.FORBIDDEN`).
- **Version:** `plugin.json` 0.8.0 → 0.9.0 (`version.WORKFLOW` follows it), plus an annotated
  tag `v0.9.0` at release (memory rule).
- **Branch:** work on `feat/s2-quarterly-job`, never on `main`.

## File map

| file | responsibility |
|---|---|
| `server/db.py` | schema 10 (DDL + migration 9→10 + a post-SQL hook); `claims`, `work_requests` |
| `server/job.py` (new) | claim, the fence over claims, F, the cursor (`next_unit`), batch budget, progress measure, pass end |
| `server/asks.py` (new) | `request_work`, `request_package`, taking and settling requests, verdicts, `job_report` |
| `server/passes.py` | `protocol` on passes, `start_pass(..., protocol, token)`, `_end_pass_tx` core, `_terminalize` requeues requests, `record_probe` `acq`/`absent`/`missing`/`gen` |
| `server/steps.py` | `_start_tx`/`_finish_tx` cores; `_ended`/`clock`/`sweep_allowance` honour `protocol='job'`; `claim(sends_only=True)` |
| `server/sweep.py` | `readback_owed`; claim-ordered, margin- and registration-gated `note_confirmed`; `swept_at` |
| `server/ledger.py` | import binding (`acq`, same claim, export once); stamps `job_id`/`read_seq`/`acq`/`export_ref` |
| `server/matches.py` | F gate on machine pairing/proposal/relabel in job passes |
| `server/alerts.py`, `server/views.py` | the no-Gmail wording; the waiting-request line; `mark_rendering_delivered` → requests `reported` |
| `server/tools.py` | the tool surface |
| `.claude-plugin/plugin.json` | `casa.jobs`, tool lists, version |
| `skills/quarterly-job/SKILL.md` (new), `skills/quarterly-accounting/SKILL.md` | the job's procedure; Ellen's thin interim |
| `tests/test_s2_*.py` (new) | red cases per task |

---

### Task 1: Schema 10, and the migration that closes a live delegation pass

**Files:**
- Modify: `server/db.py` (DDL; `SCHEMA_VERSION`; `MIGRATIONS[9]`; `migrate`)
- Modify: `server/passes.py` (`close_delegation_pass_on_upgrade`)
- Test: `tests/test_s2_schema.py`

**Interfaces:**
- Produces:
  - the tables `claims(gen INTEGER PRIMARY KEY, job_id TEXT NOT NULL, at TEXT NOT NULL,
    spent INTEGER NOT NULL DEFAULT 0, measure_json TEXT, reported INTEGER NOT NULL DEFAULT 0)`;
  - `work_requests(request_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL CHECK (kind
    IN ('check','handover')), trigger TEXT NOT NULL CHECK (trigger IN ('cron','operator')),
    doc_ids_json TEXT NOT NULL DEFAULT '[]', created_seq INTEGER NOT NULL, created_at TEXT NOT
    NULL, state TEXT NOT NULL CHECK (state IN ('queued','taken','done','reported')), pass_id
    TEXT, outcome TEXT, render_id TEXT, verdicts_json TEXT NOT NULL DEFAULT '{}')`;
  - the columns listed in Step 3;
  - `db.SCHEMA_VERSION == 10`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_s2_schema.py
from tests._base import StoreCase


class Schema10(StoreCase):
    def cols(self, table):
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def test_version_and_new_tables(self):
        import db
        self.assertEqual(db.SCHEMA_VERSION, 10)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "10")
        self.assertTrue({"gen", "job_id", "at", "spent", "measure_json", "reported"}
                        <= self.cols("claims"))
        self.assertTrue({"request_id", "kind", "trigger", "doc_ids_json", "created_seq",
                         "state", "pass_id", "outcome", "render_id", "verdicts_json"}
                        <= self.cols("work_requests"))

    def test_new_columns(self):
        self.assertTrue({"protocol", "holder_job", "orphaned_by", "adoptions", "adopters_json",
                         "acq",
                         "acq_gen", "read_seq", "w_refreshes", "judge_after", "judge_pages"}
                        <= self.cols("passes"))
        self.assertTrue({"protocol", "started_seq", "started_gen"} <= self.cols("pass_steps"))
        self.assertIn("w_pending", self.cols("passes"))
        self.assertTrue({"job_id", "read_seq", "acq", "export_ref", "swept_at"}
                        <= self.cols("snapshots"))
        self.assertTrue({"readback_owed", "note_issued_gen", "note_other_issued_gen",
                         "note_seen_gen"}
                        <= self.cols("projections"))
        self.assertIn("gen", self.cols("probes"))

    def test_migration_from_9_closes_a_live_delegation_pass(self):
        import db, passes, pathlib, sqlite3
        # a schema-9 store with a live delegation pass, built by the 0.8.0 DDL kept in
        # tests/schema_history.py (V9_DDL), then opened by this code
        path = pathlib.Path(self.tmp) / "v9.sqlite"
        from tests.schema_history import build_v9_store
        build_v9_store(path, live_pass=True)
        conn = db.open_store(path)
        self.addCleanup(conn.close)
        m = conn.execute("SELECT live FROM pass_marker").fetchone()
        self.assertEqual(m["live"], 0)
        p = conn.execute("SELECT outcome FROM passes ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(p["outcome"], "interrupted")
        # the old token is refused everywhere
        with self.assertRaises(db.Refusal):
            with db.tx(conn):
                passes.check_token(conn, 1)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_s2_schema -v`
Expected: FAIL (`SCHEMA_VERSION` is 9; there is no `claims` table; `build_v9_store` is
missing).

- [ ] **Step 3: Implement**

In `server/db.py`:
- set `SCHEMA_VERSION = 10`;
- append the two `CREATE TABLE` statements above to `DDL`;
- add every new column to its table's `CREATE TABLE` in `DDL`;
- add the migration:

```python
    # 9 -> 10 (S2): the job protocol. Job passes, claims, requests, acquisitions,
    # the sweep's read-back debt and claim-ordered note issues (spec §4, §5, §6).
    9: ["ALTER TABLE passes ADD COLUMN protocol TEXT NOT NULL DEFAULT 'delegation'",
        "ALTER TABLE passes ADD COLUMN holder_job TEXT",
        "ALTER TABLE passes ADD COLUMN orphaned_by TEXT",
        "ALTER TABLE passes ADD COLUMN adoptions INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE passes ADD COLUMN adopters_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE passes ADD COLUMN acq INTEGER",
        "ALTER TABLE passes ADD COLUMN acq_gen INTEGER",
        "ALTER TABLE passes ADD COLUMN read_seq INTEGER",
        "ALTER TABLE passes ADD COLUMN w_refreshes INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE passes ADD COLUMN judge_after TEXT",
        "ALTER TABLE passes ADD COLUMN judge_pages INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE pass_steps ADD COLUMN protocol TEXT NOT NULL DEFAULT 'delegation'",
        "ALTER TABLE pass_steps ADD COLUMN started_seq INTEGER",
        "ALTER TABLE pass_steps ADD COLUMN started_gen INTEGER",
        "ALTER TABLE passes ADD COLUMN w_pending INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE snapshots ADD COLUMN job_id TEXT",
        "ALTER TABLE snapshots ADD COLUMN read_seq INTEGER",
        "ALTER TABLE snapshots ADD COLUMN acq INTEGER",
        "ALTER TABLE snapshots ADD COLUMN export_ref TEXT",
        "ALTER TABLE snapshots ADD COLUMN swept_at TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots_export_ref ON snapshots(export_ref)"
        " WHERE export_ref IS NOT NULL",
        "ALTER TABLE projections ADD COLUMN readback_owed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE projections ADD COLUMN note_issued_gen INTEGER",
        "ALTER TABLE projections ADD COLUMN note_other_issued_gen INTEGER",
        "ALTER TABLE projections ADD COLUMN note_seen_gen INTEGER",
        "ALTER TABLE probes ADD COLUMN gen INTEGER",
        CLAIMS_DDL, WORK_REQUESTS_DDL],
```

`CLAIMS_DDL` and `WORK_REQUESTS_DDL` are module constants, and `DDL` embeds the same strings.
In `migrate`, after the loop over `MIGRATIONS`, inside the same transaction:

```python
        if current < 10:
            set_epoch(conn)     # §4: a read confirms a note only >= Z after the migration
            import passes       # lazy: passes imports db
            passes.close_delegation_pass_on_upgrade(conn)
```

In `server/passes.py` add:

```python
def close_delegation_pass_on_upgrade(conn) -> None:
    """Schema 10 (spec §8): a live delegation-protocol pass is ended `interrupted`
    through _terminalize, and the marker goes dead, so every old token is refused by
    check_token. Inside migrate's transaction."""
    m = _marker(conn)
    if m is None or not m["live"]:
        return
    _terminalize(conn, m["pass_id"])
    conn.execute("UPDATE pass_marker SET live=0, claimed_step=NULL, lease_at=NULL WHERE id=1")
```

There are no work requests before schema 10, so the upgrade has nothing to requeue. Task 6
adds the requeue to `_terminalize` for passes ended later.

In `tests/schema_history.py`, add `build_v9_store(path, live_pass=False)`. It writes the
schema-9 DDL (copied from `server/db.py` at `86656ac`, verbatim) and sets `schema_version=9`.
With `live_pass`, it also inserts a live `pass_marker` (generation 1, `pass_id 'p1'`, trigger
`cron`) and a `passes` row `p1`.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_schema tests.test_db -v`, then the whole suite.
Expected: PASS. If `tests/test_db.py` pins the schema text, update it to the schema-10 DDL in
this task.

- [ ] **Step 5: Commit**

```bash
git add server/db.py server/passes.py tests/test_s2_schema.py tests/schema_history.py tests/test_db.py
git commit -m "feat(s2): schema 10 — claims, work requests, job columns; upgrade closes a live delegation pass"
```

---

### Task 2: The sweep's read-back debt (§5.3)

**Files:**
- Modify: `server/sweep.py` (`record_observation`, `_due`)
- Test: `tests/test_s2_readback.py`

**Interfaces:**
- Produces: `projections.readback_owed`. It is set by an observation that returns
  instructions, and cleared by the next observation of that pid, by `write_error`, by
  `not_found` or by the lineage ending. `_due` lists it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_s2_readback.py
"""The sweep's read-back debt (spec §5.3), against the REAL bank-feed."""
from tests import sim
from tests.test_sweep_real import Base
import sweep  # noqa: E402


class ReadbackDebt(Base):
    def owed_item(self):
        self.bf.fetch([self.bf.row("2026-07-05", ref="R1")])
        self.new_pass()
        page = sweep.list_projections(self.conn, token=self.token)
        return page["projections"][0], page["snapshot_id"]

    def observe(self, item, snap, **extra):
        tags, notes, first_seen, rev = sim._read(self.bf, item["row_id"])
        return sweep.record_observation(self.conn, pid=item["pid"], token=self.token,
                                        snapshot_id=snap, observed_tags=tags,
                                        observed_notes=notes, observed_first_seen=first_seen,
                                        observed_tag_revision=rev, **extra)

    def debt(self, pid):
        return self.conn.execute("SELECT readback_owed FROM projections WHERE pid=?",
                                 (pid,)).fetchone()[0]

    def test_a_cut_after_the_instruction_keeps_the_row_due(self):
        item, snap = self.owed_item()
        out = self.observe(item, snap)
        self.assertTrue(out["instructions"])         # one write is owed
        # the turn is cut here: the write is never made and the row never re-read
        self.assertIn(item["pid"], sweep._due(self.conn))
        self.assertEqual(self.debt(item["pid"]), 1)

    def test_a_confirming_read_clears_it(self):
        item, snap = self.owed_item()
        self.observe(item, snap)
        self.assertEqual(self.debt(item["pid"]), 1)  # set first, so the clear is evidence
        sim.observe_and_repair(self.conn, self.bf, self.token, item, snap)
        self.assertNotIn(item["pid"], sweep._due(self.conn))
        self.assertEqual(self.debt(item["pid"]), 0)

    def test_write_error_clears_it(self):
        item, snap = self.owed_item()
        self.observe(item, snap)
        self.assertEqual(self.debt(item["pid"]), 1)
        sweep.record_observation(self.conn, pid=item["pid"], token=self.token,
                                 snapshot_id=snap, write_error="refused: test")
        self.assertEqual(self.debt(item["pid"]), 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_s2_readback -v`
Expected: FAIL. `readback_owed` stays 0, and the cut row is not in `_due`.

- [ ] **Step 3: Implement**

In `sweep.record_observation`:
- In the transaction, just before `return`: if `instructions` is non-empty,
  `UPDATE projections SET readback_owed=1 WHERE pid=?`; else
  `UPDATE projections SET readback_owed=0 WHERE pid=?`.
- On the `write_error` and `not_found` branches (each returns earlier):
  `UPDATE projections SET readback_owed=0 WHERE pid=?`.

In `_due`, extend the WHERE clause:

```python
        " AND (class_observed_snapshot IS NULL OR class_observed_snapshot < ?"
        "      OR observed_revision IS NULL OR observed_revision <> revision"
        "      OR readback_owed = 1)"
```

An ended lineage is already excluded by `ended IS NULL OR ended='vanished'`. When a lineage
ends, clear its debt in `ledger.end_lineage` (`UPDATE projections SET readback_owed=0`).

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_readback tests.test_sweep_real tests.test_e2e -v`, then
the suite. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sweep.py server/ledger.py tests/test_s2_readback.py
git commit -m "fix(s2): a row whose bank write is unconfirmed stays due (read-back debt, spec §5.3)"
```

---

### Task 3: Job passes ignore the clocks; in-tx cores for steps and pass end

**Files:**
- Modify: `server/steps.py` (`_ended`, `clock`, `sweep_allowance`, `start` → `_start_tx`, `finish` → `_finish_tx`, `_choose`/`claim` gain `sends_only`)
- Modify: `server/passes.py` (`start_pass` gains `protocol`/`token`; `end_pass` → `_end_pass_tx`)
- Test: `tests/test_s2_clockless.py`

**Interfaces:**
- Produces:
  - `passes.start_pass(conn, trigger, reply, *, protocol="delegation", token=None) -> (token, pass_id)`.
    For `protocol="job"` it uses the given `token` as the marker generation (no rotation), and
    `pass_id = f"j{token}.{db.next_seq(conn)}"`.
  - `passes._end_pass_tx(conn, token, outcome, report) -> dict`: end_pass's transaction body,
    returning `{"ended", "outcome", "report", **handed}` without reaping or rendering.
    `end_pass` wraps it unchanged.
  - `steps._start_tx(conn, token, step, carry) -> dict` and `steps._finish_tx(conn, token,
    step, *, counts, stopped=None, failed=False, by_refusal=False, out_of_time=False) ->
    dict`: the bodies; `start`/`finish` wrap them.
  - A `pass_steps` row inserted under a job pass carries `protocol='job'`.
  - `steps._choose(conn, sends_only=False)`: with `sends_only`, it skips its pass and round
    branches (used by Task 10's `_claim_sends_tx`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_s2_clockless.py
import datetime as _dt
from tests._base import StoreCase


class Clockless(StoreCase):
    def job_pass(self):
        import db, passes
        with db.tx(self.conn):
            tok = passes.rotate(self.conn)
            _, pid = passes.start_pass(self.conn, "operator", "telegram", protocol="job",
                                       token=tok)
        return tok, pid

    def test_job_pass_keeps_the_claim_token_and_records_protocol(self):
        tok, pid = self.job_pass()
        m = self.conn.execute("SELECT generation, live FROM pass_marker").fetchone()
        self.assertEqual((m["generation"], m["live"]), (tok, 1))
        self.assertEqual(self.conn.execute("SELECT protocol FROM passes WHERE pass_id=?",
                                           (pid,)).fetchone()[0], "job")

    def test_no_clock_no_expiry_no_time_up_in_a_job_pass(self):
        import db, steps
        tok, pid = self.job_pass()
        steps.start(self.conn, tok, "sweep", {})
        later = db._clock() + _dt.timedelta(seconds=steps.STEP_EXPIRY_S + 5)
        with self.patch_clock(later):
            self.assertIsNone(steps.clock(self.conn, tok))
            self.assertIsNone(steps.sweep_allowance(self.conn))
            row = steps.latest(self.conn, pid)
            self.assertIsNone(steps._ended(row))      # a job step never expires by age

    def test_delegation_pass_still_expires(self):
        import db, steps
        tok = self.pass_("cron")
        steps.start(self.conn, tok, "sweep", {})
        later = db._clock() + _dt.timedelta(seconds=steps.STEP_EXPIRY_S + 5)
        with self.patch_clock(later):
            pid = self.conn.execute("SELECT pass_id FROM pass_marker").fetchone()[0]
            self.assertEqual(steps._ended(steps.latest(self.conn, pid)), "expired")
```

`patch_clock` is a context manager added to `StoreCase` in this task. It replaces
`db._clock` with a function returning the given datetime, and restores it on exit.

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_s2_clockless -v`
Expected: FAIL (`start_pass` takes no `protocol`).

- [ ] **Step 3: Implement**

`passes.start_pass`:

```python
def start_pass(conn, trigger: str, reply: str, *, protocol="delegation", token=None) -> tuple:
    if protocol == "job":
        if token is None:
            raise RuntimeError("a job pass starts under its claim's token")
        gen, pass_id = token, f"j{token}.{db.next_seq(conn)}"
    else:
        gen = rotate(conn)
        pass_id = f"p{gen}"
    now = db.now()
    conn.execute("INSERT OR REPLACE INTO pass_marker(id, generation, live, pass_id, trigger,"
                 " started_at, claimed_step, lease_at) VALUES (1, ?, 1, ?, ?, ?, NULL, NULL)",
                 (gen, pass_id, trigger, now))
    conn.execute("INSERT INTO passes(pass_id, generation, trigger, started_at, reply, protocol)"
                 " VALUES (?, ?, ?, ?, ?, ?)", (pass_id, gen, trigger, now, reply, protocol))
    return gen, pass_id
```

`passes.protocol_of(conn, pass_id) -> str` reads `passes.protocol`.

`steps`:
- `_start_tx` and `_finish_tx` hold the current bodies of `start`/`finish` inside their
  `with db.tx(conn):` (they assert `conn.in_transaction`). The INSERT of a new step row adds
  `protocol` (from `passes.protocol_of`), `started_seq = db.next_seq(conn)` and
  `started_gen = int(token)` (design §8); a judge restart also sets both.
- `start`/`finish` become `with db.tx(conn): return _start_tx(...)` (`finish` keeps its
  raise-after-commit for a refused stop: `_finish_tx` returns `(result, refused)` and `finish`
  raises after the `with`).
- `_ended`:

```python
def _ended(step):
    if step["finished_at"] is not None:
        return "errored" if _finish(step).get("failed") else "finished"
    if step["protocol"] == "job":
        return None         # spec INV-J3: a job step never expires; the cursor resumes it
    if _age(step["started_at"]) >= STEP_EXPIRY_S:
        return "expired"
    return None
```

- `clock` and `sweep_allowance` return `None` when the live pass's protocol is `job`.
- `_choose(conn, sends_only=False)`: when `sends_only`, skip the `m is not None` pass block and
  the `round` branch, and return only the `delivery`/`request`/`none` outcomes. `claim` is
  unchanged.

`passes.judgment_gap(conn, pass_id) -> int` is the count `_judgment_owed` computes: judge-due
payments not covered by a finished judgment of this pass. `_judgment_owed` raises from it as
today. The cursor (Task 7) reads the count instead of matching refusal text.

`passes._end_pass_tx` takes the body of `end_pass`'s `with db.tx(conn):` block and returns
`{"ended": m["pass_id"], "outcome": outcome, "report": full, **(handed or {})}`, with
`_notice` still inside `handed`. `end_pass` becomes the transaction plus the existing
post-commit reap and rendering.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_clockless -v`, then the suite.
Expected: PASS, with every existing continuation and pass test unchanged.

- [ ] **Step 5: Commit**

```bash
git add server/steps.py server/passes.py tests/_base.py tests/test_s2_clockless.py
git commit -m "refactor(s2): job passes ignore step expiry and the clock; in-tx cores for step start/finish and pass end"
```

---

### Task 4: Claims, the claim fence, adoption and its budget (§4, §6.3)

**Files:**
- Create: `server/job.py`
- Test: `tests/test_s2_claim.py`

**Interfaces:**
- Consumes: `passes.rotate`, `passes._marker`, `passes._terminalize`, `passes._end_pass_tx`
  (Task 3), `passes.close_delegation_pass_on_upgrade` (Task 1).
- Produces:
  - `job.JOB_ID_RE` (`^[0-9a-fA-F-]{8,64}$`);
  - `job.ADOPTIONS_MAX = 2`;
  - `job.claim(conn, job_id) -> int`, the new token. It opens its own transaction.
  - `job.check_claim(conn, token) -> None`: in-tx. It refuses unless `token` is the highest
    `claims.gen`. When a job pass is live, it also runs `passes.check_token`.
  - `job.live_job_pass(conn) -> sqlite3.Row | None`.
  - `job.stop_exhausted_pass(conn, token, pass_id) -> None`: in-tx; a hook Task 6 completes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_s2_claim.py
from tests._base import StoreCase

A, B, C, D = "aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3", "dddddddd-4"


class Claim(StoreCase):
    def test_each_claim_rotates_and_kills_the_previous_token(self):
        import db, job
        t1 = job.claim(self.conn, A)
        t2 = job.claim(self.conn, A)
        self.assertGreater(t2, t1)
        with db.tx(self.conn):
            job.check_claim(self.conn, t2)
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                job.check_claim(self.conn, t1)

    def test_a_claim_ends_a_live_delegation_pass_first(self):
        import job
        self.pass_("cron")                      # a delegation pass (old protocol)
        job.claim(self.conn, A)
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)

    def test_adoption_by_another_job_is_counted_once_per_job(self):
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        job.claim(self.conn, A)                 # same job, next batch: not an adoption
        job.claim(self.conn, B)                 # adoption 1
        job.claim(self.conn, B)                 # same adopter again: not counted
        row = self.conn.execute("SELECT adoptions, holder_job FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual((row["adoptions"], row["holder_job"]), (1, B))

    def test_a_job_that_held_the_pass_before_is_not_charged_again(self):
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        for j in (B, A, B, A, B):                # A→B→A→B…: one adoption, by B
            job.claim(self.conn, j)
        row = self.conn.execute("SELECT adoptions, ended_at FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertEqual((row["adoptions"], row["ended_at"]), (1, None))

    def test_third_adoption_stops_the_pass(self):
        import job
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        job.claim(self.conn, B)
        job.claim(self.conn, C)
        job.claim(self.conn, D)                 # would be the third adoption
        row = self.conn.execute("SELECT ended_at, outcome FROM passes WHERE pass_id=?",
                                (pid,)).fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["outcome"], "stopped")
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone()[0], 0)

    def test_job_id_is_validated(self):
        import db, job
        with self.assertRaises(db.Refusal):
            job.claim(self.conn, "x")
```

`StoreCase.start_job_pass(token, trigger="operator")` is added in this task. It opens
`db.tx`, calls `passes.start_pass(..., protocol="job", token=token)`, and sets
`passes.holder_job` to the claim's job id and `adopters_json` to `[<that job id>]`, as
Task 7's `_begin_next` does.

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_s2_claim -v`
Expected: FAIL (there is no module `job`).

- [ ] **Step 3: Implement `server/job.py` (claim part)**

```python
"""S2 (spec §4–§6): the job's cursor. A job turn's first, token-less job_next claims:
the claim rotates the token and records itself in `claims`, and only the newest claim's
token may act (check_claim). A pass held by another job is adopted, at most
ADOPTIONS_MAX times; a claim that would adopt once more ends it `stopped`."""
from __future__ import annotations

import json
import re

import db
import passes

JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
ADOPTIONS_MAX = 2


def live_job_pass(conn):
    m = passes._marker(conn)
    if m is None or not m["live"]:
        return None
    p = conn.execute("SELECT * FROM passes WHERE pass_id=?", (m["pass_id"],)).fetchone()
    return p if p is not None and p["protocol"] == "job" else None


def check_claim(conn, token) -> None:
    assert conn.in_transaction
    if token is None:
        raise db.Refusal("a job turn starts with job_next(job_id=…): pass the pass_token it gave you")
    top = conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
    if top is None or int(token) != top:
        raise db.Refusal("this job turn is no longer the current one (a newer turn claimed "
                         "the work); stop — nothing was written")
    if live_job_pass(conn) is not None:
        passes.check_token(conn, token)


def claim(conn, job_id) -> int:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise db.Refusal("job_id is the `Job id:` line of your brief, as given")
    with db.tx(conn):
        m = passes._marker(conn)
        if m is not None and m["live"] and passes.protocol_of(conn, m["pass_id"]) != "job":
            passes.close_delegation_pass_on_upgrade(conn)        # spec §8
        token = passes.rotate(conn)
        conn.execute("INSERT INTO claims(gen, job_id, at) VALUES (?,?,?)",
                     (token, job_id, db.now()))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('drain', ?)", (job_id,))
        p = live_job_pass(conn)
        if p is not None:
            # every job id that ever held the pass, its starter first (spec §6.3: one
            # adoption per adopting job id, whatever came in between)
            held = json.loads(p["adopters_json"])
            if job_id not in held:
                if p["adoptions"] >= ADOPTIONS_MAX:
                    stop_exhausted_pass(conn, token, p["pass_id"])
                    _stamp_measure(conn, token)
                    return token
                conn.execute("UPDATE passes SET adoptions=adoptions+1, adopters_json=?"
                             " WHERE pass_id=?", (json.dumps(held + [job_id]), p["pass_id"]))
            conn.execute("UPDATE passes SET holder_job=?, orphaned_by=NULL WHERE pass_id=?",
                         (job_id, p["pass_id"]))
            conn.execute("UPDATE pass_marker SET generation=? WHERE id=1", (token,))
        _stamp_measure(conn, token)
        return token


def stop_exhausted_pass(conn, token, pass_id) -> None:
    """The adoption budget is spent (spec §6.3): the pass ends `stopped`, and Task 6
    gives its requests their dispositions in this same transaction."""
    conn.execute("UPDATE pass_marker SET generation=? WHERE id=1", (token,))
    passes._end_pass_tx(conn, token, "stopped", {"adoptions_exhausted": True})


def measure(conn) -> list:
    """INV-J8: the work measure, compared lexicographically; a fall is progress. A
    refresh raises only the last component (rows due a read)."""
    import steps, sweep, work
    open_requests = (conn.execute("SELECT count(*) FROM work_requests WHERE state IN"
                                  " ('queued','taken')").fetchone()[0]
                     + conn.execute("SELECT count(*) FROM package_requests WHERE state IN"
                                    " ('queued','snapshot')").fetchone()[0])
    p = live_job_pass(conn)
    if p is None:
        return [open_requests, 0, 0, 0]
    req = steps.round_request(conn, p["pass_id"])
    carry = steps._first_carry(conn, p["pass_id"])
    if req is not None:
        unsearched = len(work.package_work(conn, req))
    elif carry.get("since_seq") is not None:
        unsearched = len(work.check_work(conn, carry["since_seq"], owed=carry.get("owed", [])))
    else:
        unsearched = 0
    return [open_requests, unsearched, -p["judge_pages"], len(sweep._due(conn))]


def _stamp_measure(conn, token) -> None:
    conn.execute("UPDATE claims SET measure_json=? WHERE gen=?",
                 (json.dumps(measure(conn)), token))
```

Add `test_a_claim_stamps_the_measure` to `tests/test_s2_claim.py`: after
`job.claim(self.conn, A)`, `json.loads(claims.measure_json)` equals `job.measure(self.conn)`.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_claim -v`, then the suite. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/job.py tests/_base.py tests/test_s2_claim.py
git commit -m "feat(s2): job claims — per-turn token, claim fence, adoption with a two-adoption budget"
```

---

### Task 5: Acquisitions, the import binding and F (§5.2)

**Files:**
- Modify: `server/job.py` (`fresh_reason`, `require_fresh`, `hand_acquisition`)
- Modify: `server/passes.py` (`record_probe` gains `acq`, `absent`; stores `gen`; ledger `missing`)
- Modify: `server/ledger.py` (`import_ledger_export(..., acq=None)`)
- Modify: `server/sweep.py` (`list_projections` stamps `snapshots.swept_at`)
- Modify: `server/matches.py` (F gate)
- Test: `tests/test_s2_fresh.py`

**Interfaces:**
- Produces:
  - `job.W_S = 1800`, `job.W_REFRESH_MAX = 2`;
  - `job.hand_acquisition(conn, token, pass_id) -> int` (in-tx: a new `acq = next_seq`;
    `read_seq = next_seq`; `acq_gen = token`);
  - `job.fresh_reason(conn) -> str | None` (in-tx);
  - `job.require_fresh(conn) -> None` (in-tx; raises `db.Refusal` with "the bank must be read
    again: call job_next").
  - `record_probe(conn, token, kind, ok, detail="", data=None, *, acq=None, absent=False)`;
  - `import_ledger_export(conn, *, path, token, ledger_instance, acq=None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_s2_fresh.py
from tests._base import StoreCase

A = "aaaaaaaa-1"


class Acquisition(StoreCase):
    """The import is bound to its acquisition by identity (spec §5.2, INV-J14)."""

    def setUp(self):
        super().setUp()
        import job
        self.bind()
        self.tok = job.claim(self.conn, A)
        self.pid = self.start_job_pass(self.tok)

    def probes(self, acq):
        import passes
        b = self.conn.execute("SELECT account_id FROM binding").fetchone()[0]
        passes.record_probe(self.conn, self.tok, "bank_tools", True)
        passes.record_probe(self.conn, self.tok, "bank_accounts", True,
                            data={"accounts": [{"account_id": b, "category": "company",
                                                "label": "Zakelijk"}]})
        passes.record_probe(self.conn, self.tok, "bank_sync", True, acq=acq)
        passes.record_probe(self.conn, self.tok, "ledger", True,
                            data={"generation": 0, "registered": {}, "instance": self.LEDGER,
                                  "missing": []})

    def export(self, rows):
        """An export file in the handoff folder: StoreCase.export_csv publishes it and
        returns its path, as bank-feed's export_history does."""
        return self.export_csv(rows)

    def handed(self):
        import db, job
        with db.tx(self.conn):
            return job.hand_acquisition(self.conn, self.tok, self.pid)

    def test_import_needs_its_acquisitions_sync_under_the_same_claim(self):
        import db, ledger
        acq = self.handed()
        path = self.export([{"row_id": 1}])
        with self.assertRaises(db.Refusal):            # no bank_sync for this acq yet
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq)
        self.probes(acq)
        out = ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                          ledger_instance=self.LEDGER, acq=acq)
        self.assertEqual(out["rows"], 1)

    def test_an_export_is_imported_once(self):
        import db, ledger
        acq = self.handed()
        self.probes(acq)
        path = self.export([{"row_id": 1}])
        ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                    ledger_instance=self.LEDGER, acq=acq)
        acq2 = self.handed()
        self.probes(acq2)
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq2)

    def test_a_superseded_acquisition_is_refused(self):
        import db, ledger
        acq1 = self.handed()
        self.probes(acq1)
        acq2 = self.handed()                            # re-handed in the same claim
        path = self.export([{"row_id": 1}])
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq1)

    def test_another_claims_acquisition_is_refused(self):
        import db, job, ledger
        acq = self.handed()
        self.probes(acq)
        self.tok = job.claim(self.conn, A)              # the turn ended; a new turn
        path = self.export([{"row_id": 1}])
        with self.assertRaises(db.Refusal):
            ledger.import_ledger_export(self.conn, path=path, token=self.tok,
                                        ledger_instance=self.LEDGER, acq=acq)


class FreshnessF(Acquisition):
    def imported(self):
        import ledger
        acq = self.handed()
        self.probes(acq)
        ledger.import_ledger_export(self.conn, path=self.export([{"row_id": 1}]),
                                    token=self.tok, ledger_instance=self.LEDGER, acq=acq)

    def reason(self):
        import db, job
        with db.tx(self.conn):
            return job.fresh_reason(self.conn)

    def test_condition_1_another_jobs_import(self):
        import job
        self.imported()
        self.sweep_to_zero()
        self.assertIsNone(self.reason())
        self.tok = job.claim(self.conn, "bbbbbbbb-2")    # adoption by another job
        self.assertIn("another job", self.reason())

    def test_condition_2_a_request_after_the_watermark(self):
        import asks
        self.imported()
        self.sweep_to_zero()
        asks.request_work(self.conn, "check", "operator")
        import db
        with db.tx(self.conn):
            asks.take_queued(self.conn, self.pid)
        self.assertIn("asked after", self.reason())

    def test_condition_3_w_from_the_sweeps_completion_and_its_cap(self):
        import datetime as _dt, db, job
        self.imported()
        self.sweep_to_zero()
        later = db._clock() + _dt.timedelta(seconds=job.W_S + 1)
        with self.patch_clock(later):
            self.assertIn("older than", self.reason())
            with db.tx(self.conn):
                self.conn.execute("UPDATE passes SET w_refreshes=? WHERE pass_id=?",
                                  (job.W_REFRESH_MAX, self.pid))
            self.assertIsNone(self.reason())           # waived after two W refreshes

    def test_machine_pairing_refuses_while_f_fails(self):
        import db, job
        self.imported()
        self.sweep_to_zero()
        self.tok = job.claim(self.conn, "bbbbbbbb-2")    # condition 1 now fails
        pid = self.only_pid()
        doc = self.doc()
        with self.assertRaises(db.Refusal) as cm:
            self.machine_match(pid, doc, self.tok)
        self.assertIn("call job_next", str(cm.exception))
```

`StoreCase` gains these helpers in this task (each a few lines, built on the existing
`EXPORT_COLS`, `row()`, `lineage_for()` and `tests/sim.py`):
- (`export_csv(rows)` already exists: it publishes a synthetic export and returns its path);
- `sweep_to_zero()` lists and observes until `remaining_in_cycle` is 0, with no writes owed
  (pass `observed_tags` equal to `desired`);
- `only_pid()` is the single live lineage's pid;
- `machine_match(pid, doc_id, token)` calls `matches.record_match(author="auto", …)` with the
  item's `row_digest` and revision from `work.list_quarter_state(pid=…)` and a
  `document_date`.

The `FreshnessF` tests use `asks.request_work` and `asks.take_queued` from Task 6. **Order:**
write `tests/test_s2_fresh.py` now, but mark `test_condition_2…` with
`@unittest.skipUnless(_has("asks"), "Task 6")`, where `_has(m)` checks importability. Task 6
removes the skip.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_s2_fresh -v`
Expected: FAIL (`hand_acquisition` is missing; `record_probe` takes no `acq`).

- [ ] **Step 3: Implement**

`passes.record_probe`:
- Add the keyword parameters `acq=None, absent=False`.
- Store `gen = int(token)` in the new `probes.gen` column.
- For `kind == "bank_sync"`, merge `{"acq": acq}` into `data` when `acq` is given.
- For `kind == "gmail"` with `absent=True`, require `ok is False` (refuse otherwise) and store
  `{"absent": True}` in `data`.
- For `kind == "ledger"`, accept `data["missing"]` (a list of workflow strings) and store it as
  given.

`job.hand_acquisition(conn, token, pass_id)`:

```python
def hand_acquisition(conn, token, pass_id) -> int:
    """A new bank read for the pass (spec §5.2): its id and its request watermark,
    owned by this claim alone."""
    acq, read_seq = db.next_seq(conn), db.next_seq(conn)
    conn.execute("UPDATE passes SET acq=?, acq_gen=?, read_seq=? WHERE pass_id=?",
                 (acq, token, read_seq, pass_id))
    return acq
```

`ledger.import_ledger_export` and `ledger._import`:
- Add `import os` and `import pathlib` at the top of `server/ledger.py` (neither is imported
  today).
- The public function takes `acq=None`. After `casa_handoff.capture(path)` succeeds, it
  computes `export_ref = pathlib.Path(os.path.realpath(path)).parent.name`. `capture` has
  already proved the layout `<root>/<producer>/<id>/<filename>`, so this is the handoff id. It
  then passes both on: `_import(conn, rows, token, ledger_instance, acq=acq,
  export_ref=export_ref)`. The custody lock is still taken before `_import`'s transaction.
- `_import(conn, rows, token, ledger_instance, *, acq=None, export_ref=None)`. Inside its
  transaction, after `passes.check_token`, when the current pass's protocol is `job`:

```python
        if cur_pass["protocol"] == "job":
            if acq is None or cur_pass["acq"] != acq:
                raise db.Refusal("this import is not for the pass's current bank read: call "
                                 "job_next and do the bank read it hands out")
            if cur_pass["acq_gen"] != int(token):
                raise db.Refusal("this bank read belongs to an earlier turn: call job_next")
            sync = conn.execute("SELECT ok, gen, data_json FROM probes WHERE kind='bank_sync'"
                                ).fetchone()
            if (sync is None or not sync["ok"] or sync["gen"] != int(token)
                    or json.loads(sync["data_json"] or "{}").get("acq") != acq):
                raise db.Refusal("record this bank read's sync first (record_probe "
                                 "kind=\"bank_sync\" with its acq), then export and import")
            if conn.execute("SELECT 1 FROM snapshots WHERE export_ref=?",
                            (export_ref,)).fetchone():
                raise db.Refusal("this export was imported already — export again")
```

- In the `INSERT INTO snapshots`, add `job_id` (`cur_pass["holder_job"]`), `read_seq`
  (`cur_pass["read_seq"]`), `acq` and `export_ref` (for a job pass; NULL otherwise).

`sweep.list_projections`: in the `if not due_all:` branch, also run
`UPDATE snapshots SET swept_at=coalesce(swept_at, ?) WHERE snapshot_id=?` with
`(db.now(), lineage.latest_import(conn))`. For a package pass the quarter's `due` decides:
stamp when `quarter is not None and not due`.

`job.fresh_reason`:

```python
W_S = 1800
W_REFRESH_MAX = 2
NOT_READ = "the bank was not read in this pass yet"
OTHER_JOB = "the bank was read by another job"
LATE_ASK = "a check was asked after the bank was read"
UNSWEPT = "the bank read's sweep is not finished"
STALE = f"the bank read is older than {W_S // 60} minutes"


def fresh_reason(conn):
    """F (spec §5.2): None when the live job pass may decide on its latest import, else
    why not. Conditions 1 and 2 are never waived; 3 is waived after W_REFRESH_MAX."""
    p = live_job_pass(conn)
    if p is None:
        return None
    s = conn.execute("SELECT * FROM snapshots WHERE pass_id=? ORDER BY snapshot_id DESC"
                     " LIMIT 1", (p["pass_id"],)).fetchone()
    if s is None:
        return NOT_READ
    if s["job_id"] != p["holder_job"]:
        return OTHER_JOB
    late = conn.execute("SELECT max(created_seq) FROM work_requests WHERE pass_id=? AND"
                        " state='taken'", (p["pass_id"],)).fetchone()[0]
    if late is not None and (s["read_seq"] is None or late >= s["read_seq"]):
        return LATE_ASK
    if s["swept_at"] is None:
        return UNSWEPT
    if p["w_refreshes"] < W_REFRESH_MAX and passes._age_s(s["swept_at"]) >= W_S:
        return STALE
    return None


def require_fresh(conn) -> None:
    why = fresh_reason(conn)
    if why is not None:
        raise db.Refusal(f"{why}: the bank must be read again: call job_next")
```

(Condition 2 compares with `>=`, so a request in the same sequence step as the watermark is
not served.)

`matches`:
- In the transactions of `record_match` (when `author == "auto"`), `propose_match` and
  `relabel_match`, after `passes.check_token`, add `job.require_fresh(conn)`.
- `require_fresh` returns at once for a delegation pass (`live_job_pass` is None).

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_fresh tests.test_ledger tests.test_matches -v`, then
the suite. Expected: PASS, with `test_condition_2` skipped until Task 6.

- [ ] **Step 5: Commit**

```bash
git add server/job.py server/passes.py server/ledger.py server/sweep.py server/matches.py tests/_base.py tests/test_s2_fresh.py
git commit -m "feat(s2): acquisitions bound by identity; F gates machine decisions (spec §5.2)"
```

---

### Task 6: Requests and their dispositions (§6.1–6.2, INV-J9, INV-J12)

**Files:**
- Create: `server/asks.py`
- Modify: `server/passes.py` (`requeue_requests` body; `_end_pass_tx` calls `asks.settle_taken`)
- Modify: `server/job.py` (`stop_exhausted_pass` closes the package request)
- Modify: `server/binding.py:136` (`_TABLES_TO_WIPE` gains `claims` and `work_requests`;
  `reset_store` deletes `meta.drain` in the same transaction)
- Test: `tests/test_s2_asks.py`

**Interfaces:**
- Produces:
  - `asks.JOB = "quarterly-accounting:work"`;
  - `asks.request_work(conn, kind, trigger, doc_ids=None) -> {"request_id", "line",
    "start_job": {"job", "task", "context"}}`;
  - `asks.request_package(conn, quarter, channel) -> {"status": "asked"|"already", "line",
    "start_job"}`;
  - `asks.take_queued(conn, pass_id) -> list[int]` (in-tx);
  - `asks.settle_taken(conn, pass_id, outcome) -> None` (in-tx);
  - `asks.requeue_taken(conn, pass_id) -> None` (in-tx);
  - `asks.record_verdicts(conn, pass_id, documents) -> None` (in-tx);
  - `asks.handover_covered(conn, pass_id) -> bool` (in-tx).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_s2_asks.py
from tests._base import StoreCase

A = "aaaaaaaa-1"


class Requests(StoreCase):
    def test_request_records_then_offers_start_job(self):
        import asks
        out = asks.request_work(self.conn, "check", "operator")
        self.assertEqual(out["start_job"]["job"], "quarterly-accounting:work")
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "queued")

    def test_a_package_ask_opens_a_queued_request_without_a_pass(self):
        import asks
        out = asks.request_package(self.conn, "2026-Q3", "telegram")
        self.assertEqual(out["status"], "asked")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "queued")
        self.assertEqual(self.conn.execute("SELECT live FROM pass_marker").fetchone(), None)
        self.assertEqual(asks.request_package(self.conn, "2026-Q3", "email")["status"],
                         "already")

    def test_taken_only_by_the_live_pass_and_settled_at_its_end(self):
        import asks, db, job, passes
        asks.request_work(self.conn, "check", "cron")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            self.assertEqual(len(asks.take_queued(self.conn, pid)), 1)
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "stopped", {})
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))

    def test_terminalize_requeues(self):
        import asks, db, job, passes
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            passes._terminalize(self.conn, pid)
        r = self.conn.execute("SELECT state, pass_id FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["pass_id"]), ("queued", None))

    def test_exhausted_adoptions_close_a_check_pass_requests(self):
        import asks, db, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        for j in ("bbbbbbbb-2", "cccccccc-3", "dddddddd-4"):
            job.claim(self.conn, j)
        w = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((w["state"], w["outcome"]), ("done", "stopped"))

    def test_exhausted_adoptions_close_a_package_round_and_leave_other_work_queued(self):
        import asks, job
        asks.request_package(self.conn, "2026-Q3", "telegram")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t, trigger="package")
        self.bind_round_and_take(pid)
        asks.request_work(self.conn, "check", "operator")   # not this pass's to take
        for j in ("bbbbbbbb-2", "cccccccc-3", "dddddddd-4"):
            job.claim(self.conn, j)
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "stopped")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts WHERE kind="
                                           "'package-stopped'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "queued")

    def test_reset_fences_every_claim_and_drops_requests(self):
        import asks, binding, db, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        binding.reset_store(self.conn)
        with self.assertRaises(db.Refusal):
            with db.tx(self.conn):
                job.check_claim(self.conn, t)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests").fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT value FROM meta WHERE key='drain'").fetchone())

    def test_a_handover_is_done_only_after_a_later_whole_judgment_naming_its_docs(self):
        import asks, db, job, steps
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        steps.start(self.conn, t, "sweep", {})
        steps.finish(self.conn, t, "sweep", counts={"remaining_in_cycle": 0})
        self.hand_empty_chunk()
        steps.start(self.conn, t, "judge", {})
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})  # before the ask
        d = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
            self.assertFalse(asks.handover_covered(self.conn, pid))
        steps.start(self.conn, t, "judge", {})          # restarted after the ask
        with db.tx(self.conn):
            asks.record_verdicts(self.conn, pid, {str(d): "no-payment-yet"})
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})
        with db.tx(self.conn):
            self.assertTrue(asks.handover_covered(self.conn, pid))

    def test_a_verdict_from_an_earlier_judgment_does_not_cover(self):
        import asks, db, job, steps
        t = job.claim(self.conn, A)
        pid = self.start_job_pass(t)
        steps.start(self.conn, t, "sweep", {})
        steps.finish(self.conn, t, "sweep", counts={"remaining_in_cycle": 0})
        self.hand_empty_chunk()
        d = self.doc()
        asks.request_work(self.conn, "handover", "operator", doc_ids=[d])
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        steps.start(self.conn, t, "judge", {})
        with db.tx(self.conn):
            asks.record_verdicts(self.conn, pid, {str(d): "no-payment-yet"})
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})
        steps.start(self.conn, t, "judge", {})          # a restart (e.g. after a refresh)
        steps.finish(self.conn, t, "judge", counts={"triage_remaining": 0})   # names nothing
        with db.tx(self.conn):
            self.assertFalse(asks.handover_covered(self.conn, pid))
```

The helpers `StoreCase.bind_round_and_take(pass_id)` (`passes._bind_round` on the queued
package request + `asks.take_queued`) and `hand_empty_chunk()` (the existing `hand(conn, [])`)
are added here. Remove the Task-5 skip on `test_condition_2…`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_s2_asks -v`
Expected: FAIL (there is no module `asks`).

- [ ] **Step 3: Implement `server/asks.py` (request half)**

```python
"""S2 (spec §6): what Ellen asks for, recorded before start_job so no ask is lost, and
what becomes of it. A request is queued, taken by the live job pass only (INV-J9), done
at that pass's end with its outcome, and reported once its result was shown."""
from __future__ import annotations

import json

import db
import passes

JOB = "quarterly-accounting:work"
START = {"job": JOB, "task": "Run the accounting work that is waiting.", "context": ""}
LINES = {"check": "Checking the bank and your email — I'll send the result here.",
         "handover": "Filed. Checking it against the payments — I'll tell you shortly.",
         "cron": ""}


def request_work(conn, kind, trigger, doc_ids=None) -> dict:
    if kind not in ("check", "handover"):
        raise db.Refusal("kind is 'check' or 'handover'")
    if trigger not in ("cron", "operator"):
        raise db.Refusal("trigger is 'cron' or 'operator'")
    ids = list(doc_ids or [])
    if kind == "handover" and (not ids or not all(isinstance(i, int) and not isinstance(i, bool)
                                                  for i in ids)):
        raise db.Refusal("a handover names the documents you just filed: doc_ids=[…]")
    with db.tx(conn):
        rid = conn.execute("INSERT INTO work_requests(kind, trigger, doc_ids_json, created_seq,"
                           " created_at, state) VALUES (?,?,?,?,?, 'queued')",
                           (kind, trigger, json.dumps(ids), db.next_seq(conn),
                            db.now())).lastrowid
    line = LINES["cron"] if trigger == "cron" and kind == "check" else LINES[kind]
    return {"request_id": rid, "line": line, "start_job": dict(START)}


def request_package(conn, quarter, channel) -> dict:
    """The request half of begin_pass(trigger="package"): kept whatever happens next."""
    import dates
    if channel not in ("telegram", "email"):
        raise db.Refusal("a package is asked for with its channel: 'telegram' or 'email'")
    dates.parse_quarter(quarter)
    label = dates.quarter_label(quarter)
    with db.tx(conn):
        open_ = conn.execute("SELECT request_id FROM package_requests WHERE quarter=? AND"
                             " state IN ('queued', 'snapshot')", (quarter,)).fetchone()
        if open_ is not None:
            conn.execute("UPDATE package_requests SET channel=?, updated_at=? WHERE"
                         " request_id=?", (channel, db.now(), open_[0]))
            return {"status": "already", "start_job": dict(START),
                    "line": f"The {label} package is already on its way — it follows when the "
                            "check is done."}
        passes._open_request(conn, quarter, channel)
    return {"status": "asked", "start_job": dict(START),
            "line": f"Checking the bank and your email for {label} — the package follows "
                    "when that's done."}


def take_queued(conn, pass_id) -> list:
    assert conn.in_transaction
    p = conn.execute("SELECT trigger FROM passes WHERE pass_id=?", (pass_id,)).fetchone()
    if p is None or p["trigger"] == "package":
        return []                                   # a package round serves its request only
    ids = [r[0] for r in conn.execute("SELECT request_id FROM work_requests WHERE"
                                      " state='queued' ORDER BY request_id")]
    for i in ids:
        conn.execute("UPDATE work_requests SET state='taken', pass_id=? WHERE request_id=?",
                     (pass_id, i))
    if any(conn.execute("SELECT 1 FROM work_requests WHERE request_id=? AND kind='handover'",
                        (i,)).fetchone() for i in ids):
        conn.execute("UPDATE passes SET judge_after=NULL WHERE pass_id=?", (pass_id,))
    return ids


def settle_taken(conn, pass_id, outcome) -> None:
    assert conn.in_transaction
    conn.execute("UPDATE work_requests SET state='done', outcome=? WHERE pass_id=? AND"
                 " state='taken'", (outcome, pass_id))
    # a cron check with nothing to show is reported at once (spec §6.4)
    conn.execute("UPDATE work_requests SET state='reported' WHERE pass_id=? AND state='done'"
                 " AND kind='check' AND trigger='cron' AND outcome IN ('complete',"
                 " 'interrupted')", (pass_id,))


def requeue_taken(conn, pass_id) -> None:
    assert conn.in_transaction
    conn.execute("UPDATE work_requests SET state='queued', pass_id=NULL WHERE pass_id=? AND"
                 " state='taken'", (pass_id,))


def record_verdicts(conn, pass_id, documents) -> None:
    """The judge's per-document verdicts (spec §5.2) on the pass's taken handovers, each
    tagged with the running judgment's started_seq: a verdict covers only the judgment
    that made it (INV-J12; Astra plan-r1)."""
    assert conn.in_transaction
    j = conn.execute("SELECT started_seq FROM pass_steps WHERE pass_id=? AND step='judge'"
                     " AND finished_at IS NULL", (pass_id,)).fetchone()
    if j is None:
        raise db.Refusal("no judge step is running")
    allowed = {"matched", "proposed", "no-payment-yet", "clash", "unreadable",
               "out-of-range", "irrelevant"}
    for doc_id, verdict in (documents or {}).items():
        if verdict not in allowed:
            raise db.Refusal(f"a document's verdict is one of {', '.join(sorted(allowed))}")
        for r in conn.execute("SELECT request_id, doc_ids_json, verdicts_json FROM work_requests"
                              " WHERE pass_id=? AND state='taken' AND kind='handover'",
                              (pass_id,)).fetchall():
            if int(doc_id) in json.loads(r["doc_ids_json"]):
                v = json.loads(r["verdicts_json"])
                v[str(int(doc_id))] = {"verdict": verdict, "judge": j["started_seq"]}
                conn.execute("UPDATE work_requests SET verdicts_json=? WHERE request_id=?",
                             (db.canonical(v), r["request_id"]))


def handover_covered(conn, pass_id) -> bool:
    """INV-J12: every taken handover has a whole judgment that started after it was asked,
    and a verdict for each of its documents."""
    assert conn.in_transaction
    j = conn.execute("SELECT started_seq, finished_at, finish_json FROM pass_steps WHERE"
                     " pass_id=? AND step='judge'", (pass_id,)).fetchone()
    for r in conn.execute("SELECT created_seq, doc_ids_json, verdicts_json FROM work_requests"
                          " WHERE pass_id=? AND state='taken' AND kind='handover'",
                          (pass_id,)).fetchall():
        if j is None or j["finished_at"] is None or j["started_seq"] is None \
                or j["started_seq"] <= r["created_seq"] \
                or json.loads(j["finish_json"] or "{}").get("triage_remaining") is None:
            return False
        v = json.loads(r["verdicts_json"])
        if any((v.get(str(d)) or {}).get("judge") != j["started_seq"]
               for d in json.loads(r["doc_ids_json"])):
            return False
    return True
```

Then:
- `binding._TABLES_TO_WIPE` gains `"claims"` and `"work_requests"`, and `reset_store` runs
  `DELETE FROM meta WHERE key='drain'` in its wipe transaction (Astra plan-r3 S1). The
  `pass_generation` counter is bumped there already, so every new claim's token is above every
  old one; with `claims` empty, `check_claim` refuses every old token.
- `passes._terminalize` ends with `asks.requeue_taken(conn, pass_id)` (lazy import), in its
  transaction.
- In `passes._end_pass_tx`, after the `UPDATE passes SET ended_at…`, call
  `asks.settle_taken(conn, m["pass_id"], outcome)` (lazy import).
- In `passes._hand_over`, for a job pass (`protocol_of(conn, pass_id) == "job"`), settle with
  `token=None` (no lease), so `job_report` can claim a buildable request at once (§6.4).
- `job.stop_exhausted_pass` already ends the pass through `_end_pass_tx`. Its package request
  in `snapshot` reaches `_hand_over` → `settle_snapshot_request(…, "stopped")` → `_close` with
  its `package-stopped` notice. Confirm the test's alert count.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_asks tests.test_s2_fresh tests.test_package_requests -v`,
then the suite. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/asks.py server/passes.py server/job.py server/binding.py tests/_base.py tests/test_s2_asks.py tests/test_s2_fresh.py
git commit -m "feat(s2): work requests — queued before start_job, taken by the live pass, settled at its end"
```

---

### Task 7: The cursor, part 1 — the check pass (§5)

**Files:**
- Modify: `server/job.py` (`next_unit`, `_choose`, `_end`)
- Modify: `server/sweep.py` (`list_projections(quarter=…)` also lists rows absent from the
  latest export, whatever their quarter: they are end-checks, not the quarter's work)
- Test: `tests/test_s2_cursor.py`

**Interfaces:**
- Consumes: `steps._start_tx`, `steps._finish_tx`, `steps._hand_chunk`, `steps._chunk`,
  `steps._unrecorded`, `steps._another_chunk`, `steps.round_request`, `steps.chunk_owed`
  (all in-tx); `sweep._due`; `work.filed_refs`, `work.check_report`; `passes._end_pass_tx`;
  `asks.take_queued`, `asks.handover_covered`, `asks.record_verdicts`.
- Produces: `job.next_unit(conn, token, judged=None) -> dict`, with `unit` one of
  `probes | snapshot | sweep | gmail-probe | filing | item | judge | end-batch | complete`,
  plus its inputs, `pass_token`, `progress`, and `report` (Task 9 fills `progress`/`report`).

**The decision order, inside one transaction** (`_choose(conn, token)`):
1. `check_claim`. If a job pass is live and its trigger is not `package`, call
   `asks.take_queued`.
2. **No live pass:**
   - queued check/handover requests → `start_pass(trigger=<'operator' if any request is
     operator-triggered else 'cron'>, reply=<'telegram' likewise else 'silent'>,
     protocol="job", token)`, set `holder_job`, `take_queued`;
   - else a queued package request → `start_pass("package", "silent", protocol="job",
     token)`, `_bind_round`;
   - else `meta.drain = 'none'` → `{"unit": "complete"}`.
3. **Acquisition:**
   - The pass's first step row is `sweep` for a check and `snapshot` for a package. If it is
     missing, `_start_tx` it.
   - If F fails for any reason except "the sweep is not finished":
     - if the pass's `acq_gen != token`, or its `acq` already has a snapshot → if F failed for
       W, `w_refreshes += 1`; then `hand_acquisition` → `{"unit": "probes", "acq": acq}`;
     - else, if the `bank_sync` probe for this `acq` under this claim is not recorded →
       `{"unit": "probes", "acq": acq}` (the same acq);
     - else, if the bank-write gate refused, or `check_setup` says `can_run` is false → end
       the pass `stopped` (step 7);
     - else → `{"unit": "snapshot", "acq": acq}`.
4. **Sweep:** `due = sweep._due` (for a package, the quarter's) is non-empty →
   `{"unit": "sweep", "quarter": <q or None>}`. When it is empty, finish the first step row
   with `remaining_in_cycle: 0` if it is still open.
5. **Gmail:**
   - no Gmail probe this pass → `{"unit": "gmail-probe"}`;
   - probe ok, a check pass, and its first carry has no `filed` → `{"unit": "filing",
     "filed_refs": work.filed_refs(conn)}`;
   - probe ok and no open chunk and the round was never handed → `_hand_chunk(first=True)`;
   - an open chunk with unrecorded items → `{"unit": "item", "item": <the first unrecorded
     item's work_item>}`.
6. **Judge:**
   - an open chunk, all recorded (or the probe failed or is absent) → `_start_tx("judge")`;
   - a judge step running → `{"unit": "judge", "after": passes.judge_after,
     "quarter": <q>, "documents_first": <taken handovers' doc ids lacking a verdict>}`;
   - a judge step finished, `asks.handover_covered` false → restart the judge
     (`_start_tx("judge")`);
   - judge finished and `_another_chunk` → `_hand_chunk(first=False)` → item units again.
7. **End:** compute the outcome:
   - `stopped` if the first step finished stopped or the gate refused;
   - else `complete` when `steps.chunk_owed` is None, `passes._judgment_owed` does not raise,
     and the check report's `not_searched` is 0;
   - otherwise `interrupted`. A `_judgment_owed` that asks for a judge step with none
     started → back to step 6.

   Then `passes._end_pass_tx(token, outcome, {})`, and loop to step 2 within the same call, so
   the next request starts in this turn.

`judged` handling, before `_choose`:
- `judged = {"page_next": list|None, "triage_remaining": int, "documents": {doc_id: verdict}}`;
- `asks.record_verdicts(...)`;
- if `page_next` is set → `passes.judge_after = json.dumps(page_next)`, `judge_pages += 1`;
- else `_finish_tx("judge", counts={"triage_remaining": n})`, `judge_after = NULL`,
  `judge_pages += 1`;
- refuse `judged` when no judge step is running.

`record_filing(conn, token)` runs `check_claim` and sets the first carry's `filed = True`.
`ingest_document` already records each filed ref in `operator_refs` (#24).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_s2_cursor.py
from tests._base import StoreCase
from tests.sim_job import JobDriver          # added in this task: drives units mechanically

A = "aaaaaaaa-1"


class CheckPass(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self)            # a real bank-feed (tests/bankfeed.py) + Gmail fake

    def test_a_check_runs_unit_by_unit_to_completion(self):
        import asks
        asks.request_work(self.conn, "check", "operator")
        units = self.drv.run_job(A)           # claim, then job_next until complete
        kinds = [u["unit"] for u in units]
        self.assertEqual(kinds[0], "probes")
        self.assertIn("snapshot", kinds)
        self.assertLess(kinds.index("snapshot"), kinds.index("gmail-probe"))
        self.assertIn("judge", kinds)
        self.assertEqual(kinds[-1], "complete")
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "complete"))

    def test_a_turn_cut_mid_item_rehands_that_item_first(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "item")                   # the first item is handed out
        cut = self.drv.last["item"]["pid"]
        t2 = job.claim(self.conn, A)                     # next batch, fresh conversation
        nxt = self.drv.next_until(t2, "item")
        self.assertEqual(nxt["item"]["pid"], cut)

    def test_a_refresh_split_across_claims_restarts_the_acquisition(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        u = self.drv.run_until(A, "snapshot")           # probes done under claim 1
        t2 = job.claim(self.conn, A)
        self.assertEqual(job.next_unit(self.conn, t2)["unit"], "probes")   # not snapshot

    def test_a_mid_pass_request_forces_a_refresh_before_the_next_decision(self):
        import asks, job
        asks.request_work(self.conn, "check", "cron")
        self.drv.run_until(A, "gmail-probe")
        asks.request_work(self.conn, "check", "operator")
        t = self.drv.token
        self.assertEqual(job.next_unit(self.conn, t)["unit"], "probes")

    def test_a_gate_poisoned_after_the_import_stops_the_pass(self):
        import asks, db, passes
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "sweep")
        with db.tx(self.conn):
            passes.poison(self.conn, "the bank ledger changed during this pass")
        self.drv.run_job(A)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))

    def test_two_w_refreshes_import_twice(self):
        import asks, datetime as _dt, db, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "judge")
        n0 = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        for k in (1, 2):
            later = db._clock() + _dt.timedelta(seconds=job.W_S * 2 * k)
            with self.patch_clock(later):
                t = job.claim(self.conn, A)
                self.drv.next_until(t, "judge")         # a refresh, then judging again
        n = self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        self.assertEqual(n - n0, 2)
        self.assertEqual(self.conn.execute("SELECT w_refreshes, w_pending FROM passes"
                                           " ORDER BY rowid DESC LIMIT 1").fetchone()[:], (2, 0))

    def test_a_failed_sync_stops_the_pass_with_its_reason(self):
        import asks
        self.drv.fail_next_sync("bank unreachable")
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)
        r = self.conn.execute("SELECT state, outcome FROM work_requests").fetchone()
        self.assertEqual((r["state"], r["outcome"]), ("done", "stopped"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0)

    def test_eight_asks_that_each_stop_at_once_all_get_dispositions(self):
        import asks
        self.drv.bankfeed.restore_since_install()        # every pass stops at its probes
        for q in ("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4",
                  "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"):
            asks.request_package(self.conn, q, "telegram")
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM package_requests WHERE"
                                           " state='queued'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_requests WHERE"
                                           " state='queued'").fetchone()[0], 0)

    def test_the_report_carries_the_classification_queue(self):
        import asks, json
        self.drv = JobDriver(self, queue=(4, 1))
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_job(A)
        rep = json.loads(self.conn.execute("SELECT report_json FROM passes ORDER BY rowid"
                                           " DESC LIMIT 1").fetchone()[0])
        self.assertEqual(rep["awaiting_classification"], 5)

    def test_a_refused_completion_is_reissued_to_any_fresh_turn(self):
        """ha-casa-app#1180: emit_completion refused (unread inbound); a fresh batch's
        job_next answers `complete` again, and a topic turn's job_status says done."""
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        units = self.drv.run_job(A)                       # ends at `complete`
        self.assertEqual(units[-1]["unit"], "complete")
        t2 = job.claim(self.conn, A)                       # a fresh batch after the refusal
        self.assertEqual(job.next_unit(self.conn, t2)["unit"], "complete")
        self.assertEqual(job.status(self.conn, A), {"done": True,
                                                     "text": "Accounting work finished."})

    def test_job_status_never_claims_and_says_not_done_while_work_remains(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "sweep")
        top = self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
        self.assertFalse(job.status(self.conn, A)["done"])
        self.assertEqual(self.conn.execute("SELECT max(gen) FROM claims").fetchone()[0], top)

    def test_a_stopped_gate_ends_the_pass_stopped(self):
        import asks
        self.drv.bankfeed.restore_since_install()        # the ledger was restored
        asks.request_work(self.conn, "check", "operator")
        units = self.drv.run_job(A)
        r = self.conn.execute("SELECT outcome FROM work_requests").fetchone()
        self.assertEqual(r["outcome"], "stopped")
```

`tests/sim_job.py::JobDriver` is added in this task. It carries out each unit exactly as the
job skill will say: `probes` through `tests/bankfeed.py` (`sync`, `list_accounts`,
`list_backups`) and `record_probe(..., acq)`; `snapshot` (`export_history` → publish →
`import_ledger_export(acq=…)` → erase candidates); `sweep` through
`tests/sim.py::observe_and_repair`; `gmail-probe` and `item` through a Gmail fake that returns
no messages; `filing` → `record_filing(token)`; `judge` → one `list_quarter_state(triage=True)`
page and `job_next(judged=…)` with that page's `next` and `remaining`.
- `run_job(job_id)` claims and loops until `complete`, re-claiming on `end-batch`.
- `fail_next_sync(detail)` makes the next `probes` unit record `bank_sync` with `ok=False`
  and that detail.
- `JobDriver(..., cut_after_import=True)` stops the first `snapshot` unit right after
  `import_ledger_export`, before the erase candidates are observed, and ends the claim.
- The `probes` unit records the sync trailer's `Queue:` counts in the `bank_sync` probe's data
  as `{"queue": {"workable": w, "parked": k}}`; `JobDriver(queue=(w, k))` sets them.
- `run_until(job_id, unit)` stops when that unit is handed out (and stores `self.last` and
  `self.token`).
- `next_until(token, unit)` continues an existing claim.

Also write `tests/test_s2_package_rounds.py` now, exactly as listed under Task 8 (Astra
plan-r5 S2: Task 7's cursor already implements package rounds, so their red checkpoint
belongs here).

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_s2_cursor tests.test_s2_package_rounds -v`
Expected: FAIL (`next_unit` is missing).

- [ ] **Step 3: Implement** in `server/job.py`:

```python
# fresh_reason's reasons are Task 5's constants (NOT_READ, OTHER_JOB, LATE_ASK, UNSWEPT, STALE)


def next_unit(conn, token, judged=None) -> dict:
    with db.tx(conn):
        check_claim(conn, token)
        if judged is not None:
            _judged(conn, token, judged)
        out = _choose(conn, token)
        _account(conn, token, out)          # Task 9: budget, progress, report
        return out


def _choose(conn, token) -> dict:
    import asks, steps
    for _ in range(8):                      # passes may end and the next begin in one call
        p = live_job_pass(conn)
        if p is not None and p["trigger"] != "package":
            asks.take_queued(conn, p["pass_id"])
            p = live_job_pass(conn)
        if p is None:
            p = _begin_next(conn, token)
            if p is None:
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('drain','none')")
                return {"unit": "complete", "text": "Accounting work finished."}
        req = steps.round_request(conn, p["pass_id"])
        ended = False
        for step in (_poisoned, _acquisition, _sweep, _gmail, _judge):
            u = step(conn, token, p, req)
            if u == "ended":
                ended = True
                break
            if u is not None:
                return u
        if not ended:
            passes._end_pass_tx(conn, token, _outcome(conn, p), _report_extras(conn, p))
    # eight passes ended in this call (e.g. eight package asks each stopped at once):
    # their dispositions commit with this answer, and the next batch goes on
    return {"unit": "end-batch"}


def _begin_next(conn, token):
    import asks
    q = conn.execute("SELECT trigger FROM work_requests WHERE state='queued'").fetchall()
    if q:
        op = any(r["trigger"] == "operator" for r in q)
        _, pid = passes.start_pass(conn, "operator" if op else "cron",
                                   "telegram" if op else "silent", protocol="job", token=token)
    else:
        req = conn.execute("SELECT * FROM package_requests WHERE state='queued'"
                           " ORDER BY request_id LIMIT 1").fetchone()
        if req is None:
            return None
        _, pid = passes.start_pass(conn, "package", "silent", protocol="job", token=token)
        passes._bind_round(conn, req["request_id"], pid)
    who = conn.execute("SELECT job_id FROM claims WHERE gen=?", (token,)).fetchone()[0]
    conn.execute("UPDATE passes SET holder_job=?, adopters_json=? WHERE pass_id=?",
                 (who, json.dumps([who]), pid))
    asks.take_queued(conn, pid)
    _rebase(conn, token)                    # a new pass: a new population (INV-J8)
    return live_job_pass(conn)


def _first(req):
    return "snapshot" if req is not None else "sweep"


def _step(conn, p, name):
    return conn.execute("SELECT * FROM pass_steps WHERE pass_id=? AND step=?",
                        (p["pass_id"], name)).fetchone()


def _poisoned(conn, token, p, req):
    """A bank gate refused after this pass's import (an observation saw the ledger
    change, passes.poison): the pass ends `stopped` with the gate's reason, never
    re-handing a sweep the gate will refuse (INV-J6; Astra plan-r4 S1)."""
    import steps
    if conn.execute("SELECT 1 FROM snapshots WHERE pass_id=?", (p["pass_id"],)).fetchone() is None:
        return None                         # before the import, _continue_acquisition decides
    gate = passes.bank_write_gate(conn)
    if gate["allowed"]:
        return None
    row = _step(conn, p, _first(req))
    if row["finished_at"] is None:
        _, refused = steps._finish_tx(conn, token, _first(req), counts={},
                                      stopped=gate["reason"], by_refusal=True)
        assert refused is None
    passes._end_pass_tx(conn, token, "stopped", {})
    return "ended"


def _acquisition(conn, token, p, req):
    """Spec §5.2. An acquisition this claim started is finished before F is consulted
    (INV-J4; Astra plan-r2 S1). A W refresh is counted when its import lands, never when
    handed out, so a refresh cut short and handed again is charged once."""
    import binding, steps
    if _step(conn, p, _first(req)) is None:
        steps._start_tx(conn, token, _first(req), {})
    q = req["quarter"] if req is not None else None
    imported = p["acq"] is not None and conn.execute(
        "SELECT 1 FROM snapshots WHERE acq=?", (p["acq"],)).fetchone() is not None
    if imported and p["w_pending"]:
        conn.execute("UPDATE passes SET w_refreshes=w_refreshes+1, w_pending=0 WHERE pass_id=?",
                     (p["pass_id"],))
        p = live_job_pass(conn)
    if p["acq"] is not None and not imported and p["acq_gen"] == token:
        return _continue_acquisition(conn, token, p, req, q)
    why = fresh_reason(conn)
    if why is None or why == UNSWEPT:
        return None
    if why == STALE:
        conn.execute("UPDATE passes SET w_pending=1 WHERE pass_id=?", (p["pass_id"],))
    return {"unit": "probes", "acq": hand_acquisition(conn, token, p["pass_id"]), "quarter": q}


def _continue_acquisition(conn, token, p, req, q):
    import binding, steps
    sync = conn.execute("SELECT ok, gen, detail, data_json FROM probes WHERE"
                        " kind='bank_sync'").fetchone()
    led = conn.execute("SELECT gen FROM probes WHERE kind='ledger'").fetchone()
    if (sync is None or sync["gen"] != token
            or json.loads(sync["data_json"] or "{}").get("acq") != p["acq"]
            or led is None or led["gen"] != token):
        return {"unit": "probes", "acq": p["acq"], "quarter": q}
    setup, gate = binding.check_setup(conn), passes.bank_write_gate(conn)
    reason = None
    if not sync["ok"]:                      # Astra plan-r2 S2: a failed sync stops the pass
        reason = "the bank sync failed: " + (sync["detail"] or "no detail")
    elif not setup["can_run"] or not gate["allowed"]:
        reason = gate["reason"] or "; ".join(setup.get("conditions") or []) or "cannot run"
    if reason is not None:
        row = _step(conn, p, _first(req))
        if row["finished_at"] is None:
            _, refused = steps._finish_tx(conn, token, _first(req), counts={}, stopped=reason,
                                          by_refusal=True)
            assert refused is None
        passes._end_pass_tx(conn, token, "stopped", {})
        return "ended"
    return {"unit": "snapshot", "acq": p["acq"], "quarter": q}


def _sweep(conn, token, p, req):
    import steps, sweep
    due = sweep._due(conn)
    if req is not None:
        due = [x for x in due if sweep._in_quarter(conn, x, req["quarter"])
               or sweep._absent(conn, x)]
    if due:
        return {"unit": "sweep", "quarter": req["quarter"] if req is not None else None}
    row = _step(conn, p, _first(req))
    if row["finished_at"] is None:
        _, refused = steps._finish_tx(conn, token, _first(req),
                                      counts={"remaining_in_cycle": 0})
        assert refused is None
    conn.execute("UPDATE snapshots SET swept_at=coalesce(swept_at, ?) WHERE snapshot_id="
                 "(SELECT max(snapshot_id) FROM snapshots WHERE pass_id=?)",
                 (db.now(), p["pass_id"]))
    return None


EMPTY_CHUNK = {"pids": [], "recorded": [], "open": True, "calls": None, "more": 0}


def _gmail(conn, token, p, req):
    import steps, work
    probe = conn.execute("SELECT ok, pass_id FROM probes WHERE kind='gmail'").fetchone()
    if probe is None or probe["pass_id"] != p["pass_id"]:
        return {"unit": "gmail-probe"}
    carry, chunk = steps._chunk(conn, p["pass_id"])
    if not probe["ok"]:
        # no searches (spec §2, §6); an empty chunk lets the judge step start
        # (steps.round_owed: the round counts as handed out)
        if "chunk" not in carry:
            carry["chunk"] = dict(EMPTY_CHUNK)
            steps._set_first_carry(conn, p["pass_id"], carry)
        return None
    if req is None and not carry.get("filed"):
        return {"unit": "filing", "filed_refs": work.filed_refs(conn)}
    if "chunk" not in carry:
        steps._hand_chunk(conn, p["pass_id"], req, True)
        _rebase(conn, token)                # the round's population is now known (INV-J8)
        carry, chunk = steps._chunk(conn, p["pass_id"])
    if chunk is not None:
        left = steps._unrecorded(conn, chunk)
        if left:
            return {"unit": "item", "item": work.work_item(work.describe(conn, left[0]))}
    return None


def _judge(conn, token, p, req):
    import asks, steps
    j = _step(conn, p, "judge")
    if j is not None and j["finished_at"] is None:
        return _judge_unit(conn, p, req)
    _, chunk = steps._chunk(conn, p["pass_id"])
    latest_read = conn.execute("SELECT max(read_seq) FROM snapshots WHERE pass_id=?",
                               (p["pass_id"],)).fetchone()[0]
    restart = (j is None or chunk is not None
               or not asks.handover_covered(conn, p["pass_id"])
               or (latest_read is not None and (j["started_seq"] or 0) < latest_read
                   and passes.judgment_gap(conn, p["pass_id"]) > 0))
    if restart:
        steps._start_tx(conn, token, "judge", {})      # closes the chunk; may restart
        conn.execute("UPDATE passes SET judge_after=NULL WHERE pass_id=?", (p["pass_id"],))
        return _judge_unit(conn, p, req)
    if steps._another_chunk(conn, p["pass_id"], req, j):
        steps._hand_chunk(conn, p["pass_id"], req, False)
        return _gmail(conn, token, p, req)
    return None


def _judge_unit(conn, p, req) -> dict:
    p = live_job_pass(conn)                           # re-read: judge_after may have changed
    j = _step(conn, p, "judge")
    firsts = []
    for r in conn.execute("SELECT doc_ids_json, verdicts_json FROM work_requests WHERE"
                          " pass_id=? AND state='taken' AND kind='handover'", (p["pass_id"],)):
        seen = json.loads(r["verdicts_json"])
        firsts += [d for d in json.loads(r["doc_ids_json"])
                   if (seen.get(str(d)) or {}).get("judge") != j["started_seq"]]
    after = json.loads(p["judge_after"]) if p["judge_after"] else None
    return {"unit": "judge", "after": after, "quarter": req["quarter"] if req else None,
            "documents_first": firsts}


def _judged(conn, token, judged) -> None:
    import asks, steps
    if not isinstance(judged, dict):
        raise db.Refusal("judged is {page_next, triage_remaining, documents}")
    p = live_job_pass(conn)
    j = _step(conn, p, "judge") if p is not None else None
    if j is None or j["finished_at"] is not None:
        raise db.Refusal("no judge step is running: call job_next without judged")
    asks.record_verdicts(conn, p["pass_id"], judged.get("documents") or {})
    nxt = judged.get("page_next")
    if nxt:
        conn.execute("UPDATE passes SET judge_after=?, judge_pages=judge_pages+1 WHERE"
                     " pass_id=?", (json.dumps(nxt), p["pass_id"]))
        return
    n = judged.get("triage_remaining")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise db.Refusal("triage_remaining is the last page's `remaining` (0 when every page "
                         "was judged)")
    _, refused = steps._finish_tx(conn, token, "judge", counts={"triage_remaining": n})
    assert refused is None
    conn.execute("UPDATE passes SET judge_after=NULL, judge_pages=judge_pages+1 WHERE"
                 " pass_id=?", (p["pass_id"],))


def _report_extras(conn, p) -> dict:
    """What the pass's stored report says beyond the counts: the read's age when W was
    waived (§5.2), and how many payments await classification (§6; Terra plan-r6 S2)."""
    out = _read_age_note(conn, p)
    sync = conn.execute("SELECT data_json FROM probes WHERE kind='bank_sync'").fetchone()
    q = (json.loads(sync["data_json"] or "{}").get("queue") or {}) if sync else {}
    n = sum(v for v in (q.get("workable"), q.get("parked")) if isinstance(v, int) and v > 0)
    if n:
        out["awaiting_classification"] = n
    return out


def _read_age_note(conn, p) -> dict:
    """Spec §5.2: when W was waived, the report says how old the read the decisions
    rested on was (Astra plan-r4 S2)."""
    if p["w_refreshes"] < W_REFRESH_MAX:
        return {}
    s = conn.execute("SELECT swept_at FROM snapshots WHERE pass_id=? ORDER BY snapshot_id DESC"
                     " LIMIT 1", (p["pass_id"],)).fetchone()
    if s is None or s["swept_at"] is None:
        return {}
    age = int(passes._age_s(s["swept_at"]) // 60)
    return {"read_age_min": age} if age * 60 >= W_S else {}


def _outcome(conn, p) -> str:
    import steps
    if steps.chunk_owed(conn, p["pass_id"]) is not None:
        raise RuntimeError("the cursor reached a pass end with its chunk owed")   # a bug
    if passes.judgment_gap(conn, p["pass_id"]) > 0:
        return "interrupted"
    if passes.stored_report(conn, p["pass_id"]).get("not_searched"):
        return "interrupted"
    if fresh_reason(conn) is not None:
        return "interrupted"
    return "complete"


def record_filing(conn, token) -> dict:
    import steps
    with db.tx(conn):
        check_claim(conn, token)
        p = live_job_pass(conn)
        if p is None:
            raise db.Refusal("no pass is running: call job_next")
        carry = steps._first_carry(conn, p["pass_id"])
        carry["filed"] = True
        steps._set_first_carry(conn, p["pass_id"], carry)
    return {"filed": True}


def status(conn, job_id) -> dict:
    """Read-only, never a claim (ha-casa-app#1180; design delta §3): may this job end now?
    `done` when no pass is live and nothing is queued — the same condition job_next
    answers `complete` on. An operator-message turn in the job's topic calls it last, so a
    completion Casa refused (unread inbound) is re-issued from the store."""
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise db.Refusal("job_id is the `Job id:` line of your brief, as given")
    p = live_job_pass(conn)
    queued = conn.execute("SELECT 1 FROM work_requests WHERE state='queued' UNION ALL"
                          " SELECT 1 FROM package_requests WHERE state='queued'").fetchone()
    done = p is None and queued is None
    return {"done": done, "text": "Accounting work finished." if done else None}


def _account(conn, token, out) -> None:
    out["pass_token"] = token           # Task 9 replaces this with budget, progress and report


def _rebase(conn, token) -> None:
    """The claim's baseline takes the live pass's population (Astra plan-r3 S1): a pass
    begun or a round handed out in this claim sets unsearched/pages/due from zero, and
    that setting is not progress. The requests component is kept: requests done before
    the rebase still count."""
    c = conn.execute("SELECT measure_json FROM claims WHERE gen=?", (token,)).fetchone()
    base = json.loads(c["measure_json"]) if c and c["measure_json"] else measure(conn)
    conn.execute("UPDATE claims SET measure_json=? WHERE gen=?",
                 (json.dumps([base[0]] + measure(conn)[1:]), token))
```

`sweep._absent(conn, pid)` is `lineage.live_row(conn, lineage.projection(conn, pid)) is None`:
the row is absent from the latest export, so it is an erase candidate. `sweep.list_projections`
uses the same filter for its `quarter` argument:
`due = due_all if quarter is None else [p for p in due_all if _in_quarter(conn, p, quarter) or
_absent(conn, p)]`. A package round that resumes after a cut between its import and its
erase-candidate observations therefore still observes every one (Astra plan-r6 S1: 0 → 1 erased
lineage). Add `test_a_resumed_package_snapshot_still_confirms_an_out_of_quarter_erasure` to
`tests/test_s2_package_rounds.py`:
- a Q4 package;
- one Q3 row deleted from bank-feed before the snapshot;
- `JobDriver(cut_after_import=True)` ends the snapshot unit right after the import;
- re-claim and run to completion;
- assert 1 lineage `ended='erased'` and the request `snapshot-done`.

`steps._finish_tx` returns `(result, refused)` (Task 3). The cursor's finishes never take the
late-stop branch, because job steps have no clock, so `refused` is always None; the cursor
asserts it.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_cursor -v`, then the suite. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/job.py tests/sim_job.py tests/test_s2_cursor.py tests/test_s2_package_rounds.py
git commit -m "feat(s2): job_next's cursor for a check pass — acquisition, sweep, Gmail chunks, judge, end"
```

---

### Task 8: Package rounds — FOLDED INTO TASK 7

**Folded into Task 7 (Astra plan-r5 S2).** The tests below are written in Task 7's Step 1 and
pass with Task 7's code. The text below documents them; Task 8 has no steps of its own. Skip
to Task 9.

**Files:**
- Modify: `server/job.py`
- Test: `tests/test_s2_package_rounds.py`

**Interfaces:**
- Consumes: `passes.round_fate`/`_hand_over` (job: `token=None`), `work.package_work`,
  `steps._hand_chunk(req=…)`.
- Produces:
  - package passes run steps 3–7 with `quarter` set: the sweep is quarter-scoped and the
    judge unit carries `quarter`;
  - after `_end`, a request that `round_fate` puts back to `queued` is started again by the
    loop (another round, in the same job);
  - a `snapshot-done` request waits for `job_report` (Task 10).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_s2_package_rounds.py
from tests._base import StoreCase
from tests.sim_job import JobDriver

A = "aaaaaaaa-1"


class PackageRounds(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self)

    def test_a_package_ask_runs_its_rounds_and_waits_buildable(self):
        import asks
        asks.request_package(self.conn, "2026-Q3", "telegram")
        units = self.drv.run_job(A)
        self.assertEqual(units[-1]["unit"], "complete")
        r = self.conn.execute("SELECT state, token, lease_at FROM package_requests").fetchone()
        self.assertEqual(r["state"], "snapshot-done")
        self.assertIsNone(r["lease_at"])               # job_report may claim it at once

    def test_a_check_and_a_package_are_drained_by_one_job(self):
        import asks
        asks.request_work(self.conn, "check", "operator")
        asks.request_package(self.conn, "2026-Q3", "telegram")
        self.drv.run_job(A)
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "done")
        self.assertEqual(self.conn.execute("SELECT state FROM package_requests").fetchone()[0],
                         "snapshot-done")

    def test_the_package_sweep_is_quarter_scoped(self):
        import asks
        asks.request_package(self.conn, "2026-Q3", "telegram")
        units = self.drv.run_job(A)
        self.assertTrue(all(u.get("quarter") == "2026-Q3" for u in units
                            if u["unit"] in ("sweep", "judge")))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_s2_package_rounds -v`
Expected: FAIL.

- [ ] **Step 3: Implement**
- In `_choose`, pass `req = steps.round_request(conn, pass_id)` through every step, so the
  quarter scopes the sweep (`sweep._in_quarter`), `_hand_chunk(req=req)`, and the judge unit's
  `quarter`.
- In `_end`, for a package pass, `_end_pass_tx` already calls `_hand_over`. Confirm that
  `round_fate`'s `queued` re-enters step 2 (no live pass, a queued package request → a new
  round pass).

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_package_rounds tests.test_package_rounds -v`, then the
suite. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/job.py tests/test_s2_package_rounds.py
git commit -m "feat(s2): package rounds run inside the job until buildable"
```

---

### Task 9: Batch budget, the progress measure, and reporting (§5, INV-J8)

**Files:**
- Modify: `server/job.py` (`_stamp_measure`, `measure`, the budget and report fields in `next_unit`)
- Test: `tests/test_s2_progress.py`

**Interfaces:**
- Produces:
  - `job.TURNS_PER_BATCH = 80`, `job.BATCH_RESERVE = 10`;
  - `job.UNIT_COST = {"probes": 12, "snapshot": 6, "sweep": 10, "gmail-probe": 3,
    "filing": 28, "item": 11, "judge": 24}`;
  - `job.measure(conn)` (from Task 4) is the baseline at each claim;
  - every `next_unit` answer carries `progress = {"summary", "progressed", "done",
    "remaining"}` and `report: bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_s2_progress.py
from tests._base import StoreCase
from tests.sim_job import JobDriver

A = "aaaaaaaa-1"


class Progress(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.drv = JobDriver(self, payments=40)       # enough work for several batches

    def test_a_batch_ends_inside_its_turn_budget(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, A)
        spent = 0
        while True:
            u = job.next_unit(self.conn, t)
            if u["unit"] in ("end-batch", "complete"):
                break
            spent += job.UNIT_COST[u["unit"]]
            self.drv.do(u, t)
        self.assertLessEqual(spent, job.TURNS_PER_BATCH - job.BATCH_RESERVE)
        self.assertTrue(u["report"])

    def test_a_refresh_only_batch_is_not_progress(self):
        import asks, job
        asks.request_work(self.conn, "check", "cron")
        self.drv.run_until(A, "gmail-probe")
        asks.request_work(self.conn, "check", "operator")      # forces a refresh
        t = job.claim(self.conn, A)
        u = job.next_unit(self.conn, t)                           # probes
        self.drv.do(u, t)
        u = job.next_unit(self.conn, t)                           # snapshot
        self.drv.do(u, t)
        self.assertFalse(job.next_unit(self.conn, t)["progress"]["progressed"])

    def test_a_recorded_search_is_progress_and_reports_once_early(self):
        import asks, job
        asks.request_work(self.conn, "check", "operator")
        self.drv.run_until(A, "item")
        t = self.drv.token
        self.drv.do(self.drv.last, t)                             # record_search
        u = job.next_unit(self.conn, t)
        self.assertTrue(u["progress"]["progressed"])
        self.assertTrue(u["report"])
        self.assertFalse(job.next_unit(self.conn, t)["report"])   # once, until end-batch
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_s2_progress -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
TURNS_PER_BATCH, BATCH_RESERVE = 80, 10
UNIT_COST = {"probes": 12, "snapshot": 6, "sweep": 10, "gmail-probe": 3, "filing": 28,
             "item": 11, "judge": 24}
```

Replace Task 7's `_account` with the body below.

```python
def _account(conn, token, out) -> None:
    """Mutates `out` in place: next_unit returns the same dict."""
    c = conn.execute("SELECT * FROM claims WHERE gen=?", (token,)).fetchone()
    cost = UNIT_COST.get(out["unit"], 0)
    if cost and c["spent"] > 0 and c["spent"] + cost > TURNS_PER_BATCH - BATCH_RESERVE:
        out.clear()
        out["unit"] = "end-batch"           # the unit is handed again next batch (INV-J4)
        cost = 0
    conn.execute("UPDATE claims SET spent=spent+? WHERE gen=?", (cost, token))
    now = measure(conn)
    progressed = now < json.loads(c["measure_json"])
    ending = out["unit"] in ("end-batch", "complete")
    report = ending or (progressed and not c["reported"])
    if report:
        conn.execute("UPDATE claims SET reported=1 WHERE gen=?", (token,))
    out["progress"] = {"summary": _summary(out["unit"], now), "progressed": progressed,
                       "done": None, "remaining": now[1] or None}
    out["report"] = report
    out["pass_token"] = token


WORDS = {"probes": "Reading the bank", "snapshot": "Importing the bank read",
         "sweep": "Bringing the bank ledger up to date", "gmail-probe": "Checking Gmail",
         "filing": "Filing emailed documents", "item": "Searching Gmail for an invoice",
         "judge": "Matching documents to payments", "end-batch": "Batch done",
         "complete": "All accounting work done"}


def _summary(unit, now) -> str:
    left = now[1]
    return WORDS[unit] + (f" · {left} payment{'s' if left != 1 else ''} left to search"
                          if left else "")
```

Task 7's `_rebase` keeps the baseline meaningful when a pass begins, or its round is handed
out, inside a claim. Add `test_the_first_chunk_is_not_progress_but_its_first_search_is` to
`tests/test_s2_progress.py`:
- `next_unit` that hands out the first `item` reports `progressed=False`;
- after its `record_search`, it reports `progressed=True` (Astra: 40 → 39 searchable, compared
  with the rebased baseline).

Add `test_the_summary_carries_no_machinery_words` to `tests/test_s2_progress.py`. For every
unit kind, `job._summary(kind, [0, 3, 0, 0])` contains none of `views.FORBIDDEN`.

- An `end-batch` returned before any unit was handed out in this claim does not happen,
  because of `c["spent"] > 0`: the first unit of a claim is always handed out.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_progress -v`, then the suite. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/job.py tests/test_s2_progress.py
git commit -m "feat(s2): batch budget and the work-measure progress the job reports (INV-J8)"
```

---

### Task 10: `job_report`: orphans, the standing retry, results shown once, sends (§6.3–6.4)

**Files:**
- Modify: `server/asks.py` (`job_report`, `mark_reported`)
- Modify: `server/views.py` (`mark_rendering_delivered` calls `asks.mark_reported`;
  `_build_review` gains an in-tx core, `review_in_tx`)
- Modify: `server/steps.py` (`_claim_sends_tx`, the in-tx body of a sends-only claim)
- Test: `tests/test_s2_report.py`

**Interfaces:**
- Consumes: `steps._choose(conn, sends_only=True)` (Task 3), `alerts.pending_in_tx`,
  `steps._pairing`.
- Produces, in-tx: `steps._claim_sends_tx(conn) -> dict` (`{"continue": …, "_notice": …}`) and
  `views.review_in_tx(conn, view="status", **kw) -> dict`.
- Produces: `asks.job_report(conn, job_id=None, status=None) -> {"orphaned": bool,
  "start_job": dict|None, "texts": [{"render_id", "text"}], "speak": dict|None,
  "continue": dict|None, "line": str|None}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_s2_report.py
from tests._base import StoreCase

A, B = "aaaaaaaa-1", "bbbbbbbb-2"


class Report(StoreCase):
    def live_pass_of(self, job_id):
        import asks, db, job
        asks.request_work(self.conn, "check", "operator")
        t = job.claim(self.conn, job_id)
        pid = self.start_job_pass(t)
        with db.tx(self.conn):
            asks.take_queued(self.conn, pid)
        return t, pid

    def test_the_holders_death_orphans_the_pass_and_offers_a_restart(self):
        import asks
        self.live_pass_of(A)
        out = asks.job_report(self.conn, job_id=A[:8], status="error")
        self.assertTrue(out["orphaned"])
        self.assertIsNotNone(out["start_job"])

    def test_a_late_notice_about_another_job_changes_nothing(self):
        import asks
        self.live_pass_of(A)
        asks.job_report(self.conn, job_id=B[:8], status="error")
        self.assertIsNone(self.conn.execute("SELECT orphaned_by FROM passes").fetchone()[0])

    def test_the_retry_stands_on_every_report_until_a_job_claims(self):
        import asks, job
        self.live_pass_of(A)
        asks.job_report(self.conn, job_id=A[:8], status="error")
        for _ in range(3):                                        # refused starts
            self.assertIsNotNone(asks.job_report(self.conn)["start_job"])
        job.claim(self.conn, B)
        self.assertIsNone(asks.job_report(self.conn)["start_job"])

    def test_a_result_is_offered_until_its_rendering_is_marked_delivered(self):
        import asks, db, passes, views
        t, pid = self.live_pass_of(A)
        with db.tx(self.conn):
            passes._end_pass_tx(self.conn, t, "complete", {})
        first = asks.job_report(self.conn)["texts"]
        again = asks.job_report(self.conn)["texts"]               # the send was lost
        self.assertEqual([x["render_id"] for x in first], [x["render_id"] for x in again])
        views.mark_rendering_delivered(self.conn, first[0]["render_id"])
        self.assertEqual(asks.job_report(self.conn)["texts"], [])
        self.assertEqual(self.conn.execute("SELECT state FROM work_requests").fetchone()[0],
                         "reported")

    def test_buildable_and_built_packages_are_recovered(self):
        import asks
        self.package_built_unsent()          # existing package helpers, request state 'built'
        out = asks.job_report(self.conn)
        self.assertEqual(out["continue"]["next"], "stage")
```

`StoreCase.package_built_unsent()` uses `package_token()` and `build_quarterly_package`. It
then clears the request's `lease_at` to a lapsed time.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_s2_report -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
def _match_job(conn, job_id):
    """The one recorded job id that starts with `job_id` (≥ 8 characters), or None."""
    if not isinstance(job_id, str) or len(job_id) < 8:
        raise db.Refusal("job_id is the id the notification names")
    ids = {r[0] for r in conn.execute("SELECT DISTINCT job_id FROM claims")}
    hits = [i for i in ids if i.startswith(job_id)]
    return hits[0] if len(hits) == 1 else None


def job_report(conn, job_id=None, status=None) -> dict:
    """Spec §6.4: ONE transaction, under the custody lock (taken first: the store's lock
    order). The orphan handoff, the retry decision, the results, the alerts and the
    package or delivery recovery see one state and commit together."""
    import alerts, job, steps
    if job_id is not None and status not in ("ok", "error"):
        raise db.Refusal("status is 'ok' or 'error'")
    out = {"orphaned": False, "start_job": None, "texts": [], "speak": None,
           "continue": None, "line": None}
    with db.custody_lock():
        with db.tx(conn):
            if job_id is not None:
                who = _match_job(conn, job_id)
                p = job.live_job_pass(conn)
                drain = conn.execute("SELECT value FROM meta WHERE key='drain'").fetchone()
                if who is not None and p is not None and p["holder_job"] == who:
                    conn.execute("UPDATE passes SET orphaned_by=? WHERE pass_id=?",
                                 (who, p["pass_id"]))
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('drain','none')")
                elif who is not None and drain is not None and drain[0] == who:
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('drain','none')")
            p = job.live_job_pass(conn)
            drain = (conn.execute("SELECT value FROM meta WHERE key='drain'").fetchone()
                     or ["none"])[0]
            orphaned = p is not None and p["orphaned_by"] is not None
            queued = conn.execute("SELECT 1 FROM work_requests WHERE state='queued' UNION ALL"
                                  " SELECT 1 FROM package_requests WHERE state='queued'"
                                  ).fetchone()
            out["orphaned"] = orphaned
            if drain == "none" and (orphaned or queued is not None):
                out["start_job"] = dict(START)
                if orphaned:
                    out["line"] = ("The accounting check stopped before it finished — I'm "
                                   "starting it again.")
            for r in conn.execute("SELECT * FROM work_requests WHERE state='done' ORDER BY"
                                  " request_id").fetchall():
                out["texts"].append(_result_tx(conn, r))
            sends = steps._claim_sends_tx(conn)
            out["continue"] = sends.get("continue")
            notice = sends.get("_notice")
            out["speak"] = alerts.pending_in_tx(conn, must=[notice] if notice else None)
    return out


`_result_tx(conn, r)` (in-tx) returns the request's rendering and creates it at most once:
- if `r["render_id"]` is set and that rendering is undelivered → `{render_id, text}` from
  `renders`;
- an operator check whose outcome is `complete`/`interrupted` → `views.review_in_tx(conn,
  "status")`; store its `render_id` on the request;
- an outcome of `stopped` → a rendering of kind `job-stop` whose text is the stop line
  ("The accounting check stopped: <reason>." or, for exhausted adoptions, "The accounting
  check kept stopping — ask again when you want me to retry.");
- every rendering's text goes through `views.fit_lines(lines, closing=views.FIT_CLOSING)`, so it
  is at most `TELEGRAM_LIMIT` whatever the number of documents (Terra plan-r6 S2);
- a handover → a rendering of kind `handover`: one line per document from
  `steps._pairing(conn, doc_id)` with the existing case lines of SKILL.md §"a document the
  operator hands over", step 3, keyed by the verdict when the pairing is `unpaired`.

`views.review_in_tx(conn, view, **kw)` is `_build_review`'s body without its own
`with db.tx(conn):` (`views.py:915`), in a `try/finally` that resets `_NAMES`, as
`build_review` does. `_build_review` becomes `with db.tx(conn): return
review_in_tx(conn, …)`.

`steps._claim_sends_tx(conn)` is in-tx and assumes the custody lock is held:

```python
def _claim_sends_tx(conn) -> dict:
    """A sends-only claim (S2 §6.4): a stalled staged send, or a buildable or built
    package request whose lease lapsed. The caller holds the custody lock and the
    write transaction."""
    import passes
    assert conn.in_transaction
    cand = _choose(conn, sends_only=True)
    if cand[0] == "delivery":
        out = _claim_delivery(conn, cand[1])
    elif cand[0] == "request":
        out = _claim_request(conn, cand[1])
    else:
        return {"continue": None}
    out["more"] = passes.queued_waiting(conn)
    _fits(out)
    return out
```

`steps.claim(…, sends_only=True)` from Task 3 is not needed; drop that parameter from Task 3.
Keep `_choose(conn, sends_only=False)`.

Add `test_report_is_one_transaction_under_the_custody_lock` to `tests/test_s2_report.py`.
With another connection holding `db.custody_lock()` and a short bound,
`asks.job_report(self.conn)` raises `db.Busy`, and nothing changed: same `render_id`s,
`drain` and `orphaned_by` as before.

`tools._deliverable` also checks every `texts[i].text` of a `job_report` answer. Add
`test_a_handover_of_200_documents_fits_one_message` to `tests/test_s2_report.py`: every
`texts[].text` is ≤ `views.TELEGRAM_LIMIT` in UTF-16 units (`views.utf16_len`).

`mark_reported(conn, render_id)` is in-tx:
`UPDATE work_requests SET state='reported' WHERE render_id=? AND state='done'`.
`views.mark_rendering_delivered` calls it inside its transaction.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_report tests.test_views tests.test_delivery -v`, then
the suite. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/asks.py server/views.py server/steps.py tests/_base.py tests/test_s2_report.py
git commit -m "feat(s2): job_report — orphan handoff by job id, standing retry, results shown at least once, send recovery"
```

---

### Task 11: Note confirmation: claim-ordered, margin-gated, mint-registered (§4, INV-J11)

**Files:**
- Modify: `server/sweep.py` (`record_observation` stamps issue gens; `note_confirmed`)
- Modify: `server/ledger.py:430` (the call passes `conn`); `server/ledger.py:115–121` (a merge
  carries the loser's issue generations to the survivor, as it carries their times)
- Test: `tests/test_s2_notes.py`

**Interfaces:**
- Produces: `sweep.Z_S = 900`; `sweep.note_confirmed(proj, tag_revision, *, epoch,
  conn=None) -> bool`. `conn` is needed only when an other-text issue carries a claim
  generation (an S2 issue). Pre-S2 lineages carry none and keep today's path, with the epoch
  clause now at Z. A generation present with `conn=None` raises `TypeError`.

The rule, in addition to today's first two clauses (the read saw this note at this tag
revision):
- **(a)** If there is an other-text issue (`note_other_issued_gen`, or `note_issued_gen` when
  `note_issued_seq != note_seq`), the read (`note_seen_gen`, a new column set where
  `note_seen_*` is set, from the observation's token) must be under a later claim than it;
- **(b)** the read's time `note_seen_at` must be ≥ the `at` of the first claim with
  `gen >` that issue's gen, plus `Z_S`;
- **(c)** the latest ledger probe must have `gen >` that issue's gen, list `version.WORKFLOW`
  in `registered`, and not list it in `missing`.

The epoch clause stays: `note_seen_at > epoch + Z_S`. With no other-text issue and no epoch,
nothing beyond today's first two clauses.

`projections.note_seen_gen` is created in Task 1.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_s2_notes.py
"""INV-J11 on note_confirmed itself: claims, issue gens and the ledger probe are rows."""
import datetime as _dt
from tests._base import StoreCase

T0 = _dt.datetime(2026, 10, 2, 12, 0, 0)


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class NoteConfirmation(StoreCase):
    def setUp(self):
        super().setUp()
        import db, version
        with db.tx(self.conn):
            for gen, at in ((1, T0), (2, T0 + _dt.timedelta(minutes=5))):
                self.conn.execute("INSERT INTO claims(gen, job_id, at) VALUES (?,?,?)",
                                  (gen, "aaaaaaaa-1", ts(at)))
        self.workflow = version.WORKFLOW

    def ledger(self, gen, registered=True, missing=()):
        import db, json
        data = {"registered": {self.workflow: "b1"} if registered else {},
                "missing": list(missing)}
        with db.tx(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO probes(kind, ok, detail, data_json,"
                              " observed_at, pass_id, gen) VALUES ('ledger',1,'',?,?,NULL,?)",
                              (json.dumps(data), ts(T0), gen))

    def proj(self, seen_gen, seen_at, other_gen=1):
        return {"note_body": "x", "note_seq": 2, "note_seen_seq": 2, "note_seen_rev": 7,
                "note_seen_at": ts(seen_at), "note_seen_gen": seen_gen,
                "note_issued_seq": 2, "note_issued_gen": 1, "note_other_issued_gen": other_gen,
                "note_issued_at": None, "note_other_issued_at": None}

    def confirmed(self, proj):
        import sweep
        return sweep.note_confirmed(proj, 7, epoch=None, conn=self.conn)

    def test_a_same_claim_read_never_confirms_an_other_text_issue(self):
        self.ledger(gen=2)
        self.assertFalse(self.confirmed(self.proj(1, T0 + _dt.timedelta(days=1))))

    def test_z_counts_from_the_first_later_claim(self):
        import sweep
        self.ledger(gen=2)
        g2 = T0 + _dt.timedelta(minutes=5)
        z = _dt.timedelta(seconds=sweep.Z_S)
        self.assertFalse(self.confirmed(self.proj(2, g2 + z - _dt.timedelta(seconds=1))))
        self.assertTrue(self.confirmed(self.proj(2, g2 + z)))

    def test_waits_for_the_mints_registration(self):
        import sweep
        g2 = T0 + _dt.timedelta(minutes=5)
        late = g2 + _dt.timedelta(seconds=sweep.Z_S + 60)
        self.ledger(gen=2, registered=False)
        self.assertFalse(self.confirmed(self.proj(2, late)))
        self.ledger(gen=2, registered=True, missing=[self.workflow])
        self.assertFalse(self.confirmed(self.proj(2, late)))
        self.ledger(gen=1, registered=True)                 # a probe not after the issue
        self.assertFalse(self.confirmed(self.proj(2, late)))
        self.ledger(gen=2, registered=True)
        self.assertTrue(self.confirmed(self.proj(2, late)))

    def test_no_other_text_issue_needs_no_margin(self):
        self.ledger(gen=1)
        self.assertTrue(self.confirmed(self.proj(1, T0, other_gen=None)))
```

Also add one integration case to `tests/test_sweep_real.py` (`class TestNoteClaims(Base)`). It
runs a real note write and its re-read under one `job.claim`. A fresh `new_pass()` import
under the same claim then leaves the lineage due, and an import made Z after a later claim
(`patch_clock`) does not.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_s2_notes -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`record_observation`, **only when the live pass is a job pass** (`job.live_job_pass(conn)
is not None`). A delegation-protocol observation leaves every generation NULL, so the existing
path, its callers and `TestTheNoteWindow` (`tests/test_export_classification.py:229`, no
`conn`) are unchanged (Terra plan-r5 S1):
- Where `note_issued_at`/`note_issued_seq` are set, also set
  `note_issued_gen = int(token)`.
- Where `note_other_issued_at` is maxed, set
  `note_other_issued_gen = max(coalesce(note_other_issued_gen, 0), note_issued_gen)`.
- Where `note_seen_*` is set, also set `note_seen_gen = int(token)` (NULL when cleared).

`note_confirmed(proj, tag_revision, *, epoch, conn)`:

```python
Z_S = 900


def note_confirmed(proj, tag_revision, *, epoch, conn=None) -> bool:
    import version
    if proj["note_body"] is None:
        return True
    if proj["note_seen_seq"] is None or proj["note_seen_seq"] != proj["note_seq"]:
        return False
    if proj["note_seen_rev"] is None or proj["note_seen_rev"] != tag_revision:
        return False
    seen_at = proj["note_seen_at"]
    # today's time clause, with Z for the ceiling: the epoch and every other-text issue
    # recorded by time (pre-S2 issues carry no generation; S2 issues carry both)
    others = [proj["note_other_issued_at"], epoch]
    if proj["note_issued_seq"] != proj["note_seq"]:
        others.append(proj["note_issued_at"])
    others = [t for t in others if t is not None]
    if others and (seen_at is None
                   or not _parse_ts(seen_at) > max(_parse_ts(t) for t in others) + _Z()):
        return False
    gens = [proj["note_other_issued_gen"]]
    if proj["note_issued_seq"] != proj["note_seq"]:
        gens.append(proj["note_issued_gen"])
    gens = [g for g in gens if g is not None]
    if not gens:
        return True
    issue = max(gens)
    if conn is None:
        raise TypeError("note_confirmed needs conn for a claim-generation issue")
    seen_gen = proj["note_seen_gen"]
    if seen_gen is None or seen_gen <= issue:
        return False                                            # (a)
    first = conn.execute("SELECT at FROM claims WHERE gen > ? ORDER BY gen LIMIT 1",
                         (issue,)).fetchone()
    if first is None or _parse_ts(seen_at) < _parse_ts(first["at"]) + _Z():
        return False                                            # (b)
    led = conn.execute("SELECT gen, data_json FROM probes WHERE kind='ledger'").fetchone()
    data = json.loads(led["data_json"] or "{}") if led is not None else {}
    if (led is None or led["gen"] is None or led["gen"] <= issue
            or version.WORKFLOW not in (data.get("registered") or {})
            or version.WORKFLOW in (data.get("missing") or [])):
        return False                                            # (c)
    return True


def _Z():
    return _dt.timedelta(seconds=Z_S)
```

- A delegation-protocol issue (gens NULL) falls under today's clauses: `note_other_issued_at`
  and `note_issued_at` against `note_seen_at`, now with Z instead of 600 s. The epoch clause
  also uses Z, as the spec maps it (the migration's `set_epoch`).
- `tests/test_export_classification.py` calls `note_confirmed` without `conn`, on rule
  fixtures that carry no generations; those calls stay valid once
  `TestNoteConfirmedRule.proj` gains `"note_issued_gen": None, "note_other_issued_gen": None,
  "note_seen_gen": None` (the code reads those keys; Astra plan-r4 S1). Its cases that pin the 600 s
  margin (`grep -n "600\|CEILING" tests/test_export_classification.py`) move to `sweep.Z_S`,
  citing spec §4. The file joins this task's commit.
- **`note_seen_at` stays the import time of the read's snapshot, deliberately** (Terra plan-r3
  S2 is declined and asked back in round 4).
  - The `get_transaction` read happens before `record_observation` records it, so the record's
    own time overstates when the row was seen. The Z margin would then be measured from too
    late a moment, which is the unsafe direction.
  - The import time always precedes the read. Its only cost is one more read of a note that
    changed lately, now or at the next pass, which is the cost spec §4 states.
- Remove the `steps.CEILING_ASSUMED_S` use from `note_confirmed`; keep the constant if other
  code reads it (`grep -n CEILING_ASSUMED_S server/`).

`ledger.py:430`: pass `conn=conn`.

`ledger.py`, the merge (around line 115): next to the `note_other_issued_at` transfer, add:

```python
    gens = [g for g in (s["note_other_issued_gen"], lo["note_issued_gen"],
                        lo["note_other_issued_gen"]) if g is not None]
    if gens:
        conn.execute("UPDATE projections SET note_other_issued_gen=? WHERE pid=?",
                     (max(gens), survivor))
```

Add `test_a_merge_carries_the_losers_issue_generation` to `tests/test_s2_notes.py`. It builds
two lineages with `lineage_for`. The loser has `note_issued_gen=2`, `note_issued_seq` ≠ the
survivor's `note_seq`, and its `note_issued_at` set. It calls `ledger.merge(conn, survivor, loser)` inside `db.tx`, then checks
that the survivor's `note_confirmed(…)`, with a read under claim 2, is False. Before the change
the same read is True (Astra's reproduction: 0 → 1 confirmation).

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_s2_notes tests.test_sweep_real tests.test_ledger_real -v`,
then the suite. Expected: PASS. An existing test that pinned the 600 s ceiling is updated to
Z, citing spec §4.

- [ ] **Step 5: Commit**

```bash
git add server/sweep.py server/ledger.py tests/test_sweep_real.py tests/test_s2_notes.py tests/test_export_classification.py
git commit -m "fix(s2): note confirmation is claim-ordered, Z-gated and waits for the mint's registration (spec §4)"
```

---

### Task 12: No-Gmail wording, classification counts, the waiting line, and the tool surface

**Files:**
- Modify: `server/alerts.py` (the gmail-absent text), `server/views.py` (`_degraded_block`, the
  waiting line), `server/binding.py` (the `check_setup` condition text)
- Modify: `server/tools.py` (add, remove and change tools), `.claude-plugin/plugin.json`
- Test: `tests/test_s2_surface.py`

**Interfaces:**
- Produces, as tools: `job_status(job_id)` (read-only; #1180), `job_next(job_id?, pass_token?, judged?)`,
  `job_report(job_id?, status?)`, `request_work(kind, trigger, doc_ids?)`,
  `request_package(quarter, channel)`, `record_filing(pass_token)`;
  `import_ledger_export` gains `acq`; `record_probe` gains `acq`, `absent`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_s2_surface.py
import json, pathlib, subprocess, sys
from tests._base import StoreCase, ROOT


class Surface(StoreCase):
    def test_tool_lists_agree_and_old_tools_are_gone(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/check_tool_agreement.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        import qa_server, tools  # noqa: F401
        for gone in ("begin_pass", "end_pass", "continue_pass", "record_step", "more_work"):
            self.assertNotIn(gone, qa_server.TOOLS)
        for new in ("job_next", "job_status", "job_report", "request_work",
                    "request_package", "record_filing"):
            self.assertIn(new, qa_server.TOOLS)

    def test_the_job_declaration_is_verbatim(self):
        m = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(m["casa"]["jobs"], [{
            "name": "work", "skill": "quarterly-job", "title": "Accounting check",
            "summary": "Checks the bank and Gmail, judges documents, prepares packages",
            "batches": "unlimited", "turnsPerBatch": 80, "session": "fresh",
            "host": "specialist"}])
        self.assertEqual(m["version"], "0.9.0")

    def test_gmail_absent_says_not_connected_not_reauthorise(self):
        import alerts, passes, views
        tok = self.pass_("cron")
        passes.record_probe(self.conn, tok, "gmail", False, absent=True)
        text = views.build_review(self.conn, view="status")["text"]
        self.assertIn("isn't connected for the finance specialist", text)
        self.assertNotIn("Re-authorise", text)

    def test_a_waived_freshness_window_is_disclosed(self):
        import views
        tok = self.pass_("cron")
        self.end_with_counts(tok, "complete", {"read_age_min": 75})
        self.assertIn("bank read from 75 minutes",
                      views.build_review(self.conn, view="status")["text"])

    def test_payments_awaiting_classification_are_said(self):
        import views
        tok = self.pass_("cron")
        self.end_with_counts(tok, "complete", {"awaiting_classification": 3})
        self.assertIn("3 payments still await classification",
                      views.build_review(self.conn, view="status")["text"])

    def test_a_queued_request_is_visible_in_status(self):
        import asks, views
        asks.request_work(self.conn, "check", "operator")
        self.assertIn("A check is waiting to start",
                      views.build_review(self.conn, view="status")["text"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest tests.test_s2_surface -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`tools.py`:
- Delete the registrations `t_begin`, `t_end`, `t_continue`, `t_step`, `t_more_work`.
- Add:

```python
@register("job_next",
          "The job's next step. First call of every job turn: job_next(job_id=<your brief's "
          "`Job id:` line>) — it gives you a pass_token; then job_next(pass_token=…) after "
          "each step, with judged={page_next, triage_remaining, documents} after a judge "
          "step. Do exactly the unit it returns. When it says report=true, call "
          "report_job_progress with its `progress` verbatim; at end-batch, end your turn; at "
          "complete, report_job_progress then emit_completion(status=\"ok\", text=<its text>).",
          obj({"job_id": S, "pass_token": TOKEN, "judged": O}))
def t_job_next(args):
    import job
    tok = _int(args, "pass_token")
    if tok is None:
        _need(args, "job_id")
        tok = job.claim(conn(), args["job_id"])
    return _deliverable("job_next", job.next_unit(conn(), tok, judged=args.get("judged")))


@register("job_status",
          "Read-only, never a claim: may this job end now? In an operator message's turn in the "
          "job's topic, call it LAST with your brief's `Job id:`; if `done`, call "
          "report_job_progress(summary=<its text>, progressed=true) then "
          "emit_completion(status=\"ok\", text=<its text>).",
          obj({"job_id": S}, ("job_id",)))
def t_job_status(args):
    import job
    _need(args, "job_id")
    return job.status(conn(), args["job_id"])


@register("job_report",
          "Ellen: on every notification about the accounting job (pass the id it names and "
          "status ok or error) and at the start of every accounting turn (no arguments). Send "
          "every `texts` entry and `speak` verbatim, each then mark_rendering_delivered; do a "
          "`continue` as Packaging step 3 says; if `start_job` is set, call start_job with it.",
          obj({"job_id": S, "status": S}))
def t_job_report(args):
    import asks
    return _deliverable("job_report", asks.job_report(conn(), job_id=args.get("job_id"),
                                                      status=args.get("status")))


@register("request_work",
          "Ellen: record a check (kind=check, trigger cron|operator) or a handed-over "
          "document (kind=handover, trigger=operator, doc_ids) BEFORE start_job; then call "
          "start_job with the returned start_job and say the returned line.",
          obj({"kind": S, "trigger": S, "doc_ids": AI}, ("kind", "trigger")))
def t_request_work(args):
    import asks
    _need(args, "kind", "trigger")
    return asks.request_work(conn(), args["kind"], args["trigger"], args.get("doc_ids"))


@register("request_package",
          "Ellen: ask for a quarter's package (channel telegram or email) BEFORE start_job; "
          "then start_job with the returned start_job, and say the returned line.",
          obj({"quarter": Q, "channel": S}, ("quarter", "channel")))
def t_request_package(args):
    import asks
    _need(args, "quarter", "channel")
    return asks.request_package(conn(), _quarter(args), args["channel"])


@register("record_filing",
          "The job's filing unit is done (each attachment was filed with ingest_document and "
          "its source_ref).",
          obj({"pass_token": TOKEN}, ("pass_token",)))
def t_record_filing(args):
    import job
    _need(args, "pass_token")
    return job.record_filing(conn(), _int(args, "pass_token"))
```

- In `t_import`, pass `acq=_int(args, "acq")` and add `"acq": I` to its schema.
- In `t_probe`, pass `acq=_int(args, "acq")` and `absent=_bool(args, "absent", False)`, and
  add both to its schema.

`plugin.json`:
- version `0.9.0`;
- add `casa.jobs` (verbatim, Global Constraints);
- in `provides_tools` and `resultContract.tools`, remove the five old tools and add the five
  new ones, each `{"result": "safe"}`.

`server/version.py`: `WORKFLOW` follows the manifest. Check how it reads the version; if it is
hard-coded, set `acct@0.9.0`.

`alerts.py`:
- The `gmail` alert takes the probe's `data.absent`. When it is set, the text is "Gmail isn't
  connected for the finance specialist — invoices aren't being searched." (no "Re-authorise").
- `binding.check_setup`'s Gmail condition reads the same flag.

`views.py`:
- `_degraded_block` keeps "Review incomplete - Gmail unavailable." for both cases, and adds the
  absent sentence for `absent`.
- When the last pass's stored report carries `read_age_min` (Task 7's `_read_age_note`; it
  survives `stored_report`, which keeps keys other than the counts), the status head adds
  "These results use a bank read from <n> minutes before they were finished."
- When the last pass's stored report carries `awaiting_classification` (n > 0), the status
  head adds "<n> payment(s) still await classification — they're checked again once
  classified."
- The status head adds "A check is waiting to start, asked <when>." while a work request is
  `queued` and `meta.drain` is `none`. `<when>` uses the existing "N minutes ago" wording of
  `passes.BUSY`.

- [ ] **Step 4: Run the tests (not the whole suite yet)**

Run: `python3 -m unittest tests.test_s2_surface tests.test_tools tests.test_alerts tests.test_views -v`.
Expected: PASS. **Do not run the whole suite or commit here.** Removing the old tools leaves
`tests/test_skill.py`'s skill-contract tests red until Task 13 rewrites the skills (Astra
plan-r5 S1). Tasks 12 and 13 are one checkpoint: continue with Task 13. `tests/test_tools.py` tests that called the removed tools
through the tool layer are changed to call the internal functions (`passes.begin_pass` etc.),
or deleted when they test only the tool wrapper. Name each in the commit.

- [ ] **Step 5: No commit here** (see Step 4). Task 13's commit includes this task's files.

---

### Task 13: The skills — the job's procedure, and Ellen's thin interim

**Files:**
- Create: `skills/quarterly-job/SKILL.md`
- Modify: `skills/quarterly-accounting/SKILL.md`
- Modify: `tests/test_skill.py`
- Modify: `README.md` (the job, the new tools, the floor)

**Interfaces:** none (text). `tests/test_skill.py` pins the text to the tools that exist
(`test_every_backticked_tool_exists`, `test_every_named_argument_exists_on_its_tool`).

**`skills/quarterly-job/SKILL.md`** (target ≤ 20,000 characters). Frontmatter:

```
---
name: quarterly-job
description: The quarterly-accounting job's procedure, for the finance specialist inside the "Accounting check" job only. Use when the turn's brief names the job quarterly-accounting:work.
---
```

Body, in this order:
1. **"Every turn"** — "Your first call is `job_next(job_id=<the `Job id:` line of your
   brief>)`. It gives you `pass_token`. Then do exactly the unit it returns, and call
   `job_next(pass_token=…)` again."
   - On `report: true` → `report_job_progress` with `progress` verbatim.
   - On `end-batch` → end the turn.
   - On `complete` → `report_job_progress`, then `emit_completion(status="ok",
     text=<its text>)`.
   - A refusal saying the turn is no longer current → call `job_next(job_id=…)` once more; if
     that is refused too, end the turn.
   - An operator message in the topic (not a batch) → answer it read-only with
     `build_review`/`list_quarter_state`. A verdict ("the X one is wrong") gets the answer
     "Tell me that in the main chat, where you saw the list." **Never call `job_next` in a
     topic message.** Last, always: `job_status(job_id=<the Job id line>)`. If `done`, call
     `report_job_progress(summary=<its text>, progressed=true)` and `emit_completion(status="ok",
     text=<its text>)` (ha-casa-app#1180: a completion Casa refused for an unread message is
     re-issued here).
2. **"Units"**, one subsection per unit:
   - `probes` — today's "The specialist's pass" step 1, with `record_probe(kind="bank_sync",
     acq=<the unit's acq>, …)`. The `ledger` probe's data adds `missing`: the workflows
     `list_backups` marks "FILE MISSING". The sync's `Queue:` counts go in the `bank_sync`
     probe's data as `queue`. If Gmail tools are not visible to you, the `gmail-probe` unit
     is `record_probe(kind="gmail", ok=false, absent=true)`.
   - `snapshot` — today's step 3 (`import_ledger_export(path, pass_token, ledger_instance,
     acq=<the unit's acq>)`, with the export you made in THIS unit only) and step 4 (ends).
   - `sweep` — today's step 5, one `list_projections(pass_token, quarter=<the unit's
     quarter>)` page.
   - `gmail-probe` — one small `search_emails`, then `record_probe(kind="gmail", …)`.
   - `filing` — the self-addressed search and at most 8 attachments, filed with the token and
     each `source_ref`, skipping `filed_refs`; then `record_filing(pass_token)`.
   - `item` — today's Gmail round per-item rules (≤ 4 queries, ≤ 2 tries, then
     `record_search`), for the ONE item handed out.
   - `judge` — today's step 6 (triage with the auto-match bar, verbatim) and step 7, for one
     `list_quarter_state(triage=true, quarter=…, after=<the unit's after>)` page. Judge the
     unit's `documents_first` first. For a package, confirm up to 5 unread dates
     (`dates_unread=true`). Finish with `job_next(pass_token, judged={page_next: <the page's
     next>, triage_remaining: <the page's remaining>, documents: {<doc_id>: <verdict>}})`.
3. **"Never"** — today's specialist prohibitions, verbatim: `bind_account`,
   `build_quarterly_package`, `set_watermark`, `set_package_name`, `stop_chasing`. Plus:
   "never speak to the operator; never call `request_work`, `request_package` or
   `job_report`".

**`skills/quarterly-accounting/SKILL.md`** (Ellen):
- **Delete:** "Ellen: a delegation that answers later" (rules 1–8 and the outcome rule); "Ellen:
  the pass"; "The specialist's pass" (moved); Packaging steps 1–2 (the rounds).
- **Replace with "Ellen: asking for work":**
  - **The cron:** file the Telegram inbox (`list_inbound_files` → `share_inbound_file` →
    `ingest_document(source="manual-telegram", extraction_author="resident", …)`), then
    `request_work(kind="check", trigger="cron")`, then `start_job` with the returned
    `start_job`, then `<silent/>`.
  - **"Go and check now" / "check emailed invoices":** the same with `trigger="operator"`; say
    the returned line.
  - **A handed-over document:** file it, `request_work(kind="handover", trigger="operator",
    doc_ids=[…])`, `start_job`, say the returned line.
  - **A package ask:** `request_package(quarter, channel)`, `start_job`, say the returned
    line.
  - **`start_job`'s answer:**
    - `pending` or `job_busy`: done.
    - Anything else: say "I couldn't start the check yet (<its message>) — I'll start it the
      next time we talk about accounting."
- **"Ellen: the job's results":**
  - On any notification about the accounting job: `job_report(job_id=<the id in "(id …)">,
    status=ok|error)`.
  - At the start of every accounting turn: `job_report()`.
  - Send every `texts` entry and `speak` verbatim, each then `mark_rendering_delivered`.
  - If `continue` is set, do Packaging step 3 with its token (`next: build` or `stage`).
  - If `start_job` is set, call `start_job` with it (silently).
  - Never relay a notification's own text.
- **Kept unchanged:** refusals and errors; answering (views); replies (`apply_reply`) except
  the `check emailed invoices` instruction, which becomes the operator check above; Packaging
  step 3–5 (build, stage, send, record); Install; Test install and reset.
- **Install:** "install `casa-plugin-quarterly-accounting` for Ellen and the finance
  specialist" stays. The cron prompt is unchanged.
- The frontmatter description drops "or when a system notification says a delegation to the
  finance specialist returned or failed" and adds "or when a notification says the accounting
  job ended".

`tests/test_skill.py`:
- Point the specialist-procedure tests at `skills/quarterly-job/SKILL.md`:
  `test_sweep_transcribes_the_sim`, `test_the_specialist_order_matches_the_design`,
  `test_every_bank_feed_write_carries_workflow_and_generation`,
  `test_row_digest_comes_from_the_listing`, `test_only_payments_read_since_the_import_are_judged`,
  `test_every_observation_names_the_import_it_was_read_under`,
  `test_the_specialist_never_binds_and_sets_only_a_vendor_kind`,
  `test_a_chunk_is_worked_one_item_at_a_time_within_its_limits`.
- Delete the tests of deleted protocol: `test_a_check_runs_in_chunks_each_ended_by_an_async_judgment`,
  `test_ellen_holds_the_package_and_handover_passes`, `test_a_due_judgment_starts_the_judge_step`,
  `test_the_handover_suspects_the_data_first`.
- Add:
  - `test_the_job_skill_starts_every_turn_with_job_next`;
  - `test_ellen_records_the_ask_before_start_job` (`request_work`/`request_package` appear
    before `start_job` in every flow);
  - `test_ellen_never_relays_a_notification_text`;
  - `test_the_job_skill_never_calls_job_next_in_a_topic_message`;
  - `test_a_topic_message_turn_ends_with_job_status` (#1180).

- [ ] **Step 1:** Write the new and repointed tests in `tests/test_skill.py`. Run them: they fail
  (the new skill is missing).
- [ ] **Step 2:** Write `skills/quarterly-job/SKILL.md` and edit
  `skills/quarterly-accounting/SKILL.md` as above.
- [ ] **Step 3:** Run `python3 -m unittest tests.test_skill -v`, then
  `python3 scripts/check_tool_agreement.py`, then the whole suite. Expected: PASS.
- [ ] **Step 4:** Check the size: `wc -c skills/*/SKILL.md`. The job skill must be ≤ 20,000
  characters; report Ellen's skill's size in the commit.
- [ ] **Step 5: Commit**

```bash
git add server/tools.py server/alerts.py server/views.py server/binding.py server/version.py .claude-plugin/plugin.json tests/test_s2_surface.py tests/test_tools.py skills/ tests/test_skill.py README.md
git commit -m "feat(s2): the job tool surface and casa.jobs, the job's own skill, Ellen's thin interim; no-Gmail wording; a waiting check is visible"
```

This one commit covers Tasks 12 and 13 (Astra plan-r5 S1): the suite is green at it and at no
point in between.

---

## After the tasks

- **Diff review.** Astra (`gpt-6-astra`, medium) and Terra (`gpt-5.6-terra`, medium) over the
  branch, frozen at its head SHA. Brief per the global policy: the asset, silence, and a
  DO-NOT-REPORT list including the stated residuals of spec §4 and §6.4. Re-review after fixes,
  until both SHIP.
- **Live test (the playbook lane, PLAY):**
  - one real-data check and one package on casa-test;
  - the two §4/§8 live checks: the in-flight-only write on SIGKILL, and a plugin update
    restarting MCP before old code calls;
  - a cost measurement against session 45.
- **Release:** merge, push, tag `v0.9.0` (annotated) on the merge commit. The coordinator
  reports the release.
