"""S2 (spec §6): what Ellen asks for, recorded before start_job so no ask is lost, and
what becomes of it. A request is queued, taken by the run's live pass only (INV-J9), and
settled reported at that pass's end: the run's one message is its result (simple loop §1)."""
from __future__ import annotations

import json

import db

JOB = "quarterly-accounting:work"
START = {"job": JOB, "task": "Run the accounting work that is waiting.", "context": ""}
LINES = {"check": "Checking the bank and your email — I'll post the result here.",
         "handover": "Filed. Checking it against the payments — I'll post what I find."}
BUSY_NO_RESULT = "The accounting job was busy just now. If no result comes, ask again."
DONE_ALREADY = "That's done already — ask me for the status to see it."


def request_work(conn, kind, trigger, doc_ids=None, quarter=None) -> dict:
    """`quarter` (ruling Q2): an operator check that names a quarter ("check Q2") makes it
    the run's main quarter — the end message's header and its [Get package] quarter."""
    if kind not in ("check", "handover"):
        raise db.Refusal("kind is 'check' or 'handover'")
    if trigger not in ("cron", "operator"):
        raise db.Refusal("trigger is 'cron' or 'operator'")
    if quarter is not None:
        import dates
        if kind != "check":
            raise db.Refusal("quarter goes with a check")
        dates.parse_quarter(quarter)
    ids = list(doc_ids or [])
    if kind == "handover" and (not ids or not all(isinstance(i, int) and not isinstance(i, bool)
                                                  for i in ids)):
        raise db.Refusal("a handover names the documents you just filed: doc_ids=[…]")
    with db.tx(conn):
        # a handover names documents that are filed (final review FW-I1): an id the
        # run can never find would leave the handover owed forever
        unknown = [i for i in ids if conn.execute("SELECT 1 FROM documents WHERE doc_id=?",
                                                  (i,)).fetchone() is None]
        if unknown:
            raise db.Refusal(
                f"{'documents' if len(unknown) > 1 else 'document'} "
                f"{', '.join(str(i) for i in unknown)} {'are' if len(unknown) > 1 else 'is'} "
                "not among the filed documents: pass the ids the filing gave you. Nothing "
                "was asked")
        rid = conn.execute("INSERT INTO work_requests(kind, trigger, doc_ids_json, created_seq,"
                           " created_at, state, quarter) VALUES (?,?,?,?,?, 'queued', ?)",
                           (kind, trigger, json.dumps(ids), db.next_seq(conn),
                            db.now(), quarter)).lastrowid
    return {"request_id": rid, "kind": "work", "line": LINES[kind], "start_job": dict(START)}


def _live_run(conn) -> bool:
    """The latest claim's run is live: its job id has no runs.completed_at (S7 §4)."""
    top = conn.execute("SELECT job_id FROM claims ORDER BY gen DESC LIMIT 1").fetchone()
    if top is None:
        return False
    done_ = conn.execute("SELECT completed_at FROM runs WHERE job_id=?", (top[0],)).fetchone()
    return done_ is None or done_[0] is None


def ask_state(conn, kind, request_id) -> dict:
    """Read-only (S7 §4): will the running job take this ask? `taken`, or `queued` with a
    live run, says the ask's own line; `queued` with none, BUSY_NO_RESULT; anything else
    is `done` (DONE_ALREADY)."""
    if kind != "work":
        raise db.Refusal("kind is 'work', as the ask returned it")
    r = conn.execute("SELECT * FROM work_requests WHERE request_id=?",
                     (request_id,)).fetchone()
    if r is None:
        raise db.Refusal("there is no such ask: pass the request_id the ask returned")
    live = _live_run(conn)
    state = {"taken": "taken", "queued": "queued"}.get(r["state"], "done")
    own = LINES[r["kind"]]
    if state == "taken" or (state == "queued" and live):
        line = own
    elif state == "queued":
        line = BUSY_NO_RESULT
    else:
        line = DONE_ALREADY
    return {"state": state, "live_run": live, "line": line}


def take_queued(conn, pass_id) -> list:
    """The queued work requests the run's pass takes (simple loop §2: one pass per run): all
    of them, at its claim and at every job_next — a handover asked during the run joins
    that run's list (§2.5)."""
    assert conn.in_transaction
    ids = [r[0] for r in conn.execute("SELECT request_id FROM work_requests WHERE"
                                      " state='queued' ORDER BY request_id")]
    for i in ids:
        conn.execute("UPDATE work_requests SET state='taken', pass_id=? WHERE request_id=?",
                     (pass_id, i))
    return ids


def settle_taken(conn, pass_id, outcome) -> None:
    """The run's pass ended (simple loop §2): every request it took is done, and reported
    at once — the run's one message (§1) is its result; nothing else is posted for it."""
    assert conn.in_transaction
    conn.execute("UPDATE work_requests SET state='reported', outcome=? WHERE pass_id=? AND"
                 " state='taken'", (outcome, pass_id))


def requeue_taken(conn, pass_id) -> None:
    """A run interrupted by another job's claim: the requests its pass took are queued
    again, for the interrupting run's pass (job.claim)."""
    assert conn.in_transaction
    conn.execute("UPDATE work_requests SET state='queued', pass_id=NULL WHERE pass_id=? AND"
                 " state='taken'", (pass_id,))
