# tests/sim.py
"""The specialist's side of the sweep, done mechanically against a REAL
bank-feed (tests/bankfeed.py). This is the executable reference for the
procedure SKILL.md prescribes; the skill must say exactly this, in words:

  list_projections -> for each item:
    get_transaction(row_id); "no transaction #N" -> record_observation(not_found)
    else record_observation(observed_tags, observed_notes)
    make the ONE returned write (untag, tag or add_note) with workflow,
      expected_generation and expected_ledger exactly as returned
    a write that did not take -> record_observation(write_error=<reply>)
    read the row again and record_observation again; repeat until nothing is
      returned (plan §D14; round p5: never two writes without a read between)
"""
from __future__ import annotations

import sweep


def _read(bf, row_id):
    out = bf.call("get_transaction", row_id=row_id)
    if out.startswith("no transaction #"):
        return None
    first_seen = bf.conn.execute("SELECT first_seen FROM transactions WHERE row_id=?",
                                 (row_id,)).fetchone()[0]
    return bf.tags(row_id), bf.notes(row_id), first_seen


def observe_and_repair(conn, bf, token, item) -> dict:
    """Read, record, make the ONE returned write, read again, record again —
    until the server returns nothing to do (at most untag, tag and note)."""
    pid, row_id = item["pid"], item["row_id"]
    if item["ended"] == "erased":
        return {}
    got = _read(bf, row_id)
    if got is None:
        return sweep.record_observation(conn, pid=pid, token=token, not_found=True)
    r = {}
    for _ in range(4):
        tags, notes, first_seen = got
        r = sweep.record_observation(conn, pid=pid, token=token, observed_tags=tags,
                                     observed_notes=notes, observed_first_seen=first_seen)
        ins = r.get("instructions") or {}
        if not ins:
            return r
        kw = {"workflow": ins["workflow"], "expected_generation": ins["expected_generation"],
              "expected_ledger": ins["expected_ledger"]}
        if "untag" in ins:
            out = bf.call("untag_transaction", row_ids=[row_id], tags=ins["untag"], **kw)
            if set(ins["untag"]) & set(bf.tags(row_id)):
                return sweep.record_observation(conn, pid=pid, token=token, write_error=out)
        elif "tag" in ins:
            out = bf.call("tag_transaction", row_ids=[row_id], tags=ins["tag"], **kw)
            if not set(ins["tag"]) <= set(bf.tags(row_id)):
                return sweep.record_observation(conn, pid=pid, token=token, write_error=out)
        else:
            bf.call("add_note", row_ids=[row_id], note=ins["add_note"], author="agent", **kw)
        got = _read(bf, row_id)
        if got is None:
            return sweep.record_observation(conn, pid=pid, token=token, not_found=True)
    return r


def sweep_cycle(conn, bf, token, limit=25) -> int:
    n = 0
    while True:
        page = sweep.list_projections(conn, token=token, limit=limit)
        for item in page["projections"]:
            observe_and_repair(conn, bf, token, item)
            n += 1
        if page["remaining_in_cycle"] == 0:
            return n
