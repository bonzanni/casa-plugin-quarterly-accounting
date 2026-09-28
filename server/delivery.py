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
(spec §"Running the pass on demand"). A package built for a package request
also needs that request's package_token, checked in the transaction that
commits the write (a check before a lock wait is only an early refusal).

Every delivery gets a staged path of its own, never reused: a Telegram copy is
`qa-<16 hex>.<ext>`, drawn again on a collision and guarded by the UNIQUE
staged_path, so a holder that was superseded never holds the path of a later
copy. The operator-facing name travels as send_media's `filename`."""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import shutil
import sqlite3
import tempfile

import casa_handoff
import db
import documents
import package
import passes

GMAIL_ATTACHMENT_LIMIT = casa_handoff.MAX_FILE_BYTES      # the handoff folder's 25 MB per file
STAGE_ATTEMPTS = 8
# An email waits for the operator's one-tap approval, which can take far longer than a
# Telegram send: its staged copy is not recovered for a day. Telegram keeps LEASE_S.
EMAIL_RECOVERY_LEASE_S = 24 * 3600
WITHDRAW_REFUSED = ("could not withdraw a staged package — nothing was imported ({why}); fix "
                    "that and import again")


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CASA_PLUGIN_OUTBOX_DIR") or "/data/plugin-outbox")


def _nonce() -> str:
    return secrets.token_hex(8)


def _path_taken(conn, path: pathlib.Path) -> bool:
    """A staged path is never reused: not while a file is there, and never for a
    path any delivery ever named — a superseded holder may still hold it."""
    return path.exists() or conn.execute("SELECT 1 FROM deliveries WHERE staged_path=?",
                                         (str(path),)).fetchone() is not None


def _fresh_path(conn, ext: str) -> pathlib.Path:
    """A random outbox path no delivery has named (Telegram): qa-<16 hex>.<ext>,
    drawn again on a collision. Called under the custody lock, so no other
    staging draws at the same time; the UNIQUE staged_path is the durable guard."""
    d = outbox_dir()
    for _ in range(STAGE_ATTEMPTS):
        path = d / f"qa-{_nonce()}.{ext}"
        if not _path_taken(conn, path):
            return path
    raise db.Refusal("could not find a free name to stage the file under — nothing was "
                     "staged; ask again")


def _to_outbox(path: pathlib.Path, data: bytes) -> pathlib.Path:
    """Write `data` at `path` atomically (.part, then rename). The path is fresh
    (_fresh_path, under the custody lock), so nothing is ever replaced."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.part-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def _remove_staged(channel: str, path: pathlib.Path) -> None:
    if channel == "email":          # the handoff entry is its own directory
        shutil.rmtree(path.parent, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def request_of_package(conn, package_id):
    """The package request that built this package, if any (the newest)."""
    return conn.execute("SELECT * FROM package_requests WHERE package_id=? ORDER BY"
                        " request_id DESC LIMIT 1", (package_id,)).fetchone()


def stage_for_delivery(conn, *, channel, package_id=None, doc_id=None, pass_token=None,
                       package_token=None, resend=False) -> dict:
    """Stage one package or one document. A package built for a package request is
    request-bound: its package_token is required, checked early and again in the
    transaction that records the delivery, and staging it twice returns the same
    delivery. A resend ("send it again") and a single document are token-free."""
    if channel not in ("telegram", "email"):
        raise db.Refusal("channel is 'telegram' or 'email'")
    if (package_id is None) == (doc_id is None):
        raise db.Refusal("stage one package or one document")
    if conn.in_transaction:
        raise RuntimeError("stage_for_delivery takes the custody lock before its own transaction")
    passes.check_token(conn, pass_token)                     # early refusals only
    if resend and package_id is not None:
        why = resend_refusal(conn, package_id)               # THE predicate, as offered
        if why is not None:
            raise db.Refusal(why)
    req = None if resend or package_id is None else request_of_package(conn, package_id)
    if req is not None:
        passes.check_package_token(conn, req["request_id"], package_token)
        if req["channel"] != channel:
            raise db.Refusal(f"this package was asked for by {req['channel']} — stage it by "
                             f"{req['channel']}, or ask for the package again by {channel}")
    with db.custody_lock():
        return _stage(conn, channel, package_id, doc_id, pass_token,
                      req["request_id"] if req is not None else None, package_token, resend)


def _stage(conn, channel, package_id, doc_id, pass_token, request_id, package_token,
           resend=False) -> dict:
    if request_id is not None:
        again = _staged_again(conn, request_id, package_token, pass_token)
        if again is not None:
            return again
    if package_id is not None:
        pk = conn.execute("SELECT * FROM packages WHERE package_id=?", (package_id,)).fetchone()
        if pk is None:
            raise db.Refusal(f"there is no package #{package_id}")
        if channel == "telegram" and (pk["oversize"] or pk["size"] > package.MAX_ZIP_BYTES):
            raise db.Refusal(f"{pk['filename']} is over Telegram's 20 MB limit "
                             f"({pk['size'] / 1e6:.1f} MB); it is kept here, and notes.md "
                             "names the largest files")
        name, data, ext = pk["filename"], pathlib.Path(pk["path"]).read_bytes(), "zip"
    else:
        d = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if d is None:
            raise db.Refusal(f"there is no document #{doc_id}")
        name = package.doc_filename(dict(d), set(), (d["ingested_at"] or "")[:10])
        data, ext = documents.path_of(conn, doc_id).read_bytes(), d["ext"]
    if channel == "email" and len(data) > GMAIL_ATTACHMENT_LIMIT:
        raise db.Refusal(f"{name} is over the 25 MB email attachment limit "
                         f"({len(data) / 1e6:.1f} MB); it is kept here")
    for _ in range(STAGE_ATTEMPTS):
        request = None
        if channel == "telegram":
            # the staged file's own name is random and never reused; the operator sees
            # `filename` (send_media's filename argument)
            path = _to_outbox(_fresh_path(conn, ext), data)
            note = ("send it with send_media(path, kind='zip' or 'pdf', filename=<filename>), "
                    "then record_delivery")
        else:
            try:
                path = pathlib.Path(casa_handoff.publish("quarterly-accounting", name,
                                                         data=data)["path"])
            except casa_handoff.HandoffError as exc:
                raise db.Refusal(f"the handoff folder refused it ({exc.kind}): {exc}")
            request = "qa-" + secrets.token_hex(8)
            note = ("attach it with gmail's send_email to the operator's own address only, "
                    "passing this request_id; Casa shows them the recipient before it sends")
        # every copy written above is this call's own: nothing in the log names it
        # until the commit below, so any failure removes it before it can be sent
        try:
            with db.tx(conn):
                passes.check_token(conn, pass_token)
                if request_id is not None:
                    passes.check_package_token(conn, request_id, package_token)
                if package_id is not None and resend:
                    why = resend_refusal(conn, package_id)   # bound in the committing tx
                    if why is not None:
                        raise db.Refusal(why)
                elif package_id is not None:
                    _require_current_snapshot(conn, package_id)
                did = conn.execute(
                    "INSERT INTO deliveries(package_id, doc_id, channel, staged_path,"
                    " request_id, status, created_at, lease_at) VALUES (?,?,?,?,?, 'staged', ?, ?)",
                    (package_id, doc_id, channel, str(path), request, db.now(),
                     db.now())).lastrowid
                if request_id is not None:
                    conn.execute("UPDATE package_requests SET delivery_id=?, state='staged',"
                                 " updated_at=? WHERE request_id=?", (did, db.now(), request_id))
        except sqlite3.IntegrityError as exc:
            _remove_staged(channel, path)
            if "staged_path" not in str(exc):
                raise
            continue                    # a path some delivery already named: draw again
        except BaseException:
            _remove_staged(channel, path)
            raise
        out = {"delivery_id": did, "channel": channel, "path": str(path), "filename": name,
               "note": note}
        if request:
            out["request_id"] = request
        return out
    raise db.Refusal("could not find a free name to stage the file under — nothing was "
                     "staged; ask again")


def _staged_again(conn, request_id, package_token, pass_token):
    """Staging a request that already staged its send returns that send (same
    delivery, path and request id): at most one staged send per request."""
    with db.tx(conn):
        passes.check_token(conn, pass_token)
        req = passes.check_package_token(conn, request_id, package_token)
        if req["state"] != "staged":
            return None
        d = conn.execute("SELECT d.*, p.filename FROM deliveries d JOIN packages p ON"
                         " p.package_id=d.package_id WHERE d.delivery_id=?",
                         (req["delivery_id"],)).fetchone()
        conn.execute("UPDATE deliveries SET lease_at=? WHERE delivery_id=?",
                     (db.now(), req["delivery_id"]))
    out = {"delivery_id": d["delivery_id"], "channel": d["channel"], "path": d["staged_path"],
           "filename": d["filename"], "already": True,
           "note": "already staged: send this one, then record_delivery"}
    if d["request_id"]:
        out["request_id"] = d["request_id"]
    return out


def _require_current_snapshot(conn, package_id) -> None:
    """A package's FIRST send carries what the bank looked like at its build
    (round E4, Terra + Astra): once a newer import has landed, the rows it judged
    may have been reclassified, so it is refused — checked inside the write that
    logs the delivery. A resend of a file already sent (delivered or uncertain)
    is that exact file, as the spec offers; a delivered quarter that changed
    underneath is alerted separately."""
    sent = conn.execute("SELECT 1 FROM deliveries WHERE package_id=? AND status IN"
                        " ('delivered', 'uncertain')", (package_id,)).fetchone()
    if sent is not None:
        return
    built = conn.execute("SELECT snapshot_id FROM packages WHERE package_id=?",
                         (package_id,)).fetchone()[0]
    latest = conn.execute("SELECT coalesce(max(snapshot_id), 0) FROM snapshots").fetchone()[0]
    if built is None or built != latest:
        raise db.Refusal("the bank was re-read since this package was built — build it again")


def revoke_superseded_first_sends(conn, snapshot_id) -> list:
    """Inside an import's transaction, after its snapshot is recorded (round E5,
    Terra S1): every delivery still `staged` that is a package's FIRST send
    (no delivered or uncertain send of it) and was built under another snapshot
    is revoked — marked failed with revoked_at, so record_delivery refuses it.
    (A send that already failed is not revoked: resend_refusal simply offers no
    resend of it once the bank changed.)
    Its package request, if any, becomes `revoked`, with its package notice
    raised in the same transaction. Returns the revoked rows; their staged bytes
    are withdrawn before the same commit (withdraw_revoked). A resend of a file
    already sent is never revoked."""
    import alerts
    assert conn.in_transaction
    rows = conn.execute(
        "SELECT d.delivery_id, d.channel, d.staged_path, d.package_id, p.quarter"
        " FROM deliveries d JOIN packages p"
        " ON p.package_id=d.package_id WHERE d.status='staged' AND d.revoked_at IS NULL"
        " AND (p.snapshot_id IS NULL OR p.snapshot_id<>?)"
        " AND NOT EXISTS (SELECT 1 FROM deliveries e WHERE e.package_id=d.package_id"
        "  AND e.status IN ('delivered', 'uncertain'))", (snapshot_id,)).fetchall()
    now = db.now()
    latest = {}
    for r in rows:
        conn.execute("UPDATE deliveries SET status='failed', settled_at=coalesce(settled_at, ?),"
                     " revoked_at=? WHERE delivery_id=?", (now, now, r["delivery_id"]))
        req = conn.execute("SELECT request_id FROM package_requests WHERE delivery_id=? AND"
                           " state='staged'", (r["delivery_id"],)).fetchone()
        if req is not None:
            conn.execute("UPDATE package_requests SET state='revoked', updated_at=? WHERE"
                         " request_id=?", (now, req[0]))
        close_offers(conn, r["package_id"])
        if r["delivery_id"] > latest.get(r["package_id"], {"delivery_id": 0})["delivery_id"]:
            latest[r["package_id"]] = r
    # every revoked send is told, linked to a request or not (a resend has none): once
    # per package, keyed on its latest revoked delivery
    for r in latest.values():
        alerts.raise_package(conn, "package-revoked", f"delivery:{r['delivery_id']}:revoked",
                             quarter=r["quarter"], package_id=r["package_id"])
    return [dict(r) for r in rows]


def withdraw(conn, rows, *, refusal: str) -> None:
    """Remove the staged bytes of `rows` (deliveries) whose copy is still there —
    the Telegram outbox file, or this plugin's own handoff entry (one `<id>/`
    directory per publish; casa_handoff has no retract call). Inside the caller's
    transaction, under the custody lock. A path a live, staged delivery still
    names is left alone. A removal that fails raises the caller's refusal, so
    its whole transaction rolls back; copies already removed stay removed, and
    their send fails visibly."""
    assert conn.in_transaction
    for r in rows:
        path = pathlib.Path(r["staged_path"])
        target = path.parent if r["channel"] == "email" else path
        if not target.exists():
            continue
        if conn.execute("SELECT 1 FROM deliveries WHERE staged_path=? AND status='staged' AND"
                        " revoked_at IS NULL AND withdrawn_at IS NULL AND delivery_id<>?",
                        (r["staged_path"], r["delivery_id"])).fetchone() is not None:
            continue
        try:
            if r["channel"] == "email":
                shutil.rmtree(target)
            else:
                os.unlink(target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise db.Refusal(refusal.format(why=exc.strerror or exc)) from None


def withdraw_revoked(conn) -> None:
    """Inside the import's transaction, under the custody lock (rounds E6, E7):
    withdraw every revoked delivery's staged bytes. A removal that fails refuses
    the WHOLE import: it rolls back (snapshot unchanged, nothing revoked), so a
    superseded package is never left sendable under a committed newer snapshot;
    a retry after recovery withdraws and commits."""
    rows = conn.execute("SELECT delivery_id, channel, staged_path FROM deliveries"
                        " WHERE revoked_at IS NOT NULL ORDER BY delivery_id").fetchall()
    withdraw(conn, [dict(r) for r in rows], refusal=WITHDRAW_REFUSED)


def stalled_sends(conn, lease_s: float) -> list:
    """Staged package sends nobody settled within their lease (a turn that died
    between staging and record_delivery) — keyed on the DELIVERY, whether or not a
    package request is linked (a resend has none)."""
    import steps
    return [r for r in conn.execute(
        "SELECT d.*, p.quarter FROM deliveries d JOIN packages p ON p.package_id=d.package_id"
        " WHERE d.status='staged' AND d.revoked_at IS NULL AND d.withdrawn_at IS NULL"
        " ORDER BY d.delivery_id").fetchall()
        if steps._age(r["lease_at"] or r["created_at"])
        >= (EMAIL_RECOVERY_LEASE_S if r["channel"] == "email" else lease_s)]


OFFER_KINDS = ("package-uncertain", "package-send-failed")


def arrived(conn, package_id):
    """THE package-level fact the operator cares about: has this package arrived
    (any of its sends recorded delivered)? The delivered send, or None. An offer, a
    recovery's notice and "send it again" are all decided by it, never by one
    delivery."""
    return conn.execute("SELECT * FROM deliveries WHERE package_id=? AND status='delivered'"
                        " ORDER BY settled_at LIMIT 1", (package_id,)).fetchone()


def close_offers(conn, package_id) -> None:
    """A package that arrived is never offered again: every open "send it again"
    offer of it is closed (inside the caller's transaction)."""
    conn.execute("UPDATE alerts SET sent_at=? WHERE sent_at IS NULL AND kind IN (?, ?) AND"
                 " json_extract(detail, '$.package_id')=?", (db.now(), *OFFER_KINDS, package_id))


def arrived_words(conn, package_id) -> str:
    import dates
    d = arrived(conn, package_id)
    q = conn.execute("SELECT quarter FROM packages WHERE package_id=?",
                     (package_id,)).fetchone()[0]
    return (f"the {dates.quarter_label(q)} package did arrive (sent "
            f"{dates.short_day(d['settled_at'])}) — nothing to send again")


def recover_staged(conn, d):
    """Recover one stalled staged package send, inside the caller's transaction and
    under the custody lock the caller holds: its staged bytes are taken back first
    (whoever staged it may still hold the path, and Casa's send tools check no
    token of ours), the send is settled `uncertain` — it may already have gone —
    and its per-delivery notice offers "send it again" — unless the package has
    already arrived by another send: then this extra copy settles quietly. A
    removal that fails refuses the whole call: nothing changes. Returns the
    notice's alert_id, or None."""
    import alerts
    withdraw(conn, [dict(d)], refusal="could not take back a staged package — nothing changed")
    now = db.now()
    conn.execute("UPDATE deliveries SET status='uncertain', settled_at=?, withdrawn_at=?"
                 " WHERE delivery_id=?", (now, now, d["delivery_id"]))
    if arrived(conn, d["package_id"]) is not None:
        return None
    quarter = conn.execute("SELECT quarter FROM packages WHERE package_id=?",
                           (d["package_id"],)).fetchone()[0]
    return alerts.raise_package(conn, "package-uncertain", f"delivery:{d['delivery_id']}:uncertain",
                                quarter=quarter, package_id=d["package_id"])


def record_delivery(conn, *, delivery_id, outcome, message_id=None, pass_token=None,
                    package_token=None) -> dict:
    """Settle a send. A request-bound delivery needs its request's package_token,
    checked in this one transaction, which also closes the request. A package send
    recorded uncertain or failed raises its package notice here and returns the
    rendering that says it as `speak` — always including that notice, and the
    package in its scope's `offers`, so "send it again" binds to it (D3)."""
    import alerts
    if outcome not in ("delivered", "uncertain", "failed"):
        raise db.Refusal("outcome is 'delivered', 'uncertain' or 'failed'")
    with db.tx(conn):
        passes.check_token(conn, pass_token)
        d = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        if d is None:
            raise db.Refusal(f"there is no delivery #{delivery_id}")
        if d["revoked_at"] is not None:
            raise db.Refusal("the bank was re-read before this was sent — build it again")
        if d["status"] == "delivered":
            return {"delivery_id": delivery_id, "status": "delivered", "already": True}
        req = conn.execute("SELECT * FROM package_requests WHERE delivery_id=?",
                           (delivery_id,)).fetchone()
        # Evidence wins over the recovery's guess: a send recovery settled `uncertain`
        # (withdrawn) that is reported delivered was delivered — Casa had taken the
        # bytes. Only that upgrade is accepted for a recovered send, by its evidence
        # alone (the recovery rotated the request's token).
        upgrade = d["withdrawn_at"] is not None and outcome == "delivered" \
            and d["status"] == "uncertain"
        if req is not None and not upgrade:
            passes.check_package_token(conn, req["request_id"], package_token)
        if d["withdrawn_at"] is not None and not upgrade:
            raise db.Refusal("this send was taken back before it was recorded — nothing was "
                             "written")
        if outcome == "delivered" and d["channel"] == "email" and not message_id:
            raise db.Refusal("an email counts as delivered only when send_email returned a "
                             "message id; otherwise record it 'uncertain'")
        conn.execute("UPDATE deliveries SET status=?, message_id=?, settled_at=? WHERE"
                     " delivery_id=?", (outcome, message_id, db.now(), delivery_id))
        if req is not None:
            conn.execute("UPDATE package_requests SET state=?, updated_at=? WHERE request_id=?",
                         (outcome, db.now(), req["request_id"]))

        if outcome == "delivered" and d["package_id"] is not None:
            # the rows exactly as the package froze them: facts_fp is
            # db.canonical(reducer.facts_of(row)), what ledger's delivered checks compare;
            # kind is the one the accountant's copy stands for — shipped under, else the
            # last known (a row shipped unclassified or not re-read; fix wave F)
            pk = conn.execute("SELECT manifest_json FROM packages WHERE package_id=?",
                              (d["package_id"],)).fetchone()
            for r in json.loads(pk[0])["rows"]:
                conn.execute("INSERT OR REPLACE INTO delivered_rows(package_id, row_id, pid,"
                             " facts_fp, kind) VALUES (?,?,?,?,?)",
                             (d["package_id"], r["row_id"], r["pid"], r["facts_fp"],
                              r["kind"] or r.get("last_known_kind")))
            conn.execute("UPDATE binding SET package_name_announced=1 WHERE id=1")
            # the package arrived: no offer of it stays open, and the accountant's copy is
            # compared with the bank now (a late report may follow a newer import)
            close_offers(conn, d["package_id"])
            import ledger
            changed = ledger.check_delivered_package(conn, d["package_id"])
        out = {"delivery_id": delivery_id, "status": outcome}
        if outcome == "delivered" and d["package_id"] is not None and changed:
            out["speak"] = alerts.pending_in_tx(conn, must=changed)
        if outcome in ("uncertain", "failed") and d["package_id"] is not None \
                and arrived(conn, d["package_id"]) is None:
            quarter = conn.execute("SELECT quarter FROM packages WHERE package_id=?",
                                   (d["package_id"],)).fetchone()[0]
            kind = "package-uncertain" if outcome == "uncertain" else "package-send-failed"
            notice = alerts.raise_package(conn, kind, f"delivery:{delivery_id}:{outcome}",
                                          quarter=quarter, package_id=d["package_id"])
            out["speak"] = alerts.pending_in_tx(conn, must=notice)
        return out


def offer_lines(filename: str, status: str = "uncertain") -> list:
    """The words that offer a package whose send may not have arrived (or, for a
    send that failed, did not go out) — the same in the status view and in the
    package notice record_delivery raises."""
    if status == "failed":
        return [f"{filename} didn't go out —", 'say "send it again".']
    return [f"{filename} may not have arrived —", 'say "send it again".']


_LATEST = ("SELECT d.package_id, d.status, d.revoked_at, p.filename, p.quarter FROM deliveries d"
           " JOIN packages p"
           " ON p.package_id=d.package_id WHERE d.delivery_id IN (SELECT max(delivery_id) FROM"
           " deliveries WHERE package_id IS NOT NULL GROUP BY package_id)")


def resend_refusal(conn, package_id):
    """THE predicate for "send it again" (escalated after the same shape of finding
    in three rounds): None when a resend of this package can be staged, else the
    operator's sentence saying why not. Staging a resend refuses with exactly this
    sentence, and a package is OFFERED exactly when this is None (offerable, every
    composer of an offer, resend_target) — so nothing can be offered that staging
    would refuse, by construction.

    Refused when: the package has arrived (any send delivered); it was never sent;
    its latest send is still being sent (staged); its latest send was revoked (an
    import took it back); or no send of it can have reached the accountant (none
    delivered or uncertain) and the bank has been re-read since it was built — its
    first copy would carry outdated numbers. A send that may have arrived
    (uncertain) is resendable as the exact file, whatever was imported since."""
    import dates
    q = conn.execute("SELECT quarter, snapshot_id FROM packages WHERE package_id=?",
                     (package_id,)).fetchone()
    if q is None:
        return "nothing is waiting to be sent again"
    label = dates.quarter_label(q["quarter"])
    if arrived(conn, package_id) is not None:
        return arrived_words(conn, package_id)
    latest = conn.execute("SELECT * FROM deliveries WHERE package_id=? ORDER BY delivery_id"
                          " DESC LIMIT 1", (package_id,)).fetchone()
    if latest is None:
        return f"the {label} package was never sent — nothing to send again"
    if latest["status"] == "staged":
        return f"the {label} package is being sent right now — nothing to send again yet"
    if latest["revoked_at"] is not None:
        return f"the bank has changed since it was built — ask for the {label} package again"
    maybe_sent = conn.execute("SELECT 1 FROM deliveries WHERE package_id=? AND status IN"
                              " ('delivered', 'uncertain')", (package_id,)).fetchone()
    if maybe_sent is None:
        current = conn.execute("SELECT coalesce(max(snapshot_id), 0) FROM snapshots"
                               ).fetchone()[0]
        if q["snapshot_id"] is None or q["snapshot_id"] != current:
            return f"the bank has changed since it was built — ask for the {label} package again"
    return None


def offerable(conn, quarter=None) -> list:
    """Packages owed "send it again" — resend_refusal is None — as (package_id,
    filename, the latest send's status), oldest package first; `quarter` scopes
    them to a quarter-scoped view."""
    sql, args = _LATEST, []
    if quarter:
        sql += " AND p.quarter=?"
        args.append(quarter)
    return [(r["package_id"], r["filename"], r["status"])
            for r in conn.execute(sql + " ORDER BY d.package_id", args)
            if resend_refusal(conn, r["package_id"]) is None]


def resendable(conn, quarter=None):
    """The most recent package owed "send it again" (resend_refusal is None), an
    uncertain one before a failed one; None when there is none. What "send it
    again" resends is resend_target, which binds to what the operator was shown."""
    rows = sorted(offerable(conn, quarter), key=lambda r: r[0], reverse=True)
    for want in ("uncertain", "failed"):
        for pid, _, status in rows:
            if status == want:
                return pid
    return None


def uncertain(conn, quarter=None) -> list:
    """The offerable packages whose most recent send is uncertain, as (package_id,
    filename)."""
    return [(pid, f) for pid, f, st in offerable(conn, quarter) if st == "uncertain"]


def resend_target(conn) -> int:
    """What "send it again" resends: the package the most recent DELIVERED
    rendering offered (D3: an operator's words bind to what they were shown).
    An offered package that is no longer eligible (resend_refusal) is answered
    with its own reason. None waiting, or several, is a refusal in the operator's words —
    several are told apart by the date in their filenames (spec §"What the
    operator never has to learn")."""
    last = db.last_delivered(conn)
    offered = json.loads(last["scope_json"]).get("offers", []) if last else []
    waiting, why = [], None
    for pid in offered:
        refusal = resend_refusal(conn, pid)
        if refusal is None:
            waiting.append(conn.execute("SELECT package_id, filename FROM packages WHERE"
                                        " package_id=?", (pid,)).fetchone())
        else:
            why = refusal
    if not waiting:
        # an offered package no longer eligible is answered with its own reason
        raise db.Refusal(why or "nothing is waiting to be sent again")
    if len(waiting) > 1:
        names = [r["filename"] for r in waiting]
        raise db.Refusal("more than one package may not have arrived: "
                         + ", ".join(names[:-1]) + f" and {names[-1]} — which one? "
                         "Say it by the date in its name.")
    return waiting[0]["package_id"]
