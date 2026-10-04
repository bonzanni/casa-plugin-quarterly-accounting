"""The match tools. One discipline: CAS on the revision the caller was
given (the projection's for lineage-level writes, the match's for writes
naming a pairing); every decision is ONE log entry (two for lift-then-pair),
appended under the write lock, then lineage.settle() in the same
transaction. Occupancy and row cardinality are the fold's, never a write-time
error (plan §D15); the write-time preconditions are the ones the spec names."""
from __future__ import annotations

import json

import authority
import authorship
import db
import documents
import fx
import kb
import lineage
import reducer as R

LABELS = ("clean", "guessed", "no-ref", "partial-search", "recipient?")


def _labels(labels) -> str:
    labels = tuple(dict.fromkeys(labels or ("clean",)))       # issue #3: each label once
    bad = [lbl for lbl in labels if lbl not in LABELS]
    if bad:
        raise db.Refusal(f"labels are {', '.join(LABELS)}")
    if "clean" in labels and len(labels) > 1:
        raise db.Refusal("'clean' cannot be combined with another label")
    return ",".join(labels)


def _match_id_for(conn, pid, doc_id) -> int:
    """The lineage's id for this document (plan §D12: a pairing's identity is
    re-used, never duplicated). A lineage can hold two ids for one document
    after a merge of two lineages that each paired it; the one activated
    latest is re-used, deterministically, and no third id is minted."""
    row = conn.execute("SELECT m.match_id FROM matches m JOIN match_state s ON"
                       " s.match_id=m.match_id WHERE s.pid=? AND m.doc_id=?"
                       " ORDER BY s.activation DESC, m.match_id DESC LIMIT 1",
                       (pid, doc_id)).fetchone()
    if row is not None:
        return row[0]
    return conn.execute("INSERT INTO matches(pid_created, doc_id, created_seq) VALUES (?,?,0)",
                        (pid, doc_id)).lastrowid


def _state(conn, match_id):
    s = conn.execute("SELECT * FROM match_state WHERE match_id=?", (match_id,)).fetchone()
    if s is None:
        raise db.Refusal(f"there is no pairing #{match_id}")
    return s


def _result(conn, pid, red, match_id=None, effects=()) -> dict:
    out = {"applied": True, "pid": pid, "status": red.status,
           "revision": lineage.projection(conn, pid)["revision"], "effects": list(effects)}
    if match_id is not None:
        out["match_id"] = match_id
        out["state"] = _state(conn, match_id)["state"]
    return out


def _states(conn, pid) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT match_id, state FROM match_state WHERE pid=?",
                                              (pid,))}


def _effects(before: dict, after: dict) -> list:
    out = []
    for mid, st in sorted(after.items()):
        if before.get(mid) in ("matched", "proposed") and st == "rejected":
            out.append(f"unpaired {mid}")
        elif before.get(mid) in ("matched", "proposed") and st == "conflicted":
            out.append(f"set aside {mid}")
    return out


def payment_snapshot(facts, kind, fx_pair) -> str | None:
    """Issue #34 (G1, D1): what an operator's rejection is bound to on the payment's side —
    its facts and expected kind as they were when the operator rejected, and the bank's
    exchange rate (a corrected rate is new evidence, #35)."""
    if facts is None:
        return None
    return db.canonical({"pairing": R.fingerprint(facts, kind),
                         "fx": fx.canonical(fx_pair)})


def row_fx(row):
    return ({"rate": row["fx_rate"], "unit": row["fx_unit"]}
            if row is not None and row.get("fx_rate") else None)


def rejected_by_operator(conn, pid, doc, facts, kind, fx_pair):
    """Issue #34 (G2, D1): the time of the operator's rejection that blocks a machine
    pairing of document `doc` with payment `pid`, or None. The operator's latest verdict on
    that document in this lineage — over every pairing id it holds, a merge's too — is a
    rejection, made against the payment and the document exactly as they are now. A
    rejection from before 0.8.0 recorded neither: it does not block (0.7.0's behaviour —
    the operator is asked again, and that answer sticks)."""
    r = conn.execute("SELECT l.kind, l.fp, l.detail, l.created_at FROM log l JOIN matches m"
                     " ON m.match_id=l.match_id WHERE l.pid=? AND m.doc_id=?"
                     " AND l.author='operator' AND l.kind IN ('pair', 'unpair')"
                     " ORDER BY l.seq DESC LIMIT 1", (pid, doc["doc_id"])).fetchone()
    if r is None or r["kind"] != "unpair" or r["fp"] is None or r["detail"] is None:
        return None
    if (r["fp"] == payment_snapshot(facts, kind, fx_pair)
            and r["detail"] == documents.fingerprint(doc)):
        return r["created_at"]
    return None


def _why_not_kind(conn, proj, row, exp, doc) -> str:
    if exp.row == 2:
        cp = kb.counterparty_for(conn, row["counterparty"])
        return (f"{cp['name']} is set to need "
                f"{'no document' if exp.kind == 'none' else exp.kind}, and this is a {doc['kind']}")
    if exp.kind == "none":
        return f"this payment needs no document, and this is a {doc['kind']}"
    return f"this payment needs a {exp.kind}, and this is a {doc['kind']}"


def _machine(conn, kind, pid, doc_id, expected_revision, labels, rationale, runners_up,
             resolves, row_snapshot, token, row_digest=None, document_date=None):
    import job
    import passes
    if token is None:
        raise db.Refusal("a machine pairing is written during a pass: pass the pass_token")
    if document_date is not None:
        documents._validate({"document_date": document_date})
    with db.tx(conn):
        passes.check_token(conn, token)
        job.require_fresh(conn)          # INV-J10: F, decided in this write's transaction
        pid = lineage.resolve_pid(conn, pid)
        proj = lineage.projection(conn, pid)
        if proj["revision"] != expected_revision:
            raise authorship.Stale(pid, "this payment changed since list_projections; re-read it")
        st = lineage.fold_of(conn, pid)
        row = lineage.live_row(conn, proj)
        if proj["ended"] or not lineage.eligible(conn, row):
            raise db.Refusal("this payment is not managed any more (ended or ineligible)")
        if not lineage.is_fresh(conn, proj):
            # fix E2; issue #1: the kind it wants is known only from an observation at
            # the latest import (the export's tags, or a read since); an older one may
            # name a kind the payment no longer wants. Reachable for a row the latest
            # export did not carry.
            raise db.Refusal("this payment was not in the latest bank import, so its category "
                             "is not known now: observe it first (read the row and "
                             "record_observation), then judge it")
        # The document is validated BEFORE the exemption branch, so an exempt
        # lineage's residue names only a real, relevant document of the kind the
        # payment would need (fix round 1: nonexistent/irrelevant/wrong-kind
        # documents left residue lines, one more every pass).
        exp = lineage.expectation_for(conn, proj, row, exempt=False)
        if exp.unknown:
            raise db.Refusal("not yet classified: nothing is matched to it until it is")
        if not exp.seeks_document:
            raise db.Refusal("no document is expected for this payment")
        doc = documents._doc(conn, doc_id)
        if doc["irrelevant"]:
            raise db.Refusal("that document was marked irrelevant")
        if doc["kind"] != exp.kind:
            raise db.Refusal(_why_not_kind(conn, proj, row, exp, doc))
        if st.exemption is not None:
            detail = f"document #{doc_id}"
            if conn.execute("SELECT 1 FROM residue WHERE pid=? AND reason='exempt-doc' AND"
                            " detail=?", (pid, detail)).fetchone() is None:
                lineage.add_residue(conn, pid, "exempt-doc", detail)
            return {"applied": False, "refused": "the operator exempted this payment; a document "
                                                 "that turned up for it is shown as residue"}
        if doc["amount_minor"] is None:
            # issue #32: the bar compares amounts, so a document whose amount was never
            # read cannot be shown to meet it (and its package file would carry none)
            raise db.Refusal("this document's amount was never read: read its amount and "
                             "currency on the document, update_document_metadata(doc_id, "
                             "amount_minor=…, currency=…, pass_token=…), then pair it")
        if row["status"] != "BOOK":
            raise db.Refusal("a pending payment is not matched automatically")
        if (row_snapshot is None) == (row_digest is None):
            raise db.Refusal("pass the item's row_digest from list_quarter_state")
        if row_digest is not None:
            same = row_digest == R.digest(R.facts_of(row))
        else:
            same = (R.facts_of(row_snapshot) == R.facts_of(row)
                    and (row_snapshot.get("state") or "active") == "active")
        if not same:
            raise db.Refusal("the row changed since this pass's snapshot (or row_digest is not the item's value "
                             "from list_quarter_state): re-read the item with list_quarter_state(pid=…) and "
                             "pass its row_digest, or re-import before matching")
        if doc["currency"] and doc["currency"] != row["currency"]:
            # issue #35 (R3): the bank's own rate rules out an amount it cannot give
            why = fx.screen({"rate": row["fx_rate"], "unit": row["fx_unit"]}
                            if row.get("fx_rate") else None, row["amount_minor"],
                            row["currency"], doc["amount_minor"], doc["currency"])
            if why is not None:
                raise db.Refusal(why + " — not paired")
        # C1 (Terra S1, Astra S1): the document as this write leaves it — a date read on it
        # in this call is a corrected fact, and may lift a rejection
        effective = dict(doc, document_date=document_date) if document_date else doc
        rejected = rejected_by_operator(conn, pid, effective, R.facts_of(row), exp.kind,
                                        row_fx(row))
        if rejected is not None:
            raise db.Refusal(f"the operator rejected this pairing ({rejected[:10]}); it is not "
                             "proposed again unless the payment or the document changes — "
                             "leave it")
        if kind == "pair" and documents.collisions(conn, doc_id):
            raise db.Refusal("another document carries the same issuer and number: propose it "
                             "instead, or resolve the duplicate first")
        op = st.operator_current()
        if op is not None and op.doc_id == doc_id:
            raise db.Refusal("the operator already paired this document with this payment; a "
                             "machine write never touches that pairing")
        conflicted = st.conflicted_ids()
        if set(resolves or ()) != conflicted:
            raise db.Refusal("this payment has unresolved candidates "
                             f"{sorted(conflicted)}; name exactly those in resolves")
        if document_date and document_date != doc["document_date"]:
            # issue #19: the date read from the document names its file in the package
            # (package.doc_filename) and replaces the filed, provisional reading
            conn.execute("UPDATE documents SET document_date=? WHERE doc_id=?",
                         (document_date, doc_id))
            lineage.settle_doc_holders(conn, doc_id)
        if document_date:
            # issue #22: the date was read on the document, whether or not it changed
            conn.execute("UPDATE documents SET date_read_at=? WHERE doc_id=?",
                         (db.now(), doc_id))
        before = _states(conn, pid)
        mid = _match_id_for(conn, pid, doc_id)
        conn.execute("UPDATE matches SET label=?, rationale=?, runners_up_json=? WHERE match_id=?",
                     (_labels(labels), rationale or "", json.dumps(list(runners_up or ())), mid))
        lineage.append(conn, pid, kind, "auto", match_id=mid, doc_id=doc_id,
                       fp=R.fingerprint(R.facts_of(row), exp.kind), resolves=tuple(resolves or ()))
        red = lineage.settle(conn, pid)
        return _result(conn, pid, red, mid, _effects(before, _states(conn, pid)))


def _operator_pair(conn, pid, doc_id, render_id, *, match_id=None):
    """Inside the transaction: lift (if an exemption stands) then pair, the
    kind guard evaluated against the expectation AFTER the lift. A refusal
    raises before anything is appended, so the transaction rolls back whole."""
    proj = lineage.projection(conn, pid)
    row = lineage.live_row(conn, proj)
    if row is None or proj["ended"]:
        raise db.Refusal("that payment has left the bank ledger")
    st = lineage.fold_of(conn, pid)
    exp = lineage.expectation_for(conn, proj, row, exempt=False)
    doc = documents._doc(conn, doc_id)
    if exp.unknown:
        raise db.Refusal("not yet classified: nothing can be paired with it until it is")
    if doc["kind"] != exp.kind:
        raise db.Refusal(_why_not_kind(conn, proj, row, exp, doc))
    before = _states(conn, pid)
    if st.exemption is not None:
        lineage.append(conn, pid, "lift", "operator", render_id=render_id)
    mid = match_id if match_id is not None else _match_id_for(conn, pid, doc_id)
    lineage.append(conn, pid, "pair", "operator", match_id=mid, doc_id=doc_id,
                   fp=R.fingerprint(R.facts_of(row), exp.kind), render_id=render_id)
    red = lineage.settle(conn, pid)
    return _result(conn, pid, red, mid, _effects(before, _states(conn, pid)))


def _operator_pid(conn, pid) -> int:
    """The pid an operator write acts on. If it was merged into another
    lineage, what the operator was shown was the loser's item, not the
    survivor's: fail closed, so the merged item is shown again first."""
    survivor = lineage.resolve_pid(conn, pid)
    if survivor != pid:
        raise authorship.NotShown(survivor, f"#{pid} was merged into #{survivor}; show the "
                                            "merged item and apply nothing yet")
    return pid


def record_match(conn, *, pid, doc_id, author, expected_revision, render_id=None,
                 labels=("clean",), rationale="", runners_up=(), resolves=(), row_snapshot=None,
                 token=None, row_digest=None, document_date=None) -> dict:
    if author == "operator":
        # S7 §8.1: an operator pairing is a tap's (matches.confirm_in_tx under a grant)
        raise db.Refusal(authority.TAP_ONLY)
    if author != "auto":
        raise db.Refusal("author is 'auto'")
    return _machine(conn, "pair", pid, doc_id, expected_revision, labels, rationale,
                    runners_up, resolves, row_snapshot, token, row_digest, document_date)


def propose_match(conn, *, pid, doc_id, expected_revision, labels=("clean",), rationale="",
                  runners_up=(), resolves=(), row_snapshot=None, token=None,
                  row_digest=None, document_date=None) -> dict:
    return _machine(conn, "propose", pid, doc_id, expected_revision, labels, rationale,
                    runners_up, resolves, row_snapshot, token, row_digest, document_date)


def confirm_in_tx(conn, *, grant, match_id, expected_revision, render_id, bind="shown") -> dict:
    """The operator approves a pairing they were shown, inside the caller's transaction,
    under a tap's grant (S7 §8.1)."""
    authority.require(conn, grant)
    s = _state(conn, match_id)
    pid = _operator_pid(conn, s["pid"])
    authorship.require_match(conn, pid, match_id, render_id, expected_revision, bind=bind)
    if s["state"] == "rejected":
        raise db.Refusal("that pairing was already removed")
    if s["state"] == "conflicted":
        other = conn.execute("SELECT pid FROM match_state WHERE doc_id=? AND pid<>? AND state"
                             " IN ('matched','proposed')", (s["doc_id"], pid)).fetchone()
        if other is not None:
            raise db.Refusal(f"that document has since been paired with payment #{other[0]}")
    return _operator_pair(conn, pid, s["doc_id"], render_id, match_id=match_id)


def reject_in_tx(conn, *, grant, match_id, expected_revision, render_id, bind="shown") -> dict:
    """The operator removes a pairing they were shown, inside the caller's transaction,
    under a tap's grant (S7 §8.1)."""
    authority.require(conn, grant)
    s = _state(conn, match_id)
    pid = _operator_pid(conn, s["pid"])
    authorship.require_match(conn, pid, match_id, render_id, expected_revision, bind=bind)
    if s["state"] not in ("matched", "proposed", "conflicted"):
        raise db.Refusal("there is no pairing to remove there")
    before = _states(conn, pid)
    _append_rejection(conn, pid, s, render_id)
    red = lineage.settle(conn, pid)
    return _result(conn, pid, red, match_id, _effects(before, _states(conn, pid)))


def _append_rejection(conn, pid, s, render_id) -> None:
    """Issue #34 (G1, D1): the rejection is bound to the payment and the document as they
    are now — what the operator rejected — recorded on the rejection itself."""
    doc = documents._doc(conn, s["doc_id"])
    proj = lineage.projection(conn, pid)
    row = lineage.live_row(conn, proj)
    snap = None
    if row is not None and not proj["ended"]:
        exp = lineage.expectation_for(conn, proj, row, exempt=False)
        snap = payment_snapshot(R.facts_of(row), exp.kind, row_fx(row))
    lineage.append(conn, pid, "unpair", "operator", match_id=s["match_id"],
                   render_id=render_id, fp=snap, detail=documents.fingerprint(doc))


def reject_all_in_tx(conn, pid, bound, *, grant) -> list:
    """"Wrong" over every displayed candidate, inside the caller's transaction, under a
    tap's grant, each already checked against the revision the operator was shown: every
    rejection is recorded, then the payment settles ONCE (C3, Astra S1: settling between
    them let the store rule retire a merged duplicate of the same document and move the
    revision the next rejection was bound to). Returns the effects."""
    authority.require(conn, grant)
    pid = _operator_pid(conn, pid)
    before = _states(conn, pid)
    for match_id, render_id in bound:
        s = _state(conn, match_id)
        if s["state"] not in ("matched", "proposed", "conflicted"):
            raise db.Refusal("there is no pairing to remove there")
        _append_rejection(conn, pid, s, render_id)
    lineage.settle(conn, pid)
    return _effects(before, _states(conn, pid))


def set_exemption_in_tx(conn, *, grant, pid, exempt, expected_revision, render_id,
                        bind="shown") -> dict:
    """The operator says one payment needs no document (exempt) or needs one after all,
    inside the caller's transaction, under a tap's grant (S7 §8.1)."""
    authority.require(conn, grant)
    pid = _operator_pid(conn, pid)
    authorship.require_projection(conn, pid, render_id, expected_revision, bind=bind)
    st = lineage.fold_of(conn, pid)
    before = _states(conn, pid)
    if exempt:
        lineage.append(conn, pid, "exempt", "operator", render_id=render_id)
    else:
        if st.exemption is None:
            raise db.Refusal("no exemption stands on this payment")
        lineage.append(conn, pid, "lift", "operator", render_id=render_id)
    red = lineage.settle(conn, pid)
    return _result(conn, pid, red, None, _effects(before, _states(conn, pid)))


def relabel_match(conn, *, match_id, labels, rationale=None, runners_up=None, token) -> dict:
    import job
    import passes
    if token is None:
        raise db.Refusal("relabelling is a pass's work: pass the pass_token")
    with db.tx(conn):
        passes.check_token(conn, token)
        job.require_fresh(conn)          # INV-J10
        s = _state(conn, match_id)
        if s["state"] not in ("matched", "proposed"):
            # spec §Tool surface: relabel_match "re-labels an accepted match". A
            # retired pairing's label is invisible, yet it is in settle's digest:
            # relabelling one would make an operator's pending correction Stale
            # over a change they cannot see (fix round 1).
            raise db.Refusal(f"pairing #{match_id} is {s['state']}; only a standing pairing "
                             "is relabelled")
        sets = {"label": _labels(labels)}
        if rationale is not None:
            sets["rationale"] = rationale
        if runners_up is not None:
            sets["runners_up_json"] = json.dumps(list(runners_up))
        conn.execute("UPDATE matches SET %s WHERE match_id=?" % ", ".join(f"{k}=?" for k in sets),
                     (*sets.values(), match_id))
        pid = lineage.resolve_pid(conn, s["pid"])
        red = lineage.settle(conn, pid)
        return _result(conn, pid, red, match_id)
