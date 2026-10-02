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
    """The adoption budget is spent (spec §6.3): the pass ends `stopped` through
    _end_pass_tx, which dispositions what it serves in this same transaction — its work
    requests `done`/`stopped` (asks.settle_taken), its package request closed `stopped`
    with its package-stopped notice (_hand_over → _close)."""
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
        return out


def _choose(conn, token) -> dict:
    import asks, steps
    for _ in range(8):                      # passes may end and the next begin in one call
        p = live_job_pass(conn)
        if p is not None and p["trigger"] != "package":
            _take(conn, token, p)
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


def _take(conn, token, p) -> None:
    """The live pass takes the queued requests (INV-J9). A handover taken while the
    pass's judge step runs restarts that judgment now (spec §5.2, fix round 1): a new
    started_seq and started_gen, the page cursor reset, so no verdict of the judgment
    that started before the handover can cover it."""
    import asks, steps
    ids = asks.take_queued(conn, p["pass_id"])
    if not ids or conn.execute(
            "SELECT 1 FROM work_requests WHERE pass_id=? AND kind='handover' AND request_id IN"
            " (%s)" % ",".join("?" * len(ids)), (p["pass_id"], *ids)).fetchone() is None:
        return
    j = _step(conn, p, "judge")
    if j is not None and j["finished_at"] is None:
        # take_queued has already reset judge_after (the page cursor) for a handover
        steps._start_tx(conn, token, "judge", {}, restart_running=True)


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
        return _stop(conn, token, p, req, reason)
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
    return {"unit": "judge", "judgment": j["started_seq"], "after": after,
            "quarter": req["quarter"] if req else None, "documents_first": firsts}


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
