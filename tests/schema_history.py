"""The accounting store's DDL as each earlier schema version shipped it, frozen
verbatim so the migration tests open a store exactly as a released plugin left
it (not one derived from today's DDL, which moves with every migration).

- DDL_V1: schema 1, server/db.py at 6509806 (the first release).
- DDL_V2: schema 2, server/db.py at b055022 (fix wave D: renders.delivered_seq).
- DDL_V3: schema 3, server/db.py at e9b4eff (v0.1.0: classification freshness,
  package snapshots, revoked deliveries).
- DDL_V4: schema 4, server/db.py at e79f77d (v0.2.0: pass steps, package requests,
  unique staged paths).
- DDL_V5: schema 5, server/db.py at 7cd8eda (v0.3.5; issue #1: the import observes
  tags, note confirmation).
- DDL_V6: schema 6, server/db.py at 02c02c2 (v0.5.0; issues #14/#15: note issue
  sequence, package requests in rounds).
- DDL_V7: schema 7, server/db.py at 847cee7 (v0.6.0; issue #22: documents.date_read_at).
- DDL_V8: schema 8, server/db.py at 7406c98 (v0.7.0; issue #24: operator_refs).
- DDL_V9: schema 9, server/db.py at 86656ac (v0.8.0; issue #35: bank_rows.fx_rate/fx_unit).
  Used by build_v9_store (Task 1, S2) to open a schema-9 store exactly as released,
  including an optional live delegation-protocol pass, for the 9 -> 10 migration test.
- DDL_V10: schema 10, server/db.py at 2d8a802 (v0.9.0; S2: the job protocol).
- DDL_V11: schema 11, server/db.py at 26b68ee (S7, v0.10.0 never tagged).
"""

DDL_V1 = """
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

DDL_V2 = """
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

DDL_V3 = """
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
  class_observed_snapshot INTEGER,  -- the latest snapshot_id when the sweep last read it (fix E2)
  observed_revision INTEGER,     -- the projection's revision that read left it at (fix E2)
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
  snapshot_id INTEGER);          -- the import the build froze (fix E4: its first send checks it)
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT, package_id INTEGER, doc_id INTEGER,
  channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  staged_path TEXT NOT NULL, request_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('staged', 'delivered', 'uncertain', 'failed')),
  message_id TEXT, created_at TEXT NOT NULL, settled_at TEXT,
  revoked_at TEXT);              -- an unsent first send an import superseded (fix E5)
CREATE TABLE IF NOT EXISTS delivered_rows (
  package_id INTEGER NOT NULL, row_id INTEGER NOT NULL, pid INTEGER,
  facts_fp TEXT NOT NULL, kind TEXT, PRIMARY KEY (package_id, row_id));
CREATE TABLE IF NOT EXISTS alerts (
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  occurrence_key TEXT NOT NULL UNIQUE, detail TEXT NOT NULL, raised_at TEXT NOT NULL,
  render_id TEXT, sent_at TEXT);
"""

DDL_V4 = """
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
  pass_id TEXT NOT NULL, pass_outcome TEXT, reason TEXT,
  package_id INTEGER, delivery_id INTEGER,
  token INTEGER, lease_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('snapshot', 'snapshot-done', 'built', 'staged',
        'delivered', 'uncertain', 'failed', 'stopped', 'recovery-failed', 'revoked',
        'withdrawn', 'superseded')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
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
  class_observed_snapshot INTEGER,  -- the latest snapshot_id when the sweep last read it (fix E2)
  observed_revision INTEGER,     -- the projection's revision that read left it at (fix E2)
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
  snapshot_id INTEGER);          -- the import the build froze (fix E4: its first send checks it)
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT, package_id INTEGER, doc_id INTEGER,
  channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  staged_path TEXT NOT NULL, request_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('staged', 'delivered', 'uncertain', 'failed')),
  message_id TEXT, created_at TEXT NOT NULL, settled_at TEXT,
  revoked_at TEXT,               -- an unsent first send an import superseded (fix E5)
  withdrawn_at TEXT,             -- staged bytes taken back when a stalled send was recovered
  lease_at TEXT);                -- a staged send's lease: past LEASE_S it is recovered
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
"""

DDL_V5 = """
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
  pass_id TEXT NOT NULL, pass_outcome TEXT, reason TEXT,
  package_id INTEGER, delivery_id INTEGER,
  token INTEGER, lease_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('snapshot', 'snapshot-done', 'built', 'staged',
        'delivered', 'uncertain', 'failed', 'stopped', 'recovery-failed', 'revoked',
        'withdrawn', 'superseded')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
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
  class_observed_snapshot INTEGER,  -- the snapshot_id it was last observed at: import or read (E2, #1)
  observed_revision INTEGER,     -- the projection's revision that read left it at (fix E2)
  observed_tags_json TEXT, observed_at TEXT,
  export_tag_revision INTEGER,   -- the row's tag_revision in the latest import (issue #1)
  note_seen_seq INTEGER,         -- the note_seq a read last saw visible (issue #1)
  note_seen_rev INTEGER,         -- that read's `Tag revision:` (issue #1)
  note_seen_at TEXT,             -- the import time of the snapshot that read belongs to
  note_issued_at TEXT,           -- when an add_note was last returned to the specialist
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
  snapshot_id INTEGER);          -- the import the build froze (fix E4: its first send checks it)
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT, package_id INTEGER, doc_id INTEGER,
  channel TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
  staged_path TEXT NOT NULL, request_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('staged', 'delivered', 'uncertain', 'failed')),
  message_id TEXT, created_at TEXT NOT NULL, settled_at TEXT,
  revoked_at TEXT,               -- an unsent first send an import superseded (fix E5)
  withdrawn_at TEXT,             -- staged bytes taken back when a stalled send was recovered
  lease_at TEXT);                -- a staged send's lease: past LEASE_S it is recovered
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
"""

DDL_V6 = """
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
"""

DDL_V7 = """
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
"""

DDL_V8 = """
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

DDL_V9 = """
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


DDL_V10 = """
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
  reply TEXT NOT NULL DEFAULT 'telegram',   -- where a continuation reports: telegram | silent
  protocol TEXT NOT NULL DEFAULT 'delegation',   -- 'delegation' (pre-S2) | 'job' (spec §3)
  holder_job TEXT,               -- the job run id that holds this pass (job protocol)
  orphaned_by TEXT,               -- the job run id that adopted this pass's work, if orphaned
  adoptions INTEGER NOT NULL DEFAULT 0,          -- adoptions spent (at most 2 per pass, §6.3)
  adopters_json TEXT NOT NULL DEFAULT '[]',      -- job run ids that adopted this pass's work
  acq INTEGER,                    -- the bank-feed acquisition id this pass last imported under
  acq_gen INTEGER,                -- that acquisition's restore generation
  read_seq INTEGER,               -- the store sequence this pass's read-back is owed from
  w_refreshes INTEGER NOT NULL DEFAULT 0,        -- W-refreshes spent (at most 2 per pass, §5.2)
  judge_after TEXT,               -- the running judgment's page start: a validated [pid]
  w_pending INTEGER NOT NULL DEFAULT 0,
  judge_epoch INTEGER NOT NULL DEFAULT 0,       -- judgments started for a bounded cause (INV-J8)
  late_takes INTEGER NOT NULL DEFAULT 0);       -- requests taken while live (LATE_TAKES_MAX)
CREATE TABLE IF NOT EXISTS pass_steps (
  pass_id TEXT NOT NULL,
  step TEXT NOT NULL CHECK (step IN ('sweep', 'judge', 'handover', 'snapshot')),
  started_at TEXT NOT NULL,      -- written by Ellen before she delegates
  finished_at TEXT,
  finished_by TEXT CHECK (finished_by IN ('specialist', 'resident')),
  finish_json TEXT,              -- counts, stopped (clipped), failed
  carry_json TEXT NOT NULL DEFAULT '{}',   -- doc_ids / report: ids and counts only
  protocol TEXT NOT NULL DEFAULT 'delegation',
  started_seq INTEGER,            -- the store sequence this step started at (job protocol)
  started_gen INTEGER,            -- the pass generation this step started under (job protocol)
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
  observed_at TEXT NOT NULL, pass_id TEXT, failing_since TEXT,
  gen INTEGER);                  -- the pass generation this probe was recorded under (job protocol)

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
  bank_through TEXT,             -- the date bank data is known good through (sync ok this pass)
  job_id TEXT,                   -- the job run id this import belongs to (job protocol)
  read_seq INTEGER,              -- the store sequence this import's read-back is owed from
  acq INTEGER,                   -- the bank-feed acquisition id this import ran under
  export_ref TEXT,               -- the export's own ref (idempotency: at most one import per ref)
  swept_at TEXT);                -- when the sweep this import belongs to completed (§5.2's W)
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
  readback_owed INTEGER NOT NULL DEFAULT 0,  -- a read confirms a note only after it (job protocol)
  note_issued_gen INTEGER,       -- the pass generation add_note was last issued under
  note_other_issued_gen INTEGER, -- the pass generation any OTHER note_seq was last issued under
  note_seen_gen INTEGER,         -- the pass generation a read last saw the note under
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
  delivered_seq INTEGER,         -- store sequence at delivery: what "most recent delivered" orders by
  binding INTEGER);              -- job_report's latest hand-out: 1 notification, 0 operator turn (R5)
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
CREATE TABLE IF NOT EXISTS claims (
  gen INTEGER PRIMARY KEY, job_id TEXT NOT NULL, at TEXT NOT NULL,
  spent INTEGER NOT NULL DEFAULT 0, reported INTEGER NOT NULL DEFAULT 0,
  batch INTEGER NOT NULL,        -- the batch this claim belongs to: its first claim's gen
  closed INTEGER NOT NULL DEFAULT 0);   -- this claim was answered end-batch or complete
CREATE TABLE IF NOT EXISTS work_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('check', 'handover')),
  trigger TEXT NOT NULL CHECK (trigger IN ('cron', 'operator')),
  doc_ids_json TEXT NOT NULL DEFAULT '[]',
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued', 'taken', 'done', 'reported')),
  pass_id TEXT, outcome TEXT,
  render_ids_json TEXT NOT NULL DEFAULT '[]',
  verdicts_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS credits (
  pass_id TEXT NOT NULL, key TEXT NOT NULL, gen INTEGER NOT NULL,
  PRIMARY KEY (pass_id, key));
CREATE INDEX IF NOT EXISTS ix_credits_gen ON credits(gen);
CREATE TABLE IF NOT EXISTS runs (
  job_id TEXT PRIMARY KEY, passes INTEGER NOT NULL DEFAULT 0);
"""


def build_v9_store(path, live_pass=False):
    """A schema-9 store exactly as v0.8.0 released it (DDL_V9, server/db.py at
    86656ac), for the 9 -> 10 migration test (Task 1, S2). With `live_pass`, a live
    delegation-protocol pass is also on the marker (generation 1, pass_id 'p1',
    trigger 'cron') with its own `passes` row, so the migration's
    close_delegation_pass_on_upgrade has something live to close."""
    import sqlite3

    import db

    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for stmt in db._statements(DDL_V9):
            conn.execute(stmt)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '9')")
        if live_pass:
            conn.execute("INSERT INTO counters(name, value) VALUES ('pass_generation', 1)"
                         " ON CONFLICT(name) DO UPDATE SET value=1")
            conn.execute("INSERT OR REPLACE INTO pass_marker(id, generation, live, pass_id,"
                         " trigger, started_at, claimed_step, lease_at) VALUES"
                         " (1, 1, 1, 'p1', 'cron', '2026-09-01T00:00:00Z', NULL, NULL)")
            conn.execute("INSERT INTO passes(pass_id, generation, trigger, started_at)"
                         " VALUES ('p1', 1, 'cron', '2026-09-01T00:00:00Z')")
    finally:
        conn.close()


def build_v10_store(path, drain=None, cancelled=None, email_request=False, staged_email=False):
    """A schema-10 store exactly as v0.9.0 left it, with optional S2-era state the 10 -> 11
    migration must clear or convert."""
    import sqlite3
    conn = sqlite3.connect(path)
    conn.executescript(DDL_V10)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '10')")
    conn.execute("INSERT INTO meta(key, value) VALUES ('store_epoch_at', '2026-10-01T00:00:00Z')")
    conn.execute("INSERT INTO claims(gen, job_id, at, batch) VALUES (1, 'cccccccc-3',"
                 " '2026-10-01T00:00:00Z', 1)")
    if drain:
        conn.execute("INSERT INTO meta(key, value) VALUES ('drain', ?)", (drain,))
    if cancelled:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, '2026-10-01T00:00:00Z')",
                     (f"cancelled:{cancelled}",))
    if email_request:
        conn.execute("INSERT INTO package_requests(quarter, channel, state, created_at,"
                     " updated_at, created_seq) VALUES ('2026-Q3', 'email', 'queued',"
                     " '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 7)")
    if staged_email:
        conn.execute("INSERT INTO packages(quarter, filename, path, built_at, partial, digest,"
                     " size, caption, manifest_json) VALUES ('2026-Q2', 'books-2026-Q2.zip',"
                     " '/x/books-2026-Q2.zip', '2026-10-01T00:00:00Z', 0, 'd', 10, 'c',"
                     " '{\"rows\": []}')")
        conn.execute("INSERT INTO deliveries(package_id, channel, staged_path, request_id,"
                     " status, created_at, lease_at) VALUES (1, 'email', '/h/qa-1/x.zip',"
                     " 'qa-0011223344556677', 'staged', '2026-10-01T00:00:00Z',"
                     " '2026-10-01T00:00:00Z')")
    conn.commit()
    conn.close()


DDL_V11 = """
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
  reply TEXT NOT NULL DEFAULT 'telegram',   -- where a continuation reports: telegram | silent
  protocol TEXT NOT NULL DEFAULT 'delegation',   -- 'delegation' (pre-S2) | 'job' (spec §3)
  holder_job TEXT,               -- the job run id that holds this pass (job protocol)
  orphaned_by TEXT,               -- the job run id that adopted this pass's work, if orphaned
  adoptions INTEGER NOT NULL DEFAULT 0,          -- adoptions spent (at most 2 per pass, §6.3)
  adopters_json TEXT NOT NULL DEFAULT '[]',      -- job run ids that adopted this pass's work
  acq INTEGER,                    -- the bank-feed acquisition id this pass last imported under
  acq_gen INTEGER,                -- that acquisition's restore generation
  read_seq INTEGER,               -- the store sequence this pass's read-back is owed from
  w_refreshes INTEGER NOT NULL DEFAULT 0,        -- W-refreshes spent (at most 2 per pass, §5.2)
  judge_after TEXT,               -- the running judgment's page start: a validated [pid]
  w_pending INTEGER NOT NULL DEFAULT 0,
  judge_epoch INTEGER NOT NULL DEFAULT 0,       -- judgments started for a bounded cause (INV-J8)
  late_takes INTEGER NOT NULL DEFAULT 0);       -- requests taken while live (LATE_TAKES_MAX)
CREATE TABLE IF NOT EXISTS pass_steps (
  pass_id TEXT NOT NULL,
  step TEXT NOT NULL CHECK (step IN ('sweep', 'judge', 'handover', 'snapshot')),
  started_at TEXT NOT NULL,      -- written by Ellen before she delegates
  finished_at TEXT,
  finished_by TEXT CHECK (finished_by IN ('specialist', 'resident')),
  finish_json TEXT,              -- counts, stopped (clipped), failed
  carry_json TEXT NOT NULL DEFAULT '{}',   -- doc_ids / report: ids and counts only
  protocol TEXT NOT NULL DEFAULT 'delegation',
  started_seq INTEGER,            -- the store sequence this step started at (job protocol)
  started_gen INTEGER,            -- the pass generation this step started under (job protocol)
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
  checked_snapshot INTEGER,                -- the import its finished check ran on (#15)
  asked_seq INTEGER NOT NULL DEFAULT 0);   -- the request's latest ask (S7 §10)
CREATE INDEX IF NOT EXISTS ix_package_requests_open ON package_requests(quarter, state);
CREATE TABLE IF NOT EXISTS probes (
  kind TEXT PRIMARY KEY, ok INTEGER NOT NULL, detail TEXT, data_json TEXT,
  observed_at TEXT NOT NULL, pass_id TEXT, failing_since TEXT,
  gen INTEGER);                  -- the pass generation this probe was recorded under (job protocol)

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
  bank_through TEXT,             -- the date bank data is known good through (sync ok this pass)
  job_id TEXT,                   -- the job run id this import belongs to (job protocol)
  read_seq INTEGER,              -- the store sequence this import's read-back is owed from
  acq INTEGER,                   -- the bank-feed acquisition id this import ran under
  export_ref TEXT,               -- the export's own ref (idempotency: at most one import per ref)
  swept_at TEXT);                -- when the sweep this import belongs to completed (§5.2's W)
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
  readback_owed INTEGER NOT NULL DEFAULT 0,  -- a read confirms a note only after it (job protocol)
  note_issued_gen INTEGER,       -- the pass generation add_note was last issued under
  note_other_issued_gen INTEGER, -- the pass generation any OTHER note_seq was last issued under
  note_seen_gen INTEGER,         -- the pass generation a read last saw the note under
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
  -- issue #24 (D5): each file the operator supplied (an attachment of a self-addressed
  -- mail, a Telegram file) by its own ref, once filed, so a pass's capped filing skips it
  ref TEXT PRIMARY KEY, source TEXT NOT NULL, doc_id INTEGER NOT NULL, filed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_operator_refs_filed ON operator_refs(filed_at);
CREATE TABLE IF NOT EXISTS claims (
  gen INTEGER PRIMARY KEY, job_id TEXT NOT NULL, at TEXT NOT NULL,
  spent INTEGER NOT NULL DEFAULT 0, reported INTEGER NOT NULL DEFAULT 0,
  batch INTEGER NOT NULL,        -- the batch this claim belongs to: its first claim's gen
  closed INTEGER NOT NULL DEFAULT 0,    -- this claim was answered end-batch or complete
  seq INTEGER);                  -- the store sequence taken at the claim (S7 §10)
CREATE TABLE IF NOT EXISTS work_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('check', 'handover')),
  trigger TEXT NOT NULL CHECK (trigger IN ('cron', 'operator')),
  doc_ids_json TEXT NOT NULL DEFAULT '[]',
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued', 'taken', 'done', 'reported')),
  pass_id TEXT, outcome TEXT,
  render_ids_json TEXT NOT NULL DEFAULT '[]',
  verdicts_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS credits (
  pass_id TEXT NOT NULL, key TEXT NOT NULL, gen INTEGER NOT NULL,
  PRIMARY KEY (pass_id, key));
CREATE INDEX IF NOT EXISTS ix_credits_gen ON credits(gen);
CREATE TABLE IF NOT EXISTS runs (
  job_id TEXT PRIMARY KEY, passes INTEGER NOT NULL DEFAULT 0,
  completed_at TEXT);            -- the run answered `complete` (S7 §10)
CREATE TABLE IF NOT EXISTS readings (
  reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE,      -- 128 random bits, minted into the proposal's deposit only
  text TEXT NOT NULL,            -- the words the desk turn received, verbatim
  quoted TEXT,                   -- the quoted post's text, when the desk context had one
  render_id TEXT,                -- the rendering the reading was bound to (§8)
  plan_json TEXT NOT NULL,       -- the writes, each with the revisions it read
  created_seq INTEGER NOT NULL, created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'applied', 'cancelled', 'stale')),
  settled_at TEXT);
CREATE TABLE IF NOT EXISTS render_keys (
  key TEXT PRIMARY KEY, render_id TEXT NOT NULL,
  action TEXT NOT NULL,          -- all-good | right | wrong | no-invoice
  pid INTEGER,                   -- NULL for all-good
  created_at TEXT NOT NULL, spent_at TEXT);
CREATE TABLE IF NOT EXISTS account_choices (
  key TEXT NOT NULL, n INTEGER NOT NULL, account_id TEXT NOT NULL, label TEXT NOT NULL,
  created_at TEXT NOT NULL, spent_at TEXT, PRIMARY KEY (key, n));
CREATE TABLE IF NOT EXISTS post_offers (
  render_id TEXT NOT NULL, job_id TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (render_id, job_id));
"""


def build_v11_store(path, with_rows=False):
    """A schema-11 store as 26b68ee leaves it; with_rows: one payment, one document, one
    machine match, so the 11 -> 12 migration is seen keeping data."""
    import sqlite3
    conn = sqlite3.connect(path)
    conn.executescript(DDL_V11)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '11')")
    if with_rows:
        conn.execute("INSERT INTO projections(dest_row_id, admitted_at) VALUES (1, 'x')")
        conn.execute("INSERT INTO documents(sha256, ext, size, kind, source,"
                     " extraction_author, ingested_at, ingest_quarter) VALUES"
                     " ('ab', 'pdf', 1, 'invoice', 'gmail', 'specialist', 'x', '2026-Q3')")
        conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq) VALUES (1, 1, 0)")
    conn.commit()
    conn.close()
