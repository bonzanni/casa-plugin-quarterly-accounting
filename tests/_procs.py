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
