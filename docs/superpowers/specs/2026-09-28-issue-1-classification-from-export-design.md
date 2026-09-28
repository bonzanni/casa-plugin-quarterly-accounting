# Issue #1 — classification from the export (design)

Status: shipped in 0.3.0, 2026-09-28. Design rounds D1 (Terra SWF, Astra SWF) and D2 (Terra SHIP, Astra SWF; its S2 folded in as a patch). Code round C1 on 7cd8eda: Terra SHIP; Astra SWF, whose one S2 (notes pushed out of view) the operator accepted as a residual, filed upstream as casa-specialist-finance#89. Ruling: issue #1, OPERATOR DECISION
2026-09-28 ("option A — the best fix"). Upstream: casa-specialist-finance#86 — feature commit cc1a2fb (bank-feed 0.19.0 in-tree),
released as tag v0.21.0 (commit 3479640) = component 0.21.0 / bank-feed 0.20.0.

## The asset, and the failure that must stay impossible

The accountant must never receive a document for a payment whose category changed
(`software` → `refund` turns a required invoice into a required credit note). Today's
rule guarantees it by re-reading every payment after every import (`lineage.is_fresh`),
which caps matching and packaging at what one delegation can read (~50 rows). The fix
must keep the guarantee and let a catch-up quarter finish in a few passes.

Silent failure: a package built on a stale kind looks exactly like a good one. The suite
is green today because every test re-reads.

## What upstream gives (verified against cc1a2fb)

- `export_history` appends two columns after the ledger's own: `tags` (sorted,
  comma-joined in CSV, a list in JSONL; empty = no tags) and `tag_revision` (int), read
  in the same `BEGIN … COMMIT` as the rows and the ledger instance id.
- Contract: for one ledger instance id and one row_id, **an equal revision means an equal
  tag set**. A differing one may be spurious (a restore stamps every row it rewrites).
  Triggers on `transaction_tags` move it for every writer (tag, untag, rename, delete,
  rules, supersede, erasures, restore). `0` = no change since the revision was installed.
- `get_transaction` prints `Tag revision: N` with the same value.
- Notes are NOT in the export.

## The change

### 1. The import is the classification observation

`import_ledger_export` requires `tags` and `tag_revision` (missing → refused: "bank-feed
is below this plugin's floor (0.20.0)"; nothing imported). After lineage resolution,
fan-in and admission (steps 1–3) and **before** `settle_all` (step 4), for every live
lineage that is not ended `erased` and whose `dest_row_id` is in the snapshot:

- `observed_tags_json` := the export's tags; `class_tags_json` := those not in
  `reducer.OWNED`; `class_observed_at` := now; `class_observed_snapshot` := this
  snapshot id; `export_tag_revision` := the row's `tag_revision`.

So after an import every present lineage is FRESH by the unchanged `is_fresh` rule
(observed at the latest snapshot). A lineage whose row is absent (an erase candidate) is
not stamped and stays not fresh — as today.

After `settle_all`, the import runs the classification half of the delivered-quarter
check once over the latest delivered packages (`ledger._kind_changes(conn, latest)`),
the same check `record_observation` runs per row today.

### 2. One function decides the owed write

`sweep.owed_write(conn, pid, actual_tags, note_visible) -> step | None` — the untag /
tag / add_note rule that `record_observation` computes inline today (one write,
untag before tag before note; a tag write bank-feed refused with the same observed tags
stays blocked). `record_observation` calls it with the read's tags and the read's note
check; the import calls it with the export's tags and the stored note confirmation (§3).

### 3. Note visibility is remembered, keyed by the tag revision

Notes are not exported, and `purge(user_work=erase)` strips notes from rows it keeps.
New columns: `note_seen_seq`, `note_seen_rev`.

- `record_observation` (a read): when the lineage's current note is visible (the check
  it makes today), `note_seen_seq := note_seq`, `note_seen_rev := observed_tag_revision`
  (new required argument: the `Tag revision:` line); otherwise both NULL.
- `record_observation`, when it RETURNS an `add_note` instruction, stamps
  `note_issued_at := now` (round D1, Astra S2: an `add_note` issued earlier and carried
  out after a later read confirmed the newer note leaves a stale assertion on top; notes
  do not move `tag_revision`, so no export can show it). A confirming read stamps
  `note_seen_at` := the `imported_at` of the snapshot the read belongs to (the
  `snapshot_id` it carries) — a server-known time that PRECEDES the read, never the time
  the read was recorded (round D2, Astra S2: a read held past the window and recorded
  late certified a note a stale write had since buried). A read of an older snapshot is
  already refused (`_require_snapshot`).
- The import treats the note as visible iff `note_seen_seq = note_seq` AND
  `note_seen_rev = export tag_revision` AND (`note_issued_at` is NULL OR `note_seen_at` ≥
  `note_issued_at` + `steps.CEILING_ASSUMED_S`). Any tag change since the confirming read
  (erasure included — it strips tags too) therefore re-checks the note, and so does a
  confirmation taken while an issued note write could still be in flight.
- Why the ceiling bounds it: an instruction lives only inside the delegation it was
  returned to, and Casa ends a delegation at its 600 s ceiling (spec assumption A2, the
  same bound issue #2's step expiry and claim lease rest on). A write carried out later
  than that has no carrier. Cost: a lineage whose note was written in a pass is read once
  more at a later pass's import (its confirming read-back is inside the window), then
  settles.

Residuals (stated, accepted): notes change without moving the tag revision, so a
confirmed note that later drops out of sight is restated only when the lineage's note
next changes. Two ways are known: (1) an erasure strips a note from a row that carried
**no tags at all**; (2) round C1 (Astra S2, reproduced): twenty or more newer notes are
appended to one row, and `get_transaction` shows only the newest 20. Operator ruling
2026-09-28: accept both, and ask upstream for a per-row note revision in the export
(casa-specialist-finance#89). When it ships, `note_confirmed` also requires the note
revision to be unchanged, and both residuals close. Every lineage that has an accounting note carries
either an owned `acct::` tag or a classification tag, except `optional` with no class tag
— which arises only from an operator/KB expectation on an unclassified row.

### 4. Due = owes a write, or unread since a decision moved

The due predicate (`sweep._due`) keeps its shape — `class_observed_snapshot < latest OR
observed_revision IS NULL OR observed_revision <> revision` — and the import now sets,
for every stamped lineage after `settle_all`: `observed_revision := revision` when
`owed_write(... export tags, stored note confirmation)` is None, else NULL. Consequences:

- a lineage whose export tags already equal its desired set and whose note is confirmed
  is not due: no read;
- one that owes a write is due; its read and read-back go through `record_observation`
  exactly as today (one write per observation, re-read, `expected_*` fences unchanged);
- a decision made later in the pass bumps `revision` → due (unchanged);
- a gate that is closed or a refused write does not loop: the read sets
  `observed_revision` as today.

### 5. Reporting

`passes.throughput.swept_this_pass` counts reads, not import stamps: a new column
`read_snapshot`, set only by `record_observation`. `remaining_in_cycle` unchanged.
Coverage (`views.coverage`) needs no change: `class_observed_at` is now the import time
for present rows, so "classification through" follows the bank date.

### 6. Unchanged on purpose

`is_fresh`, the machine-match refusal, the package's UNCLASSIFIED rule, triage's
`fresh` filter, `_require_snapshot` on reads, the first_seen check, the gate, the
one-write-per-observation discipline. They now pass for every present row after an
import instead of after a read.

### 7. Floor, schema, version

- bank-feed floor 0.15.0 → **0.20.0** (component v0.21.0). The test harness vendors
  component v0.21.0 as the floor tree; v0.13.2 stays the below-floor tree.
- Schema 4 → 5: `projections` gains `export_tag_revision`, `note_seen_seq`,
  `note_seen_rev`, `note_seen_at`, `note_issued_at`, `read_snapshot`. Existing lineages: all NULL → note unconfirmed → due
  once after the first import (one catch-up read per note-bearing row, then settled).
- Plugin 0.2.0 → 0.3.0. Skill: record `observed_tag_revision`; triage text drops "wait
  for the sweep's read"; spec §"The sweep", §"classification observation", floor table.

## Questions for the reviewers

1. Is there a sequence under which a package or a machine match acts on a kind older
   than the latest import (the export snapshot vs a read in the same pass, two imports
   in one pass, a merge, a restore under the same instance id, a supersede)?
2. Is there a sequence under which a needed tag or note write is never owed (a lineage
   left not due with desired ≠ actual, or its note missing)?
3. Is there a sequence under which the sweep loops within a pass?
