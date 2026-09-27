# server/delivery.py
"""Delivery staging and the delivery log (spec §Packaging). A package or one
invoice is staged, never rebuilt: Telegram through Casa's plugin outbox
(atomic .part -> rename; consumed on send, reaped at 2 h), email through
Casa's handoff folder for gmail's send_email, which Casa gates with one
approval tap showing the recipient (the operator's own mailbox only — the
skill says so; the tap is where it is checked). An ambiguous outcome is
`uncertain`, never retried automatically; a resend is the exact retained file.

Staging reads bytes out of packages/ or documents/, so it holds the custody
lock (db.custody_lock) from the read through the delivery-log row, taken
BEFORE any transaction (lock order: custody, then SQLite). Every write into
the delivery log checks the pass token: a stale pass is refused there too
(spec §"Running the pass on demand")."""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import tempfile

import casa_handoff
import db
import documents
import package
import passes

GMAIL_ATTACHMENT_LIMIT = casa_handoff.MAX_FILE_BYTES      # the handoff folder's 25 MB per file


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CASA_PLUGIN_OUTBOX_DIR") or "/data/plugin-outbox")


def _to_outbox(name: str, data: bytes) -> tuple:
    """(path, created). Called under the custody lock, so no other staging
    races it. A file already at `name` is never replaced: the same bytes (a
    resend of a retained package whose earlier copy is still waiting) are
    reused and are not this call's to remove; other bytes are refused, so one
    staged delivery can never overwrite another's file."""
    d = outbox_dir()
    if (d / name).exists():
        if (d / name).read_bytes() == data:
            return d / name, False
        raise db.Refusal(f"another file named {name} is still waiting to be sent; "
                         "send or clear that one first")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.part-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, d / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return d / name, True


def stage_for_delivery(conn, *, channel, package_id=None, doc_id=None, pass_token=None) -> dict:
    if channel not in ("telegram", "email"):
        raise db.Refusal("channel is 'telegram' or 'email'")
    if (package_id is None) == (doc_id is None):
        raise db.Refusal("stage one package or one document")
    if conn.in_transaction:
        raise RuntimeError("stage_for_delivery takes the custody lock before its own transaction")
    passes.check_token(conn, pass_token)
    with db.custody_lock():
        return _stage(conn, channel, package_id, doc_id, pass_token)


def _stage(conn, channel, package_id, doc_id, pass_token) -> dict:
    if package_id is not None:
        pk = conn.execute("SELECT * FROM packages WHERE package_id=?", (package_id,)).fetchone()
        if pk is None:
            raise db.Refusal(f"there is no package #{package_id}")
        if channel == "telegram" and (pk["oversize"] or pk["size"] > package.MAX_ZIP_BYTES):
            raise db.Refusal(f"{pk['filename']} is over Telegram's 20 MB limit "
                             f"({pk['size'] / 1e6:.1f} MB); it is kept here, and notes.md "
                             "names the largest files")
        name, data = pk["filename"], pathlib.Path(pk["path"]).read_bytes()
    else:
        d = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if d is None:
            raise db.Refusal(f"there is no document #{doc_id}")
        # named clear of every file still in the outbox (casefolded, as
        # doc_filename compares): two invoices of one vendor, day and amount
        # otherwise share a name, and the second would replace the first's bytes
        try:
            taken = {n.casefold() for n in os.listdir(outbox_dir())} if channel == "telegram" \
                else set()
        except FileNotFoundError:
            taken = set()
        name = package.doc_filename(dict(d), taken, (d["ingested_at"] or "")[:10])
        data = documents.path_of(conn, doc_id).read_bytes()
    if channel == "email" and len(data) > GMAIL_ATTACHMENT_LIMIT:
        raise db.Refusal(f"{name} is over the 25 MB email attachment limit "
                         f"({len(data) / 1e6:.1f} MB); it is kept here")
    request_id, created = None, False
    if channel == "telegram":
        path, created = _to_outbox(name, data)
        note = "send it with send_media(kind='zip' or 'pdf'), then record_delivery"
    else:
        try:
            path = pathlib.Path(casa_handoff.publish("quarterly-accounting", name, data=data)["path"])
        except casa_handoff.HandoffError as exc:
            raise db.Refusal(f"the handoff folder refused it ({exc.kind}): {exc}")
        request_id = "qa-" + secrets.token_hex(8)
        note = ("attach it with gmail's send_email to the operator's own address only, passing "
                "this request_id; Casa shows them the recipient before it sends")
    try:
        with db.tx(conn):
            passes.check_token(conn, pass_token)
            did = conn.execute("INSERT INTO deliveries(package_id, doc_id, channel, staged_path,"
                               " request_id, status, created_at) VALUES (?,?,?,?,?, 'staged', ?)",
                               (package_id, doc_id, channel, str(path), request_id,
                                db.now())).lastrowid
    except BaseException:
        if created:                     # nothing in the log names it: never leave it to be sent
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise
    out = {"delivery_id": did, "channel": channel, "path": str(path), "filename": name,
           "note": note}
    if request_id:
        out["request_id"] = request_id
    return out


def record_delivery(conn, *, delivery_id, outcome, message_id=None, pass_token=None) -> dict:
    if outcome not in ("delivered", "uncertain", "failed"):
        raise db.Refusal("outcome is 'delivered', 'uncertain' or 'failed'")
    with db.tx(conn):
        passes.check_token(conn, pass_token)
        d = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if d is None:
            raise db.Refusal(f"there is no delivery #{delivery_id}")
        if d["status"] == "delivered":
            return {"delivery_id": delivery_id, "status": "delivered", "already": True}
        if outcome == "delivered" and d["channel"] == "email" and not message_id:
            raise db.Refusal("an email counts as delivered only when send_email returned a "
                             "message id; otherwise record it 'uncertain'")
        conn.execute("UPDATE deliveries SET status=?, message_id=?, settled_at=? WHERE"
                     " delivery_id=?", (outcome, message_id, db.now(), delivery_id))
        if outcome == "delivered" and d["package_id"] is not None:
            # the rows exactly as the package froze them: facts_fp is
            # db.canonical(reducer.facts_of(row)), what ledger's delivered checks compare
            pk = conn.execute("SELECT manifest_json FROM packages WHERE package_id=?",
                              (d["package_id"],)).fetchone()
            for r in json.loads(pk[0])["rows"]:
                conn.execute("INSERT OR REPLACE INTO delivered_rows(package_id, row_id, pid,"
                             " facts_fp, kind) VALUES (?,?,?,?,?)",
                             (d["package_id"], r["row_id"], r["pid"], r["facts_fp"], r["kind"]))
            conn.execute("UPDATE binding SET package_name_announced=1 WHERE id=1")
        return {"delivery_id": delivery_id, "status": outcome}


_LATEST = ("SELECT d.package_id, d.status, p.filename, p.quarter FROM deliveries d JOIN packages p"
           " ON p.package_id=d.package_id WHERE d.delivery_id IN (SELECT max(delivery_id) FROM"
           " deliveries WHERE package_id IS NOT NULL GROUP BY package_id)")


def resendable(conn, quarter=None):
    """The most recent package whose latest send is uncertain, else the most
    recent one whose latest send was delivered (the brief's interface). A
    package whose latest send failed is skipped whatever its older sends said
    (round p8, Terra S2). What "send it again" resends is resend_target, which
    binds to what the operator was shown, not this."""
    sql, args = _LATEST, []
    if quarter:
        sql += " AND p.quarter=?"
        args.append(quarter)
    rows = conn.execute(sql + " ORDER BY d.delivery_id DESC", args).fetchall()
    for want in ("uncertain", "delivered"):
        for r in rows:
            if r["status"] == want:
                return r["package_id"]
    return None


def uncertain(conn, quarter=None) -> list:
    """Packages whose most recent send is uncertain (offered in words), as
    (package_id, filename), oldest package first; `quarter` scopes them to a
    quarter-scoped view."""
    sql, args = _LATEST + " AND d.status='uncertain'", []
    if quarter:
        sql += " AND p.quarter=?"
        args.append(quarter)
    return [(r["package_id"], r["filename"])
            for r in conn.execute(sql + " ORDER BY d.package_id", args)]


def resend_target(conn) -> int:
    """What "send it again" resends: the package the most recent DELIVERED
    rendering offered (D3: an operator's words bind to what they were shown).
    An offered package whose latest send has since been delivered is no longer
    waiting. None waiting, or several, is a refusal in the operator's words —
    several are told apart by the date in their filenames (spec §"What the
    operator never has to learn")."""
    last = conn.execute("SELECT scope_json FROM renders WHERE delivered_at IS NOT NULL"
                        " ORDER BY delivered_at DESC, rowid DESC LIMIT 1").fetchone()
    offered = json.loads(last["scope_json"]).get("offers", []) if last else []
    waiting = []
    for pid in offered:
        r = conn.execute(_LATEST + " AND d.package_id=?", (pid,)).fetchone()
        if r is not None and r["status"] != "delivered":
            waiting.append(r)
    if not waiting:
        raise db.Refusal("nothing is waiting to be sent again")
    if len(waiting) > 1:
        names = [r["filename"] for r in waiting]
        raise db.Refusal("more than one package may not have arrived: "
                         + ", ".join(names[:-1]) + f" and {names[-1]} — which one? "
                         "Say it by the date in its name.")
    return waiting[0]["package_id"]
