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


def _report(out, fn):
    try:
        out.put(("ok", fn()))
    except BaseException as exc:             # the parent asserts on what happened
        out.put(("error", f"{type(exc).__name__}: {exc}"))


def gate_paused(path, decided, resume, out):
    """bank_write_gate, paused right after _decide_gate returned its verdict
    and before that verdict is persisted (fix wave B, Astra S1)."""
    import db
    import passes
    real = passes._decide_gate

    def paused(conn):
        verdict = real(conn)
        decided.set()
        resume.wait(30)
        return verdict
    passes._decide_gate = paused
    conn = db.open_store(path)
    try:
        _report(out, lambda: passes.bank_write_gate(conn))
    finally:
        conn.close()


def ingest_paused(source_path, installed, resume, out):
    """ingest_document, paused after its bytes are installed and before the
    index row commits (fix wave B, Astra + Terra S1)."""
    import db
    import documents
    real = documents._install

    def paused(*a, **kw):
        final = real(*a, **kw)
        installed.set()
        resume.wait(30)
        return final
    documents._install = paused
    conn = db.open_store()
    try:
        _report(out, lambda: documents.ingest_document(
            conn, source_path=source_path, kind="invoice", source="gmail",
            extraction_author="resident"))
    finally:
        conn.close()


def reset_paused(committed, resume, out):
    """reset_store, paused after its row wipe committed and before documents/
    is removed (fix wave B, Terra's variant)."""
    import shutil

    import binding
    import db
    real = shutil.rmtree
    first = []

    def paused(*a, **kw):
        if not first:
            first.append(1)
            committed.set()
            resume.wait(30)
        return real(*a, **kw)
    shutil.rmtree = paused
    conn = db.open_store()
    try:
        _report(out, lambda: binding.reset_store(conn))
    finally:
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

def pending_rendering(path, barrier, out):
    """Both siblings call this on the SAME already-seeded store, released by
    the barrier as near as the OS gets to the same instant, modeling end_pass
    freeing the pass marker and a second pass reaching alerts.pending_rendering
    while the first pass's own call is still in flight."""
    import db
    import alerts
    conn = db.open_store(path)
    try:
        barrier.wait(30)
        out.put(alerts.pending_rendering(conn))
    finally:
        conn.close()



def import_export(export_path, token, instance, out):
    """A second session's import_ledger_export under the same pass token (round
    E3, Astra S1: an import landing between a read and its record)."""
    import db
    import ledger
    conn = db.open_store()
    try:
        _report(out, lambda: ledger.import_ledger_export(
            conn, path=export_path, token=token, ledger_instance=instance)["snapshot"])
    finally:
        conn.close()


def build_paused(quarter, rendered, resume, out):
    """build_quarterly_package, paused after its frozen inputs were rendered and
    before the package is registered (round E3, Terra S1)."""
    import db
    import package
    real = package._render

    def paused(*a, **kw):
        result = real(*a, **kw)
        rendered.set()
        resume.wait(30)
        return result
    package._render = paused
    conn = db.open_store()
    try:
        _report(out, lambda: package.build_quarterly_package(conn, quarter))
    finally:
        conn.close()
