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
