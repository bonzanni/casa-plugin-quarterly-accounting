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
SCHEMA_VERSION = 1
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
  generation INTEGER NOT NULL, live INTEGER NOT NULL,
  pass_id TEXT, trigger TEXT, started_at TEXT);
CREATE TABLE IF NOT EXISTS passes (
  pass_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, trigger TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT,
  account_seen INTEGER NOT NULL DEFAULT 0,
  snapshot_id INTEGER,
  gate_json TEXT,                -- this pass's bank-write verdict, decided once (sticky refusal)
  report_json TEXT);
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
  ingested_at TEXT NOT NULL, ingest_quarter TEXT NOT NULL);

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
  review_reason TEXT, snapshot_id INTEGER NOT NULL);

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
  observed_tags_json TEXT, observed_at TEXT,
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
  membership_json TEXT NOT NULL);
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
  oversize INTEGER NOT NULL DEFAULT 0, caption TEXT NOT NULL, manifest_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT, package_id INTEGER, doc_id INTEGER,
  channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  staged_path TEXT NOT NULL, request_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('staged', 'delivered', 'uncertain', 'failed')),
  message_id TEXT, created_at TEXT NOT NULL, settled_at TEXT);
CREATE TABLE IF NOT EXISTS delivered_rows (
  package_id INTEGER NOT NULL, row_id INTEGER NOT NULL, pid INTEGER,
  facts_fp TEXT NOT NULL, kind TEXT, PRIMARY KEY (package_id, row_id));
CREATE TABLE IF NOT EXISTS alerts (
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  occurrence_key TEXT NOT NULL UNIQUE, detail TEXT NOT NULL, raised_at TEXT NOT NULL,
  render_id TEXT, sent_at TEXT);
"""

# Migrations from version N to N+1, appended when the schema changes. Each is
# a list of statements applied inside the migrating transaction.
MIGRATIONS: dict[int, list[str]] = {}


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
            return
        for version in range(current, SCHEMA_VERSION):
            for stmt in MIGRATIONS[version]:
                conn.execute(stmt)
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


@contextlib.contextmanager
def tx(conn: sqlite3.Connection, bound_s: float = LOCK_BOUND_S):
    if conn.in_transaction:
        raise RuntimeError("tx() does not nest; the caller already holds the write lock")
    _retry_locked(lambda: conn.execute("BEGIN IMMEDIATE"), bound_s)
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def next_seq(conn: sqlite3.Connection) -> int:
    assert conn.in_transaction, "the sequence is allocated inside the write transaction"
    conn.execute("UPDATE counters SET value = value + 1 WHERE name='seq'")
    return conn.execute("SELECT value FROM counters WHERE name='seq'").fetchone()[0]


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
