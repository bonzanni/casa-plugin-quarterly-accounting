"""decide (design rev 17 §2.2 step 4): a vendor group's decisions in one call. The floor
(matches.machine_in_tx) is applied to each entry on its own, in the order given; a refused
entry is reported with its reason and rolls back alone (a savepoint), the others commit.
An earlier entry's write is in the same transaction, so the document it took is taken for
the later ones by construction. "No invoice needed" is never the job's (r9)."""
from __future__ import annotations

import json

import db
import lineage
import matches
import passes

ENTRIES_MAX = 30
OUTCOMES = ("match", "propose", "missing",      # no `not-needed`: the operator's (§2.2, r9)
            "keep", "replace",                 # a handover onto a paired payment (rev 18.4)
            "optional",                        # #97: nice-to-have, by the job's judgement
            "leave")                           # #105: an unsettled classification, left
REASON_MAX = 200
NOT_NEEDED = ("outcome is match, propose, missing or optional: \"no invoice needed\" is the "
              "operator's tap, a KB rule or an expectation of none, never the job's")
UNSETTLED = ("its classification is not settled (no tags, parked, or tags that conflict), "
             "so it is no missing invoice: match or propose a document that fits; for "
             "conflicting tags, optional when its tags and history make clear it needs no "
             "invoice (wage tax tagged payroll and taxes); otherwise leave it")
DATE_READ = ("pass document_date: the date printed on the document you opened (its issue "
             "date), YYYY-MM-DD")


def note_progress(conn, token) -> None:
    """§2.2 `progressed`: the batch persisted work (a decision, a filing, a search) — Casa's
    batch progress report. (A hand-out's own progress is the queues' stamps: queues.settle.)"""
    conn.execute("UPDATE claims SET progressed=1, progressed_seq=? WHERE gen=?",
                 (db.next_seq(conn), int(token)))


def record_outcome(conn, token, pid, outcome, reason=None) -> None:
    """The run's work-list entry for `pid` (run_work, the job of claim `token`) takes
    `outcome`; a payment not on the list: no row, nothing. Marks the batch progressed."""
    job = conn.execute("SELECT job_id FROM claims WHERE gen=?", (int(token),)).fetchone()
    # the work row handed out for this payment: its own pid, or a pid since merged into it
    rows = [r for r in conn.execute("SELECT pid FROM run_work WHERE job_id=?", (job[0],))
            if lineage.resolve_pid(conn, r["pid"]) == pid] if job is not None else []
    for r in rows:
        conn.execute("UPDATE run_work SET outcome=?, reason=?, closed_seq=? WHERE job_id=? AND"
                     " pid=?", (outcome, reason, db.next_seq(conn), job[0], r["pid"]))
    note_progress(conn, token)


def _handover_onto_pairing(conn, token, pid, e) -> bool:
    """Rev 18.4 §R18.3: a handover entry whose payment already holds a document (a match or
    a proposal) is answered keep or replace — never a match or proposal of another document
    by the job (nothing is replaced without the operator's tap)."""
    import loop
    import queues
    import replace
    job_id = queues.job_of(conn, token)
    if job_id is None or replace.current(conn, pid) is None:
        return False
    why = conn.execute("SELECT why FROM run_work WHERE job_id=? AND pid=?",
                       (job_id, pid)).fetchone()
    # e2 (Astra S2): a handed document is never committed onto a paired payment by the job,
    # whatever listed the payment
    return (why is not None and why[0] == "handover") or \
        e.get("doc_id") in loop.run_handover_docs(conn, job_id)


def _handover_entry(conn, token, pid, e, outcome) -> dict:
    """keep | replace (rev 18.4 §R18.3), checked against the payment as handed out."""
    import queues
    import replace
    proj = lineage.projection(conn, pid)
    if _int(e, "expected_revision") != proj["revision"]:
        raise db.Refusal("this payment changed since it was handed out; decide it again "
                         "with the revision job_next gives now")
    if outcome not in ("keep", "replace"):
        raise db.Refusal("this payment already has a document: answer replace (the handed "
                         "document belongs to it — the operator is asked) or keep")
    if replace.current(conn, pid) is None:
        raise db.Refusal(f"{outcome} is for a payment that already has a document")
    if outcome == "keep":
        return {"applied": True, "wrote": False}
    replace.ask_in_tx(conn, queues.job_of(conn, token), pid, _int(e, "doc_id"))
    return {"applied": True, "wrote": True}


def _int(e, k):
    v = e.get(k)
    if isinstance(v, bool) or not isinstance(v, int):
        raise db.Refusal(f"{k} must be an integer")
    return v


def _alternatives(e) -> tuple:
    alts = e.get("alternatives") or ()
    if not isinstance(alts, (list, tuple)) or any(
            isinstance(a, bool) or not isinstance(a, int) for a in alts):
        raise db.Refusal("alternatives is a list of document ids")
    return tuple(alts)


def _labels(e) -> tuple:
    labels = e.get("labels") or ("clean",)
    if not isinstance(labels, (list, tuple)) or not all(isinstance(x, str) for x in labels):
        raise db.Refusal("labels is a list of label names")
    return tuple(labels)


def _revoke_judgement(conn, pid) -> None:
    """#97 (d2, Astra S1): a later decision of the job (match, propose, missing) supersedes
    its nice-to-have judgement: a revoking `judge` entry (no basis), so restored facts never
    bring the old judgement back."""
    r = conn.execute("SELECT fp FROM log WHERE pid=? AND kind='judge' ORDER BY seq DESC"
                     " LIMIT 1", (pid,)).fetchone()
    if r is not None and r["fp"] is not None:
        lineage.append(conn, pid, "judge", "auto", detail="superseded by a later decision")
        lineage.settle(conn, pid)


def optional_in_tx(conn, pid, expected_revision, reason) -> dict:
    """#97 (operator ruling, a ruled exception — not a licence): the job judged this
    payment's document nice-to-have (money back from a tax authority), with its reason.
    Recorded as a `judge` log entry bound to the facts it judged (lineage.judgement_basis):
    it applies while the payment keeps them; new facts bring the default back and the
    payment onto the work list. Never on a payment holding a pairing or exempted."""
    if not reason:
        raise db.Refusal("optional needs a reason: what makes this document nice-to-have")
    proj, row, _exp = matches.decidable(conn, pid, expected_revision)
    st = lineage.fold_of(conn, pid)
    if st.exemption is not None:
        raise db.Refusal("the operator exempted this payment: nothing to judge")
    if any(c.state in ("matched", "proposed", "conflicted") for c in st.cands.values()):
        raise db.Refusal("this payment holds a pairing: decide it as match or propose")
    tags = json.loads(proj["class_tags_json"]) if proj["class_tags_json"] else []
    lineage.append(conn, pid, "judge", "auto", fp=lineage.judgement_basis(row, tags),
                   detail=reason)
    lineage.settle(conn, pid)
    return {"applied": True, "wrote": True}


def missing_in_tx(conn, pid, expected_revision, reason) -> dict:
    """A `missing` decision, inside the caller's transaction: the floor's own preconditions
    (matches.decidable: as handed out, managed, fresh, a document expected, booked), no
    operator exemption, and no pairing held (§2.2: missing is for a payment no document
    fits)."""
    proj, _row, _exp = matches.decidable(conn, pid, expected_revision)
    st = lineage.fold_of(conn, pid)
    if st.exemption is not None:
        raise db.Refusal("the operator exempted this payment: it is not missing")
    if any(c.state in ("matched", "proposed", "conflicted") for c in st.cands.values()):
        raise db.Refusal("this payment holds a pairing: keep it (match the same document) or "
                         "propose; missing is for a payment no document fits")
    return {"applied": True, "wrote": False, "status": proj["status"]}


def _entry(conn, token, e, seen) -> dict:
    """One entry, inside its savepoint. Every refusal raises, so the savepoint rolls back
    whatever the entry wrote; the exemption's residue is the one non-raising refusal."""
    if not isinstance(e, dict):
        raise db.Refusal("an entry is {pid, outcome, expected_revision, …}")
    pid, rev, outcome = _int(e, "pid"), _int(e, "expected_revision"), e.get("outcome")
    if outcome not in OUTCOMES:
        raise db.Refusal(NOT_NEEDED)
    pid = lineage.resolve_pid(conn, pid)
    if pid in seen:
        raise db.Refusal("this payment was decided earlier in this call")
    reason = None
    if outcome in ("keep", "replace") or _handover_onto_pairing(conn, token, pid, e):
        out = _handover_entry(conn, token, pid, e, outcome)
        record_outcome(conn, token, pid, outcome)
        seen.add(pid)
        return {"pid": pid, **out, "status": lineage.projection(conn, pid)["status"]}
    if outcome == "optional":
        reason = str(e.get("reason") or "").strip()[:REASON_MAX]
        out = optional_in_tx(conn, pid, rev, reason)
        record_outcome(conn, token, pid, "settled", reason)
        seen.add(pid)
        return {"pid": pid, **out, "status": lineage.projection(conn, pid)["status"]}
    if outcome in ("missing", "leave"):
        unsettled = lineage.projection(conn, pid)["exp_kind"] is None
        if outcome == "missing" and unsettled:
            raise db.Refusal(UNSETTLED)
        if outcome == "leave" and not unsettled:
            raise db.Refusal("leave is for a payment whose classification is not settled; "
                             "decide this one match, propose or missing")
    if outcome == "leave":
        reason = str(e.get("reason") or "").strip()[:REASON_MAX]
        matches.decidable(conn, pid, rev)
        _revoke_judgement(conn, pid)            # d1 (Astra S2): a later decision, as missing
        record_outcome(conn, token, pid, "settled", reason or "classification not settled")
        seen.add(pid)
        return {"pid": pid, "applied": True, "wrote": False,
                "status": lineage.projection(conn, pid)["status"]}
    if outcome == "missing":
        reason = str(e.get("reason") or "")[:REASON_MAX]
        _check_listed(conn, token, pid)
        out = missing_in_tx(conn, pid, rev, reason)
    else:
        if not e.get("document_date") or not isinstance(e["document_date"], str):
            raise db.Refusal(DATE_READ)
        out = matches.machine_in_tx(
            conn, "pair" if outcome == "match" else "propose", pid, _int(e, "doc_id"),
            expected_revision=rev, alternatives=_alternatives(e),
            labels=_labels(e), rationale=e.get("rationale") or "",
            document_date=e["document_date"], row_digest=e.get("row_digest"))
        if not out["applied"]:
            # an exempt payment (machine_in_tx's one non-raising refusal): its residue line
            # is kept, as record_match keeps it; the entry is reported refused, no outcome
            return {"pid": pid, "applied": False, "wrote": False, "refused": out["refused"]}
    _revoke_judgement(conn, pid)
    record_outcome(conn, token, pid, outcome, reason)
    seen.add(pid)                  # only a decided entry: a refused one may come again
    return {"pid": pid, "applied": True, "wrote": bool(out.get("wrote")),
            "status": lineage.projection(conn, pid)["status"]}


def _check_handed(conn, token, entries) -> None:
    """e1 (Terra S2): once the run's work list exists, the job decides the payment handed
    out now, alone (rev 18.4 §R18.1) — never one whose facts and candidates it was not
    handed."""
    import queues
    job_id = queues.job_of(conn, token)
    if job_id is None:
        return
    run = conn.execute("SELECT listed_at FROM runs WHERE job_id=?", (job_id,)).fetchone()
    if run is None or run["listed_at"] is None:
        return
    pid = queues.pid_of_unit(queues.handed_unit(conn, job_id))
    e = entries[0] if len(entries) == 1 and isinstance(entries[0], dict) else {}
    want = e.get("pid")
    if pid is None or isinstance(want, bool) or not isinstance(want, int) or \
            lineage.resolve_pid(conn, want) != lineage.resolve_pid(conn, pid):
        raise db.Refusal("decide the payment handed out now, in one entry: call job_next")


def guard_single(conn, token, pid, doc_id) -> None:
    """e3 (Astra S2): the single-decision tools (record_match, propose_match) pass the same
    job checks as decide — the payment handed out now, its found attachments filed, and a
    handed document never committed onto a paired payment (rev 18.4 §R18.3). Inside the
    caller's transaction, its token checked."""
    entry = {"pid": pid, "doc_id": doc_id}
    _check_handed(conn, token, [entry])
    _check_order(conn, token, [entry])
    if _handover_onto_pairing(conn, token, lineage.resolve_pid(conn, pid), entry):
        raise db.Refusal("this payment already has a document: answer it with decide — "
                         "replace (the operator is asked) or keep")


def _check_listed(conn, token, pid) -> None:
    """Issue #50 (operator ruling A, the narrow reading): `missing` waits while an email this
    payment's own reference search returned has never had its attachments listed — as
    record_search reported it (a reminder the model fills in: it catches a skipped step on
    an honest report, never an omitted email). The refusal names the emails."""
    import queues
    job_id = queues.job_of(conn, token)
    if job_id is None:
        return
    owed = queues.unlisted(conn, job_id, queues.unit_of_payment(pid))
    if owed:
        named = ", ".join(owed[:3]) + (f" and {len(owed) - 3} more" if len(owed) > 3 else "")
        raise db.Refusal(
            f"this payment's own search returned email{'s' if len(owed) > 1 else ''} "
            f"{named}, never listed: list_attachments on "
            f"{'each' if len(owed) > 1 else 'it'} (an invoice may be attached), report it "
            "with record_search(pid, queries=[], refs=[what it found], emails=[{id, listed: "
            "true}]), then decide")


def _check_order(conn, token, entries) -> None:
    """Rev 18.4 §R18.2: a payment whose searches found an attachment still queued decides
    nothing — what a search found is filed (or set aside) before the decision."""
    import queues
    job_id = queues.job_of(conn, token)
    if job_id is None:
        return
    for e in entries:
        pid = e.get("pid") if isinstance(e, dict) else None
        if isinstance(pid, int) and not isinstance(pid, bool) and queues.blocks_decide(
                conn, job_id, queues.unit_of_payment(pid)):
            raise db.Refusal("this payment's searches found attachments not yet filed: file "
                             "them (or set_aside what is no invoice), then decide")


def decide(conn, token, entries) -> dict:
    if token is None:
        raise db.Refusal("decide is the job's: pass the pass_token")
    if not isinstance(entries, list) or not 1 <= len(entries) <= ENTRIES_MAX:
        raise db.Refusal(f"entries is a list of 1 to {ENTRIES_MAX} decisions")
    results, seen = [], set()
    with db.tx(conn):
        passes.check_token(conn, token)
        _check_handed(conn, token, entries)
        _check_order(conn, token, entries)
        for e in entries:
            head = {"pid": e.get("pid") if isinstance(e, dict) else None,
                    "outcome": e.get("outcome") if isinstance(e, dict) else None}
            try:
                with db.savepoint(conn, "entry"):
                    results.append({**head, **_entry(conn, token, e, seen)})
            except db.Refusal as exc:
                results.append({**head, "applied": False, "wrote": False, "refused": str(exc)})
    n = sum(1 for r in results if r["applied"])
    return {"results": results, "applied": n, "refused": len(results) - n}


def record_missing(conn, token, pid, expected_revision, reason) -> dict:
    """A single `missing` decision (a continuation run, a handover; §2.2)."""
    out = decide(conn, token, [{"pid": pid, "outcome": "missing", "reason": reason,
                                "expected_revision": expected_revision}])["results"][0]
    if not out["applied"]:
        raise db.Refusal(out["refused"])
    return out
