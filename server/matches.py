"""The match tools. One discipline: CAS on the revision the caller was
given (the projection's for lineage-level writes, the match's for writes
naming a pairing); every decision is ONE log entry (two for lift-then-pair),
appended under the write lock, then lineage.settle() in the same
transaction. Occupancy and row cardinality are the fold's, never a write-time
error (plan §D15); the write-time preconditions are the ones the spec names."""
from __future__ import annotations

import json

import authorship
import db
import documents
import kb
import lineage
import reducer as R

LABELS = ("clean", "guessed", "no-ref", "partial-search", "recipient?")


def _labels(labels) -> str:
    labels = tuple(labels or ("clean",))
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


def _why_not_kind(conn, proj, row, exp, doc) -> str:
    if exp.row == 2:
        cp = kb.counterparty_for(conn, row["counterparty"])
        return (f"{cp['name']} is set to need "
                f"{'no document' if exp.kind == 'none' else exp.kind}, and this is a {doc['kind']}")
    if exp.kind == "none":
        return f"this payment needs no document, and this is a {doc['kind']}"
    return f"this payment needs a {exp.kind}, and this is a {doc['kind']}"


def _machine(conn, kind, pid, doc_id, expected_revision, labels, rationale, runners_up,
             resolves, row_snapshot, token):
    import passes
    if token is None:
        raise db.Refusal("a machine pairing is written during a pass: pass the pass_token")
    with db.tx(conn):
        passes.check_token(conn, token)
        pid = lineage.resolve_pid(conn, pid)
        proj = lineage.projection(conn, pid)
        if proj["revision"] != expected_revision:
            raise authorship.Stale(pid, "this payment changed since list_projections; re-read it")
        st = lineage.fold_of(conn, pid)
        row = lineage.live_row(conn, proj)
        if proj["ended"] or not lineage.eligible(conn, row):
            raise db.Refusal("this payment is not managed any more (ended or ineligible)")
        if not lineage.is_fresh(conn, proj):
            # fix E2: the kind it wants is known only from a read made after the latest
            # import; an older read may name a kind the payment no longer wants
            raise db.Refusal("this payment has not been re-read since the latest bank import: "
                             "observe it first (read the row and record_observation), then "
                             "judge it")
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
        if row["status"] != "BOOK":
            raise db.Refusal("a pending payment is not matched automatically")
        if row_snapshot is None or R.facts_of(row_snapshot) != R.facts_of(row) \
                or (row_snapshot.get("state") or "active") != "active":
            raise db.Refusal("the row changed since this pass's snapshot (or row_snapshot is not the item's value "
                             "from list_quarter_state): pass the item's row_snapshot from list_quarter_state, "
                             "verbatim, or re-import before matching")
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
                 token=None) -> dict:
    if author == "auto":
        return _machine(conn, "pair", pid, doc_id, expected_revision, labels, rationale,
                        runners_up, resolves, row_snapshot, token)
    if author != "operator":
        raise db.Refusal("author is 'auto' or 'operator'")
    with db.tx(conn):
        pid = _operator_pid(conn, pid)
        authorship.require_projection_shown(conn, pid, render_id, expected_revision)
        return _operator_pair(conn, pid, doc_id, render_id)


def propose_match(conn, *, pid, doc_id, expected_revision, labels=("clean",), rationale="",
                  runners_up=(), resolves=(), row_snapshot=None, token=None) -> dict:
    return _machine(conn, "propose", pid, doc_id, expected_revision, labels, rationale,
                    runners_up, resolves, row_snapshot, token)


def confirm_match(conn, *, match_id, expected_revision, render_id) -> dict:
    with db.tx(conn):
        s = _state(conn, match_id)
        pid = _operator_pid(conn, s["pid"])
        authorship.require_match_shown(conn, pid, match_id, render_id, expected_revision)
        if s["state"] == "rejected":
            raise db.Refusal("that pairing was already removed")
        if s["state"] == "conflicted":
            other = conn.execute("SELECT pid FROM match_state WHERE doc_id=? AND pid<>? AND state"
                                 " IN ('matched','proposed')", (s["doc_id"], pid)).fetchone()
            if other is not None:
                raise db.Refusal(f"that document has since been paired with payment #{other[0]}")
        return _operator_pair(conn, pid, s["doc_id"], render_id, match_id=match_id)


def reject_match(conn, *, match_id, expected_revision, render_id) -> dict:
    with db.tx(conn):
        return reject_in_tx(conn, match_id=match_id, expected_revision=expected_revision,
                            render_id=render_id)


def reject_in_tx(conn, *, match_id, expected_revision, render_id) -> dict:
    """reject_match inside the caller's transaction (apply_reply sets aside
    every displayed candidate of a payment, all or none)."""
    s = _state(conn, match_id)
    pid = _operator_pid(conn, s["pid"])
    authorship.require_match_shown(conn, pid, match_id, render_id, expected_revision)
    if s["state"] not in ("matched", "proposed", "conflicted"):
        raise db.Refusal("there is no pairing to remove there")
    before = _states(conn, pid)
    lineage.append(conn, pid, "unpair", "operator", match_id=match_id, render_id=render_id)
    red = lineage.settle(conn, pid)
    return _result(conn, pid, red, match_id, _effects(before, _states(conn, pid)))


def set_exemption(conn, *, pid, exempt, expected_revision, render_id) -> dict:
    with db.tx(conn):
        pid = _operator_pid(conn, pid)
        authorship.require_projection_shown(conn, pid, render_id, expected_revision)
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
    import passes
    if token is None:
        raise db.Refusal("relabelling is a pass's work: pass the pass_token")
    with db.tx(conn):
        passes.check_token(conn, token)
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
