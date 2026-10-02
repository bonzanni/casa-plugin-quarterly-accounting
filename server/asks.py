"""S2 (spec §6): what Ellen asks for, recorded before start_job so no ask is lost, and
what becomes of it. A request is queued, taken by the live job pass only (INV-J9), done
at that pass's end with its outcome, and reported once its result was shown."""
from __future__ import annotations

import json

import db
import passes

JOB = "quarterly-accounting:work"
START = {"job": JOB, "task": "Run the accounting work that is waiting.", "context": ""}
LINES = {"check": "Checking the bank and your email — I'll send the result here.",
         "handover": "Filed. Checking it against the payments — I'll tell you shortly.",
         "cron": ""}


def request_work(conn, kind, trigger, doc_ids=None) -> dict:
    if kind not in ("check", "handover"):
        raise db.Refusal("kind is 'check' or 'handover'")
    if trigger not in ("cron", "operator"):
        raise db.Refusal("trigger is 'cron' or 'operator'")
    ids = list(doc_ids or [])
    if kind == "handover" and (not ids or not all(isinstance(i, int) and not isinstance(i, bool)
                                                  for i in ids)):
        raise db.Refusal("a handover names the documents you just filed: doc_ids=[…]")
    with db.tx(conn):
        rid = conn.execute("INSERT INTO work_requests(kind, trigger, doc_ids_json, created_seq,"
                           " created_at, state) VALUES (?,?,?,?,?, 'queued')",
                           (kind, trigger, json.dumps(ids), db.next_seq(conn),
                            db.now())).lastrowid
    line = LINES["cron"] if trigger == "cron" and kind == "check" else LINES[kind]
    return {"request_id": rid, "line": line, "start_job": dict(START)}


def request_package(conn, quarter, channel) -> dict:
    """The request half of begin_pass(trigger="package"): kept whatever happens next."""
    import dates
    if channel not in ("telegram", "email"):
        raise db.Refusal("a package is asked for with its channel: 'telegram' or 'email'")
    dates.parse_quarter(quarter)
    label = dates.quarter_label(quarter)
    with db.tx(conn):
        open_ = conn.execute("SELECT request_id FROM package_requests WHERE quarter=? AND"
                             " state IN ('queued', 'snapshot')", (quarter,)).fetchone()
        if open_ is not None:
            conn.execute("UPDATE package_requests SET channel=?, updated_at=? WHERE"
                         " request_id=?", (channel, db.now(), open_[0]))
            return {"status": "already", "start_job": dict(START),
                    "line": f"The {label} package is already on its way — it follows when the "
                            "check is done."}
        passes._open_request(conn, quarter, channel)
    return {"status": "asked", "start_job": dict(START),
            "line": f"Checking the bank and your email for {label} — the package follows "
                    "when that's done."}


def take_queued(conn, pass_id) -> list:
    assert conn.in_transaction
    p = conn.execute("SELECT trigger FROM passes WHERE pass_id=?", (pass_id,)).fetchone()
    if p is None or p["trigger"] == "package":
        return []                                   # a package round serves its request only
    ids = [r[0] for r in conn.execute("SELECT request_id FROM work_requests WHERE"
                                      " state='queued' ORDER BY request_id")]
    for i in ids:
        conn.execute("UPDATE work_requests SET state='taken', pass_id=? WHERE request_id=?",
                     (pass_id, i))
    if any(conn.execute("SELECT 1 FROM work_requests WHERE request_id=? AND kind='handover'",
                        (i,)).fetchone() for i in ids):
        conn.execute("UPDATE passes SET judge_after=NULL WHERE pass_id=?", (pass_id,))
    return ids


def settle_taken(conn, pass_id, outcome) -> None:
    assert conn.in_transaction
    conn.execute("UPDATE work_requests SET state='done', outcome=? WHERE pass_id=? AND"
                 " state='taken'", (outcome, pass_id))
    # a cron check with nothing to show is reported at once (spec §6.4)
    conn.execute("UPDATE work_requests SET state='reported' WHERE pass_id=? AND state='done'"
                 " AND kind='check' AND trigger='cron' AND outcome IN ('complete',"
                 " 'interrupted')", (pass_id,))


def requeue_taken(conn, pass_id) -> None:
    assert conn.in_transaction
    conn.execute("UPDATE work_requests SET state='queued', pass_id=NULL WHERE pass_id=? AND"
                 " state='taken'", (pass_id,))


def record_verdicts(conn, pass_id, documents) -> None:
    """The judge's per-document verdicts (spec §5.2) on the pass's taken handovers, each
    tagged with the running judgment's started_seq: a verdict covers only the judgment
    that made it (INV-J12; Astra plan-r1)."""
    assert conn.in_transaction
    j = conn.execute("SELECT started_seq FROM pass_steps WHERE pass_id=? AND step='judge'"
                     " AND finished_at IS NULL", (pass_id,)).fetchone()
    if j is None:
        raise db.Refusal("no judge step is running")
    allowed = {"matched", "proposed", "no-payment-yet", "clash", "unreadable",
               "out-of-range", "irrelevant"}
    for doc_id, verdict in (documents or {}).items():
        if verdict not in allowed:
            raise db.Refusal(f"a document's verdict is one of {', '.join(sorted(allowed))}")
        for r in conn.execute("SELECT request_id, doc_ids_json, verdicts_json FROM work_requests"
                              " WHERE pass_id=? AND state='taken' AND kind='handover'",
                              (pass_id,)).fetchall():
            if int(doc_id) in json.loads(r["doc_ids_json"]):
                v = json.loads(r["verdicts_json"])
                v[str(int(doc_id))] = {"verdict": verdict, "judge": j["started_seq"]}
                conn.execute("UPDATE work_requests SET verdicts_json=? WHERE request_id=?",
                             (db.canonical(v), r["request_id"]))


def handover_covered(conn, pass_id) -> bool:
    """INV-J12: every taken handover has a whole judgment (work.judge_whole: finished, not
    failed, stopped or out of time, every triage page seen) that started after it was
    asked, and a verdict from that judgment for each of its documents."""
    import work
    assert conn.in_transaction
    j = conn.execute("SELECT started_seq, finished_at, finish_json FROM pass_steps WHERE"
                     " pass_id=? AND step='judge'", (pass_id,)).fetchone()
    for r in conn.execute("SELECT created_seq, doc_ids_json, verdicts_json FROM work_requests"
                          " WHERE pass_id=? AND state='taken' AND kind='handover'",
                          (pass_id,)).fetchall():
        if not work.judge_whole(j) or j["started_seq"] is None \
                or j["started_seq"] <= r["created_seq"]:
            return False
        v = json.loads(r["verdicts_json"])
        if any((v.get(str(d)) or {}).get("judge") != j["started_seq"]
               for d in json.loads(r["doc_ids_json"])):
            return False
    return True
