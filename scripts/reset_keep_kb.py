#!/usr/bin/env python3
"""A fresh store that keeps the KB, the binding and the watermark (simple loop §6.8), for the
live acceptance's second run.

    python3 scripts/reset_keep_kb.py        # store at $CLAUDE_PLUGIN_DATA

It reads the binding row, every `counterparties` row (learned hints included) and every
`chain_overrides` row, runs `binding.reset_store` (the same erasure the reset_store tool does),
and re-inserts the kept rows in ONE transaction. Table by table, at schema 15:

  KEPT      counterparties (verbatim, hint_sender/hint_subject included), chain_overrides
            (verbatim), binding: account_id, account_label, watermark, bound_at, package_name,
            package_name_announced, watermark_announced (ledger_* stay NULL, row_high_water 0,
            ledger_reset_ack 0 as a reset leaves them), meta (schema_version; a dirty-ledger
            refusal), counters (seq reset to 0, pass_generation bumped).
  WIPED     everything else: passes, probes, documents (and the files), snapshots, bank_rows,
            projections, aliases, matches, log, match_state, residue, renders, render_items,
            render_states, shown, packages (and the files), deliveries, delivered_rows, alerts,
            operator_refs, claims, runs, readings, render_keys, account_choices, post_offers,
            run_work, run_mirror, quarter_notices, pass_marker, work_requests.

Refuses a store whose schema version is not 15. It never touches bank-feed's ledger: PLAY
restores bank-feed's install backup first, as its reset recipe does. Before erasing, `main`
copies the store beside itself (accounting.sqlite.pre-reset-<time>); the copy is never deleted.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
import binding  # noqa: E402
import db  # noqa: E402

REQUIRED_SCHEMA = 15
_KEPT_BINDING = ("account_id", "account_label", "watermark", "bound_at", "package_name",
                 "package_name_announced", "watermark_announced")
_KB_TABLES = ("counterparties", "chain_overrides")


def _schema_version(conn) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return -1
    return int(row[0]) if row else -1


def _insert(conn, table: str, row) -> None:
    cols = list(row.keys())
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 tuple(row[c] for c in cols))


def reset_keep_kb(conn) -> dict:
    version = _schema_version(conn)
    if version != REQUIRED_SCHEMA:
        raise db.Refusal(f"the store is at schema {version}, this script needs "
                         f"{REQUIRED_SCHEMA}; nothing was changed")
    conn.row_factory = sqlite3.Row
    b = conn.execute("SELECT * FROM binding WHERE id=1").fetchone()
    if b is None:
        raise db.Refusal("the store has no binding to keep; nothing was changed")
    kept_binding = {c: b[c] for c in _KEPT_BINDING}
    kb_rows = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
               for t in _KB_TABLES}
    out = binding.reset_store(conn)
    with db.tx(conn):
        cols = list(kept_binding)
        conn.execute(f"INSERT INTO binding (id,{','.join(cols)}) VALUES (1,{','.join('?' * len(cols))})",
                     tuple(kept_binding.values()))
        for t in _KB_TABLES:
            for r in kb_rows[t]:
                _insert(conn, t, r)
    return {"erasure": out["erasure"], "report": out["report"],
            "kept": {t: len(kb_rows[t]) for t in _KB_TABLES},
            "binding": {"account_id": kept_binding["account_id"],
                        "watermark": kept_binding["watermark"]}}


def main(argv) -> int:
    path = db.data_dir() / db.DB_NAME
    if not path.exists():
        print(f"no store at {path}", file=sys.stderr)
        return 2
    # Look at the version BEFORE open_store, which would migrate an older store.
    raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = _schema_version(raw)
        if version != REQUIRED_SCHEMA:
            print(f"refused: the store is at schema {version}, this script needs "
                  f"{REQUIRED_SCHEMA}; nothing was changed", file=sys.stderr)
            return 2
        stem = f"{path.name}.pre-reset-{db.now().replace(':', '')}"
        backup, n = path.with_name(stem), 0
        while True:     # never overwrite: a second run in the same second gets a counter
            try:
                fd = os.open(str(backup), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                break
            except FileExistsError:
                n += 1
                backup = path.with_name(f"{stem}-{n}")
        dest = sqlite3.connect(str(backup))
        try:
            raw.backup(dest)
        finally:
            dest.close()
        os.chmod(backup, 0o600)
    finally:
        raw.close()
    conn = db.open_store()
    try:
        out = reset_keep_kb(conn)
    except db.Refusal as exc:
        kind = "busy" if isinstance(exc, db.Busy) else "refused"
        print(f"{kind}: {exc}; the store was copied to {backup}", file=sys.stderr)
        return 1 if kind == "busy" else 2
    except sqlite3.IntegrityError as exc:
        print(f"failed: {exc}; the store may be partly reset, the copy is at {backup}",
              file=sys.stderr)
        return 1
    finally:
        conn.close()
    out["backup"] = str(backup)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out["erasure"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
