"""The bank mirror (design rev 17 §2.4, R3): every in-scope row whose acct:: tags differ
from what its import carried, or whose desired note differs from the note last written
(`mirror_note`), is owed a write — whatever changed it (a decision, a tap between runs).
Tags group by identical add/remove lists, notes by identical text, ≤ 100 rows a call. No
read-backs: a write the model reports done is taken as written (D9).

Nothing is planned ahead (plan round 7): every hand-out re-hands the calls in flight, then
diffs the store afresh. A row's write is held — never handed again in this run — while a
call carrying it is in flight or after bank-feed refused it (§2.4: retried at the next
run). Held is per row and per write, not per call, so a row that joins a group later gets
a call of its own instead of repeating an append-only note on the rows already in flight."""
from __future__ import annotations

import json
import sys

import amounts
import dates
import db
import lineage
import passes
import reducer as R
import views

SUFFIX = " (quarterly check)"
ROWS_PER_CALL = 100            # bank-feed MAX_ROWS_PER_CALL (tools_annotate.py:63)
NOTE_MAX = 1000                # bank-feed NOTE_MAX (tools_annotate.py:57)
ORDER = ("untag_transaction", "tag_transaction", "add_note")
NEEDS_NONE = f"Accounting: no invoice needed{SUFFIX}"


def _doc_words(conn, match_id) -> str:
    """§2.4: kind, issuer, number (when it has one), date and the document's own amount."""
    d = conn.execute("SELECT d.* FROM matches m JOIN documents d ON d.doc_id=m.doc_id"
                     " WHERE m.match_id=?", (match_id,)).fetchone()
    parts = [views.KIND_WORD.get(d["kind"], "document")]
    who = (d["issuer"] or d["counterparty"] or "")[:80]
    if who:
        parts.append(who)
    if d["document_number"]:
        parts.append(d["document_number"][:60])
    tail = [dates.long_day(d["document_date"]) if d["document_date"] else None,
            amounts.fmt(d["amount_minor"], d["currency"])
            if d["amount_minor"] is not None and d["currency"] else None]
    return " · ".join([" ".join(parts)] + [t for t in tail if t])


def _pending(row) -> bool:
    """D18: a pending (non-BOOK) row carries no accounting tag and no accounting note; its
    stored desired tags stay as they are — only the mirror treats it specially."""
    return row is not None and row["status"] != "BOOK"


def note_text(conn, pid):
    """§2.4's plain words for `pid`'s outcome; None when no note is owed."""
    p = lineage.projection(conn, pid)
    status = p["status"]
    if status in (None, "ended", "ineligible") or _pending(lineage.live_row(conn, p)):
        return None
    if status == "matched" and p["current_match"]:
        text = f"Accounting: matched — {_doc_words(conn, p['current_match'])}{SUFFIX}"
    elif status == "proposed" and p["current_match"]:
        alts = json.loads(conn.execute("SELECT alternatives_json FROM matches WHERE"
                                       " match_id=?", (p["current_match"],)).fetchone()[0])
        more = (f" (or {len(alts)} other invoice{'s' if len(alts) != 1 else ''})"
                if alts else "")
        text = (f"Accounting: proposed — {_doc_words(conn, p['current_match'])}{more}, "
                f"awaiting confirmation{SUFFIX}")
    elif status == "proposed":            # D3: a joint machine set, the operator picks
        n = len(lineage.fold_of(conn, pid).machine_set())
        text = f"Accounting: proposed — {n} invoices fit, awaiting confirmation{SUFFIX}"
    elif status == "open":
        text = f"Accounting: invoice missing{SUFFIX}"
    else:                                 # exempt, no-document, optional
        text = NEEDS_NONE
    return text[:NOTE_MAX]


def _rows(conn) -> list:
    """Every row the latest import carried (its export tags are known), as (pid, p, row,
    in_scope): §2.4 "a diff over every in-scope row". A row the export still carries that
    left scope (ended, or ineligible: before a moved watermark) is included out of scope:
    it owes the removal of its acct:: tags, as 26b68ee's sweep untagged it (fix round 1).
    A row the import did not carry (or an erased one) is left alone."""
    latest = lineage.latest_import(conn)
    out = []
    for pid in lineage.live_pids(conn):
        p = lineage.projection(conn, pid)
        row = lineage.live_row(conn, p)
        if row is None:
            continue
        if p["class_observed_snapshot"] is None or p["class_observed_snapshot"] < latest:
            continue
        out.append((pid, p, row, not p["ended"] and lineage.eligible(conn, row)))
    return out


def _writes(conn) -> list:
    """Every row-level write the store owes the bank now: (tool, payload, row_id, pid),
    payload the sorted tag tuple or the note text."""
    out = []
    for pid, p, row, in_scope in _rows(conn):
        observed = set(json.loads(p["observed_tags_json"] or "[]"))
        desired = (set(json.loads(p["desired_json"] or "[]"))
                   if in_scope and not _pending(row) else set())
        removes = (observed & set(R.OWNED)) - desired
        adds = desired - observed
        if removes:
            out.append(("untag_transaction", tuple(sorted(removes)), row["row_id"], pid))
        if adds:
            out.append(("tag_transaction", tuple(sorted(adds)), row["row_id"], pid))
        text = note_text(conn, pid) if in_scope else None
        if text is not None and text != p["mirror_note"]:
            out.append(("add_note", text, row["row_id"], pid))
    return out


def _fence(conn) -> dict:
    gate = passes.bank_write_gate(conn)
    return {"workflow": gate["workflow"], "expected_generation": gate["expected_generation"],
            "expected_ledger": gate["expected_ledger"]}


def _calls(conn, writes) -> list:
    """Group row-level writes into bank-feed calls: identical tool and payload together,
    ≤ ROWS_PER_CALL rows each, in ORDER (untag, tag, note)."""
    groups = {}
    for tool, payload, row_id, pid in writes:
        groups.setdefault((ORDER.index(tool), payload), {})[row_id] = pid
    fence = _fence(conn) if groups else {}
    calls = []
    for (rank, payload) in sorted(groups):
        tool, members = ORDER[rank], groups[(rank, payload)]
        ids = sorted(members)
        for i in range(0, len(ids), ROWS_PER_CALL):
            chunk = ids[i:i + ROWS_PER_CALL]
            what = ({"note": payload, "author": "agent"} if tool == "add_note"
                    else {"tags": list(payload)})
            calls.append({"tool": tool, "args": {"row_ids": chunk, **what, **fence},
                          "pids": [members[r] for r in chunk]})
    return calls


def plan(conn) -> list:
    """Every owed call, in order: untag, tag, add_note. Each {"tool", "args", "pids"}; the
    args carry the row_ids and the gate's workflow fence."""
    return _calls(conn, _writes(conn))


def _held(conn, job_id) -> set:
    """(tool, row_id, payload) of every write this run has in flight or bank-feed refused."""
    out = set()
    for r in conn.execute("SELECT args_json FROM run_mirror WHERE job_id=? AND state IN"
                          " ('handed', 'failed')", (job_id,)):
        tool, args = json.loads(r["args_json"])
        payload = args["note"] if tool == "add_note" else tuple(args["tags"])
        out.update((tool, rid, payload) for rid in args["row_ids"])
    return out


def _fresh(conn, job_id) -> list:
    """THE mirror's owed calls, computed now (plan round 7, Astra S2 — no frozen plan): the
    store's writes against the export tags and mirror_note, minus the writes this run holds."""
    held = _held(conn, job_id)
    return _calls(conn, [w for w in _writes(conn) if (w[0], w[2], w[1]) not in held])


def start(conn, job_id) -> None:
    """§2.4: one log line when the run's mirror phase starts, so a restart inside it can be
    placed; runs.mirror_at stamped. Once per run."""
    with db.tx(conn):
        run = conn.execute("SELECT mirror_at FROM runs WHERE job_id=?", (job_id,)).fetchone()
        if run is None:
            raise db.Refusal(f"there is no run {job_id}")
        if run[0]:
            return
        rows = len({p for c in plan(conn) for p in c["pids"]})
        conn.execute("UPDATE runs SET mirror_at=? WHERE job_id=?", (db.now(), job_id))
    print(f"mirror: start job={job_id} rows={rows}", file=sys.stderr, flush=True)


def hand_calls(conn, job_id, budget: int) -> list:
    """The next calls, each {"n", "tool", "args"}: the in-flight ones first (a restart
    between the bank writes and record_mirror re-hands them, D9), then a fresh diff, up to
    `budget`."""
    budget = max(0, int(budget))
    with db.tx(conn):
        out = [{"n": r["n"], "tool": r["tool"], "args": json.loads(r["args_json"])[1]}
               for r in conn.execute("SELECT n, tool, args_json FROM run_mirror WHERE"
                                     " job_id=? AND state='handed' ORDER BY n", (job_id,))]
        n = conn.execute("SELECT coalesce(max(n), 0) FROM run_mirror WHERE job_id=?",
                         (job_id,)).fetchone()[0]
        for c in _fresh(conn, job_id)[:max(0, budget - len(out))]:
            n += 1
            conn.execute("INSERT INTO run_mirror(job_id, n, tool, args_json, pids_json, state)"
                         " VALUES (?,?,?,?,?, 'handed')",
                         (job_id, n, c["tool"], db.canonical([c["tool"], c["args"]]),
                          json.dumps(c["pids"])))
            out.append({"n": n, "tool": c["tool"], "args": c["args"]})
    return out[:budget]


def _numbers(items) -> list:
    try:
        return [int(n) for n in items]
    except (TypeError, ValueError):
        raise db.Refusal("done is a list of call numbers (n)") from None


def _superseded(conn, job_id, n, pid, tool) -> bool:
    """A later call of the same kind (the note; or the tags, tag and untag alike) for `pid`
    was already recorded done in this run: this older one's acknowledgement must not
    overwrite it, across record_mirror calls too (fix round 1)."""
    kinds = ("add_note",) if tool == "add_note" else ("tag_transaction", "untag_transaction")
    return conn.execute(
        "SELECT 1 FROM run_mirror m, json_each(m.pids_json) j WHERE m.job_id=? AND m.n>?"
        " AND m.state='done' AND m.tool IN (%s) AND j.value=? LIMIT 1"
        % ",".join("?" * len(kinds)), (job_id, n, *kinds, pid)).fetchone() is not None


def record(conn, token, done, failed) -> dict:
    """The model's report of a mirror unit (D9): the calls bank-feed accepted are taken as
    written (the export tags and mirror_note move, no read-back); a refused call is kept
    for the end message and not handed again in this run. Unknown or already recorded
    numbers are ignored."""
    import decide
    done = sorted(_numbers(done or []))        # hand-out order: the later note wins
    bad = [f for f in failed or [] if not isinstance(f, dict)]
    if bad:
        raise db.Refusal("each failed entry is {n, error}")
    fails = [(_numbers([f.get("n")])[0], str(f.get("error") or "")[:300]) for f in failed or []]
    recorded = 0
    with db.tx(conn):
        passes.check_token(conn, token)
        claim = conn.execute("SELECT job_id FROM claims WHERE gen=?", (int(token),)).fetchone()
        if claim is None:
            raise db.Refusal("that pass_token belongs to no job run")
        job_id = claim[0]
        for n in done:
            r = conn.execute("SELECT args_json, pids_json FROM run_mirror WHERE job_id=? AND"
                             " n=? AND state='handed'", (job_id, n)).fetchone()
            if r is None:
                continue
            tool, args = json.loads(r["args_json"])
            for pid in json.loads(r["pids_json"]):
                if _superseded(conn, job_id, n, pid, tool):
                    continue                  # a later call's acknowledgement already landed
                if tool == "add_note":
                    conn.execute("UPDATE projections SET mirror_note=?, last_error=NULL"
                                 " WHERE pid=?", (args["note"], pid))
                    continue
                p = lineage.projection(conn, pid)
                tags = set(json.loads(p["observed_tags_json"] or "[]"))
                tags = ((tags | set(args["tags"])) if tool == "tag_transaction"
                        else (tags - set(args["tags"])))
                conn.execute("UPDATE projections SET observed_tags_json=?, last_error=NULL"
                             " WHERE pid=?", (json.dumps(sorted(tags)), pid))
            conn.execute("UPDATE run_mirror SET state='done' WHERE job_id=? AND n=?",
                         (job_id, n))
            recorded += 1
        for n, err in fails:
            r = conn.execute("SELECT pids_json FROM run_mirror WHERE job_id=? AND n=? AND"
                             " state='handed'", (job_id, n)).fetchone()
            if r is None:
                continue
            conn.execute("UPDATE run_mirror SET state='failed', error=? WHERE job_id=? AND"
                         " n=?", (err, job_id, n))
            conn.executemany("UPDATE projections SET last_error=? WHERE pid=?",
                             [(err, pid) for pid in json.loads(r["pids_json"])])
            recorded += 1
        if recorded:
            decide.note_progress(conn, token)
        left = owed(conn, job_id)
    return {"recorded": recorded, "owed": left}


def owed(conn, job_id) -> int:
    """In flight plus a FRESH diff: 0 only when the bank agrees with the store as it is now,
    whatever changed since the mirror phase began (a handover, a tap). The run reaches
    `post` only at 0."""
    inflight = conn.execute("SELECT count(*) FROM run_mirror WHERE job_id=? AND"
                            " state='handed'", (job_id,)).fetchone()[0]
    return inflight + len(_fresh(conn, job_id))


def failed_lines(conn, job_id) -> list:
    """§2.4 last bullet: a failed write never blocks the work; the end message lists it."""
    n = conn.execute("SELECT count(*) FROM run_mirror WHERE job_id=? AND state='failed'",
                     (job_id,)).fetchone()[0]
    return ([f"{n} bank-ledger update{'s' if n != 1 else ''} did not go through — tried "
             "again at the next check."] if n else [])
