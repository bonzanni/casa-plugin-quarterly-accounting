"""The plugin's store: ONE SQLite file in $CLAUDE_PLUGIN_DATA, written by
several server processes — Casa spawns this MCP server per agent session,
so Ellen's and each specialist's are separate processes on one file (spec
§Match records, rounds 20-21). The one serialization: every write runs
inside tx(), which takes the write lock with BEGIN IMMEDIATE, retried until
acquired or LOCK_BOUND_S expires; the store-wide sequence is allocated
inside that transaction, so the later-serialized write always has the
higher sequence. Contention past the bound raises Busy, a refusal the tool
reports, so nothing is silently dropped."""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import pathlib
import sqlite3
import time

DB_NAME = "accounting.sqlite"
CUSTODY_LOCK = ".custody.lock"
SCHEMA_VERSION = 9
BUSY_TIMEOUT_MS = 2000
LOCK_BOUND_S = 30.0


class Refusal(Exception):
    """An expected, explained refusal. The dispatcher renders it as
    `refused: <message>` (spec §Error handling: explicit, loud, never silent)."""


class Busy(Refusal):
    """The store's write lock stayed held past the bound: nothing applied."""


def _clock() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def now() -> str:
    return _clock().strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def data_dir() -> pathlib.Path:
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        raise RuntimeError("CLAUDE_PLUGIN_DATA is not set; refusing to place the "
                           "accounting store anywhere else")
    return pathlib.Path(d)


DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO counters(name, value) VALUES ('seq', 0), ('pass_generation', 0);

CREATE TABLE IF NOT EXISTS binding (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  account_id TEXT NOT NULL,
  account_label TEXT,
  watermark TEXT NOT NULL,
  bound_at TEXT NOT NULL,
  package_name TEXT NOT NULL,
  package_name_announced INTEGER NOT NULL DEFAULT 0,
  watermark_announced INTEGER NOT NULL DEFAULT 0,
  row_high_water INTEGER NOT NULL DEFAULT 0,
  ledger_generation INTEGER,
  ledger_instance TEXT,          -- bank-feed's ledger instance id this store is bound to (#69)
  ledger_reset_ack INTEGER NOT NULL DEFAULT 0);   -- the operator said the ledger was reset

CREATE TABLE IF NOT EXISTS pass_marker (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  generation INTEGER NOT NULL, live INTEGER NOT NULL,   -- generation: the live pass token
  pass_id TEXT, trigger TEXT, started_at TEXT,
  claimed_step TEXT,             -- the step the current token continues (continue_pass)
  lease_at TEXT);                -- when the token's holder last made progress
CREATE TABLE IF NOT EXISTS passes (
  pass_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, trigger TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT,
  account_seen INTEGER NOT NULL DEFAULT 0,
  snapshot_id INTEGER,
  gate_json TEXT,                -- this pass's bank-write verdict, decided once (sticky refusal)
  report_json TEXT,
  reply TEXT NOT NULL DEFAULT 'telegram');   -- where a continuation reports: telegram | silent
CREATE TABLE IF NOT EXISTS pass_steps (
  pass_id TEXT NOT NULL,
  step TEXT NOT NULL CHECK (step IN ('sweep', 'judge', 'handover', 'snapshot')),
  started_at TEXT NOT NULL,      -- written by Ellen before she delegates
  finished_at TEXT,
  finished_by TEXT CHECK (finished_by IN ('specialist', 'resident')),
  finish_json TEXT,              -- counts, stopped (clipped), failed
  carry_json TEXT NOT NULL DEFAULT '{}',   -- doc_ids / report: ids and counts only
  PRIMARY KEY (pass_id, step));
CREATE TABLE IF NOT EXISTS package_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  quarter TEXT NOT NULL, channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  pass_id TEXT,                  -- the pass of its latest round (NULL: queued, never run)
  pass_outcome TEXT, reason TEXT,
  package_id INTEGER, delivery_id INTEGER,
  token INTEGER, lease_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('queued', 'snapshot', 'snapshot-done', 'built',
        'staged', 'delivered', 'uncertain', 'failed', 'stopped', 'recovery-failed',
        'revoked', 'withdrawn', 'superseded')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  created_seq INTEGER NOT NULL DEFAULT 0,  -- a search counts for it only after this (#15)
  round INTEGER NOT NULL DEFAULT 0,        -- rounds of its check finished (#15)
  remaining INTEGER,                       -- what the last round left (#15)
  check_json TEXT,                         -- what the caption says about the check (#15)
  checked_snapshot INTEGER);               -- the import its finished check ran on (#15)
CREATE INDEX IF NOT EXISTS ix_package_requests_open ON package_requests(quarter, state);
CREATE TABLE IF NOT EXISTS probes (
  kind TEXT PRIMARY KEY, ok INTEGER NOT NULL, detail TEXT, data_json TEXT,
  observed_at TEXT NOT NULL, pass_id TEXT, failing_since TEXT);

CREATE TABLE IF NOT EXISTS documents (
  doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT NOT NULL UNIQUE, ext TEXT NOT NULL, size INTEGER NOT NULL,
  kind TEXT NOT NULL, counterparty TEXT, issuer TEXT, document_date TEXT,
  document_number TEXT, amount_minor INTEGER, currency TEXT, recipient TEXT,
  source TEXT NOT NULL, source_ref TEXT, acquisition_json TEXT,
  extraction_author TEXT NOT NULL, original_name TEXT,
  irrelevant INTEGER NOT NULL DEFAULT 0,
  ingested_at TEXT NOT NULL, ingest_quarter TEXT NOT NULL,
  date_read_at TEXT);            -- when document_date was last read on the document (#22)

CREATE TABLE IF NOT EXISTS counterparties (
  cp_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  patterns_json TEXT NOT NULL DEFAULT '[]',
  exp_kind TEXT, exp_tier TEXT, exp_author TEXT,
  source TEXT CHECK (source IN ('email', 'portal')),
  document_link TEXT, link_note TEXT, search_hint TEXT, notes TEXT,
  window_days INTEGER NOT NULL DEFAULT 10, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chain_overrides (
  scope TEXT PRIMARY KEY, kind TEXT NOT NULL, tier TEXT,
  rows_json TEXT NOT NULL, key_json TEXT NOT NULL,   -- expectation.normalize_scope, fixed at set time
  author TEXT NOT NULL, set_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, pass_id TEXT,
  imported_at TEXT NOT NULL, rows INTEGER NOT NULL, max_row_id INTEGER NOT NULL,
  bank_through TEXT);            -- the date bank data is known good through (sync ok this pass)
CREATE TABLE IF NOT EXISTS bank_rows (
  row_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, first_seen TEXT,
  booking_date TEXT, value_date TEXT, amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL, direction TEXT NOT NULL, status TEXT,
  counterparty TEXT, remittance TEXT, state TEXT NOT NULL,
  superseded_by INTEGER, needs_review INTEGER NOT NULL DEFAULT 0,
  review_reason TEXT, snapshot_id INTEGER NOT NULL,
  fx_rate TEXT, fx_unit TEXT);   -- the bank's exchange rate and its unit (issue #35), verbatim

CREATE TABLE IF NOT EXISTS projections (
  pid INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_row_id INTEGER NOT NULL,
  admitted_at TEXT NOT NULL, admitted_snapshot INTEGER,
  ended TEXT CHECK (ended IN ('vanished', 'erased')), ended_at TEXT, ended_snapshot INTEGER,
  broken_floor TEXT,
  merged_into INTEGER,
  revision INTEGER NOT NULL DEFAULT 0, digest TEXT,
  status TEXT, desired_json TEXT NOT NULL DEFAULT '[]', current_match INTEGER,
  reasons_json TEXT NOT NULL DEFAULT '[]',
  exp_kind TEXT, exp_tier TEXT, exp_row INTEGER,
  class_tags_json TEXT, class_observed_at TEXT, last_known_kind TEXT,
  class_observed_snapshot INTEGER,  -- the snapshot_id it was last observed at: import or read (E2, #1)
  observed_revision INTEGER,     -- the projection's revision that read left it at (fix E2)
  observed_tags_json TEXT, observed_at TEXT,
  export_tag_revision INTEGER,   -- the row's tag_revision in the latest import (issue #1)
  note_seen_seq INTEGER,         -- the note_seq a read last saw visible (issue #1)
  note_seen_rev INTEGER,         -- that read's `Tag revision:` (issue #1)
  note_seen_at TEXT,             -- the import time of the snapshot that read belongs to
  note_issued_at TEXT,           -- when an add_note was last returned to the specialist
  note_issued_seq INTEGER,       -- the note_seq that add_note carried (issue #14)
  note_other_issued_at TEXT,     -- the latest add_note of any OTHER note_seq (issue #14)
  read_snapshot INTEGER,         -- the snapshot the sweep's latest READ belongs to
  last_facts_json TEXT,          -- the destination row's facts when last seen (names an erased row)
  note_seq INTEGER, note_body TEXT,
  unprojectable TEXT, last_error TEXT,
  search_state TEXT NOT NULL DEFAULT 'active'
    CHECK (search_state IN ('active', 'aged-out', 'accepted-missing')),
  search_json TEXT NOT NULL DEFAULT '{}',
  passes_without_candidate INTEGER NOT NULL DEFAULT 0,
  identity_question INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS aliases (
  row_id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, first_seen TEXT);
CREATE INDEX IF NOT EXISTS ix_aliases_pid ON aliases(pid);
CREATE TABLE IF NOT EXISTS cursor (
  id INTEGER PRIMARY KEY CHECK (id = 1), last_pid INTEGER NOT NULL DEFAULT 0,
  cycle_started_at TEXT, last_cycle_completed_at TEXT);
INSERT OR IGNORE INTO cursor(id, last_pid) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS matches (
  match_id INTEGER PRIMARY KEY AUTOINCREMENT,
  pid_created INTEGER NOT NULL, doc_id INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT 'clean', rationale TEXT,
  runners_up_json TEXT NOT NULL DEFAULT '[]', created_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS log (
  seq INTEGER PRIMARY KEY, pid INTEGER NOT NULL, kind TEXT NOT NULL,
  author TEXT NOT NULL, match_id INTEGER, doc_id INTEGER, fp TEXT, render_id TEXT,
  resolves_json TEXT NOT NULL DEFAULT '[]', retire_activation INTEGER,
  retire_to TEXT, cause TEXT, detail TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_log_pid ON log(pid);
CREATE TABLE IF NOT EXISTS match_state (
  match_id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, doc_id INTEGER NOT NULL,
  state TEXT NOT NULL, author TEXT NOT NULL, activation INTEGER NOT NULL,
  fp TEXT, revision INTEGER NOT NULL DEFAULT 0, digest TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_match_state_active_doc
  ON match_state(doc_id) WHERE state IN ('matched', 'proposed');
CREATE INDEX IF NOT EXISTS ix_match_state_pid ON match_state(pid);

CREATE VIEW IF NOT EXISTS document_status AS
  SELECT d.doc_id AS doc_id,
         CASE WHEN d.irrelevant = 1 THEN 'irrelevant'
              WHEN EXISTS (SELECT 1 FROM match_state m WHERE m.doc_id = d.doc_id
                           AND m.state IN ('matched', 'proposed')) THEN 'matched'
              ELSE 'unmatched' END AS status
  FROM documents d;

CREATE TABLE IF NOT EXISTS residue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, reason TEXT NOT NULL,
  detail TEXT, seq INTEGER, created_at TEXT NOT NULL, shown_render TEXT);

CREATE TABLE IF NOT EXISTS renders (
  render_id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope_json TEXT NOT NULL,
  created_at TEXT NOT NULL, delivered_at TEXT, text TEXT NOT NULL,
  membership_json TEXT NOT NULL,
  delivered_seq INTEGER);        -- store sequence at delivery: what "most recent delivered" orders by
CREATE TABLE IF NOT EXISTS render_items (
  render_id TEXT NOT NULL, pid INTEGER NOT NULL, projection_revision INTEGER NOT NULL,
  match_revisions_json TEXT NOT NULL, PRIMARY KEY (render_id, pid));
CREATE TABLE IF NOT EXISTS shown (
  pid INTEGER PRIMARY KEY, render_id TEXT NOT NULL, projection_revision INTEGER NOT NULL,
  match_revisions_json TEXT NOT NULL, delivered_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS packages (
  package_id INTEGER PRIMARY KEY AUTOINCREMENT, quarter TEXT NOT NULL,
  filename TEXT NOT NULL UNIQUE, path TEXT NOT NULL, built_at TEXT NOT NULL,
  partial INTEGER NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
  oversize INTEGER NOT NULL DEFAULT 0, caption TEXT NOT NULL, manifest_json TEXT NOT NULL,
  snapshot_id INTEGER,           -- the import the build froze (fix E4: its first send checks it)
  request_id INTEGER);           -- the package request it was built for (#15, D3)
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT, package_id INTEGER, doc_id INTEGER,
  channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  staged_path TEXT NOT NULL, request_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('staged', 'delivered', 'uncertain', 'failed')),
  message_id TEXT, created_at TEXT NOT NULL, settled_at TEXT,
  revoked_at TEXT,               -- an unsent first send an import superseded (fix E5)
  withdrawn_at TEXT,             -- staged bytes taken back when a stalled send was recovered
  lease_at TEXT,                 -- a staged send's lease: past LEASE_S it is recovered
  as_built INTEGER NOT NULL DEFAULT 0);   -- "send me the last package you built" (#15)
-- every delivery has a path of its own: a holder that was superseded can never hold
-- the path of a later copy
CREATE UNIQUE INDEX IF NOT EXISTS ux_deliveries_staged_path ON deliveries(staged_path);
CREATE TABLE IF NOT EXISTS delivered_rows (
  package_id INTEGER NOT NULL, row_id INTEGER NOT NULL, pid INTEGER,
  facts_fp TEXT NOT NULL, kind TEXT, PRIMARY KEY (package_id, row_id));
CREATE TABLE IF NOT EXISTS alerts (
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  occurrence_key TEXT NOT NULL UNIQUE, detail TEXT NOT NULL, raised_at TEXT NOT NULL,
  render_id TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS operator_refs (
  -- issue #24 (D5): each file the operator supplied (an attachment of a self-addressed
  -- mail, a Telegram file) by its own ref, once filed, so a pass's capped filing skips it
  ref TEXT PRIMARY KEY, source TEXT NOT NULL, doc_id INTEGER NOT NULL, filed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_operator_refs_filed ON operator_refs(filed_at);
"""

# Migrations from version N to N+1, appended when the schema changes. Each is
# a list of statements applied inside the migrating transaction.
MIGRATIONS: dict[int, list[str]] = {
    # 1 -> 2 (fix wave D, Astra S1): delivered_at has one-second resolution, so two
    # deliveries in one second tied and "the most recent delivered rendering" fell
    # back to creation order. A rendering delivered before this migration gets 0:
    # every later delivery (a fresh next_seq, >= 1) orders after it; among the old
    # ones the previous delivered_at order is kept as the tie-break.
    1: ["ALTER TABLE renders ADD COLUMN delivered_seq INTEGER",
        "UPDATE renders SET delivered_seq = 0 WHERE delivered_at IS NOT NULL"],
    # 2 -> 3 (fix E2; its 1 -> 2 renumbered when merged after fix D): classification
    # freshness. A migrated lineage has no stamp, so it is non-fresh until the next
    # sweep reads it: the conservative start.
    2: ["ALTER TABLE projections ADD COLUMN class_observed_snapshot INTEGER",
        "ALTER TABLE projections ADD COLUMN observed_revision INTEGER",
        # a package built before it names no import: its first send is refused (rebuild)
        "ALTER TABLE packages ADD COLUMN snapshot_id INTEGER",
        "ALTER TABLE deliveries ADD COLUMN revoked_at TEXT"],
    # 3 -> 4: a pass spans turns. Its steps and the claim that continues them live in
    # the store, and a package request carries its own rotating token. Every delivery
    # gets a staged path of its own; a v0.1.0 resend could reuse an earlier send's
    # outbox name, so before the UNIQUE index goes on, every older row that shares a
    # path with a newer one is renamed out of the way (the newest row owns the file).
    3: ["ALTER TABLE pass_marker ADD COLUMN claimed_step TEXT",
        "ALTER TABLE pass_marker ADD COLUMN lease_at TEXT",
        "ALTER TABLE passes ADD COLUMN reply TEXT NOT NULL DEFAULT 'telegram'",
        """CREATE TABLE IF NOT EXISTS pass_steps (
  pass_id TEXT NOT NULL,
  step TEXT NOT NULL CHECK (step IN ('sweep', 'judge', 'handover', 'snapshot')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  finished_by TEXT CHECK (finished_by IN ('specialist', 'resident')),
  finish_json TEXT,
  carry_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (pass_id, step))""",
        """CREATE TABLE IF NOT EXISTS package_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  quarter TEXT NOT NULL, channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  pass_id TEXT NOT NULL, pass_outcome TEXT, reason TEXT,
  package_id INTEGER, delivery_id INTEGER,
  token INTEGER, lease_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('snapshot', 'snapshot-done', 'built', 'staged',
        'delivered', 'uncertain', 'failed', 'stopped', 'recovery-failed', 'revoked',
        'withdrawn', 'superseded')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_package_requests_open ON package_requests(quarter, state)",
        "ALTER TABLE deliveries ADD COLUMN withdrawn_at TEXT",
        "ALTER TABLE deliveries ADD COLUMN lease_at TEXT",
        "UPDATE deliveries SET staged_path = staged_path || '#' || delivery_id WHERE delivery_id"
        " NOT IN (SELECT max(delivery_id) FROM deliveries GROUP BY staged_path)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_deliveries_staged_path ON deliveries(staged_path)"],
    # 4 -> 5 (issue #1): the import is the classification observation. A migrated
    # lineage has no note confirmation, so it is read once after the first import.
    4: ["ALTER TABLE projections ADD COLUMN export_tag_revision INTEGER",
        "ALTER TABLE projections ADD COLUMN note_seen_seq INTEGER",
        "ALTER TABLE projections ADD COLUMN note_seen_rev INTEGER",
        "ALTER TABLE projections ADD COLUMN note_seen_at TEXT",
        "ALTER TABLE projections ADD COLUMN note_issued_at TEXT",
        "ALTER TABLE projections ADD COLUMN read_snapshot INTEGER"],
    # 5 -> 6 (issue #14): a read-back confirms the note it shows unless a note of another
    # revision could still land. An issue this version did not record by revision is
    # treated as another revision's, and the store's epoch (every write an earlier
    # version could have issued) is the upgrade itself.
    5: ["ALTER TABLE projections ADD COLUMN note_issued_seq INTEGER",
        "ALTER TABLE projections ADD COLUMN note_other_issued_at TEXT",
        "UPDATE projections SET note_other_issued_at = note_issued_at",
        # issue #15 (D3): a package remembers the request it was built for, so a build
        # whose request no longer owns it is never sent as a first send
        "ALTER TABLE packages ADD COLUMN request_id INTEGER",
        "UPDATE packages SET request_id = (SELECT max(r.request_id) FROM package_requests r"
        " WHERE r.package_id = packages.package_id)",
        # issue #15: a package request is worked in rounds (state `queued`, pass_id
        # nullable; SQLite cannot alter a CHECK, so the table is rebuilt). A request an
        # older version left buildable without its quarter's Gmail round and judging
        # goes back to `queued` with its token, lease and package cleared (design D1):
        # the new flow checks it before anything is built or sent.
        """CREATE TABLE package_requests_v6 (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  quarter TEXT NOT NULL, channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  pass_id TEXT,                  -- the pass of its latest round (NULL: queued, never run)
  pass_outcome TEXT, reason TEXT,
  package_id INTEGER, delivery_id INTEGER,
  token INTEGER, lease_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('queued', 'snapshot', 'snapshot-done', 'built',
        'staged', 'delivered', 'uncertain', 'failed', 'stopped', 'recovery-failed',
        'revoked', 'withdrawn', 'superseded')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  created_seq INTEGER NOT NULL DEFAULT 0,  -- a search counts for it only after this (#15)
  round INTEGER NOT NULL DEFAULT 0,        -- rounds of its check finished (#15)
  remaining INTEGER,                       -- what the last round left (#15)
  check_json TEXT,                         -- what the caption says about the check (#15)
  checked_snapshot INTEGER);               -- the import its finished check ran on (#15)""",
        "INSERT INTO package_requests_v6(request_id, quarter, channel, pass_id, pass_outcome,"
        " reason, package_id, delivery_id, token, lease_at, state, created_at, updated_at)"
        " SELECT request_id, quarter, channel, pass_id, pass_outcome, reason,"
        " CASE WHEN state IN ('snapshot-done', 'built') THEN NULL ELSE package_id END,"
        " delivery_id,"
        " CASE WHEN state IN ('snapshot-done', 'built') THEN NULL ELSE token END,"
        " CASE WHEN state IN ('snapshot-done', 'built') THEN NULL ELSE lease_at END,"
        " CASE WHEN state IN ('snapshot-done', 'built') THEN 'queued' ELSE state END,"
        " created_at, updated_at FROM package_requests",
        "DROP TABLE package_requests",
        "ALTER TABLE package_requests_v6 RENAME TO package_requests",
        "CREATE INDEX IF NOT EXISTS ix_package_requests_open ON package_requests(quarter, state)",
        "ALTER TABLE deliveries ADD COLUMN as_built INTEGER NOT NULL DEFAULT 0"],
    # issue #22: a document's date is marked when it was read on the document (a machine
    # pairing, a confirmation). Nothing earlier kept that mark, so every filed document
    # starts unread and the next package round's judge confirms its date.
    6: ["ALTER TABLE documents ADD COLUMN date_read_at TEXT"],
    # issue #24 (D5): the files the operator supplied, by their own ref, once filed. None
    # was kept before, so the next pass's filing tries each once more (ingest is
    # idempotent) and records it.
    7: ["""CREATE TABLE IF NOT EXISTS operator_refs (
  ref TEXT PRIMARY KEY, source TEXT NOT NULL, doc_id INTEGER NOT NULL, filed_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_operator_refs_filed ON operator_refs(filed_at)"],
    # 8 -> 9 (issue #35): a payment's exchange rate, from bank-feed 0.22.0's export. Rows
    # imported before it carry none until the next import.
    8: ["ALTER TABLE bank_rows ADD COLUMN fx_rate TEXT",
        "ALTER TABLE bank_rows ADD COLUMN fx_unit TEXT"],
}


def set_epoch(conn: sqlite3.Connection) -> None:
    """The store's epoch (issue #14): a store is created, reset or upgraded from a
    version that did not record its note writes by revision. Any write an earlier
    generation handed out may still land within a delegation's ceiling after it, so
    no read confirms a note until then (sweep.note_confirmed)."""
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('store_epoch_at', ?)",
                 (now(),))


def epoch(conn: sqlite3.Connection):
    row = conn.execute("SELECT value FROM meta WHERE key='store_epoch_at'").fetchone()
    return row[0] if row else None


def migrate(conn: sqlite3.Connection, bound_s: float = LOCK_BOUND_S) -> None:
    with tx(conn, bound_s=bound_s):
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"the accounting store is schema {current}, newer than this "
                               f"plugin's {SCHEMA_VERSION}; refusing to open it")
        if current == 0:
            for stmt in _statements(DDL):
                conn.execute(stmt)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
            set_epoch(conn)
            return
        for version in range(current, SCHEMA_VERSION):
            for stmt in MIGRATIONS[version]:
                conn.execute(stmt)
        if current < 6:
            set_epoch(conn)
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))


def _statements(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        joined = "\n".join(buf)
        if sqlite3.complete_statement(joined):
            if joined.strip():
                out.append(joined.strip())
            buf = []
    return out


def _retry_locked(stmt, bound_s: float):
    """Run a no-arg statement that may raise 'database is locked'/'busy' while
    another process holds the file (a WAL-mode conversion, or BEGIN IMMEDIATE),
    retrying with backoff until bound_s elapses. Past the bound: Busy, not a
    raw sqlite3.OperationalError — every open-time or write-time statement that
    can contend for the file's lock shares this one bounded retry (spec
    §Match records: "contention past the bound surfaces as an error")."""
    deadline = time.monotonic() + bound_s
    while True:
        try:
            stmt()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise Busy("the accounting store stayed locked by another session past "
                           f"{bound_s:g} s; this change was NOT applied — ask again") from exc
            time.sleep(0.05)


def open_store(path=None, bound_s: float = LOCK_BOUND_S) -> sqlite3.Connection:
    p = pathlib.Path(path) if path else data_dir() / DB_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # A fresh file's journal-mode conversion writes the file header, so it
        # contends with another process's simultaneous first open exactly like
        # BEGIN IMMEDIATE does; give it the same bounded retry.
        _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"), bound_s)
        migrate(conn, bound_s=bound_s)
    except BaseException:
        conn.close()
        raise
    return conn


def _rollback_if_open(conn) -> None:
    """ROLLBACK only while a transaction is still open: SQLite may already have
    rolled it back itself (an I/O error, a full disk), and a ROLLBACK then raises
    "no transaction is active", masking the error that caused it (fix wave F)."""
    if conn.in_transaction:
        conn.execute("ROLLBACK")


@contextlib.contextmanager
def tx(conn: sqlite3.Connection, bound_s: float = LOCK_BOUND_S):
    if conn.in_transaction:
        raise RuntimeError("tx() does not nest; the caller already holds the write lock")
    _retry_locked(lambda: conn.execute("BEGIN IMMEDIATE"), bound_s)
    try:
        yield conn
    except BaseException:
        _rollback_if_open(conn)
        raise
    # A COMMIT that fails leaves the transaction open on this connection; the tool
    # layer keeps ONE connection per process (tools._CONN), so every later call would
    # answer "tx() does not nest" until a restart. Roll it back, then re-raise.
    try:
        conn.execute("COMMIT")
    except BaseException:
        _rollback_if_open(conn)
        raise


def next_seq(conn: sqlite3.Connection) -> int:
    assert conn.in_transaction, "the sequence is allocated inside the write transaction"
    conn.execute("UPDATE counters SET value = value + 1 WHERE name='seq'")
    return conn.execute("SELECT value FROM counters WHERE name='seq'").fetchone()[0]


def last_delivered(conn: sqlite3.Connection):
    """THE most recent DELIVERED rendering (D2/D3: an operator's words bind to
    what they were shown last) — the one place it is resolved. Ordered by the
    store sequence mark_rendering_delivered allocates inside its transaction,
    so a later delivery always wins, even within one second (fix wave D)."""
    return conn.execute("SELECT * FROM renders WHERE delivered_at IS NOT NULL ORDER BY"
                        " delivered_seq DESC, delivered_at DESC, rowid DESC LIMIT 1").fetchone()


@contextlib.contextmanager
def custody_lock(bound_s: float = LOCK_BOUND_S):
    """The interprocess lock over the files this store holds custody of
    (documents/, packages/) together with the rows that claim them (fix wave B,
    Astra + Terra S1). An ingest holds it from installing the bytes through
    committing the index row; reset_store holds it across its row wipe AND the
    file erasure; reap_orphans across its scan. So no index row can name bytes a
    concurrent reset or reap removed, under any interleaving. An flock on a lock
    file in the data dir: the kernel releases it when its holder dies, so a crash
    never leaves it held. Lock order: this lock FIRST, then the SQLite write lock
    (tx) — never taken while a write transaction is open. Past the bound: Busy,
    and nothing was changed."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / CUSTODY_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + bound_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("another session is filing or erasing documents past "
                               f"{bound_s:g} s; this change was NOT applied — ask again")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)                    # closing the descriptor releases the lock
