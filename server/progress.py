"""Progress, defined in ONE place (rev 18.4 §R18.5; d7 Astra S1b): a run made progress since
store sequence `since` when, after it, a payment was decided or (re)listed, a search was
recorded, an owed item (an erase check, the own-mail search, a found attachment) was closed
or enqueued, a document was filed, or a mirror call was reported. Casa's batch report
(loop._close) and the hand-out settle (queues.settle) both read it; the import and the
probes are progress through claims.progressed (decide.note_progress), which _close reads
alongside."""
from __future__ import annotations


def made(conn, job_id, since, unit=None) -> bool:
    """Did the run `job_id` progress after `since`? `unit` narrows it to one hand-out's
    unit: `payment:<pid>`, `erasures`, `filing` or `mirror`."""
    import queues
    pid = queues.pid_of_unit(unit)
    if unit is None or unit in ("erasures", "filing") or pid is not None:
        sql = "SELECT 1 FROM run_items WHERE job_id=? AND (closed_seq > ? OR seq > ?)"
        args = [job_id, since, since]
        if unit is not None:
            sql += " AND unit=?"
            args.append(unit)
        if conn.execute(sql, args).fetchone():
            return True
    if unit is None or pid is not None:
        sql = ("SELECT 1 FROM run_work WHERE job_id=? AND (closed_seq > ? OR seq > ? OR"
               " searched_seq > ?)")
        args = [job_id, since, since, since]
        if pid is not None:
            sql += " AND pid=?"
            args.append(pid)
        if conn.execute(sql, args).fetchone():
            return True
    if unit in (None, "mirror") and conn.execute(
            "SELECT 1 FROM run_mirror WHERE job_id=? AND closed_seq > ?",
            (job_id, since)).fetchone():
        return True
    if unit is None and conn.execute("SELECT 1 FROM documents WHERE filed_seq > ?",
                                     (since,)).fetchone():
        return True
    return False
