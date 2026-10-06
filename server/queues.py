"""Server-owned per-run work queues (operator ruling A; docs specs/rounds-2026-10-06-simple-
loop-diff/queues-design.md, q6). Every piece of work a unit owes is a server row from the
moment it is known, and a unit is finished only when it owes nothing:

- `run_items`: an erase candidate (unit `erasures`), the own-mail search and the attachments
  it found (unit `filing`), the attachments a vendor's search found (unit `vendor:<norm>`);
- `run_work`: a vendor's payments to decide (loop.py builds it); `run_mirror`: the mirror's
  calls (mirror.py hands them out).

THE rule: a unit is handed with its queued items, oldest first, as many as fit; the next
`job_next` settles that hand-out once (`settle`): one that closed or enqueued an item — or
first recorded a search kind for one of its payments — progressed; one that did not raises
the attempts of every item it carried, and at ATTEMPTS_MAX an item is given up, visibly.
Closing writes close only `queued` items: `done` and `given_up` are terminal for the run."""
from __future__ import annotations

import db
import kb

ATTEMPTS_MAX = 2         # D8: an item is handed again at most once without progress
COST = {"erase": 2, "search": 2, "ref": 3}   # calls per item: get_transaction + a write;
# search_emails + the probe; download + Read + ingest_document
UPSTREAM = ("erase", "search", "ref")        # every kind a decision depends on (rule 5)


def unit_of_vendor(vendor) -> str:
    return "vendor:" + kb.norm(vendor or "")


def job_of(conn, token):
    r = conn.execute("SELECT job_id FROM claims WHERE gen=?", (int(token),)).fetchone()
    return r[0] if r is not None else None


def handed_unit(conn, job_id):
    r = conn.execute("SELECT hand_unit, end_render_id FROM runs WHERE job_id=?",
                     (job_id,)).fetchone()
    if r is None or r["end_render_id"] is not None:
        return None                   # the post cutoff: nothing joins a queue after it
    return r["hand_unit"]


def enqueue(conn, job_id, unit, kind, keys) -> list:
    """Items `keys` of `kind` join `unit`'s queue (a key already there, in any state, stays
    as it is). Returns the keys newly enqueued."""
    assert conn.in_transaction
    new = []
    for k in dict.fromkeys(str(k) for k in keys):
        cur = conn.execute("INSERT OR IGNORE INTO run_items(job_id, unit, kind, key, state,"
                           " seq) VALUES (?,?,?,?, 'queued', ?)",
                           (job_id, unit, kind, k, db.next_seq(conn)))
        if cur.rowcount:
            new.append(k)
    return new


def require_handed(conn, job_id, unit, what) -> None:
    """A model's write enqueues only into the unit currently handed out (q2)."""
    if handed_unit(conn, job_id) != unit:
        raise db.Refusal(f"{what} goes with the unit that is handed out now: call job_next")


def close(conn, job_id, kind, key, *, reason=None, unit=None) -> int:
    """The item's closing write: every QUEUED item of `kind` with `key` in the run (every
    unit, unless `unit`) is done. `job_id` None: in every run. Returns how many closed."""
    assert conn.in_transaction
    sql = "UPDATE run_items SET state='done', closed_seq=?, reason=coalesce(?, reason) WHERE" \
          " kind=? AND key=? AND state='queued'"
    args = [db.next_seq(conn), reason, kind, str(key)]
    if job_id is not None:
        sql += " AND job_id=?"
        args.append(job_id)
    if unit is not None:
        sql += " AND unit=?"
        args.append(unit)
    return conn.execute(sql, args).rowcount


def queued(conn, job_id, unit, kind=None) -> list:
    sql, args = "SELECT * FROM run_items WHERE job_id=? AND unit=? AND state='queued'", \
        [job_id, unit]
    if kind is not None:
        sql += " AND kind=?"
        args.append(kind)
    return conn.execute(sql + " ORDER BY seq", args).fetchall()


CHARS_MAX = 12_000       # d4: a hand-out's exact refs, at most this many characters (≥ 1 item)
KEY_MAX = 4_000          # one ref's length: refused beyond it, never clipped


def take_fitting(rows, room) -> list:
    """The queued items that fit `room` calls (and CHARS_MAX of keys), oldest first: a
    hand-out."""
    out, used, chars = [], 0, 0
    for r in rows:
        chars += len(r["key"]) + 4
        if used + COST[r["kind"]] > room or (out and chars > CHARS_MAX):
            break
        out.append(r)
        used += COST[r["kind"]]
    return out


def check_refs(refs, what) -> list:
    if not isinstance(refs, list) or not all(isinstance(r, str) and r and len(r) <= KEY_MAX
                                             for r in refs):
        raise db.Refusal(f"{what} is a list of <message id>:<attachment id>, each found "
                         "attachment exactly ([] when the search found none)")
    return list(dict.fromkeys(refs))


def stamp(conn, job_id, rows, hand_seq) -> None:
    for r in rows:
        conn.execute("UPDATE run_items SET hand_seq=? WHERE job_id=? AND unit=? AND kind=?"
                     " AND key=?", (hand_seq, job_id, r["unit"], r["kind"], r["key"]))


def owing_vendors(conn, job_id) -> set:
    """The vendor units with a queued found attachment."""
    return {r[0] for r in conn.execute("SELECT DISTINCT unit FROM run_items WHERE job_id=?"
                                       " AND unit LIKE 'vendor:%' AND state='queued'",
                                       (job_id,))}


def blocks_decide(conn, job_id, unit) -> bool:
    """Rule 3: a vendor with a queued or given-up found attachment decides nothing."""
    return conn.execute("SELECT 1 FROM run_items WHERE job_id=? AND unit=? AND kind='ref'"
                        " AND state IN ('queued', 'given_up')", (job_id, unit)
                        ).fetchone() is not None


def gave_up_upstream(conn, job_id) -> bool:
    """Rule 5: the run gave up an item a decision depends on, at any time."""
    return conn.execute("SELECT 1 FROM run_items WHERE job_id=? AND state='given_up'",
                        (job_id,)).fetchone() is not None


def given_up(conn, job_id) -> dict:
    return dict(conn.execute("SELECT kind, count(*) FROM run_items WHERE job_id=? AND"
                             " state='given_up' GROUP BY kind", (job_id,)).fetchall())


# ---- the settle (rule 2) -------------------------------------------------------------------
def _progressed(conn, job_id, unit, h) -> bool:
    if conn.execute("SELECT 1 FROM run_items WHERE job_id=? AND unit=? AND (closed_seq > ?"
                    " OR seq > ?)", (job_id, unit, h, h)).fetchone():
        return True
    if unit == "mirror":
        return conn.execute("SELECT 1 FROM run_mirror WHERE job_id=? AND closed_seq > ?",
                            (job_id, h)).fetchone() is not None
    if unit.startswith("vendor:"):
        return any(unit_of_vendor(r["vendor"]) == unit for r in conn.execute(
            "SELECT vendor FROM run_work WHERE job_id=? AND (closed_seq > ? OR seq > ? OR"
            " searched_seq > ?)", (job_id, h, h, h)))
    return False


def settle(conn, job_id) -> None:
    """The last hand-out judged, once (rule 2). Inside the cursor's transaction."""
    assert conn.in_transaction
    run = conn.execute("SELECT hand_unit, hand_seq FROM runs WHERE job_id=?",
                       (job_id,)).fetchone()
    if run is None or run["hand_unit"] is None:
        return
    unit, h = run["hand_unit"], run["hand_seq"]
    conn.execute("UPDATE runs SET hand_unit=NULL WHERE job_id=?", (job_id,))
    if _progressed(conn, job_id, unit, h):
        return
    conn.execute("UPDATE run_items SET attempts=attempts+1 WHERE job_id=? AND unit=? AND"
                 " hand_seq=? AND state='queued'", (job_id, unit, h))
    conn.execute("UPDATE run_items SET state='given_up', reason='not reached', closed_seq=?"
                 " WHERE job_id=? AND unit=? AND state='queued' AND attempts >= ?",
                 (db.next_seq(conn), job_id, unit, ATTEMPTS_MAX))
    if unit.startswith("vendor:"):
        for r in conn.execute("SELECT pid, vendor FROM run_work WHERE job_id=? AND hand_seq=?"
                              " AND outcome IS NULL", (job_id, h)).fetchall():
            if unit_of_vendor(r["vendor"]) == unit:
                conn.execute("UPDATE run_work SET attempts=attempts+1 WHERE job_id=? AND"
                             " pid=?", (job_id, r["pid"]))
    elif unit == "mirror":
        import mirror
        conn.execute("UPDATE run_mirror SET attempts=attempts+1 WHERE job_id=? AND"
                     " hand_seq=? AND state='handed'", (job_id, h))
        mirror.give_up(conn, job_id, ATTEMPTS_MAX)


REASON_MAX = 200


def set_aside(conn, token, items, reason) -> dict:
    """The one closing write for an item no other write closes (q1): a found attachment that
    is no invoice or no file this plugin takes ({"ref": …}), an erase candidate bank-feed
    still has ({"pid": …}). `reason` is kept on the item. Inside its own transaction."""
    import decide
    import job
    if not isinstance(items, list) or not items or not all(
            isinstance(i, dict) and len(i) == 1 and (
                (isinstance(i.get("ref"), str) and i["ref"]) or
                (isinstance(i.get("pid"), int) and not isinstance(i.get("pid"), bool)))
            for i in items):
        raise db.Refusal('items is a list of {"ref": <message id>:<attachment id>} or '
                         '{"pid": <an erase candidate\'s pid>}')
    if not isinstance(reason, str) or not reason.strip():
        raise db.Refusal("reason says why, in a few words (no invoice; still in bank-feed)")
    reason = reason.strip()[:REASON_MAX]
    done, not_queued = [], []
    with db.tx(conn):
        job.check_claim(conn, token)
        job_id = job_of(conn, token)
        for i in items:
            kind, key = ("ref", i["ref"]) if "ref" in i else ("erase", i["pid"])
            (done if close(conn, job_id, kind, key, reason=reason) else not_queued).append(i)
        if done:
            decide.note_progress(conn, token)
    return {"set_aside": done, "not_queued": not_queued}
