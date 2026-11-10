"""Progress, defined in ONE place: a hand-out of unit `unit` made progress since store
sequence `since` when, after it, one of its items (an erase check, the own-mail search, a
found attachment) was closed or enqueued, its payment was decided, (re)listed or searched,
or one of its mirror calls was reported. The hand-out settle (queues.settle) reads it."""
from __future__ import annotations


def made(conn, job_id, since, unit) -> bool:
    """Did the run `job_id`'s hand-out of `unit` (`payment:<pid>`, `erasures`, `filing` or
    `mirror`) progress after `since`?"""
    import queues
    pid = queues.pid_of_unit(unit)
    if unit in ("erasures", "filing") or pid is not None:
        if conn.execute("SELECT 1 FROM run_items WHERE job_id=? AND unit=? AND"
                        " (closed_seq > ? OR seq > ?)", (job_id, unit, since, since)).fetchone():
            return True
    if pid is not None and conn.execute(
            "SELECT 1 FROM run_work WHERE job_id=? AND pid=? AND (closed_seq > ? OR seq > ? OR"
            " searched_seq > ?)", (job_id, pid, since, since, since)).fetchone():
        return True
    if unit == "mirror" and conn.execute(
            "SELECT 1 FROM run_mirror WHERE job_id=? AND closed_seq > ?",
            (job_id, since)).fetchone():
        return True
    return False
