"""The match tools. One discipline: CAS on the revision the caller was
given (the projection's for lineage-level writes, the match's for writes
naming a pairing); every decision is ONE log entry (two for lift-then-pair),
appended under the write lock, then lineage.settle() in the same
transaction. A machine write meets the floor (design rev 17 §2 "The floor",
machine_in_tx): same currency and exact amount for a match, a document no other
payment holds (`holders`, R5); row cardinality stays the fold's."""
from __future__ import annotations

import json

import amounts
import authority
import authorship
import budget
import db
import documents
import fx
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


# rev 18.4 §R18.4: a document is held only by a match or a proposal's PRIMARY document (a
# joint set's members included); a proposal's alternatives hold nothing (r2 Astra S1 #2)
HOLDERS_SQL = (
    "SELECT s.pid, s.state AS how FROM match_state s JOIN projections p ON p.pid=s.pid"
    " WHERE s.doc_id=:d AND (s.state IN ('matched','proposed') OR (s.state='conflicted'"
    " AND p.status='proposed' AND p.current_match IS NULL))")
ALTERNATIVES_MAX = 3      # §1: up to four named candidates on a card, the chosen one included


def holders(conn, doc_id) -> list:
    """THE ownership of a document (R5), the one function the floor, the candidates and the
    exact fit all read: every (pid, how) holding it — `matched`, `proposed` (a proposal's
    primary) or `conflicted` (a joint set, D3). A proposal's alternatives hold nothing (rev
    18.4 §R18.4)."""
    return [(r["pid"], r["how"]) for r in conn.execute(HOLDERS_SQL, {"d": doc_id})]


def purchase_holders(conn, doc_id) -> list:
    """Issue #48: every (pid, how, held doc) holding ANY document of `doc_id`'s purchase
    (documents.purchase: the same issuer and number) — the job's ownership. `holders`
    stays per document: the operator's taps are not limited by the purchase."""
    return [(p, how, d) for d in documents.purchase(conn, doc_id)
            for p, how in holders(conn, d)]


def taken_elsewhere(conn, doc_id, pid) -> bool:
    """R5, purchase-wide (issue #48): held by ANOTHER payment through any document of its
    purchase. What `pid` itself holds is never taken against `pid` (§2.2 reopening). The
    job's readers: the floor, the exact fit, the candidates, a card's offered alternatives,
    the replace question."""
    return taken_by(conn, doc_id, pid) is not None


def taken_by(conn, doc_id, pid):
    """(other pid, held doc) holding `doc_id`'s purchase, the document itself first; None."""
    return next(((p, d) for p, _how, d in sorted(purchase_holders(conn, doc_id),
                                                  key=lambda h: h[2] != doc_id)
                 if p != pid), None)


def _payment_words(conn, pid) -> str:
    row = lineage.live_row(conn, lineage.projection(conn, pid))
    if row is None:
        return f"payment #{pid}"
    return (f"the payment of {row['booking_date'] or row['value_date']} "
            f"({amounts.fmt(row['amount_minor'], row['currency'])})")


def taken_refusal(conn, doc_id, pid) -> str | None:
    """The job's refusal when `doc_id`'s purchase backs another payment, naming that
    payment so the job searches for this payment's own document (issue #48); None."""
    t = taken_by(conn, doc_id, pid)
    if t is None:
        return None
    other, held = t
    if held == doc_id:
        return (f"document #{doc_id} is taken: another payment's match or proposal holds "
                "it")
    d = documents._doc(conn, held)
    return (f"document #{doc_id} is the same purchase as document #{held} "
            f"({budget.clip(d['issuer'] or d['counterparty'], 80)} "
            f"{budget.clip(d['document_number'], 40)}), which already "
            f"backs {_payment_words(conn, other)}: one purchase backs one payment — search "
            "for this payment's own document")


def _own_machine(st) -> list:
    """What the payment holds by the machine: a match, a proposal, or a joint set's members
    (§2.2 reopening: replaced by its own re-decision, never taken against it)."""
    return [c for c in st.cands.values()
            if c.author == "auto" and c.state in ("matched", "proposed", "conflicted")]


def _alternatives(conn, match_id) -> list:
    return json.loads(conn.execute("SELECT alternatives_json FROM matches WHERE match_id=?",
                                   (match_id,)).fetchone()[0])


def alternatives(conn, match_id) -> list:
    """D3: the alternative documents a machine proposal names (doc ids, in its order)."""
    return _alternatives(conn, match_id)


def _relevant_doc(conn, doc_id):
    """A real document not marked irrelevant (checked BEFORE the exemption branch too, so an
    exempt payment's residue names only a real, relevant document: fix round 1)."""
    doc = documents._doc(conn, doc_id)
    if doc["irrelevant"]:
        raise db.Refusal(f"document #{doc_id} was marked irrelevant")
    return doc


def _floor_doc(conn, kind, pid, row, exp, doc_id, document_date):
    """The document side of the floor, for the chosen document and each alternative."""
    doc = _relevant_doc(conn, doc_id)
    unknown = documents.amount_unknown(doc)
    if unknown and kind == "pair":
        # issue #32; Q2 run 1: a document of unknown amount (never read, or two readings
        # that disagree) is never matched by the job — only proposed, for the operator
        raise db.Refusal(f"document #{doc_id}'s amount is unknown (never read, or its "
                         "readings disagree): propose it, never match it")
    taken = taken_refusal(conn, doc_id, pid)
    if taken is not None:
        raise db.Refusal(taken)
    if kind == "pair" and doc["currency"] != row["currency"]:
        raise db.Refusal(f"document #{doc_id} is in {doc['currency']} and the payment in "
                         f"{row['currency']}: a different currency is only ever proposed")
    if kind == "pair" and doc["amount_minor"] != row["amount_minor"]:
        raise db.Refusal(f"the amounts differ (document #{doc_id}: "
                         f"{amounts.fmt(doc['amount_minor'], doc['currency'])}, payment: "
                         f"{amounts.fmt(row['amount_minor'], row['currency'])}): propose it "
                         "if it may still be the one")
    if not unknown and doc["currency"] != row["currency"]:
        why = fx.screen(row_fx(row), row["amount_minor"], row["currency"],
                        doc["amount_minor"], doc["currency"])           # #35, kept
        if why is not None:
            raise db.Refusal(why + " — not proposed")
    # C1 (Terra S1, Astra S1): the document as this write leaves it — a date read on it in
    # this call is a corrected fact, and may lift a rejection
    effective = dict(doc, document_date=document_date) if document_date else doc
    rejected = rejected_by_operator(conn, pid, effective, R.facts_of(row), exp.kind,
                                    row_fx(row))
    if rejected is not None:
        raise db.Refusal(f"the operator rejected document #{doc_id} for this payment "
                         f"({rejected[:10]}); it is not proposed again unless the payment "
                         "or the document changes — leave it")
    return doc


def decidable(conn, pid, expected_revision):
    """The job's decision preconditions, shared by every outcome (machine_in_tx and
    decide.missing_in_tx): the payment as handed out, still managed, in the latest import,
    expecting a document, booked. `pid` is resolved. Returns (proj, row, exp)."""
    proj = lineage.projection(conn, pid)
    if proj["revision"] != expected_revision:
        raise authorship.Stale(pid, "this payment changed since it was handed out; decide "
                                    "it again with the revision job_next gives now")
    row = lineage.live_row(conn, proj)
    if proj["ended"] or not lineage.eligible(conn, row):
        raise db.Refusal("this payment is not managed any more (ended or ineligible)")
    if not lineage.is_fresh(conn, proj):
        raise db.Refusal("this payment was not in the latest bank import: it is decided at "
                         "the next run")
    exp = lineage.expectation_for(conn, proj, row, exempt=False)
    if exp.kind == "none":
        raise db.Refusal("no document is expected for this payment")
    if row["status"] != "BOOK":
        raise db.Refusal("a pending payment is decided once the bank books it")
    return proj, row, exp


def machine_in_tx(conn, kind, pid, doc_id, *, expected_revision, alternatives=(),
                  labels=("clean",), rationale="", runners_up=(), document_date=None,
                  row_digest=None, row_snapshot=None) -> dict:
    """THE floor (design rev 17 §2 "The floor", R5), at the write, inside the caller's
    transaction, its token already checked. Every floor refusal raises (db.Refusal or
    authorship.Stale), so the caller's transaction rolls back whole; an exempt payment
    returns {"applied": False, "refused": …} and records residue."""
    assert conn.in_transaction
    if kind not in ("pair", "propose"):
        raise ValueError(kind)
    if document_date is not None:
        documents._validate({"document_date": document_date})
    pid = lineage.resolve_pid(conn, pid)
    proj, row, exp = decidable(conn, pid, expected_revision)
    if row_digest is not None and row_digest != R.digest(R.facts_of(row)):
        raise db.Refusal("the payment's facts changed since it was handed out: decide it "
                         "again with what job_next gives now")
    if row_snapshot is not None and (R.facts_of(row_snapshot) != R.facts_of(row) or
                                     (row_snapshot.get("state") or "active") != "active"):
        raise db.Refusal("the payment's facts changed since it was handed out (its "
                         "row_snapshot is not the live row)")
    alts = list(dict.fromkeys(int(a) for a in (alternatives or ())))
    if alts and kind == "pair":
        raise db.Refusal("alternatives go with a proposal, never with a match")
    if doc_id in alts or len(alts) > ALTERNATIVES_MAX:
        raise db.Refusal(f"alternatives are up to {ALTERNATIVES_MAX} other documents")
    st = lineage.fold_of(conn, pid)
    if st.exemption is not None:
        _relevant_doc(conn, doc_id)
        detail = f"document #{doc_id}"
        if conn.execute("SELECT 1 FROM residue WHERE pid=? AND reason='exempt-doc' AND"
                        " detail=?", (pid, detail)).fetchone() is None:
            lineage.add_residue(conn, pid, "exempt-doc", detail)
        return {"applied": False, "refused": "the operator exempted this payment; a document "
                                             "that turned up for it is shown as residue"}
    if st.operator_current() is not None:
        raise db.Refusal("the operator confirmed this payment's pairing; it is never reopened")
    own = _own_machine(st)
    if kind == "propose":
        # §2.2 reopening (ruling, Task 3 review): a proposal that replaces the payment's own
        # machine pairing keeps the replaced document as one of its alternatives, so it stays
        # held by this payment and on its card; a match replaces it outright (G1)
        kept = [c.doc_id for c in own if c.doc_id != doc_id and c.doc_id not in alts]
        if len(alts) + len(kept) > ALTERNATIVES_MAX:
            raise db.Refusal(
                f"this proposal replaces the payment's own pairing of "
                f"{', '.join(f'document #{d}' for d in kept)}, which stays as an alternative: "
                f"name at most {ALTERNATIVES_MAX - len(kept)} other alternatives")
        alts += list(dict.fromkeys(kept))
    for d in [doc_id, *alts]:
        _floor_doc(conn, kind, pid, row, exp, d, document_date if d == doc_id else None)
    want = "matched" if kind == "pair" else "proposed"
    current = (len(own) == 1 and own[0].fp is not None
               and json.loads(own[0].fp)["facts"] == R.facts_of(row))
    filed_date = documents._doc(conn, doc_id)["document_date"]
    if (current and own[0].state == want and own[0].doc_id == doc_id
            and proj["status"] == want and document_date in (None, filed_date)
            and (kind == "pair" or _alternatives(conn, own[0].match_id) == alts)):
        # §2.2: the same EFFECTIVE outcome and the same document, made against the payment
        # as it is now, writes nothing — no revision moves, so a delivered card's buttons
        # stay valid. A pairing whose payment changed since (its fingerprint's facts differ:
        # the reducer shows it proposed, `facts-changed`) is written again, which
        # re-fingerprints it (plan round 1, Astra S2). A date read that corrects the filed
        # one (issue #19) is a changed document, so it is written too. The date-read stamp
        # (issue #22) is not in any digest: set when missing, it moves no revision
        if document_date:
            conn.execute("UPDATE documents SET date_read_at=? WHERE doc_id=? AND"
                         " date_read_at IS NULL", (db.now(), doc_id))
        return {"applied": True, "wrote": False, "pid": pid, "status": proj["status"],
                "revision": proj["revision"], "match_id": own[0].match_id, "state": want,
                "effects": []}
    if document_date and document_date != filed_date:
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
    conn.execute("UPDATE matches SET label=?, rationale=?, runners_up_json=?,"
                 " alternatives_json=? WHERE match_id=?",
                 (_labels(labels), rationale or "", json.dumps(list(runners_up or ())),
                  json.dumps(alts), mid))
    # §2.2 reopening: what this payment holds by the machine — a match or a proposal — is
    # replaced by this decision, never counted as taken against it (fold `resolves`)
    replaced = tuple(c.match_id for c in own if c.match_id != mid)
    lineage.append(conn, pid, kind, "auto", match_id=mid, doc_id=doc_id,
                   fp=R.fingerprint(R.facts_of(row), exp.kind), resolves=replaced)
    red = lineage.settle(conn, pid)
    return {**_result(conn, pid, red, mid, _effects(before, _states(conn, pid))),
            "wrote": True}


def _operator_pair(conn, pid, doc_id, render_id, *, match_id=None):
    """Inside the transaction: lift (if an exemption stands) then pair. The operator pairs
    whatever kind of document they were shown (design rev 17 §2: the kind and
    classification gates are deleted); the pairing is fingerprinted against the
    expectation as it is now."""
    proj = lineage.projection(conn, pid)
    row = lineage.live_row(conn, proj)
    if row is None or proj["ended"]:
        raise db.Refusal("that payment has left the bank ledger")
    st = lineage.fold_of(conn, pid)
    exp = lineage.expectation_for(conn, proj, row, exempt=False)
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


def record_match(conn, *, pid, doc_id, author, expected_revision, token, render_id=None,
                 **kw) -> dict:
    """A machine match (G1: certain), the floor applied at the write (machine_in_tx)."""
    import passes
    if author == "operator":
        # S7 §8.1: an operator pairing is a tap's (matches.confirm_in_tx under a grant)
        raise db.Refusal(authority.TAP_ONLY)
    if author != "auto":
        raise db.Refusal("author is 'auto'")
    if token is None:
        raise db.Refusal("a machine pairing is written during a run: pass the pass_token")
    with db.tx(conn):
        passes.check_token(conn, token)
        import decide
        decide.guard_single(conn, token, pid, doc_id)
        return _single(conn, token, "match", machine_in_tx(
            conn, "pair", pid, doc_id, expected_revision=expected_revision, **kw))


def propose_match(conn, *, pid, doc_id, expected_revision, token, **kw) -> dict:
    """A machine proposal for the operator to confirm, the floor applied at the write."""
    import passes
    if token is None:
        raise db.Refusal("a machine pairing is written during a run: pass the pass_token")
    with db.tx(conn):
        passes.check_token(conn, token)
        import decide
        decide.guard_single(conn, token, pid, doc_id)
        return _single(conn, token, "propose", machine_in_tx(
            conn, "propose", pid, doc_id, expected_revision=expected_revision, **kw))


def _single(conn, token, outcome, out) -> dict:
    """A single decision lands on the run's work list too (§2.2: record_match and
    propose_match "stay for single decisions"). decide imports this module, so it is
    imported here, lazily."""
    import decide
    if out["applied"]:
        decide.record_outcome(conn, token, out["pid"], outcome)
    return out


def confirm_in_tx(conn, *, grant, match_id, expected_revision, render_id, bind="rendered") -> dict:
    """The operator approves a pairing they were shown, inside the caller's transaction,
    under a tap's grant (S7 §8.1)."""
    authority.require(conn, grant)
    s = _state(conn, match_id)
    pid = _operator_pid(conn, s["pid"])
    authorship.require_match(conn, pid, match_id, render_id, expected_revision, bind=bind)
    if s["state"] == "rejected":
        raise db.Refusal("that pairing was already removed")
    _require_unheld(conn, s["doc_id"], pid)
    out = _operator_pair(conn, pid, s["doc_id"], render_id, match_id=match_id)
    lineage.settle_doc_holders(conn, s["doc_id"])
    return out


def _require_unheld(conn, doc_id, pid) -> None:
    """R5 at an operator pairing (Task 3 review carry): a document another payment holds —
    its match, its proposal's primary, a joint set (`holders`, the one ownership function)
    — is never paired onto `pid` as well."""
    other = next(((p, how) for p, how in holders(conn, doc_id) if p != pid), None)
    if other is None:
        return
    raise db.Refusal(f"that document has since been paired with payment #{other[0]}")


def pick_in_tx(conn, *, grant, pid, doc_id, render_id, mrevs: dict,
               alternatives_shown=()) -> dict:
    """§1 named candidate: the operator pairs `doc_id`, which the card displayed (the chosen
    document, an alternative, or one of a joint set). Every OTHER candidate the card
    displayed — and only those (plan round 8: `mrevs` is what the card bound,
    `alternatives_shown` the alternatives it printed) — is the operator's rejection (#34),
    so it is never proposed again for this payment while neither side changes."""
    authority.require(conn, grant)
    pid = _operator_pid(conn, pid)
    st = lineage.fold_of(conn, pid)
    own = [c for c in _own_machine(st) if str(c.match_id) in mrevs]
    for c in own:
        now = _state(conn, c.match_id)["revision"]
        if now != mrevs[str(c.match_id)]:
            raise authorship.Stale(pid, "this pairing changed since the operator looked; "
                                        "show the current facts")
    shown_alts = [int(a) for a in alternatives_shown]
    if doc_id not in {c.doc_id for c in own} | set(shown_alts):
        raise db.Refusal("that candidate was not on the card the operator saw")
    _require_unheld(conn, doc_id, pid)
    for c in own:
        if c.doc_id != doc_id:
            _append_rejection(conn, pid, _state(conn, c.match_id), render_id)
    _append_doc_rejections(conn, pid, [a for a in shown_alts if a != doc_id], render_id)
    hit = next((c for c in own if c.doc_id == doc_id), None)
    out = _operator_pair(conn, pid, doc_id, render_id,
                         match_id=hit.match_id if hit is not None else None)
    lineage.settle_doc_holders(conn, doc_id)
    return out


def reject_in_tx(conn, *, grant, match_id, expected_revision, render_id, bind="rendered") -> dict:
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
    _append_unpair(conn, pid, s["match_id"], s["doc_id"], render_id)


def _append_unpair(conn, pid, match_id, doc_id, render_id) -> None:
    doc = documents._doc(conn, doc_id)
    proj = lineage.projection(conn, pid)
    row = lineage.live_row(conn, proj)
    snap = None
    if row is not None and not proj["ended"]:
        exp = lineage.expectation_for(conn, proj, row, exempt=False)
        snap = payment_snapshot(R.facts_of(row), exp.kind, row_fx(row))
    lineage.append(conn, pid, "unpair", "operator", match_id=match_id,
                   render_id=render_id, fp=snap, detail=documents.fingerprint(doc))


def _append_doc_rejections(conn, pid, doc_ids, render_id) -> None:
    """An alternative has no match_state row: its rejection is an operator `unpair` on the
    lineage's `matches` row for that document, which is what rejected_by_operator reads.
    The fold ignores an unpair of a match id it holds no candidate for, so this moves no
    pairing state."""
    for d in dict.fromkeys(doc_ids):
        _append_unpair(conn, pid, _match_id_for(conn, pid, d), d, render_id)


def reject_alternatives_in_tx(conn, *, grant, pid, doc_ids, render_id) -> None:
    """D3 (plan round 2, Astra S1): the operator's rejection of the alternatives a card
    displayed, bound as #34 binds any rejection — never proposed again for this payment
    while neither side changes."""
    authority.require(conn, grant)
    pid = _operator_pid(conn, pid)
    if not doc_ids:
        return
    _append_doc_rejections(conn, pid, doc_ids, render_id)
    lineage.settle(conn, pid)


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
                        bind="rendered") -> dict:
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
