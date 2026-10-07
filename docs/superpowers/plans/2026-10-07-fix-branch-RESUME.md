# QUART resume: fix/empty-check-message (2026-10-07)

Self-contained. A fresh QUART session starts here, then `git log` of this branch.

## Where things are
- Plugin repo `bonzanni/casa-plugin-quarterly-accounting` (PUBLIC: no names, chat ids or live data in
  issues/commits). Branch **`fix/empty-check-message`**, worktree `~/Projects/ha-casa-worktrees/quart-empty`,
  cut from **`8044f908d28a10ea0d22fe19796e16ed2abe1331`** (= `feat/s7-quarterly-off-ellen`, the converged
  no-budget build with Casa floor v0.344.39). **Do NOT move `feat/s7-quarterly-off-ellen`**: PLAY's Q2
  re-run tested 8044f908. This branch becomes the next re-run's SHA. No merge, no tag without the
  operator's word.
- History before this branch: docs `specs/rounds-2026-10-06-simple-loop-diff/status.md` (f1–f3, g1–g3;
  g3 SHIP/SHIP at a6c288d), design rev 18 §R18.8 (no call budget; #1312 key). Some docs edits are on disk
  UNCOMMITTED in ha-casa-app-docs (g1–g3 rows, g2/g3 briefs, §R18.8 #1312 "closed" note), because BRAIN's
  **docs-push HOLD** stands until the operator rules on the swept commit 13c7bca0 (6,259 files pushed by
  mistake; a GitHub PAT in it; 82d76d9a untracked them). Never `git add -A` in a shared repo.

## The three items on this branch
1. **#47, the empty check** (filed: bug, severity:medium). A check with no payment in its quarter now says
   why. `cards.nothing_to_check(conn, q)` gives "Nothing to check for Q2 2026: the books start 1 Oct 2026.
   Say 'start from Q2 2026' to include it." (q before the watermark's quarter), "Nothing to check yet: the
   books start … and the bank has no payment since. Say 'start from <prev Q>' to include <prev Q>." (q =
   the watermark's quarter), or "Nothing to check for <Q> yet: the bank has no payment in it." It is used
   (a) in the operator's end message in place of "<Q> checked · 0 payments · all accounted for." and (b) in
   the completion text, `job.run_end` → `cards.checked_line`: a run that worked completes with
   "Q3 2026 checked: N matched, M to confirm, K missing[, P pending]" (BRAIN's wording). An assistant
   started run (`Started by: agent` → scheduled per #45) still posts no message of its own; Ellen relays the
   completion text. **The #45 mapping stays** (BRAIN: an operator's relayed ask counting as operator is
   Casa's #1277 territory, DRIVE 2's routing brief). Tests: `tests/test_issue_47.py` (4, red at 8044f90).
2. **Q2 re-run R7 (PLAY, live, URGENT, BRAIN GO): the run-1 "Read FIRST" order was impossible.**
   Finance's path_scope denies `Read` on downloads (`/data/handoff/gmail/…`), so R7 set every found
   invoice aside and decided its payment missing (AWS was matched in run 1). Run 1 read through
   `read_document(doc_id)` (119 calls), which saves the PDF under the session's own path that Read may
   open. The fix is skill-only: **file first with no amount, date or number → `read_document(doc_id)` →
   `Read` the path it names → `update_document_metadata(...)` with only what is printed** (never a value
   from the payment; an unreadable amount stays out, so the document is propose-only, floor unchanged).
   A cut between filing and the reading left a document never read, and its payment went missing. So
   `loop.candidates` now marks such a candidate **`unread: true`** (amount unknown AND not
   `amount_conflict`), and the skill reads an `unread` candidate first.
   **Sim path_scope enforcement**: `tests/sim_job.py` `_read(path)` allows only `JobDriver.SESSION`
   (Claude Code's tool-results path); `read_mode` "store" (the skill now) vs "handoff" (the R7 skill,
   reproduces the live loss); `drv.reads` lists every Read. Tests: `tests/test_q2_r7.py` (4).
   PLAY stopped R7.
3. **started_by mis-map of the binding run** (BRAIN #2: Casa's job said `operator`, the store recorded
   `runs.started_by="scheduled"`; the main run mapped correctly). **NOT FIXED YET; PLAY owes QUART the
   binding run's `job_next` calls** (the first `job_next(job_id, started_by=…)` of each turn).
   Hypothesis: `job.claim` creates the runs row only on the job id's FIRST claim, with
   `INSERT OR IGNORE` and the starter of THAT call. If the first `job_next(job_id)` of that job carried no
   `started_by` (or a refused/retried first call: the skill passes `started_by` only on a turn's first
   call), the row is "scheduled" for good, and later claims can't correct it. Also possible: a
   `reset_store` mid-run wipes `claims`/`runs`, so the next claim (a retry without `started_by`) recreates
   the row as scheduled. `binding` itself wipes nothing (checked). Fix once confirmed: let a later
   claim's explicit `Started by: operator` upgrade a run that has not posted yet, or record the starter
   on the claim and take the first explicit one. Test-first from PLAY's transcript.

## Suite state at this commit
- Last FULL run (before the final edits): 1291 tests, 5 failures + 2 errors, all since fixed:
  4 old completion-text assertions (tests/test_s2_t7.py, tests/test_s7_asks.py: now a regex for the
  #47 line), 1 skill text (tests/test_skill.py), 2 sim KeyErrors (a files-only payment unit has no
  `candidates`). The affected modules rerun green (57 tests). **A full re-run is still owed.**
- Gates at that run: check_removed 0, tool agreement 0, identifier scan 0, Casa gate OK 426 records
  against v0.344.39 (`CASA_TREE=~/Projects/ha-casa-worktrees/quart-casa-0344-39/casa/rootfs/opt/casa
  CASA_TESTS=…/quart-casa-0344-39/tests ~/Projects/ha-casa-app/venv_test/bin/python
  scripts/check_casa_shapes.py .tmp/shapes.jsonl`, after `python3 tests/gen_casa_shapes.py .tmp/shapes.jsonl`).

## Next
1. Full suite + gates green.
2. Item 3 once PLAY sends the transcript: red test, fix, green.
3. Astra+Terra round on the branch tip (brief: these three items; reproduce against `tests/sim_job.py`
   with `read_mode`, path_scope and Casa's cut; DO-NOT-REPORT the #45 mapping, the no-budget ruling, the
   #1312 residuals). Freeze the review tree `~/Projects/ha-casa-worktrees/quart-s7-plugin` at the tip SHA.
   The brief and round files go under the docs rounds folder ON DISK ONLY while the docs hold stands.
4. Fold to SHIP/SHIP; send BRAIN the issue number (#47) and the branch SHA.
5. When the docs hold lifts: commit the on-disk docs edits WITH EXPLICIT PATHS, plus §R18.3's
   "(proposed by the job)" → "(suggested by the job)" (BRAIN).
