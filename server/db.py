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
import re
import sqlite3
import time

DB_NAME = "accounting.sqlite"
CUSTODY_LOCK = ".custody.lock"
SCHEMA_VERSION = 13
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


# The job protocol's own tables (spec §4, §5, §6), module constants so a fresh store's
# DDL and MIGRATIONS[9] create byte-identical tables.
CLAIMS_DDL = """CREATE TABLE IF NOT EXISTS claims (
  gen INTEGER PRIMARY KEY, job_id TEXT NOT NULL, at TEXT NOT NULL,
  batch INTEGER NOT NULL,        -- the batch this claim belongs to: its first claim's gen
  closed INTEGER NOT NULL DEFAULT 0,    -- this claim was answered complete
  seq INTEGER,                   -- the store sequence taken at the claim (S7 §10)
  progressed INTEGER NOT NULL DEFAULT 0,    -- the batch moved the work list on (simple loop §2.2)
  said INTEGER NOT NULL DEFAULT 0,          -- d3: this claim's progress was handed for reporting
  progressed_seq INTEGER,        -- e3: when note_progress last stamped it (progress.made)
  handed INTEGER NOT NULL DEFAULT 0,    -- e4: this claim handed out a work unit (progress)
  said_seq INTEGER);             -- Q2 run 1: the store sequence when it last reported progress"""

# Schema 10's credits (INV-J8), frozen for MIGRATIONS[9]; MIGRATIONS[11] drops it.
CREDITS_DDL = """CREATE TABLE IF NOT EXISTS credits (
  pass_id TEXT NOT NULL, key TEXT NOT NULL, gen INTEGER NOT NULL,
  PRIMARY KEY (pass_id, key));"""
CREDITS_GEN_DDL = "CREATE INDEX IF NOT EXISTS ix_credits_gen ON credits(gen);"
# Each Casa job run (simple loop §3 "Run").
RUNS_DDL = """CREATE TABLE IF NOT EXISTS runs (
  job_id TEXT PRIMARY KEY,
  completed_at TEXT,             -- the run answered `complete` (S7 §10)
  started_by TEXT, pass_id TEXT,            -- who started the run, its pass (simple loop §3 "Run")
  listed_at TEXT,                           -- the work list built (§3 "Run")
  mirror_at TEXT, mirrored_at TEXT,         -- mirror calls handed out / all settled (§2.4)
  end_render_id TEXT,                       -- the end message's rendering (§1); '' = none
  partial INTEGER NOT NULL DEFAULT 0,       -- the run ended partial (§3 "Run")
  quarter TEXT,                             -- the run's main quarter, when a check named it
  hand_unit TEXT, hand_seq INTEGER);        -- queues: the last unit handed (queues.settle), at this seq"""

WORK_REQUESTS_DDL = """CREATE TABLE IF NOT EXISTS work_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('check', 'handover')),
  trigger TEXT NOT NULL CHECK (trigger IN ('cron', 'operator')),
  doc_ids_json TEXT NOT NULL DEFAULT '[]',
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued', 'taken', 'done', 'reported')),
  pass_id TEXT, outcome TEXT,
  render_ids_json TEXT NOT NULL DEFAULT '[]',
  quarter TEXT);                 -- an operator check that names its quarter (ruling Q2)"""
# removed-name: schema history begin
# Schema 10's own work_requests, frozen: MIGRATIONS[9] creates it and MIGRATIONS[11] adds
# quarter, so a store migrated from 9 does not add the column twice.
WORK_REQUESTS_DDL_V10 = """CREATE TABLE IF NOT EXISTS work_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('check', 'handover')),
  trigger TEXT NOT NULL CHECK (trigger IN ('cron', 'operator')),
  doc_ids_json TEXT NOT NULL DEFAULT '[]',
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued', 'taken', 'done', 'reported')),
  pass_id TEXT, outcome TEXT,
  render_ids_json TEXT NOT NULL DEFAULT '[]',
  verdicts_json TEXT NOT NULL DEFAULT '{}');"""
# removed-name: schema history end

# Schema 10's own claims and runs tables, frozen: MIGRATIONS[9] creates these and
# MIGRATIONS[10] adds the S7 columns, so a store migrated from 9 does not add them twice.
CLAIMS_DDL_V10 = """CREATE TABLE IF NOT EXISTS claims (
  gen INTEGER PRIMARY KEY, job_id TEXT NOT NULL, at TEXT NOT NULL,
  spent INTEGER NOT NULL DEFAULT 0, reported INTEGER NOT NULL DEFAULT 0,
  batch INTEGER NOT NULL,        -- the batch this claim belongs to: its first claim's gen
  closed INTEGER NOT NULL DEFAULT 0);   -- this claim was answered end-batch or complete"""
RUNS_DDL_V10 = """CREATE TABLE IF NOT EXISTS runs (
  job_id TEXT PRIMARY KEY, passes INTEGER NOT NULL DEFAULT 0);"""

# S7 (spec §7.5, §8, §11, §5): what a tap may spend, a typed reading awaiting its tap, an
# account page's frozen choices, and how often this run handed out each rendering
READINGS_DDL = """CREATE TABLE IF NOT EXISTS readings (
  reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,      -- 128 random bits, minted into the proposal's deposit only
  text TEXT NOT NULL,            -- the words the desk turn received, verbatim
  quoted TEXT,                   -- the quoted post's text, when the desk context had one
  render_id TEXT,                -- the rendering the reading was bound to (§8)
  plan_json TEXT NOT NULL,       -- the writes, each with the revisions it read
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'applied', 'cancelled', 'stale')),
  settled_at TEXT);"""
RENDER_KEYS_DDL = """CREATE TABLE IF NOT EXISTS render_keys (
  key TEXT PRIMARY KEY, render_id TEXT NOT NULL,
  action TEXT NOT NULL,          -- all-good | right | wrong | no-invoice
  pid INTEGER,                   -- NULL for all-good
  created_at TEXT NOT NULL, spent_at TEXT,
  doc_id INTEGER);               -- a candidate button's document (simple loop §3)"""
# Schema 11's own render_keys, frozen: MIGRATIONS[10] creates this and MIGRATIONS[11] adds
# doc_id, so a store migrated from 10 does not add the column twice.
RENDER_KEYS_DDL_V11 = """CREATE TABLE IF NOT EXISTS render_keys (
  key TEXT PRIMARY KEY, render_id TEXT NOT NULL,
  action TEXT NOT NULL,          -- all-good | right | wrong | no-invoice
  pid INTEGER,                   -- NULL for all-good
  created_at TEXT NOT NULL, spent_at TEXT);"""
ACCOUNT_CHOICES_DDL = """CREATE TABLE IF NOT EXISTS account_choices (
  key TEXT NOT NULL, n INTEGER NOT NULL, account_id TEXT NOT NULL, label TEXT NOT NULL,
  created_at TEXT NOT NULL, spent_at TEXT, PRIMARY KEY (key, n));"""
POST_OFFERS_DDL = """CREATE TABLE IF NOT EXISTS post_offers (
  render_id TEXT NOT NULL, job_id TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (render_id, job_id));"""

# Simple loop (design rev 17 §3), schema 12.
# §3 "Run": the run's work list, each entry with its vendor group and outcome (§2.1, §2.2)
RUN_WORK_DDL = """CREATE TABLE IF NOT EXISTS run_work (
  job_id TEXT NOT NULL, pid INTEGER NOT NULL,
  vendor TEXT NOT NULL,          -- loop.vendor_of: the group it is handed out in (D1)
  why TEXT NOT NULL CHECK (why IN ('open', 'new', 'changed', 'handover')),
  outcome TEXT CHECK (outcome IN ('match', 'propose', 'missing', 'keep', 'replace',
                                  'settled')),   -- settled: no longer work at hand-out;
                                                 -- keep / replace: a handover (rev 18.4 §R18.3)
  reason TEXT,                   -- a `missing` outcome's reason, as the model gave it (§2.2)
  attempts INTEGER NOT NULL DEFAULT 0,   -- queues: hand-outs that carried it and progressed nothing
  searches INTEGER NOT NULL DEFAULT 0,   -- rev 18.4: its searches recorded this run (SEARCHES_MAX)
  hand_seq INTEGER,              -- the hand-out (runs.hand_seq) that last carried it
  seq INTEGER,                   -- queues: when it was (re)listed
  closed_seq INTEGER,            -- queues: when it took its outcome
  searched_seq INTEGER,          -- the latest search recorded for it (a hand-out's progress)
  PRIMARY KEY (job_id, pid));"""
# §2.4: the mirror calls a run handed out, numbered, and what became of them (D9);
# args_json holds the call's canonical [tool, args]. Nothing is planned ahead (round 7)
RUN_MIRROR_DDL = """CREATE TABLE IF NOT EXISTS run_mirror (
  job_id TEXT NOT NULL, n INTEGER NOT NULL,
  tool TEXT NOT NULL CHECK (tool IN ('untag_transaction', 'tag_transaction', 'add_note')),
  args_json TEXT NOT NULL, pids_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('handed', 'done', 'failed')),
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, hand_seq INTEGER, closed_seq INTEGER,   -- queues
  PRIMARY KEY (job_id, n));"""
# Rev 18.4 §R18.3: a handed-over document the job judged belongs to a payment that already
# has one — the question its one card asks, bound to the pairing it displayed
REPLACE_QUESTIONS_DDL = """CREATE TABLE IF NOT EXISTS replace_questions (
  question_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL, pid INTEGER NOT NULL,
  match_id INTEGER NOT NULL,     -- the pairing the payment held when asked (match or proposal)
  new_doc_id INTEGER NOT NULL,   -- the handed-over document
  state TEXT NOT NULL CHECK (state IN ('open', 'kept', 'used', 'superseded')),
  created_seq INTEGER NOT NULL, answered_at TEXT);"""
# Queues (operator ruling A): every other item a unit owes, from the moment it is known —
# an erase candidate, the own-mail search, a found attachment (docs queues-design.md)
RUN_ITEMS_DDL = """CREATE TABLE IF NOT EXISTS run_items (
  job_id TEXT NOT NULL,
  unit TEXT NOT NULL,            -- erasures | filing | vendor:<kb.norm vendor>
  kind TEXT NOT NULL CHECK (kind IN ('erase', 'search', 'ref')),
  key TEXT NOT NULL,             -- the pid, 'own-mail', <message id>:<attachment id>
  state TEXT NOT NULL CHECK (state IN ('queued', 'done', 'given_up')),
  attempts INTEGER NOT NULL DEFAULT 0, hand_seq INTEGER,
  seq INTEGER NOT NULL, closed_seq INTEGER, reason TEXT,
  PRIMARY KEY (job_id, unit, kind, key));"""
# §1 "new state" (rounds 1-2): every state a rendering REPORTS — a payment it displays,
# or one it only counts (an end message's "4 missing") — read by cards.seen_state. Binding
# stays in render_items, which holds only the payments whose lines the text displays
RENDER_STATES_DDL = """CREATE TABLE IF NOT EXISTS render_states (
  render_id TEXT NOT NULL, pid INTEGER NOT NULL, item_state TEXT NOT NULL,
  PRIMARY KEY (render_id, pid));"""
# §3 "Per quarter": the completion a "package ready" notice was delivered for (§1) — by
# its signature, so a reopening and a re-completion within one run is still a new one
QUARTER_NOTICES_DDL = """CREATE TABLE IF NOT EXISTS quarter_notices (
  quarter TEXT PRIMARY KEY,
  sig TEXT,                      -- loop.completion_sig of the completion last delivered
  times INTEGER NOT NULL DEFAULT 0, render_id TEXT);"""

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
  lease_at TEXT);                -- when the token's holder last made progress
CREATE TABLE IF NOT EXISTS passes (
  pass_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, trigger TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT,
  account_seen INTEGER NOT NULL DEFAULT 0,
  snapshot_id INTEGER,
  gate_json TEXT,                -- this pass's bank-write verdict, decided once (sticky refusal)
  report_json TEXT,
  reply TEXT NOT NULL DEFAULT 'telegram',   -- where a continuation reports: telegram | silent
  protocol TEXT NOT NULL DEFAULT 'delegation',   -- 'delegation' (pre-S2) | 'job' (spec §3)
  holder_job TEXT,               -- the job run id that holds this pass (job protocol)
  acq INTEGER,                    -- the bank-feed acquisition id this pass last imported under
  acq_gen INTEGER);               -- the claim that handed that acquisition out
CREATE TABLE IF NOT EXISTS probes (
  kind TEXT PRIMARY KEY, ok INTEGER NOT NULL, detail TEXT, data_json TEXT,
  observed_at TEXT NOT NULL, pass_id TEXT, failing_since TEXT,
  gen INTEGER,                   -- the pass generation this probe was recorded under (job protocol)
  fail_runs INTEGER NOT NULL DEFAULT 0);   -- consecutive runs the Gmail probe failed (D10)

CREATE TABLE IF NOT EXISTS documents (
  doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT NOT NULL UNIQUE, ext TEXT NOT NULL, size INTEGER NOT NULL,
  kind TEXT NOT NULL, counterparty TEXT, issuer TEXT, document_date TEXT,
  document_number TEXT, amount_minor INTEGER, currency TEXT, recipient TEXT,
  source TEXT NOT NULL, source_ref TEXT, acquisition_json TEXT,
  extraction_author TEXT NOT NULL, original_name TEXT,
  irrelevant INTEGER NOT NULL DEFAULT 0,
  ingested_at TEXT NOT NULL, ingest_quarter TEXT NOT NULL,
  date_read_at TEXT,             -- when document_date was last read on the document (#22)
  vendor TEXT,                   -- the vendor group that filed it (simple loop §2.2)
  filed_seq INTEGER,             -- store sequence at ingest: "newly filed" (§2.1)
  amount_conflict INTEGER NOT NULL DEFAULT 0);  -- Q2 run 1: two readings disagreed (sticky)

CREATE TABLE IF NOT EXISTS counterparties (
  cp_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  patterns_json TEXT NOT NULL DEFAULT '[]',
  exp_kind TEXT, exp_tier TEXT, exp_author TEXT,
  source TEXT CHECK (source IN ('email', 'portal')),
  document_link TEXT, link_note TEXT, search_hint TEXT, notes TEXT,
  window_days INTEGER NOT NULL DEFAULT 10, updated_at TEXT NOT NULL,
  hint_sender TEXT, hint_subject TEXT);   -- the learned search hint (D6)
CREATE TABLE IF NOT EXISTS chain_overrides (
  scope TEXT PRIMARY KEY, kind TEXT NOT NULL, tier TEXT,
  rows_json TEXT NOT NULL, key_json TEXT NOT NULL,   -- expectation.normalize_scope, fixed at set time
  author TEXT NOT NULL, set_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, pass_id TEXT,
  imported_at TEXT NOT NULL, rows INTEGER NOT NULL, max_row_id INTEGER NOT NULL,
  bank_through TEXT,             -- the date bank data is known good through (sync ok this pass)
  job_id TEXT,                   -- the job run id this import belongs to (job protocol)
  acq INTEGER,                   -- the bank-feed acquisition id this import ran under
  export_ref TEXT);              -- the export's own ref (idempotency: at most one import per ref)
CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots_export_ref ON snapshots(export_ref)
  WHERE export_ref IS NOT NULL;
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
  class_observed_snapshot INTEGER,  -- the snapshot_id it was last observed at: an import (E2, #1)
  observed_tags_json TEXT, observed_at TEXT,
  export_tag_revision INTEGER,   -- the row's tag_revision in the latest import (issue #1)
  mirror_note TEXT,              -- the note text last written (simple loop §3 "Per projection")
  last_facts_json TEXT,          -- the destination row's facts when last seen (names an erased row)
  unprojectable TEXT, last_error TEXT,
  search_state TEXT NOT NULL DEFAULT 'active'
    CHECK (search_state IN ('active', 'aged-out', 'accepted-missing')),
  search_json TEXT NOT NULL DEFAULT '{}',
  passes_without_candidate INTEGER NOT NULL DEFAULT 0,
  identity_question INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS aliases (
  row_id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, first_seen TEXT);
CREATE INDEX IF NOT EXISTS ix_aliases_pid ON aliases(pid);

CREATE TABLE IF NOT EXISTS matches (
  match_id INTEGER PRIMARY KEY AUTOINCREMENT,
  pid_created INTEGER NOT NULL, doc_id INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT 'clean', rationale TEXT,
  runners_up_json TEXT NOT NULL DEFAULT '[]', created_seq INTEGER NOT NULL,
  alternatives_json TEXT NOT NULL DEFAULT '[]');   -- the other candidates considered (D3)
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
  delivered_seq INTEGER,         -- store sequence at delivery: what "most recent delivered" orders by
  binding INTEGER,               -- job_report's latest hand-out: 1 notification, 0 operator turn (R5)
  posted_seq INTEGER);           -- S7 r3 #3: show_view deposited it (attempted; monotone, never cleared)
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
  snapshot_id INTEGER);          -- the import the build froze (fix E4: its first send checks it)
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT, package_id INTEGER, doc_id INTEGER,
  channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  staged_path TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('staged', 'delivered', 'uncertain', 'failed')),
  message_id TEXT, created_at TEXT NOT NULL, settled_at TEXT,
  revoked_at TEXT,               -- an unsent first send an import superseded (fix E5)
  withdrawn_at TEXT,             -- staged bytes taken back when a stalled send was recovered
  lease_at TEXT,                 -- a staged send's lease: past LEASE_S it is recovered
  as_built INTEGER NOT NULL DEFAULT 0,   -- "send me the last package you built" (#15)
  posted_at TEXT);               -- S7 §6.1: post_package deposited it (at most once)
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
  -- issue #24 (D5), d5: each filed file by its own ref (an own-mail attachment, a Telegram
  -- file, a vendor's message), so a search's refs are answered unfiled or not (work.filed)
  ref TEXT PRIMARY KEY, source TEXT NOT NULL, doc_id INTEGER NOT NULL, filed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_operator_refs_filed ON operator_refs(filed_at);
""" + "\n".join((CLAIMS_DDL, WORK_REQUESTS_DDL, RUNS_DDL,
                         READINGS_DDL, RENDER_KEYS_DDL, ACCOUNT_CHOICES_DDL, POST_OFFERS_DDL,
                         RUN_WORK_DDL, RUN_MIRROR_DDL, RUN_ITEMS_DDL, REPLACE_QUESTIONS_DDL, QUARTER_NOTICES_DDL,
                         RENDER_STATES_DDL)) + "\n"

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
        "ALTER TABLE pass_steps ADD COLUMN protocol TEXT NOT NULL DEFAULT 'delegation'",
        "ALTER TABLE pass_steps ADD COLUMN started_seq INTEGER",
        "ALTER TABLE pass_steps ADD COLUMN started_gen INTEGER",
        "ALTER TABLE passes ADD COLUMN w_pending INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE passes ADD COLUMN judge_epoch INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE passes ADD COLUMN late_takes INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE renders ADD COLUMN binding INTEGER",
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
        CLAIMS_DDL_V10, WORK_REQUESTS_DDL_V10, CREDITS_DDL, CREDITS_GEN_DDL, RUNS_DDL_V10],
    # 10 -> 11 (S7): tap keys, readings, account choices, post offers; the run's stamps;
    # the request's latest ask; a send's and a view's post marks. Data steps follow in migrate (after_10_to_11)
    10: ["ALTER TABLE claims ADD COLUMN seq INTEGER",
         "ALTER TABLE runs ADD COLUMN completed_at TEXT",
         "ALTER TABLE package_requests ADD COLUMN asked_seq INTEGER NOT NULL DEFAULT 0",
         "UPDATE package_requests SET asked_seq = created_seq",
         "UPDATE package_requests SET channel='telegram' WHERE channel='email' AND state IN"
         " ('queued', 'snapshot', 'snapshot-done', 'built')",
         "DELETE FROM meta WHERE key='drain' OR key LIKE 'cancelled:%'",
         "ALTER TABLE deliveries ADD COLUMN posted_at TEXT",
         # R5 + the d3 amendment: a v0.9.0 telegram package send, staged or recovered
         # uncertain, may already have gone out (send_media) — its late receipt is accepted
         # (an email send is settled uncertain at the upgrade instead)
         "UPDATE deliveries SET posted_at = created_at WHERE status IN ('staged', 'uncertain')"
         " AND channel = 'telegram' AND package_id IS NOT NULL AND posted_at IS NULL",
         "ALTER TABLE renders ADD COLUMN posted_seq INTEGER",
         READINGS_DDL, RENDER_KEYS_DDL_V11, ACCOUNT_CHOICES_DDL, POST_OFFERS_DDL],
    # 11 -> 12 (simple loop, design rev 17 §3): the run's work list, mirror calls and end
    # message; a quarter's ready notice; mirror_note; the filing vendor; alternatives;
    # keyed documents; item states; the Gmail streak; batch progress; the learned hint.
    # Task 11 of the plan appends the drops of the deleted machinery to this same list.
    # 12 → 13 (Q2 run 1, round f1 Astra S1: schema 12 stores exist live since run 1)
    12: ["ALTER TABLE claims ADD COLUMN said_seq INTEGER",
         "ALTER TABLE documents ADD COLUMN amount_conflict INTEGER NOT NULL DEFAULT 0"],
    11: ["ALTER TABLE projections ADD COLUMN mirror_note TEXT",
         "ALTER TABLE documents ADD COLUMN vendor TEXT",
         "ALTER TABLE documents ADD COLUMN filed_seq INTEGER",
         "ALTER TABLE matches ADD COLUMN alternatives_json TEXT NOT NULL DEFAULT '[]'",
         "ALTER TABLE render_keys ADD COLUMN doc_id INTEGER",
         "ALTER TABLE probes ADD COLUMN fail_runs INTEGER NOT NULL DEFAULT 0",
         "ALTER TABLE claims ADD COLUMN progressed INTEGER NOT NULL DEFAULT 0",
         "ALTER TABLE claims ADD COLUMN said INTEGER NOT NULL DEFAULT 0",
         "ALTER TABLE claims ADD COLUMN progressed_seq INTEGER",
         "ALTER TABLE claims ADD COLUMN handed INTEGER NOT NULL DEFAULT 0",
         "ALTER TABLE counterparties ADD COLUMN hint_sender TEXT",
         "ALTER TABLE counterparties ADD COLUMN hint_subject TEXT",
         "ALTER TABLE runs ADD COLUMN started_by TEXT",
         "ALTER TABLE runs ADD COLUMN pass_id TEXT",
         "ALTER TABLE runs ADD COLUMN listed_at TEXT",
         "ALTER TABLE runs ADD COLUMN mirror_at TEXT",
         "ALTER TABLE runs ADD COLUMN mirrored_at TEXT",
         "ALTER TABLE runs ADD COLUMN end_render_id TEXT",
         "ALTER TABLE runs ADD COLUMN partial INTEGER NOT NULL DEFAULT 0",
         "ALTER TABLE runs ADD COLUMN quarter TEXT",
         "ALTER TABLE runs ADD COLUMN hand_unit TEXT",
         "ALTER TABLE runs ADD COLUMN hand_seq INTEGER",
         "ALTER TABLE work_requests ADD COLUMN quarter TEXT",
         RUN_WORK_DDL, RUN_MIRROR_DDL, RUN_ITEMS_DDL, REPLACE_QUESTIONS_DDL, QUARTER_NOTICES_DDL, RENDER_STATES_DDL,
         # ... then the machinery §4 deletes: the sweep, the chunk carry, the judge,
         # credits, nested passes, package requests and the delegation protocol
         # a package asked for and not yet sent is told, once, before its request goes
         "INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
         " SELECT 'package-not-sent', 'request:' || request_id || ':dropped',"
         " json_object('package_id', NULL, 'pass_id', '', 'quarter', quarter,"
         " 'reason', 'it was asked for before the update'),"
         " strftime('%Y-%m-%dT%H:%M:%SZ', 'now') FROM package_requests"
         " WHERE state IN ('queued', 'snapshot', 'snapshot-done', 'built')"
         " ORDER BY request_id",
         "DROP TABLE IF EXISTS credits", "DROP TABLE IF EXISTS cursor",
         "DROP TABLE IF EXISTS pass_steps", "DROP TABLE IF EXISTS package_requests",
         *(f"ALTER TABLE passes DROP COLUMN {c}" for c in (
             "orphaned_by", "adoptions", "adopters_json", "read_seq", "w_refreshes",
             "judge_after", "w_pending", "judge_epoch", "late_takes")),
         *(f"ALTER TABLE snapshots DROP COLUMN {c}" for c in ("read_seq", "swept_at")),
         *(f"ALTER TABLE projections DROP COLUMN {c}" for c in (
             "observed_revision", "note_seen_seq", "note_seen_rev", "note_seen_at",
             "note_issued_at", "note_issued_seq", "note_other_issued_at", "readback_owed",
             "note_issued_gen", "note_other_issued_gen", "note_seen_gen", "read_snapshot",
             "note_seq", "note_body")),
         *(f"ALTER TABLE claims DROP COLUMN {c}" for c in ("spent", "reported")),
         "ALTER TABLE runs DROP COLUMN passes",
         "ALTER TABLE pass_marker DROP COLUMN claimed_step",
         "ALTER TABLE work_requests DROP COLUMN verdicts_json",
         "ALTER TABLE packages DROP COLUMN request_id",
         "ALTER TABLE deliveries DROP COLUMN request_id",
         "DELETE FROM meta WHERE key='store_epoch_at' OR key LIKE 'left:%'",
         # a package request's own notices (stopped / the bank unread) lose their wording
         # with their raisers: one still unsaid is said as the request's not-sent notice
         "UPDATE alerts SET kind='package-not-sent' WHERE sent_at IS NULL AND kind IN"
         " ('package-stopped', 'package-failed')",
         # e1 (Astra S1): a pass live at the upgrade is the old machinery's — it ends
         # interrupted, and the requests it had taken (a handover among them) are queued
         # again for the first run of the new loop, which takes them (asks.requeue_taken)
         "UPDATE passes SET ended_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
         " outcome='interrupted' WHERE ended_at IS NULL",
         "UPDATE work_requests SET state='queued', pass_id=NULL WHERE state='taken'",
         "UPDATE pass_marker SET live=0, lease_at=NULL"],
}


def _close_delegation_pass(conn: sqlite3.Connection) -> None:
    """The 9 -> 10 data step (spec §8): a live delegation-protocol pass is ended
    `interrupted` and the marker goes dead, so every old token is refused by check_token.
    (Its package request goes with the 11 -> 12 drops; a 9 -> 10 store has no work request
    yet.) The store epoch it also set went with the sweep (simple loop §4)."""
    m = conn.execute("SELECT live, pass_id FROM pass_marker WHERE id=1").fetchone()
    if m is None or not m[0]:
        return
    conn.execute("UPDATE passes SET ended_at=?, outcome='interrupted' WHERE pass_id=? AND"
                 " ended_at IS NULL", (now(), m[1]))
    conn.execute("UPDATE pass_marker SET live=0, lease_at=NULL WHERE id=1")


def _settle_staged_email_on_upgrade(conn) -> None:
    """S7 §14: no email after S7 (G3). A send staged for email at the upgrade can no longer
    be sent by anyone (finance's Gmail is read-only): it is settled `uncertain`, withdrawn,
    with its package notice — "send it again" then sends it here, as a file. Its handoff
    copy is left to the handoff folder's own retention (the custody lock is not taken
    inside the migration's transaction)."""
    import alerts
    ts = now()
    for d in conn.execute("SELECT d.*, p.quarter FROM deliveries d JOIN packages p ON"
                          " p.package_id=d.package_id WHERE d.status='staged' AND"
                          " d.channel='email'").fetchall():
        conn.execute("UPDATE deliveries SET status='uncertain', settled_at=?, withdrawn_at=?"
                     " WHERE delivery_id=?", (ts, ts, d["delivery_id"]))
        conn.execute("UPDATE package_requests SET state='uncertain', updated_at=? WHERE"  # removed-name: schema history
                     " delivery_id=? AND state='staged'", (ts, d["delivery_id"]))
        alerts.raise_package(conn, "package-uncertain", f"delivery:{d['delivery_id']}:uncertain",
                             quarter=d["quarter"], package_id=d["package_id"], pass_id="")


# A version's data step runs right after MIGRATIONS[version], inside the loop, so a later
# version's drop can never break an earlier step (MIGRATIONS[11] drops a table the step reads).
# The 4 -> 5 store epoch (issue #14) went with the sweep that read it (simple loop §4): no
# step at 5, and 9 -> 10 only closes a live delegation pass.
def _backfill_render_states(conn) -> None:
    """The 11 -> 12 data step (d3, Astra S2): schema 11 kept what a rendering showed only in
    render_items. A delivered one's item whose projection still has the revision it was
    shown at is provably unchanged since: its current item state is recorded as shown
    (render_states), so the first scheduled run after the upgrade does not announce it
    again. A payment that changed since, or no longer missing or proposed, is not."""
    import cards
    import work
    for r in conn.execute(
            "SELECT i.render_id, i.pid FROM render_items i JOIN renders r ON"
            " r.render_id=i.render_id JOIN projections p ON p.pid=i.pid WHERE"
            " r.delivered_at IS NOT NULL AND p.merged_into IS NULL AND"
            " p.revision=i.projection_revision ORDER BY i.render_id, i.pid").fetchall():
        d = work.describe(conn, r[1])
        if d["status"] in ("open", "proposed"):
            conn.execute("INSERT OR IGNORE INTO render_states(render_id, pid, item_state)"
                         " VALUES (?,?,?)", (r[0], r[1], cards.item_state(d)))


SCHEMA_DATA_STEPS = {9: _close_delegation_pass, 10: _settle_staged_email_on_upgrade,
                     11: _backfill_render_states}


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
            step = SCHEMA_DATA_STEPS.get(version)
            if step is not None:
                step(conn)
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


# Renderings that offer nothing to answer (S2 §6.4): a handover's case lines, a stop line
# and the package file's caption (S7 §6.1); an upgraded store's package notes and
# asks-waiting lines (no longer made since schema 12) stay classed as they were. Delivered after a view or an offer, they never take the operator's
# reply from it (diff round 1, R3; Astra S2: a handover page delivered after `speak`'s
# resend offer made "send it again" refuse).
INFORMATIONAL_KINDS = ("handover", "job-stop", "package-file",
                       "package-note", "job-left")     # removed-name: schema history (legacy kinds, before 12)
# Of those, the ones a QUOTE can never bind (views.bound_rendering). The package file and
# a legacy package note are quotable (#44): a swipe-reply "send it again" on either names
# its package (its scope's `offers`), answered by delivery.resend_target.
UNQUOTABLE_KINDS = ("handover", "job-stop", "job-left")    # removed-name: schema history (legacy, before 12)


def seen_render(row) -> bool:
    """§1 of the binding design: the ONE "seen" predicate — a rendering marked delivered, or
    one show_view posted (posted_seq: a deposit was attempted). It makes a rendering a quote
    candidate (views.bound_rendering) and an operator rule's provenance (kb); it is never
    continuation evidence (V1: a continued page merges only its explicit predecessor)."""
    return row is not None and (row["delivered_at"] is not None
                                or row["posted_seq"] is not None)


def last_delivered(conn: sqlite3.Connection):
    """THE most recent DELIVERED rendering an operator's reply is about (D2/D3: their
    words bind to what they were shown last) — the one place it is resolved. Ordered by
    the store sequence mark_rendering_delivered allocates inside its transaction, so a
    later delivery always wins, even within one second (fix wave D). Informational
    renderings (INFORMATIONAL_KINDS) are skipped: they offer nothing to answer, so the
    reply is still about what came before them.

    S7 §9: it binds a reading's words that carry no quote (views.bound_rendering), and
    "send it again" to the offer last seen."""
    return conn.execute("SELECT * FROM renders WHERE delivered_at IS NOT NULL AND kind NOT IN"
                        " (%s) ORDER BY delivered_seq DESC, delivered_at DESC, rowid DESC"
                        " LIMIT 1" % ",".join("?" * len(INFORMATIONAL_KINDS)),
                        INFORMATIONAL_KINDS).fetchone()


@contextlib.contextmanager
def savepoint(conn: sqlite3.Connection, name: str):
    """A nested unit inside the caller's write transaction (S7 §8: one reading's clauses run
    in ONE transaction, each clause in a savepoint of its own). Any exception rolls the
    savepoint back and re-raises; the outer transaction stays open."""
    assert conn.in_transaction, "a savepoint lives inside the write transaction"
    if not re.fullmatch(r"[a-z_]{1,32}", name):
        raise ValueError(name)
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")


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
