"""S2 (spec §4–§6): the job's cursor. A job turn's first, token-less job_next claims:
the claim rotates the token and records itself in `claims`, and only the newest claim's
token may act (check_claim). A pass held by another job is adopted: every holder change
spends one of its ADOPTIONS_MAX adoptions, and a claim that would change it once more
ends it `stopped`.

INV-J8 (spec §15): a batch progressed iff, under its claims, the job earned a credit it had
not earned before in that pass. A credit is a `credits(pass_id, key, gen)` row inserted
INSERT OR IGNORE in the transaction of the work it records, under the unit's claim token
(`credit`). Every key is drawn from a set finite per pass, so a pass earns finitely many."""
from __future__ import annotations

import hashlib
import json
import re

import db
import passes

JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
ADOPTIONS_MAX = 2          # holder changes per pass (§6.3), returns of an earlier holder too
LATE_TAKES_MAX = 2         # requests a live pass takes after it began (each needs a read)
MAX_PASSES_PER_JOB = 4     # passes one Casa job run begins; past it the run completes
K_STATES = 32              # distinct settled states credited per (pass, acquisition, payment)
K_SEARCH = 4               # recorded searches credited per (pass, acquisition, payment)
TURNS_PER_BATCH, BATCH_RESERVE = 80, 10     # turnsPerBatch: the manifest's casa.jobs
UNIT_COST = {"probes": 12, "snapshot": 6, "sweep": 10, "gmail-probe": 3, "filing": 28,
             "item": 11, "judge": 24, "post": 3, "view": 3, "build": 3, "deliver": 5}
W_S = 1800                 # W (spec §5.2): counted from the import's sweep completion
W_REFRESH_MAX = 2          # W-refreshes per pass; after them W is waived for the pass
NOT_READ = "the bank was not read in this pass yet"
OTHER_JOB = "the bank was read by another job"
LATE_ASK = "a check was asked after the bank was read"
UNSWEPT = "the bank read's sweep is not finished"
STALE = f"the bank read is older than {W_S // 60} minutes"


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


LEFT_BEHIND = "the check stopped before it finished — ask again when you want it"

# #45: the line Casa (0.344.31 on) writes right after the first `Job id:` of the job's launch
# prompt and brief, as the job model copies it. Only §4.1's implicit check reads it: the
# trigger it records. `scheduled` and `agent` keep today's cron; anything else, and no line
# (an older Casa), is "Casa did not say" — today's cron too.
STARTED_BY = {"Started by: operator": "operator", "Started by: scheduled": "cron",
              "Started by: agent": "cron"}


def starter_trigger(started_by) -> str:
    """The implicit check's trigger for the copied starter line: surrounding whitespace
    (CR and LF included) stripped, then an exact match; otherwise cron."""
    if not isinstance(started_by, str):
        return "cron"
    return STARTED_BY.get(started_by.strip(), "cron")


def _queued_any(conn) -> bool:
    return conn.execute("SELECT 1 FROM work_requests WHERE state='queued' UNION ALL SELECT 1"
                        " FROM package_requests WHERE state IN ('queued', 'snapshot')"
                        ).fetchone() is not None


def _completed(conn, job_id) -> bool:
    r = conn.execute("SELECT completed_at FROM runs WHERE job_id=?", (job_id,)).fetchone()
    return r is not None and r[0] is not None


def claim(conn, job_id, started_by=None) -> int:
    """A job turn's token-less job_next (S7 §4.1, §10). Under the custody lock, taken
    before the transaction (the store's lock order: the stalled-send recovery removes
    staged bytes), in one transaction: the claim is recorded with its store sequence
    (`claims.seq`); a live pass held by another job is adopted (§6.3); the left-behind
    run's package asks are closed (§10); then a job id's first claim that leaves no live
    pass and nothing queued records a check (§4.1) — trigger operator when `started_by`
    is Casa's `Started by: operator`, cron otherwise (#45); a stalled staged send is
    recovered (§6.1). Nothing restarts a job: no drain, no orphan mark (§9). `started_by`
    is read by that check only, so a later claim of the job, or a claim that finds an ask
    queued, ignores it."""
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise db.Refusal("job_id is the `Job id:` line of your brief, as given")
    import delivery, steps
    with db.custody_lock():                 # the stalled-send recovery removes staged bytes
        with db.tx(conn):
            # r3 #1 (INV-S7-6): a posted send has left the plugin — before anything can
            # requeue its ask, it is settled `uncertain`, whatever its lease (a late
            # receipt still upgrades it; "send it again" is the only second file)
            for d in delivery.posted_unrecorded(conn):
                _settle_recovered(conn, d)
            m = passes._marker(conn)
            if m is not None and m["live"] and passes.protocol_of(conn, m["pass_id"]) != "job":
                passes.close_delegation_pass_on_upgrade(conn)        # spec §8
            first = conn.execute("SELECT 1 FROM claims WHERE job_id=?",
                                 (job_id,)).fetchone() is None
            prev = conn.execute("SELECT job_id FROM claims WHERE job_id<>? ORDER BY gen DESC"
                                " LIMIT 1", (job_id,)).fetchone() if first else None
            p = live_job_pass(conn)
            token = passes.rotate(conn)
            changed = p is not None and p["holder_job"] != job_id
            conn.execute("INSERT INTO claims(gen, job_id, at, batch, seq) VALUES (?,?,?,?,?)",
                         (token, job_id, db.now(), _batch_of(conn, job_id, token, changed),
                          db.next_seq(conn)))
            left, exhausted = None, False
            if changed:
                left = p["holder_job"]
            elif prev is not None and not _completed(conn, prev[0]):
                left = prev[0]
            if p is not None:
                if changed:
                    # spec §6.3, amended (design r1, Astra S2): every claim that takes the
                    # pass from a different job id spends the budget — a return of an
                    # earlier holder (A→B→A) too — since each needs a bank read of its own
                    if p["adoptions"] >= ADOPTIONS_MAX:
                        stop_exhausted_pass(conn, token, p["pass_id"], job_id)
                        exhausted = True
                    else:
                        held = json.loads(p["adopters_json"])
                        conn.execute("UPDATE passes SET adoptions=adoptions+1, adopters_json=?,"
                                     " holder_job=? WHERE pass_id=?",
                                     (json.dumps(held + [job_id]), job_id, p["pass_id"]))
                        conn.execute("UPDATE pass_marker SET generation=? WHERE id=1", (token,))
                else:
                    conn.execute("UPDATE pass_marker SET generation=? WHERE id=1", (token,))
            if left is not None:
                _close_left_behind(conn, token, left)                         # §10
            # §4.1, on the state this run will work: after §10's closure and pass-ending
            # (fix r1 ruling), so a launch left with nothing to do is still a check — but
            # never on a claim that spent the adoption budget: the operator was just told
            # "kept stopping — ask again", and a check now would be a restart (G2; r2 ruling)
            if first and not exhausted and live_job_pass(conn) is None \
                    and not _queued_any(conn):
                conn.execute("INSERT INTO work_requests(kind, trigger, doc_ids_json,"
                             " created_seq, created_at, state) VALUES ('check', ?, '[]',"
                             " ?, ?, 'queued')", (starter_trigger(started_by),
                                                  db.next_seq(conn), db.now()))
            for d in delivery.stalled_sends(conn, steps.LEASE_S):             # §6.1
                _settle_recovered(conn, d)
            return token


def _settle_recovered(conn, d) -> None:
    """A staged send nobody recorded: settled `uncertain` with its notice, its request
    withdrawn (inside the claim's transaction, under the custody lock)."""
    import delivery
    delivery.recover_staged(conn, d)
    conn.execute("UPDATE package_requests SET state='withdrawn', updated_at=? WHERE"
                 " delivery_id=? AND state='staged'", (db.now(), d["delivery_id"]))


def _close_left_behind(conn, token, left) -> None:
    """§10 (G2): the left-behind run's package asks — queued or snapshot, asked before its
    last claim (asked_seq below that claim's seq) — are closed `stopped` with LEFT_BEHIND,
    each with its package-stopped notice; a closed snapshot request's live package pass
    ends `stopped` with it. A renewed ask (asked_seq after) is served. Check and handover
    asks are untouched."""
    last = conn.execute("SELECT coalesce(max(seq), 0) FROM claims WHERE job_id=?",
                        (left,)).fetchone()[0]
    for req in conn.execute("SELECT * FROM package_requests WHERE state IN ('queued',"
                            " 'snapshot') AND asked_seq < ? ORDER BY request_id",
                            (last,)).fetchall():
        passes._close(conn, req, "stopped", "stopped", reason=LEFT_BEHIND)
        p = live_job_pass(conn)
        if req["state"] == "snapshot" and p is not None and p["pass_id"] == req["pass_id"]:
            passes._end_pass_tx(conn, token, "stopped", {"stopped_reason": LEFT_BEHIND},
                                credit=False)


def _batch_of(conn, job_id, token, holder_changed) -> int:
    """The batch claim `token` belongs to (design r6, round 5): a new one — named by this
    claim's own gen — iff it is the job id's first claim, or that job id's latest claim
    belongs to a batch answered `end-batch` or `complete`, or already holding
    TURNS_PER_BATCH claims, or the live pass's holder changes with it; otherwise the
    latest claim's batch (a re-claim inside one batch). The claim count (coordinator's
    ruling on the j8-credits report, concern 1): a turn makes one claim and a Casa batch
    has at most turnsPerBatch turns, so the window is bounded even when every Casa batch
    is cut before an `end-batch` answer."""
    prev = conn.execute("SELECT batch FROM claims WHERE job_id=? ORDER BY gen DESC LIMIT 1",
                        (job_id,)).fetchone()
    if prev is None or holder_changed:
        return token
    closed, n = conn.execute("SELECT max(closed), count(*) FROM claims WHERE batch=?",
                             (prev["batch"],)).fetchone()
    return token if closed or n >= TURNS_PER_BATCH else prev["batch"]


def stop_exhausted_pass(conn, token, pass_id, job_id=None) -> None:
    """The adoption budget is spent (spec §6.3): the pass ends `stopped` through
    _end_pass_tx, which dispositions what it serves in this same transaction — its work
    requests `done`/`stopped` (asks.settle_taken), its package request closed `stopped`
    with its package-stopped notice (_hand_over → _close). The stopping claim never takes
    the pass, so its report names the job that stopped it (`stopped_by`): that job's run
    ended a pass stopped, and says so (run_end)."""
    conn.execute("UPDATE pass_marker SET generation=? WHERE id=1", (token,))
    passes._end_pass_tx(conn, token, "stopped", {"adoptions_exhausted": True,
                                                 "stopped_by": job_id})


def measure(conn) -> int:
    """The progress summary's `remaining` only: the live pass's payments still to search
    (0 with none). Never progress — INV-J8 is credits (spec §15)."""
    import steps, work
    p = live_job_pass(conn)
    if p is None:
        return 0
    req = steps.round_request(conn, p["pass_id"])
    carry = steps._first_carry(conn, p["pass_id"])
    if req is not None:
        return len(work.package_work(conn, req))
    if carry.get("since_seq") is not None:
        return len(work.check_work(conn, carry["since_seq"], owed=carry.get("owed", [])))
    return 0


# --- INV-J8: credits (spec §15; design revision 6) ------------------------------------
def credit(conn, token, pass_id, key, cap_prefix=None, cap=None) -> None:
    """Earn `key` for job pass `pass_id` under claim `token`, in the caller's transaction
    — the work's own. INSERT OR IGNORE: a key earned before in the pass earns nothing.
    With `cap`, inserted only while fewer than `cap` keys of the pass start with
    `cap_prefix`. A no-op without a token or for a pass that is not a job's."""
    assert conn.in_transaction
    if token is None or pass_id is None or passes.protocol_of(conn, pass_id) != "job":
        return
    if cap is not None and conn.execute(
            "SELECT count(*) FROM credits WHERE pass_id=? AND substr(key, 1, ?)=?",
            (pass_id, len(cap_prefix), cap_prefix)).fetchone()[0] >= cap:
        return
    conn.execute("INSERT OR IGNORE INTO credits(pass_id, key, gen) VALUES (?,?,?)",
                 (pass_id, key, int(token)))


def _acq(conn, p):
    """The acquisition a key is drawn under: the one the pass's latest import belongs to."""
    import lineage
    row = conn.execute("SELECT acq FROM snapshots WHERE snapshot_id=?",
                       (lineage.latest_import(conn),)).fetchone()
    return row["acq"] if row is not None and row["acq"] is not None else p["acq"]


def credit_sweep(conn, token, pid, what) -> None:
    """The sweep's credits for payment `pid` (design table): a settlement completed, keyed
    by the projection's full semantic state (`projections.digest`), at most K_STATES
    distinct states per (pass, acquisition, pid); a confirmed erasure (`erased`); a refused
    write (`refused`)."""
    p = live_job_pass(conn)
    if p is None or token is None:
        return
    prefix = f"sweep:{_acq(conn, p)}:{pid}:"
    if what in ("erased", "refused"):
        credit(conn, token, p["pass_id"], prefix + what)
        return
    digest = conn.execute("SELECT digest FROM projections WHERE pid=?", (pid,)).fetchone()[0]
    d = hashlib.sha256((digest or "").encode()).hexdigest()[:24]
    credit(conn, token, p["pass_id"], prefix + d, cap_prefix=prefix, cap=K_STATES)


def credit_search(conn, token, pid) -> None:
    """A search item recorded for `pid`: `search:<acq>:<pid>:<k>`, the k-th recorded in that
    acquisition, k ≤ K_SEARCH."""
    p = live_job_pass(conn)
    if p is None or token is None:
        return
    prefix = f"search:{_acq(conn, p)}:{pid}:"
    k = conn.execute("SELECT count(*) FROM credits WHERE pass_id=? AND substr(key, 1, ?)=?",
                     (p["pass_id"], len(prefix), prefix)).fetchone()[0] + 1
    if k <= K_SEARCH:
        credit(conn, token, p["pass_id"], f"{prefix}{k}")


def hand_acquisition(conn, token, pass_id) -> int:
    """A new bank read for the pass (spec §5.2): its id and its request watermark,
    owned by this claim alone."""
    acq, read_seq = db.next_seq(conn), db.next_seq(conn)
    conn.execute("UPDATE passes SET acq=?, acq_gen=?, read_seq=? WHERE pass_id=?",
                 (acq, token, read_seq, pass_id))
    return acq


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
    """INV-J10: a machine pairing, proposal or relabel commits only while F holds, decided
    in the write's own transaction. A no-op outside a live job pass."""
    why = fresh_reason(conn)
    if why is not None:
        raise db.Refusal(f"{why}: the bank must be read again: call job_next")


# --- the cursor (spec §5) -----------------------------------------------------------
def next_unit(conn, token, judged=None) -> dict:
    with db.tx(conn):
        check_claim(conn, token)
        if judged is not None:
            _judged(conn, token, judged)
        out = _choose(conn, token)
        _account(conn, token, out)          # Task 9: budget, progress, report
        job_id = conn.execute("SELECT job_id FROM claims WHERE gen=?", (token,)).fetchone()[0]
        _record_offers(conn, out, job_id)
        return out


def _choose(conn, token) -> dict:
    import asks, steps
    job_id = conn.execute("SELECT job_id FROM claims WHERE gen=?", (token,)).fetchone()[0]
    for _ in range(8):                      # passes may end and the next begin in one call
        p = live_job_pass(conn)
        if p is not None and p["trigger"] != "package":
            _take(conn, token, p)
            p = live_job_pass(conn)
        if p is None:
            u = _sends(conn, token, job_id)           # §6.1: build / deliver
            if u is None:
                u = _posts(conn, job_id)
            if u is not None:
                return u
            if done(conn, job_id, token):
                conn.execute("INSERT OR IGNORE INTO runs(job_id, passes) VALUES (?, 0)",
                             (job_id,))
                conn.execute("UPDATE runs SET completed_at=coalesce(completed_at, ?) WHERE"
                             " job_id=?", (db.now(), job_id))
                return {"unit": "complete", "text": run_end(conn, job_id)[0]}
            p = _begin_next(conn, token, job_id)
            if p is None:
                raise RuntimeError("the cursor found nothing to do but the run is not done")
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


def _take(conn, token, p) -> None:
    """The live pass takes the queued requests (INV-J9). A handover taken while the
    pass's judge step runs restarts that judgment now (spec §5.2, fix round 1): a new
    started_seq and started_gen, the page cursor reset, so no verdict of the judgment
    that started before the handover can cover it."""
    import asks, steps
    ids = asks.take_queued(conn, p["pass_id"], late=True)
    if not ids or conn.execute(
            "SELECT 1 FROM work_requests WHERE pass_id=? AND kind='handover' AND request_id IN"
            " (%s)" % ",".join("?" * len(ids)), (p["pass_id"], *ids)).fetchone() is None:
        return
    j = _step(conn, p, "judge")
    if j is not None and j["finished_at"] is None:
        _start_judgment(conn, token, p, restart_running=True)


def run_passes(conn, job_id) -> int:
    row = conn.execute("SELECT passes FROM runs WHERE job_id=?", (job_id,)).fetchone()
    return row[0] if row is not None else 0


def done(conn, job_id, token=None) -> bool:
    """THE completion predicate, shared by job_next's `complete` and job_status's `done`
    (design r6, round 5; S7 §5): `_done_now`, evaluated whole inside a savepoint that is
    always rolled back, so it agrees with the cursor by construction — what the cursor
    would write on the way (a status rendering made, a request reported, a stale request
    requeued) is seen, and never kept. Inside the caller's transaction."""
    assert conn.in_transaction
    conn.execute("SAVEPOINT done")
    try:
        return _done_now(conn, token, job_id)
    finally:
        conn.execute("ROLLBACK TO done")
        conn.execute("RELEASE done")


def _done_now(conn, token, job_id) -> bool:
    """THE completion predicate's body, as the cursor would reach it: no live pass;
    _sends and _posts hand out nothing (their writes — a stale request's requeue included —
    in force); then nothing queued, or the run's pass budget spent (S7 §4.2: nothing
    restarts a job, so what is queued waits for the next start)."""
    if live_job_pass(conn) is not None:
        return False
    if (_sends(conn, token, job_id) or _posts(conn, job_id)) is not None:
        return False
    queued = conn.execute("SELECT 1 FROM work_requests WHERE state='queued' UNION ALL"
                          " SELECT 1 FROM package_requests WHERE state='queued'").fetchone()
    return queued is None or run_passes(conn, job_id) >= MAX_PASSES_PER_JOB


# --- the job posts its own results (S7 §5, §4.2) ------------------------------------
OFFER_MAX = 2              # hand-outs of one rendering per run (§5: a broken channel)
POST_MAX = 3               # renderings per post: 3 × BODY_LIMIT + label < 12,000
POST_CHARS = 11_900        # a post's joined text: 12,000 minus the label
LEFT_WAITING = "Some asks are waiting: ask again to start them."


def _sends(conn, token, job_id):
    """§6.1: a package whose check is done is built, then posted — by this claim, which
    holds its request (the token is the claim's gen; passes.check_package_token refuses it
    once a newer claim exists). A request whose check no longer describes the bank goes
    back to its check here (D2). A `staged` request is never handed out again (INV-S7-6):
    its send is recorded in the same turn, or recovered `uncertain` at a later claim.
    Called with token=None by job.status, through done()'s savepoint, which is rolled back:
    what it writes then is seen and never kept."""
    import lineage
    latest = lineage.latest_import(conn)
    for req in conn.execute("SELECT * FROM package_requests WHERE state IN ('snapshot-done',"
                            " 'built') ORDER BY request_id").fetchall():
        if req["checked_snapshot"] is None or req["checked_snapshot"] != latest:
            passes.requeue(conn, req["request_id"])
            continue
        if req["state"] == "built" and _oversize(conn, req["package_id"]):
            # plan round 2, Astra S2: Telegram refuses it forever (delivery._stage) — the
            # request ends with its notice instead of a deliver unit handed out again
            _close_oversize(conn, req)
            continue
        conn.execute("UPDATE package_requests SET token=?, lease_at=?, updated_at=? WHERE"
                     " request_id=?", (token, db.now(), db.now(), req["request_id"]))
        if req["state"] == "snapshot-done":
            return {"unit": "build", "quarter": req["quarter"], "package_token": token,
                    "request_id": req["request_id"]}
        return {"unit": "deliver", "package_id": req["package_id"], "package_token": token,
                "request_id": req["request_id"]}
    return None


def _oversize(conn, package_id) -> bool:
    import package
    pk = conn.execute("SELECT oversize, size FROM packages WHERE package_id=?",
                      (package_id,)).fetchone()
    return pk is not None and (bool(pk["oversize"]) or pk["size"] > package.MAX_ZIP_BYTES)


def _close_oversize(conn, req) -> None:
    """A built package over Telegram's 20 MB cannot be posted (delivery._stage refuses it,
    every time): the request ends `stopped` with its package-not-sent notice, which the next
    `post` carries. The zip is kept; notes.md names the largest files (its caption says so)."""
    import alerts
    pk = conn.execute("SELECT size FROM packages WHERE package_id=?",
                      (req["package_id"],)).fetchone()
    reason = (f"it is {pk['size'] / 1e6:.1f} MB, over Telegram's 20 MB limit; it is kept "
              "here, and notes.md names the largest files")
    conn.execute("UPDATE package_requests SET state='stopped', reason=?, updated_at=? WHERE"
                 " request_id=?", (reason, db.now(), req["request_id"]))
    alerts.raise_package(conn, "package-not-sent", f"request:{req['request_id']}:oversize",
                         quarter=req["quarter"], reason=reason)


def offers(conn, render_id, job_id) -> int:
    """How often run `job_id` handed rendering `render_id` out (the accounts unit is keyed
    "accounts")."""
    r = conn.execute("SELECT n FROM post_offers WHERE render_id=? AND job_id=?",
                     (render_id, job_id)).fetchone()
    return r[0] if r else 0


def _offer(conn, render_id, job_id) -> None:
    conn.execute("INSERT INTO post_offers(render_id, job_id, n) VALUES (?,?,1) ON CONFLICT"
                 "(render_id, job_id) DO UPDATE SET n=n+1", (render_id, job_id))


def _left_owed(conn, job_id) -> bool:
    """§4.2: the run spent its pass budget with asks still queued."""
    return (live_job_pass(conn) is None and run_passes(conn, job_id) >= MAX_PASSES_PER_JOB
            and conn.execute("SELECT 1 FROM work_requests WHERE state='queued' UNION ALL"
                             " SELECT 1 FROM package_requests WHERE state='queued'").fetchone()
            is not None)


def _left_render(conn, job_id):
    """The run's one `job-left` rendering (LEFT_WAITING), made on first need."""
    key = f"left:{job_id}"
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is not None:
        return row[0]
    rid = f"r{db.next_seq(conn)}"
    conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                 " membership_json) VALUES (?, 'job-left', '{}', ?, ?, '[]')",
                 (rid, db.now(), LEFT_WAITING))
    conn.execute("INSERT INTO meta(key, value) VALUES (?,?)", (key, rid))
    return rid


def _undelivered(conn, rid) -> bool:
    r = conn.execute("SELECT delivered_at FROM renders WHERE render_id=?", (rid,)).fetchone()
    return r is not None and r[0] is None


def _exhausted_alerts(conn, job_id) -> set:
    """The unsent alerts whose rendering this run already handed out OFFER_MAX times: left
    out of the next alerts rendering, so the following occurrences are composed and an
    exhausted one is never re-minted within the run (plan round 1, Astra S1)."""
    return {r[0] for r in conn.execute(
        "SELECT a.alert_id FROM alerts a JOIN post_offers o ON o.render_id=a.render_id AND"
        " o.job_id=? WHERE a.sent_at IS NULL AND o.n >= ?", (job_id, OFFER_MAX))}


def _posts(conn, job_id):
    """§5: what this run still owes the operator, as ONE unit (or None). In order, under
    the per-run cap: the pending alerts rendering, every done request's undelivered stop
    and handover pages, undelivered package notes, this run's job-left line — at most
    POST_MAX of them within POST_CHARS joined. Only when none is owed: each done operator
    check's status rendering, a `view` at its first hand-out when it fits a proposal, else
    a plain `post`. Then the accounts question (§11), once a run. Writes no offer: next_unit
    records the hand-out (_record_offers)."""
    import alerts, asks, views
    pick, status, made = [], [], {}

    def take(rid):
        if rid not in pick and _undelivered(conn, rid) and offers(conn, rid, job_id) < OFFER_MAX:
            pick.append(rid)
    a = alerts.pending_in_tx(conn, skip=_exhausted_alerts(conn, job_id))
    if a is not None:
        take(a["render_id"])
    for r in conn.execute("SELECT * FROM work_requests WHERE state='done' ORDER BY request_id"
                          ).fetchall():
        cls = asks._result_class(r)
        for page in asks._result_tx(conn, r["request_id"], made):
            if cls == "status":
                if page["render_id"] not in status:
                    status.append(page["render_id"])
            else:
                take(page["render_id"])
    for (rid,) in conn.execute("SELECT render_id FROM renders WHERE kind='package-note' AND"
                               " delivered_at IS NULL ORDER BY rowid").fetchall():
        take(rid)
    if _left_owed(conn, job_id):
        take(_left_render(conn, job_id))
    chosen, size = [], 0
    for rid in pick:
        n = len(conn.execute("SELECT text FROM renders WHERE render_id=?", (rid,)).fetchone()[0])
        if chosen and (len(chosen) >= POST_MAX or size + 2 + n > POST_CHARS):
            break
        chosen.append(rid)
        size += n + (2 if len(chosen) > 1 else 0)
    if chosen:
        return {"unit": "post", "render_ids": chosen}
    for rid in status:
        n = offers(conn, rid, job_id)
        if not _undelivered(conn, rid) or n >= OFFER_MAX:
            continue
        text = conn.execute("SELECT text FROM renders WHERE render_id=?", (rid,)).fetchone()[0]
        if n == 0 and views.fits_proposal(text):
            return {"unit": "view", "render_id": rid}
        return {"unit": "post", "render_ids": [rid]}
    if _accounts_owed(conn, job_id):
        return {"unit": "view", "accounts": True}
    return None


def _accounts_owed(conn, job_id) -> bool:
    """§11: the store is unbound, the bank lists two or more company accounts, and this
    run has not asked yet."""
    import posting
    if conn.execute("SELECT 1 FROM binding WHERE id=1").fetchone() is not None:
        return False
    return (len(posting._company_accounts(conn)) >= 2
            and offers(conn, "accounts", job_id) < 1)


def _record_offers(conn, out, job_id) -> None:
    """A hand-out is counted only when it is HANDED OUT: called by next_unit after _account,
    whose batch budget may replace the unit with end-batch (plan round 3, Astra S2: an
    offer counted for a unit the budget swapped out was a hand-out that never happened)."""
    if out.get("unit") == "post":
        for rid in out["render_ids"]:
            _offer(conn, rid, job_id)
    elif out.get("unit") == "view":
        _offer(conn, "accounts" if out.get("accounts") else out["render_id"], job_id)


def _begin_next(conn, token, who):
    """A pass begins for what is queued (the caller found `done` false), counted against
    the run's MAX_PASSES_PER_JOB in this transaction; adopting a live pass never counts."""
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
    conn.execute("UPDATE passes SET holder_job=?, adopters_json=? WHERE pass_id=?",
                 (who, json.dumps([who]), pid))
    conn.execute("INSERT OR IGNORE INTO runs(job_id, passes) VALUES (?, 0)", (who,))
    conn.execute("UPDATE runs SET passes=passes+1 WHERE job_id=?", (who,))
    asks.take_queued(conn, pid)
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
    return _stop(conn, token, p, req, gate["reason"])


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
    tools_ = conn.execute("SELECT ok, gen FROM probes WHERE kind='bank_tools'").fetchone()
    if tools_ is not None and tools_["gen"] == token and not tools_["ok"]:
        # no bank-feed tools in this session: nothing more can be probed (Astra plan-r7 S2)
        return _stop(conn, token, p, req, "bank-feed's tools are not available to the "
                                          "finance specialist")
    sync = conn.execute("SELECT gen, data_json FROM probes WHERE kind='bank_sync'").fetchone()
    led = conn.execute("SELECT gen FROM probes WHERE kind='ledger'").fetchone()
    if (sync is None or sync["gen"] != token
            or json.loads(sync["data_json"] or "{}").get("acq") != p["acq"]
            or led is None or led["gen"] != token):
        return {"unit": "probes", "acq": p["acq"], "quarter": q}
    # a failed sync never stops the pass (PLAY T7, as v0.8.0): the import reads bank-feed's
    # cached ledger and leaves bank_through where it was; the bank_sync alert says the
    # connection stopped, and the views say how far the bank was checked
    setup, gate = binding.check_setup(conn), passes.bank_write_gate(conn)
    if not setup["can_run"] or not gate["allowed"]:
        return _stop(conn, token, p, req, gate["reason"] or "; ".join(
            setup.get("conditions") or []) or "cannot run")
    return {"unit": "snapshot", "acq": p["acq"], "quarter": q}


def _stop(conn, token, p, req, reason) -> str:
    import steps
    row = _step(conn, p, _first(req))
    if row["finished_at"] is None:
        _, refused = steps._finish_tx(conn, token, _first(req), counts={}, stopped=reason,
                                      by_refusal=True)
        assert refused is None
    # the ONE place a pass is ended stopped by the cursor. The reason is kept in the pass's
    # stored report (Astra plan-r8 S2): the probe that carried it is overwritten by the
    # next acquisition, and the result must still say it
    passes._end_pass_tx(conn, token, "stopped", {"stopped_reason": reason})
    return "ended"


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
        _start_judgment(conn, token, p)                # closes the chunk; may restart
        return _judge_unit(conn, p, req)
    if steps._another_chunk(conn, p["pass_id"], req, j):
        steps._hand_chunk(conn, p["pass_id"], req, False)
        return _gmail(conn, token, p, req)
    return None


def _start_judgment(conn, token, p, restart_running=False) -> None:
    """THE one place a job pass's judgment starts or starts again (from _judge, and from
    _take for a handover taken mid-judgment): its page cursor goes back to the start. The
    pass's judge epoch moves on only for a cause drawn from a finite budget (`bounded`):
    then the new judgment's pages earn credit again; restarted for an unbounded cause, it
    re-judges pages already credited, which earns nothing (INV-J8)."""
    import steps
    cause = restart_cause(conn, p, _step(conn, p, "judge"))   # before the start moves
    steps._start_tx(conn, token, "judge", {}, restart_running=restart_running)
    conn.execute("UPDATE passes SET judge_after=NULL, judge_epoch=judge_epoch+? WHERE"
                 " pass_id=?", (int(bounded(cause)), p["pass_id"]))


def restart_cause(conn, p, j) -> str:
    """Why the judgment `j` (None: none yet) is (re)started: a cause of BOUNDED or of
    UNBOUNDED (see `bounded`)."""
    import asks, work
    if j is None:
        return "first-judgment"
    started = j["started_seq"] or 0
    import steps
    if steps._chunk(conn, p["pass_id"])[1] is not None:
        return "search-chunk"
    latest_read = conn.execute("SELECT max(read_seq) FROM snapshots WHERE pass_id=?",
                               (p["pass_id"],)).fetchone()[0]
    if latest_read is not None and started < latest_read:
        return "bank-read"
    if conn.execute("SELECT 1 FROM work_requests WHERE pass_id=? AND state='taken' AND"
                    " kind='handover' AND created_seq > ?", (p["pass_id"], started)).fetchone():
        return "handover-take"
    return "not-covered" if work.judge_whole(j) else "not-whole"


# The causes a judgment is started (again) for, each with the budget that bounds how often
# it occurs in a pass: only these move the judge epoch on (INV-J8, spec §15)
BOUNDED = {
    "bank-read": "an accepted import: the pass's first read, then at most W_REFRESH_MAX "
                 "W refreshes, LATE_TAKES_MAX late-take reads and ADOPTIONS_MAX "
                 "holder-change reads",
    "search-chunk": "_another_chunk hands a chunk out only while its count of payments "
                    "still to search falls strictly: finitely many per pass",
    "handover-take": "one per handover request, and a pass takes finitely many",
    "first-judgment": "once per pass",
}
UNBOUNDED = {
    "not-whole": "the judgment did not come out whole (triage_remaining > 0): the same "
                 "work judged again, as often as it keeps happening",
    "not-covered": "a taken handover is still not covered by a whole judgment: the same",
}


def bounded(cause) -> bool:
    """THE one predicate: is a judgment (re)started for `cause` drawn from a finite budget
    (BOUNDED)? Only then does the judge epoch move on. An UNBOUNDED cause never does:
    re-judging the same pages forever must stop earning credit, so Casa ends the job."""
    if cause in BOUNDED:
        return True
    assert cause in UNBOUNDED, cause
    return False


def _judge_unit(conn, p, req) -> dict:
    p = live_job_pass(conn)                           # re-read: judge_after may have changed
    j = _step(conn, p, "judge")
    after = json.loads(p["judge_after"]) if p["judge_after"] else None
    return {"unit": "judge", "judgment": j["started_seq"], "after": after,
            "quarter": req["quarter"] if req else None,
            "documents_first": _unjudged_handovers(conn, p, j)}


def _unjudged_handovers(conn, p, j) -> list:
    """The documents of the pass's taken handovers that have no verdict from judgment
    `j` yet (the judge unit's `documents_first`), in request order."""
    out = []
    for r in conn.execute("SELECT doc_ids_json, verdicts_json FROM work_requests WHERE"
                          " pass_id=? AND state='taken' AND kind='handover' ORDER BY"
                          " request_id", (p["pass_id"],)):
        seen = json.loads(r["verdicts_json"])
        out += [d for d in json.loads(r["doc_ids_json"])
                if (seen.get(str(d)) or {}).get("judge") != j["started_seq"] and d not in out]
    return out


def _doc_key(k) -> str:
    """A `documents` key as record_verdicts stores it ("12" for 12 or "12"); any other
    key as given (record_verdicts refuses it)."""
    try:
        return str(int(k)) if not isinstance(k, bool) else str(k)
    except (TypeError, ValueError):
        return str(k)


def _judged(conn, token, judged) -> None:
    import asks, steps
    if not isinstance(judged, dict):
        raise db.Refusal("judged is {page_next, triage_remaining, documents}")
    p = live_job_pass(conn)
    j = _step(conn, p, "judge") if p is not None else None
    if j is None or j["finished_at"] is not None:
        raise db.Refusal("no judge step is running: call job_next without judged")
    # the answer names the unit it answers (fix round 1, INV-J4): its judgment and its
    # page cursor, as handed out. A repeated answer, or one for an older judgment, is
    # refused, so it can never finish a judgment it did not see
    after = json.loads(p["judge_after"]) if p["judge_after"] else None
    if ("judgment" not in judged or "after" not in judged
            or isinstance(judged["judgment"], bool)
            or judged["judgment"] != j["started_seq"] or judged["after"] != after):
        raise db.Refusal("this answer is for another judge step: call job_next and judge "
                         "the page it hands out")
    documents = judged.get("documents") or {}
    if not isinstance(documents, dict):
        raise db.Refusal("documents is {<doc_id>: <verdict>, …}")
    nxt = judged.get("page_next")
    if nxt:
        # INV-J8 (design r3): the page start a credit is keyed by is never a free value.
        # page_next is the triage page's `next` — [the last payment it showed], whose
        # number is past this page's start — checked before anything is stored
        start = after[0] if after else 0
        if (not isinstance(nxt, list) or len(nxt) != 1 or not isinstance(nxt[0], int)
                or isinstance(nxt[0], bool) or nxt[0] <= start
                or conn.execute("SELECT 1 FROM projections WHERE pid=?",
                                (nxt[0],)).fetchone() is None):
            raise db.Refusal("page_next is the `next` the triage page returned, unchanged "
                             "(null on its last page): it names the last payment that page "
                             "showed, after this page's start. Nothing was recorded: answer "
                             "again with the page's `next`")
    else:
        # the answer that finishes the judgment gives every handed-over document its
        # verdict (final review FW-I1): a judgment finished without one never covers
        # its handover, and would be started again forever. Checked before any write
        given = {_doc_key(k) for k in documents}
        missing = [d for d in _unjudged_handovers(conn, p, j) if str(d) not in given]
        if missing:
            many = len(missing) > 1
            them, theirs = ("them", "their verdicts") if many else ("it", "its verdict")
            raise db.Refusal(
                f"the operator handed over {'documents' if many else 'document'} "
                f"{', '.join(str(d) for d in missing)}, and this last page's answer gives "
                f"{them} no verdict. Judge {them}, then answer again for the same page with "
                f"{theirs} under documents. Nothing was recorded")
    asks.record_verdicts(conn, p["pass_id"], documents)
    if nxt:
        conn.execute("UPDATE passes SET judge_after=? WHERE pass_id=?",
                     (json.dumps(nxt), p["pass_id"]))
        _credit_page(conn, token, p, after)
        return
    n = judged.get("triage_remaining")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise db.Refusal("triage_remaining is the last page's `remaining` (0 when every page "
                         "was judged)")
    _, refused = steps._finish_tx(conn, token, "judge", counts={"triage_remaining": n})
    assert refused is None
    conn.execute("UPDATE passes SET judge_after=NULL WHERE pass_id=?", (p["pass_id"],))
    _credit_page(conn, token, p, after)


def _credit_page(conn, token, p, after) -> None:
    """A judge page answered: `judge:<epoch>:<its start>` — 0, or a validated page_next.
    Within an epoch the starts strictly increase over the pids, so the keys are finite."""
    credit(conn, token, p["pass_id"], f"judge:{p['judge_epoch']}:{after[0] if after else 0}")


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
        credit(conn, token, p["pass_id"], "file:unit")
    return {"filed": True}


def status(conn, job_id) -> dict:
    """Never a claim; it stamps the run complete, as job_next's complete does
    (ha-casa-app#1180; design delta §3; S7 §10): may this job end now? `done` by THE
    predicate job_next answers `complete` on (`done`). An operator-message turn in the
    job's topic calls it last, so a completion Casa refused (unread inbound) is re-issued
    from the store."""
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise db.Refusal("job_id is the `Job id:` line of your brief, as given")
    with db.tx(conn):
        ok = done(conn, job_id)
        if ok:
            conn.execute("INSERT OR IGNORE INTO runs(job_id, passes) VALUES (?, 0)",
                         (job_id,))
            conn.execute("UPDATE runs SET completed_at=coalesce(completed_at, ?) WHERE"
                         " job_id=?", (db.now(), job_id))
        return {"done": ok, "text": run_end(conn, job_id)[0] if ok else None}


RUN_FINISHED = "Accounting work finished."
TOPIC_MAX = 200     # one topic line: Casa keeps a summary's or completion's first line, cut
                    # at 300 characters; the full stop line is posted by the job (S7 §5)


def run_end(conn, job_id) -> tuple:
    """THE closing words of job run `job_id` (PLAY T7 F2): (the completion's text, its
    progress summary), shared by job_next's `complete` and job_status. "Finished" only
    when every pass of the run finished; otherwise each pass of the run that ended
    stopped, interrupted or failed, with its reason, in the order they began. A pass of
    the run is one the run held when it ended (every cursor end is under the holder's
    claim), or one a claim of the run stopped (`stopped_by`, stop_exhausted_pass)."""
    import views
    out = []
    for r in conn.execute(
            "SELECT outcome, report_json FROM passes WHERE protocol='job' AND ended_at IS NOT"
            " NULL AND outcome<>'complete' AND (holder_job=? OR"
            " json_extract(report_json, '$.stopped_by')=?) ORDER BY generation, pass_id",
            (job_id, job_id)):
        line = _end_line(r["outcome"], json.loads(r["report_json"] or "{}"))
        if line not in out:
            out.append(line)
    if not out:
        return RUN_FINISHED, WORDS["complete"]
    text = views.clip(" ".join(out), TOPIC_MAX)
    return text, text


def _end_line(outcome, rep) -> str:
    """One pass's end, in the operator's words (the stop line's reason: asks._stop_line)."""
    if outcome == "stopped":
        reason = ("it kept stopping" if rep.get("adoptions_exhausted")
                  else " ".join(str(rep.get("stopped_reason") or "").split()).rstrip(". "))
        return f"Accounting check stopped: {reason}." if reason else "Accounting check stopped."
    if outcome == "interrupted":
        if "checked" in rep and "total" in rep:
            return (f"Accounting check interrupted: {int(rep['checked'])} of "
                    f"{int(rep['total'])} new payments checked.")
        return "Accounting check interrupted before it finished."
    return "Accounting check failed."


def _account(conn, token, out) -> None:
    """Mutates `out` in place: next_unit returns the same dict. The budget and progress
    are the batch's — every claim of it (`claims.batch`)."""
    c = conn.execute("SELECT * FROM claims WHERE gen=?", (token,)).fetchone()
    spent = conn.execute("SELECT sum(spent) FROM claims WHERE batch=?",
                         (c["batch"],)).fetchone()[0]
    cost = UNIT_COST.get(out["unit"], 0)
    if cost and spent > 0 and spent + cost > TURNS_PER_BATCH - BATCH_RESERVE:
        out.clear()
        out["unit"] = "end-batch"           # the unit is handed again next batch (INV-J4)
        cost = 0
    ending = out["unit"] in ("end-batch", "complete")
    conn.execute("UPDATE claims SET spent=spent+?, closed=? WHERE gen=?",
                 (cost, int(ending or c["closed"]), token))
    # INV-J8 (spec §15): a credit, in any pass, earned under a claim of this batch
    progressed = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM credits WHERE gen IN (SELECT gen FROM claims WHERE"
        " batch=?))", (c["batch"],)).fetchone()[0] == 1
    left = measure(conn)
    report = ending or (progressed and not c["reported"])
    if report:
        conn.execute("UPDATE claims SET reported=1 WHERE gen=?", (token,))
    summary = (run_end(conn, c["job_id"])[1] if out["unit"] == "complete"
               else _summary(out["unit"], left))
    out["progress"] = {"summary": summary, "progressed": progressed,
                       "done": None, "remaining": left or None}
    out["report"] = report
    out["pass_token"] = token


WORDS = {"probes": "Reading the bank", "snapshot": "Importing the bank read",
         "sweep": "Bringing the bank ledger up to date", "gmail-probe": "Checking Gmail",
         "filing": "Filing emailed documents", "item": "Searching Gmail for an invoice",
         "judge": "Matching documents to payments", "post": "Posting results",
         "view": "Posting the status sheet", "build": "Building the package",
         "deliver": "Sending the package", "end-batch": "Batch done",
         "complete": "All accounting work done"}


def _summary(unit, left) -> str:
    return WORDS[unit] + (f" · {left} payment{'s' if left != 1 else ''} left to search"
                          if left else "")
