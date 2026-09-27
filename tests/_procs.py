"""Functions run in child processes (multiprocessing 'spawn'): each one puts
server/ on sys.path itself, because a spawned child imports this module
fresh."""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))


def hold_lock(path, seconds, ready):
    import db
    conn = db.open_store(path)
    try:
        with db.tx(conn):
            ready.set()
            time.sleep(seconds)
    finally:
        conn.close()


def allocate(path, n, out):
    import db
    conn = db.open_store(path)
    got = []
    try:
        for _ in range(n):
            with db.tx(conn):
                got.append(db.next_seq(conn))
    finally:
        conn.close()
    out.put(got)


def open_fresh(path, barrier):
    """Every spawned sibling calls this on the SAME not-yet-existing path,
    released by the barrier at (as near as the OS gets to) the same instant,
    so the very first WAL-mode conversion is contended. A non-zero process
    exit (an uncaught exception) is the failure signal the test reads."""
    import db
    barrier.wait(30)
    conn = db.open_store(path)
    conn.close()


def machine_pair(path, pid, doc_id, token, expected_revision, snapshot, out):
    import db
    import matches
    conn = db.open_store(path)
    try:
        r = matches.record_match(conn, pid=pid, doc_id=doc_id, author="auto",
                                 expected_revision=expected_revision, row_snapshot=snapshot,
                                 token=token)
        out.put(("ok", r["match_id"], r["state"]))
    except Exception as exc:          # reported to the parent, never swallowed
        out.put(("error", type(exc).__name__, str(exc)))
    finally:
        conn.close()
