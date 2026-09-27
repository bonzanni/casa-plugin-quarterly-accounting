"""settle(): the ONE place a lineage's log becomes state (spec §Match
records; §"The projection"). Called inside the write transaction of every
change that can move a lineage — a decision, an import, an observation, a
KB or document edit. It:

  1. folds the lineage's log (fold.py) with the occupancy check;
  2. records every retirement the fold newly produced, and the store's own
     rules — an ended lineage retires every pairing `rejected` (cause
     row-ended); a MACHINE pairing whose document kind differs from a known
     expectation is retired `rejected` (cause kind-mismatch) — then folds
     again, until nothing new is produced;
  3. materializes match_state; the partial unique index on active documents
     is the backstop, and a violation becomes a recorded `conflicted`
     retirement for that later activation, never an error;
  4. reduces (reducer.py) and bumps revisions by digest (plan §D3).

Document availability is not stored: the document_status view derives it
from match_state, so every retirement recomputes it by construction."""
from __future__ import annotations

import json
import sqlite3

import db
import dates
import expectation as ex
import fold as F
import kb
import reducer as R

STATUS_PHRASE = {
    "matched": "document matched",
    "proposed": "document paired, awaiting review",
    "open": "required document missing",
    "no-document": "no document expected",
    "exempt": "no document expected (operator)",
    "optional": "optional document not found",
}


def projection(conn, pid: int):
    row = conn.execute("SELECT * FROM projections WHERE pid=?", (pid,)).fetchone()
    if row is None:
        raise db.Refusal(f"there is no transaction #{pid} in the accounting store")
    return row


def resolve_pid(conn, pid: int) -> int:
    seen = set()
    while True:
        p = projection(conn, pid)
        if p["merged_into"] is None:
            return pid
        if pid in seen:
            raise RuntimeError("merge cycle")
        seen.add(pid)
        pid = p["merged_into"]


def entries(conn, pid: int) -> list:
    return [F.Entry(r["seq"], r["kind"], r["author"], r["match_id"], r["doc_id"], r["fp"],
                    tuple(json.loads(r["resolves_json"])), r["retire_activation"],
                    r["retire_to"], r["cause"])
            for r in conn.execute("SELECT * FROM log WHERE pid=? ORDER BY seq", (pid,))]


def append(conn, pid, kind, author, *, match_id=None, doc_id=None, fp=None, render_id=None,
           resolves=(), retire_activation=None, retire_to=None, cause=None, detail=None) -> int:
    seq = db.next_seq(conn)
    conn.execute("INSERT INTO log(seq, pid, kind, author, match_id, doc_id, fp, render_id,"
                 " resolves_json, retire_activation, retire_to, cause, detail, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (seq, pid, kind, author, match_id, doc_id, fp, render_id,
                  json.dumps(list(resolves)), retire_activation, retire_to, cause, detail,
                  db.now()))
    return seq


def add_residue(conn, pid, reason: str, detail: str = "") -> None:
    conn.execute("INSERT INTO residue(pid, reason, detail, created_at) VALUES (?,?,?,?)",
                 (pid, reason, detail, db.now()))


def live_row(conn, proj):
    """The destination row in the latest snapshot. An ERASED lineage has none,
    ever: its id belongs to a ledger (or a row) that is gone, and after a
    re-bind the same number can name another ledger's payment (round p11,
    Astra S2). Its last known facts stay in last_facts_json."""
    if proj["ended"] == "erased":
        return None
    r = conn.execute("SELECT * FROM bank_rows WHERE row_id=?", (proj["dest_row_id"],)).fetchone()
    return dict(r) if r is not None else None


def eligible(conn, row) -> bool:
    b = conn.execute("SELECT account_id, watermark FROM binding WHERE id=1").fetchone()
    if b is None or row is None or row["state"] != "active":
        return False
    eff = dates.effective_date(row)
    return row["account_id"] == b["account_id"] and eff is not None and eff >= b["watermark"]


def expectation_for(conn, proj, row, exempt: bool) -> ex.Expectation:
    if row is None:
        return ex.Expectation(None, "required", 4)
    tags = json.loads(proj["class_tags_json"]) if proj["class_tags_json"] else []
    cp = kb.counterparty_for(conn, row["counterparty"])
    return ex.derive(row["direction"], tags, exempt=exempt,
                     counterparty_override=kb.override_of(cp),
                     chain_overrides=kb.chain_overrides(conn))


def _occupied(conn, pid):
    def occupied(doc_id, match_id):
        return conn.execute("SELECT 1 FROM match_state WHERE doc_id=? AND pid<>? AND state IN"
                            " ('matched','proposed')", (doc_id, pid)).fetchone() is not None
    return occupied


def fold_of(conn, pid: int) -> F.FoldState:
    return F.fold(entries(conn, pid), _occupied(conn, pid))


def _doc_kinds(conn, doc_ids) -> dict:
    if not doc_ids:
        return {}
    q = ",".join("?" * len(doc_ids))
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT doc_id, kind FROM documents WHERE doc_id IN ({q})", tuple(doc_ids))}


def _store_rules(proj, st: F.FoldState, exp: ex.Expectation, kinds: dict) -> list:
    out = []
    if proj["ended"]:
        for c in st.cands.values():
            if c.state in F.ACTIVE + ("conflicted",):
                out.append(F.Retirement(c.match_id, c.activation, "rejected", "row-ended"))
    elif not exp.unknown:
        for c in st.active():
            if c.author == "auto" and kinds.get(c.doc_id) != exp.kind:
                out.append(F.Retirement(c.match_id, c.activation, "rejected", "kind-mismatch"))
    return out


def _holds(st: F.FoldState, r: F.Retirement) -> bool:
    """A produced retirement is recorded only if the fold's final state still
    shows it: the candidate is on that same activation, in the retired-to
    state. Recording exists to keep a retirement from being undone by a
    re-fold (spec §Match records); one that a later entry of the same
    activation already superseded (an unpair, a writer's `resolves=`) or a
    newer activation replaced protects nothing, and replaying it could never
    change the state. The case it filters is real: the occupancy check reads
    TODAY's store, so a re-fold of [pair m@a d, unpair m] with d now active
    on another lineage produces `retire m@a -> conflicted (occupied)` for a
    pairing unpaired long ago (fold review finding)."""
    c = st.cands.get(r.match_id)
    return c is not None and c.activation == r.activation and c.state == r.to


def _record_retirement(conn, pid, r: F.Retirement) -> None:
    append(conn, pid, "retire", "store", match_id=r.match_id, retire_activation=r.activation,
           retire_to=r.to, cause=r.cause)
    if r.cause == "occupied":
        doc = conn.execute("SELECT doc_id FROM matches WHERE match_id=?", (r.match_id,)).fetchone()
        other = conn.execute("SELECT pid FROM match_state WHERE doc_id=? AND pid<>? AND state IN"
                             " ('matched','proposed')", (doc[0], pid)).fetchone()
        add_residue(conn, pid, "occupied", f"document already on #{other[0] if other else '?'}")
    elif r.cause in ("row-ended", "kind-mismatch"):
        add_residue(conn, pid, r.cause, f"match {r.match_id}")


def _materialize(conn, pid, st: F.FoldState) -> list:
    refused = []
    for c in sorted(st.cands.values(), key=lambda c: c.state in F.ACTIVE):   # inactive first
        try:
            conn.execute(
                "INSERT INTO match_state(match_id, pid, doc_id, state, author, activation, fp)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET pid=excluded.pid,"
                " doc_id=excluded.doc_id, state=excluded.state, author=excluded.author,"
                " activation=excluded.activation, fp=excluded.fp",
                (c.match_id, pid, c.doc_id, c.state, c.author, c.activation, c.fp))
        except sqlite3.IntegrityError:
            refused.append(c)
    return refused


def _doc_digest(conn, doc_id) -> dict:
    d = conn.execute("SELECT kind, counterparty, issuer, document_date, document_number,"
                     " amount_minor, currency, recipient, sha256 FROM documents WHERE doc_id=?",
                     (doc_id,)).fetchone()
    return dict(d) if d is not None else {}


def _bump(old_digest, digest, revision) -> int:
    return revision + (1 if old_digest != digest else 0)


def _note_body(conn, red: R.Reduction) -> str | None:
    if red.status in ("ended", "ineligible"):
        return None
    body = STATUS_PHRASE[red.status]
    if red.current is not None:
        d = conn.execute("SELECT d.kind, d.issuer, d.counterparty, d.document_number, d.sha256"
                         " FROM matches m JOIN documents d ON d.doc_id=m.doc_id"
                         " WHERE m.match_id=?", (red.current,)).fetchone()
        who = d["issuer"] or d["counterparty"] or ""
        num = f" {d['document_number']}" if d["document_number"] else ""
        body += f"; document: {d['kind']} {who}{num} [{d['sha256'][:8]}]"
    return body + ". Supersedes earlier accounting notes."


def note_text(conn, pid) -> str | None:
    p = projection(conn, pid)
    if p["note_body"] is None:
        return None
    return f"Accounting revision {p['note_seq']}: {p['note_body']}"


def settle(conn, pid: int) -> R.Reduction:
    assert conn.in_transaction, "settle runs inside the write transaction"
    proj = projection(conn, pid)
    if proj["merged_into"] is not None:
        raise RuntimeError(f"#{pid} was merged into #{proj['merged_into']}")
    row = live_row(conn, proj)
    for _ in range(32):
        log = entries(conn, pid)
        st = F.fold(log, _occupied(conn, pid))
        kinds = _doc_kinds(conn, {c.doc_id for c in st.cands.values()})
        exp = expectation_for(conn, proj, row, exempt=st.exemption is not None)
        recorded = {(e.match_id, e.retire_activation, e.retire_to) for e in log
                    if e.kind == "retire"}
        new, seen = [], set()
        produced = [r for r in st.produced if _holds(st, r)]
        for r in produced + _store_rules(proj, st, exp, kinds):
            key = (r.match_id, r.activation, r.to)
            if key in recorded or key in seen:
                continue
            seen.add(key)
            new.append(r)
        if new:
            for r in new:
                _record_retirement(conn, pid, r)
            continue
        refused = _materialize(conn, pid, st)
        if refused:
            for c in refused:
                _record_retirement(conn, pid, F.Retirement(c.match_id, c.activation,
                                                           "conflicted", "occupied"))
            continue
        break
    else:
        raise RuntimeError(f"settle of #{pid} did not converge")

    last_known = proj["last_known_kind"]
    if not exp.unknown and exp.row != 1:
        last_known = exp.kind
    cp = kb.counterparty_for(conn, row["counterparty"]) if row else None
    inp = R.Inputs(ended=proj["ended"], eligible=eligible(conn, row), fold=st,
                   facts=R.facts_of(row) if row else None, expectation=exp,
                   last_known_kind=last_known, doc_kinds=kinds, portal=kb.is_portal(cp))
    red = R.reduce(inp)

    match_digests = {}
    for c in st.cands.values():
        m = conn.execute("SELECT label, rationale, runners_up_json FROM matches WHERE match_id=?",
                         (c.match_id,)).fetchone()
        live = c.state in F.ACTIVE + ("conflicted",)
        digest = db.canonical({
            # the live facts and expectation under the pairing, not only whether they
            # still agree with its fingerprint (round p2, Astra S1: €100 -> €90 -> €80
            # kept one revision, so a confirmation shown at €90 committed at €80)
            "facts": inp.facts if live else None,
            "exp": [exp.kind, exp.tier] if live else None,
            "payee": kb.display_name(conn, row["counterparty"]) if (live and row) else None,
            "state": c.state, "author": c.author, "activation": c.activation, "fp": c.fp,
            "verdict": R.kind_verdict(c, inp) if live else None,
            "row_ok": (json.loads(c.fp)["facts"] == inp.facts) if (live and c.fp) else None,
            "label": m["label"], "rationale": m["rationale"], "runners_up": m["runners_up_json"],
            "doc": _doc_digest(conn, c.doc_id)})
        old = conn.execute("SELECT digest, revision FROM match_state WHERE match_id=?",
                           (c.match_id,)).fetchone()
        conn.execute("UPDATE match_state SET digest=?, revision=? WHERE match_id=?",
                     (digest, _bump(old["digest"], digest, old["revision"]), c.match_id))
        match_digests[c.match_id] = digest

    pdigest = db.canonical({
        "status": red.status, "desired": sorted(red.desired), "current": red.current,
        "reasons": list(red.reasons), "exempt": st.exemption is not None,
        "cands": match_digests, "facts": inp.facts,
        "exp": [exp.kind, exp.tier, exp.row, exp.conflict], "ended": proj["ended"],
        "search_state": proj["search_state"], "identity": proj["identity_question"],
        # what the KB makes visible on the line (round p6, Astra S2: a renamed payee
        # kept the shown revision and a correction landed on an unseen identity)
        "payee": kb.display_name(conn, row["counterparty"]) if row else None,
        "link": cp["document_link"] if cp is not None else None,
        "portal": kb.is_portal(cp)})
    body = _note_body(conn, red)
    note_seq = proj["note_seq"]
    if body != proj["note_body"]:
        note_seq = db.next_seq(conn) if body is not None else None
    conn.execute(
        "UPDATE projections SET digest=?, revision=?, status=?, desired_json=?, current_match=?,"
        " reasons_json=?, exp_kind=?, exp_tier=?, exp_row=?, last_known_kind=?, note_seq=?,"
        " note_body=?, last_facts_json=coalesce(?, last_facts_json) WHERE pid=?",
        (pdigest, _bump(proj["digest"], pdigest, proj["revision"]), red.status,
         json.dumps(sorted(red.desired)), red.current, json.dumps(list(red.reasons)),
         exp.kind, exp.tier, exp.row, last_known, note_seq, body,
         db.canonical(row) if row else None, pid))
    return red


def latest_import(conn) -> int:
    """The latest successful import, as its snapshot id: AUTOINCREMENT, so it only
    grows (a refused import rolls back and leaves none). 0 before any import."""
    return conn.execute("SELECT coalesce(max(snapshot_id), 0) FROM snapshots").fetchone()[0]


def is_fresh(conn, proj) -> bool:
    """A lineage's classification is FRESH iff the sweep read its row after the
    latest successful import (fix E2). The export carries no tags, so an import
    makes every classification stale until the sweep re-reads the row; the one
    thing that refreshes it is sweep.record_observation. Compared by snapshot id,
    never by timestamp (a one-second clock cannot order an import and a read)."""
    seen = proj["class_observed_snapshot"]
    return seen is not None and seen >= latest_import(conn)


def live_pids(conn) -> list:
    return [r[0] for r in conn.execute("SELECT pid FROM projections WHERE merged_into IS NULL"
                                       " ORDER BY pid")]


def settle_all(conn, pids=None) -> None:
    for pid in (pids if pids is not None else live_pids(conn)):
        settle(conn, pid)


def settle_doc_holders(conn, doc_id: int) -> None:
    pids = sorted({r[0] for r in conn.execute("SELECT pid FROM match_state WHERE doc_id=?",
                                                (doc_id,))})
    settle_all(conn, pids)
