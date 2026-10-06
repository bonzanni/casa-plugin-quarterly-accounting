"""decide (design rev 17 §2.2 step 4): a vendor group's decisions in one call. The floor
(matches.machine_in_tx) is applied to each entry on its own, in the order given; a refused
entry is reported with its reason and rolls back alone (a savepoint), the others commit.
An earlier entry's write is in the same transaction, so the document it took is taken for
the later ones by construction. "No invoice needed" is never the job's (r9)."""
from __future__ import annotations

import authorship
import db
import lineage
import matches
import passes

ENTRIES_MAX = 30
OUTCOMES = ("match", "propose", "missing")      # no `not-needed`: the operator's (§2.2, r9)
REASON_MAX = 200
NOT_NEEDED = ("outcome is match, propose or missing: \"no invoice needed\" is the "
              "operator's tap, a KB rule or an expectation of none, never the job's")
DATE_READ = ("pass document_date: the date printed on the document you opened (its issue "
             "date), YYYY-MM-DD")


def note_progress(conn, token) -> None:
    """§2.2 `progressed`: the batch persisted work (a decision, a filing, a search)."""
    conn.execute("UPDATE claims SET progressed=1 WHERE gen=?", (int(token),))


def record_outcome(conn, token, pid, outcome, reason=None) -> None:
    """The run's work-list entry for `pid` (run_work, the job of claim `token`) takes
    `outcome`; a payment not on the list: no row, nothing. Marks the batch progressed."""
    conn.execute("UPDATE run_work SET outcome=?, reason=? WHERE pid=? AND job_id=(SELECT"
                 " job_id FROM claims WHERE gen=?)", (outcome, reason, pid, int(token)))
    # §2.1, per payment (plan round 5): the job considered the documents it was HANDED for
    # this payment — a re-decision that writes nothing included — and no others (plan
    # round 6: a capped hand-out must never mark an unseen document as considered). A
    # decision outside a work entry (no hand-out) advances nothing.
    conn.execute("UPDATE projections SET considered_seq=max(coalesce(considered_seq, 0),"
                 " coalesce((SELECT handed_upto FROM run_work WHERE pid=? AND job_id=(SELECT"
                 " job_id FROM claims WHERE gen=?)), 0)) WHERE pid=?", (pid, int(token), pid))
    note_progress(conn, token)


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


def missing_in_tx(conn, pid, expected_revision, reason) -> dict:
    """A `missing` decision, inside the caller's transaction: the payment as handed out,
    still managed, holding no pairing (§2.2: missing is for a payment no document fits)."""
    proj = lineage.projection(conn, pid)
    if proj["revision"] != expected_revision:
        raise authorship.Stale(pid, "this payment changed since it was handed out; decide it "
                                    "again with the revision job_next gives now")
    row = lineage.live_row(conn, proj)
    if proj["ended"] or not lineage.eligible(conn, row):
        raise db.Refusal("this payment is not managed any more (ended or ineligible)")
    st = lineage.fold_of(conn, pid)
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
    if outcome == "missing":
        reason = str(e.get("reason") or "")[:REASON_MAX]
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
    record_outcome(conn, token, pid, outcome, reason)
    seen.add(pid)                  # only a decided entry: a refused one may come again
    return {"pid": pid, "applied": True, "wrote": bool(out.get("wrote")),
            "status": lineage.projection(conn, pid)["status"]}


def decide(conn, token, entries) -> dict:
    if token is None:
        raise db.Refusal("decide is the job's: pass the pass_token")
    if not isinstance(entries, list) or not 1 <= len(entries) <= ENTRIES_MAX:
        raise db.Refusal(f"entries is a list of 1 to {ENTRIES_MAX} decisions")
    results, seen = [], set()
    with db.tx(conn):
        passes.check_token(conn, token)
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
