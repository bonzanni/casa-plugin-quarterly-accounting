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


# --- the relay: job_report (spec §6.3–§6.4) ------------------------------------------
ORPHANED = "The accounting check stopped before it finished — I'm starting it again."
KEPT_STOPPING = "The accounting check kept stopping — ask again when you want me to retry."
STOPPED = "The accounting check stopped"
CANCELLED = "The accounting check was cancelled — ask again when you want it."
CANCEL_REASON = "you cancelled the check"
# never an offer phrase reply.py parses ("send it again" is a package resend)
NOT_FOUND = "I can't find that document in what I've filed — please send the file once more."
NOT_AN_INVOICE = "Filed. It doesn't look like an invoice for any payment — say if it is one."
NEXT_CHECK = "Filed. I'll match it at the next check."


def _match_job(conn, job_id):
    """The one recorded job id that starts with `job_id` (≥ 8 characters, a prefix no
    other recorded job id shares: the #17 rule), or None."""
    ids = {r[0] for r in conn.execute("SELECT DISTINCT job_id FROM claims")}
    if job_id in ids:
        return job_id
    hits = [i for i in ids if i.startswith(job_id)]
    return hits[0] if len(hits) == 1 else None


def job_report(conn, job_id=None, status=None) -> dict:
    """Spec §6.4: ONE transaction, under the custody lock (taken first: the store's lock
    order). The orphan handoff, the retry decision, the results, the alerts and the
    package or delivery recovery see one state and commit together."""
    import alerts, job, steps
    if job_id is not None:
        if not isinstance(job_id, str) or len(job_id) < 8:
            raise db.Refusal("job_id is the id the notification names")
        if status not in ("ok", "error", "cancelled"):
            raise db.Refusal("status is 'ok', 'error' or 'cancelled'")
    if conn.in_transaction:
        raise RuntimeError("job_report opens its own transaction")
    out = {"orphaned": False, "start_job": None, "texts": [], "speak": None,
           "continue": None, "line": None, "more": False}
    with db.custody_lock():
        with db.tx(conn):
            if job_id is not None:
                who = _match_job(conn, job_id)
                p = job.live_job_pass(conn)          # an ended pass is never live
                drain = conn.execute("SELECT value FROM meta WHERE key='drain'").fetchone()
                if who is not None and p is not None and p["holder_job"] == who:
                    if status == "cancelled":
                        _withdraw(conn, p)          # #38: the operator ended it
                    else:
                        # the job holding the live pass ended: the pass is orphaned, stays
                        # adoptable, its requests stay taken, and the drain is cleared
                        conn.execute("UPDATE passes SET orphaned_by=? WHERE pass_id=?",
                                     (who, p["pass_id"]))
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES"
                                 " ('drain','none')")
                elif who is not None and drain is not None and drain[0] == who:
                    if status == "cancelled":
                        _withdraw(conn, None)
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES"
                                 " ('drain','none')")
                # any other id (a late or replayed notice): nothing changes
            p = job.live_job_pass(conn)
            drain = (conn.execute("SELECT value FROM meta WHERE key='drain'").fetchone()
                     or ["none"])[0]
            orphaned = p is not None and p["orphaned_by"] is not None
            queued = conn.execute("SELECT 1 FROM work_requests WHERE state='queued' UNION ALL"
                                  " SELECT 1 FROM package_requests WHERE state='queued'"
                                  ).fetchone()
            out["orphaned"] = orphaned
            # the standing retry (Astra r3 S1): offered on every report while it holds
            if drain == "none" and (orphaned or queued is not None):
                out["start_job"] = dict(START)
                if orphaned:
                    out["line"] = ORPHANED
            seen, made, pages, views_last = set(), {}, [], []
            for r in conn.execute("SELECT * FROM work_requests WHERE state='done'"
                                  " ORDER BY request_id").fetchall():
                # the operator's reply is about the LAST rendering delivered
                # (db.last_delivered skips handover and stop pages, which offer nothing
                # to answer): a status view still goes after every handover and stop page
                into = views_last if _result_class(r) == "status" else pages
                for page in _result_tx(conn, r["request_id"], made):   # a list of pages
                    if page["render_id"] not in seen:
                        seen.add(page["render_id"])
                        into.append(page)
            pages += views_last
            sends = steps._claim_sends_tx(conn)
            out["continue"] = sends.get("continue")
            notice = sends.get("_notice")
            out["speak"] = alerts.pending_in_tx(conn, must=[notice] if notice else None)
            _bounded(out, pages)
            # R5 (diff round 1): what this call hands out binds a later reply only when
            # it is a notification's (job_id given: no operator words are pending). A
            # no-id call runs in an operator's turn, after their message: what it hands
            # out is non-binding, so their words never bind to a result sent after they
            # wrote. The latest hand-out wins: a re-offer re-stamps the rendering
            handed = [x["render_id"] for x in out["texts"]] + (
                [out["speak"]["render_id"]] if out["speak"] else [])
            if handed:
                conn.execute("UPDATE renders SET binding=? WHERE render_id IN (%s)"
                             % ",".join("?" * len(handed)),
                             (1 if job_id is not None else 0, *handed))
    return out


def _withdraw(conn, p) -> None:
    """#38: the operator's /cancel ended the job that holds the live pass `p` (or, with
    `p` None, the drain's job, before it took a pass). Unlike an error, nothing restarts:
    the pass ends `stopped` through the pass-ending path, and every open ask — queued, or
    taken by `p`; work or package — is withdrawn as `stopped`. The work requests share
    ONE stop rendering, relayed through `texts` and consumed by mark_rendering_delivered
    as every result is; a package request gets its package-stopped notice (`speak`). No
    job claim made this, so no INV-J8 credit is earned."""
    assert conn.in_transaction
    pid = p["pass_id"] if p is not None else None
    ids = [r[0] for r in conn.execute(
        "SELECT request_id FROM work_requests WHERE state='queued' OR (state='taken' AND"
        " pass_id IS ?) ORDER BY request_id", (pid,))]
    for req in conn.execute("SELECT * FROM package_requests WHERE state='queued' OR"
                            " (state='snapshot' AND pass_id IS ?) ORDER BY request_id",
                            (pid,)).fetchall():
        passes._close(conn, req, "stopped", "stopped", reason=CANCEL_REASON)
    if p is not None:
        gen = passes._marker(conn)["generation"]
        passes._end_pass_tx(conn, gen, "stopped", {"stopped_reason": CANCEL_REASON},
                            credit=False)
    conn.execute("UPDATE work_requests SET state='done', outcome='stopped' WHERE"
                 " state='queued'")
    if ids:
        rid = _insert(conn, "job-stop", CANCELLED, {"request_id": ids[0]})
        conn.execute("UPDATE work_requests SET render_ids_json=? WHERE request_id IN (%s)"
                     % ",".join("?" * len(ids)), (json.dumps([rid]), *ids))


def _bounded(out, pages) -> None:
    """The answer within budget.RESULT_LIMIT (issue #3), measured whole — start_job,
    line, continue and speak included: the pages in order while they fit, always the
    first. A page left out stays undelivered, so the next call offers it (`more`)."""
    import budget
    out["more"] = False                     # measured as the longer of its two values:
                                            # JSON `false` is a character longer than `true`
    for i, page in enumerate(pages):
        out["texts"].append(page)
        if i > 0 and budget.size(out) > budget.RESULT_LIMIT:
            out["texts"].pop()
            break
    out["more"] = len(out["texts"]) < len(pages)


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
    import amounts
    if minor is None or not currency:
        return None
    return amounts.fmt(abs(int(minor)), currency)


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
