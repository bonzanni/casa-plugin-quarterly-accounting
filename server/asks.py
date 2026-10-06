"""S2 (spec §6): what Ellen asks for, recorded before start_job so no ask is lost, and
what becomes of it. A request is queued, taken by the live job pass only (INV-J9), done
at that pass's end with its outcome, and reported once its result was shown."""
from __future__ import annotations

import json

import db
import passes

JOB = "quarterly-accounting:work"
START = {"job": JOB, "task": "Run the accounting work that is waiting.", "context": ""}
LINES = {"check": "Checking the bank and your email — I'll post the result here.",
         "handover": "Filed. Checking it against the payments — I'll post what I find."}
BUSY_NO_RESULT = "The accounting job was busy just now. If no result comes, ask again."
DONE_ALREADY = "That's done already — ask me for the status to see it."


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
        # a handover names documents that are filed (final review FW-I1): an id the
        # judge can never find would leave the handover owed a verdict forever
        unknown = [i for i in ids if conn.execute("SELECT 1 FROM documents WHERE doc_id=?",
                                                  (i,)).fetchone() is None]
        if unknown:
            raise db.Refusal(
                f"{'documents' if len(unknown) > 1 else 'document'} "
                f"{', '.join(str(i) for i in unknown)} {'are' if len(unknown) > 1 else 'is'} "
                "not among the filed documents: pass the ids the filing gave you. Nothing "
                "was asked")
        rid = conn.execute("INSERT INTO work_requests(kind, trigger, doc_ids_json, created_seq,"
                           " created_at, state) VALUES (?,?,?,?,?, 'queued')",
                           (kind, trigger, json.dumps(ids), db.next_seq(conn),
                            db.now())).lastrowid
    return {"request_id": rid, "kind": "work", "line": LINES[kind], "start_job": dict(START)}


def request_package(conn, quarter) -> dict:
    """The request half of begin_pass(trigger="package"): kept whatever happens next. A
    package goes to Telegram only (S7 §4: never email)."""
    import dates
    dates.parse_quarter(quarter)
    label = dates.quarter_label(quarter)
    with db.tx(conn):
        open_ = conn.execute("SELECT request_id FROM package_requests WHERE quarter=? AND"
                             " state IN ('queued', 'snapshot')", (quarter,)).fetchone()
        if open_ is not None:
            # §10: a repeat renews the ask, so a closure of a failed run's asks spares it
            conn.execute("UPDATE package_requests SET asked_seq=?, updated_at=? WHERE"
                         " request_id=?", (db.next_seq(conn), db.now(), open_[0]))
            return {"status": "already", "request_id": open_[0], "kind": "package",
                    "start_job": dict(START),
                    "line": f"The {label} package is already on its way — it follows when the "
                            "check is done."}
        rid = passes._open_request(conn, quarter)["id"]
    return {"status": "asked", "request_id": rid, "kind": "package", "start_job": dict(START),
            "line": f"Checking the bank and your email for {label} — the package follows "
                    "when that's done."}


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
    if kind not in ("work", "package"):
        raise db.Refusal("kind is 'work' or 'package', as the ask returned it")
    table = "work_requests" if kind == "work" else "package_requests"
    r = conn.execute(f"SELECT * FROM {table} WHERE request_id=?", (request_id,)).fetchone()
    if r is None:
        raise db.Refusal("there is no such ask: pass the request_id the ask returned")
    live = _live_run(conn)
    if kind == "work":
        state = {"taken": "taken", "queued": "queued"}.get(r["state"], "done")
        own = LINES[r["kind"]]
    else:
        state = {"snapshot": "taken", "queued": "queued"}.get(r["state"], "done")
        import dates
        own = (f"Checking the bank and your email for {dates.quarter_label(r['quarter'])} — "
               "the package follows when that's done.")
    if state == "taken" or (state == "queued" and live):
        line = own
    elif state == "queued":
        line = BUSY_NO_RESULT
    else:
        line = DONE_ALREADY
    return {"state": state, "live_run": live, "line": line}


def take_queued(conn, pass_id, late=False) -> list:
    """The queued work requests `pass_id` takes: all of them as it begins; once it is live
    (`late`), only while it has taken fewer than job.LATE_TAKES_MAX — each needs another bank
    read (F's condition 2) — and the rest stay queued for the next pass (design r1, Terra
    S1: the pass's acquisitions stay bounded)."""
    import job
    assert conn.in_transaction
    p = conn.execute("SELECT trigger, late_takes FROM passes WHERE pass_id=?",
                     (pass_id,)).fetchone()
    if p is None or p["trigger"] == "package":
        return []                                   # a package round serves its request only
    ids = [r[0] for r in conn.execute("SELECT request_id FROM work_requests WHERE"
                                      " state='queued' ORDER BY request_id")]
    if late:
        ids = ids[:max(0, job.LATE_TAKES_MAX - p["late_takes"])]
        conn.execute("UPDATE passes SET late_takes=late_takes+? WHERE pass_id=?",
                     (len(ids), pass_id))
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
        try:
            if isinstance(doc_id, bool):
                raise ValueError(doc_id)
            int(doc_id)
        except (TypeError, ValueError):
            raise db.Refusal(f"documents are keyed by their document id (a number), not "
                             f"{str(doc_id)[:40]!r}") from None
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


# --- results (spec §6.4; S7 §5 posts them from the job) -------------------------------
KEPT_STOPPING = "The accounting check kept stopping — ask again when you want me to retry."
STOPPED = "The accounting check stopped"
# never an offer phrase reply.py parses ("send it again" is a package resend)
NOT_FOUND = "I can't find that document in what I've filed — please send the file once more."
NOT_AN_INVOICE = "Filed. It doesn't look like an invoice for any payment — say if it is one."
NEXT_CHECK = "Filed. I'll match it at the next check."


def mark_reported(conn, render_id) -> None:
    """A done request is reported once every rendering of its result was delivered
    (spec §6.4: consumed only when shown). Inside mark_rendering_delivered's
    transaction, after it stamped the delivery."""
    assert conn.in_transaction
    for r in conn.execute("SELECT request_id, render_ids_json FROM work_requests WHERE"
                          " state='done'").fetchall():
        ids = json.loads(r["render_ids_json"])
        if render_id in ids and not _undelivered(conn, ids):
            conn.execute("UPDATE work_requests SET state='reported' WHERE request_id=?",
                         (r["request_id"],))


def _undelivered(conn, ids) -> list:
    if not ids:
        return []
    rows = {x["render_id"]: x for x in conn.execute(
        "SELECT render_id, text, delivered_at FROM renders WHERE render_id IN (%s)"
        % ",".join("?" * len(ids)), ids)}
    return [{"render_id": i, "text": rows[i]["text"]} for i in ids
            if i in rows and rows[i]["delivered_at"] is None]


def _result_class(r):
    """What the done request's result is: a stop line, the status view, the handover's
    case lines, or nothing (a cron check served: `speak` says the rest)."""
    if r["outcome"] in ("stopped", "failed"):
        return "stop"
    if r["outcome"] in ("complete", "interrupted"):
        if r["kind"] == "handover":
            return "handover"
        if r["trigger"] == "operator":
            return "status"
    return None


def _result_tx(conn, request_id, made=None) -> list:
    """The done request's result, as pages ({render_id, text}) still to be shown. The
    renderings are created once and stored on the request, in order. Requests the same
    pass served with the same stop line or status view share its renderings, and so do
    operator checks given their status view in the same call (`made`: one state, one
    view). A request with nothing left to show is reported."""
    made = {} if made is None else made
    assert conn.in_transaction
    r = conn.execute("SELECT * FROM work_requests WHERE request_id=?",
                     (request_id,)).fetchone()
    if r is None or r["state"] != "done":
        return []
    ids = json.loads(r["render_ids_json"])
    if not ids:
        cls = _result_class(r)
        ids = ((_sibling_ids(conn, r, cls) or (made.get(cls) if cls == "status" else None)
                or _render_result(conn, r, cls)) if cls else [])
        if cls == "status":
            made.setdefault(cls, ids)
        if ids:
            conn.execute("UPDATE work_requests SET render_ids_json=? WHERE request_id=?",
                         (json.dumps(ids), request_id))
    pages = _undelivered(conn, ids)
    if not pages:
        conn.execute("UPDATE work_requests SET state='reported' WHERE request_id=?",
                     (request_id,))
    return pages


def _sibling_ids(conn, r, cls):
    if cls not in ("stop", "status") or r["pass_id"] is None:
        return None
    for s in conn.execute("SELECT * FROM work_requests WHERE pass_id=? AND request_id<>? AND"
                          " state IN ('done', 'reported') ORDER BY request_id",
                          (r["pass_id"], r["request_id"])).fetchall():
        ids = json.loads(s["render_ids_json"])
        if ids and _result_class(s) == cls:
            return ids
    return None


def _render_result(conn, r, cls) -> list:
    import views
    if cls == "status":
        return [views.review_in_tx(conn, "status")["render_id"]]
    if cls == "stop":
        return [_insert(conn, "job-stop", t, r) for t in _pages([_stop_line(conn, r)])]
    return [_insert(conn, "handover", t, r) for t in _pages(_case_lines(conn, r))]


def _insert(conn, kind, text, r) -> str:
    rid = f"r{db.next_seq(conn)}"
    conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                 " membership_json) VALUES (?,?,?,?,?, '[]')",
                 (rid, kind, db.canonical({"work_request": r["request_id"]}), db.now(), text))
    return rid


def _pages(lines) -> list:
    """Every line, paged, never clipped (Terra plan-r7 S2): each page is fit_lines's
    whole leading lines; a single line that does not fit is clipped by fit_lines."""
    import views
    rest, out = list(lines), []
    while rest:
        page, whole = views.fit_lines(rest)
        out.append("\n".join(page))
        rest = rest[max(whole, 1):]
    return out


def _stop_line(conn, r) -> str:
    """The reason is the pass's stored report's (job._stop keeps it there): the probe
    that carried it is overwritten by the next acquisition."""
    p = conn.execute("SELECT report_json FROM passes WHERE pass_id=?",
                     (r["pass_id"],)).fetchone()
    rep = json.loads((p["report_json"] if p else None) or "{}")
    if rep.get("adoptions_exhausted"):
        return KEPT_STOPPING
    reason = str(rep.get("stopped_reason") or "").strip().rstrip(".")
    import views
    reason = views.field(reason, 300)
    return f"{STOPPED}: {reason}." if reason else f"{STOPPED}."


def _case_lines(conn, r) -> list:
    """One line per handed-over document, from its recorded pairing (steps._pairing) and,
    when unpaired, the judge's verdict — the cases of SKILL.md's handover section."""
    import steps
    docs = json.loads(r["doc_ids_json"])
    verdicts = json.loads(r["verdicts_json"])
    lines = []
    for d in docs:
        doc = conn.execute("SELECT * FROM documents WHERE doc_id=?", (d,)).fetchone()
        line = _case(steps._pairing(conn, d), (verdicts.get(str(d)) or {}).get("verdict"),
                     doc)
        lines.append(f"{_doc_label(d, doc)}: {line}" if len(docs) > 1 else line)
    return lines


def _amount(minor, currency):
    import amounts, views
    if minor is None or not currency:
        return None
    return views.esc(amounts.fmt(abs(int(minor)), currency))


def _case(pairing, verdict, doc) -> str:
    import dates
    kind = pairing["pairing"]
    if kind == "unknown":
        return NOT_FOUND
    if kind == "matched":
        p = pairing["payment"]
        amt = _amount(p["amount_minor"], p["currency"]) or "matching"
        if not p["date"]:
            return f"Matched to the {amt} payment."
        q = dates.parse_quarter(dates.quarter_of(p["date"]))[1]
        return f"Matched to the {amt} payment of {dates.short_day(p['date'])}. Q{q}."
    if kind == "proposed":
        return "Filed. It could fit more than one payment — it's in 'anything I should check?'."
    if kind == "irrelevant" or verdict == "irrelevant":
        return NOT_AN_INVOICE
    amt = _amount(doc["amount_minor"], doc["currency"]) if doc is not None else None
    if verdict == "no-payment-yet":
        return (f"Filed. No payment matches {amt or 'it'} yet — the charge may not have posted. "
                "It'll match when it appears.")
    if verdict == "clash":
        return (f"Filed. I see {'a ' + amt + ' payment' if amt else 'a payment'} it fits, but "
                "it's already matched to another document. Which one is right?")
    if verdict == "unreadable":
        return ("Filed, but I can't read an amount from it — "
                + (f"is it {amt}?" if amt else "what is the amount?"))
    if verdict == "out-of-range":
        day = doc["document_date"] if doc is not None else None
        q = dates.quarter_of(day) if day else (doc["ingest_quarter"] if doc is not None else None)
        where = f"in {dates.quarter_label(q)}" if q else "in this quarter"
        return (f"Filed. Nothing {where} is close to {amt or 'it'}. Is this for a different "
                "quarter?")
    return NEXT_CHECK


def _doc_label(doc_id, doc) -> str:
    """How a line names its document when the handover held several."""
    import dates, views
    if doc is None:
        return f"Document {doc_id}"
    who = doc["issuer"] or doc["counterparty"]
    parts = [views.field(who)] if who else []
    parts.append(views.KIND_WORD.get(doc["kind"], "document"))
    if doc["document_number"]:
        parts.append(views.field(doc["document_number"]))
    label = " ".join(parts)
    return f"{label} ({dates.short_day(doc['document_date'])})" if doc["document_date"] else label
