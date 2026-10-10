# server/delivery.py
"""Delivery staging and the delivery log (spec §Packaging). A package or one
invoice is staged, never rebuilt: through Casa's plugin outbox (atomic .part ->
rename; consumed on send, reaped at 2 h), posted to Telegram by post_package as an
operator_file (S7 §6.1). No email (S7 §6.2). An ambiguous outcome is
`uncertain`, never retried automatically; a resend is the exact retained file.

Staging reads bytes out of packages/ or documents/, so it holds the custody
lock (db.custody_lock) from the read through the delivery-log row, taken
BEFORE any transaction (lock order: custody, then SQLite). Every write into
the delivery log checks the pass token: a stale pass is refused there too
(spec §"Running the pass on demand").

Every delivery gets a staged path of its own, never reused: a Telegram copy is
`qa-<16 hex>.<ext>`, drawn again on a collision and guarded by the UNIQUE
staged_path, so a holder that was superseded never holds the path of a later
copy. The operator-facing name travels as the deposit's `filename` (S7a)."""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import sqlite3
import tempfile

import db
import documents
import views
import package
import passes

STAGE_ATTEMPTS = 8
EMAIL_GONE = "Packages come here as a file now — forward it from Telegram."
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


def _remove_staged(path: pathlib.Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def stage_for_delivery(conn, *, channel="telegram", package_id=None, doc_id=None,
                       pass_token=None, resend=False, last_built=False, quarter=None) -> dict:
    """Stage one package or one document: a package's first send (it must carry the latest
    import), a resend ("send it again"), the last build asked for by name (`last_built`,
    issue #15) or a single document. Telegram only (S7 §6.2): email is refused with
    EMAIL_GONE."""
    if channel == "email":
        raise db.Refusal(EMAIL_GONE)
    if channel != "telegram":
        raise db.Refusal("channel is 'telegram'")
    if last_built:
        if package_id is not None or doc_id is not None or resend:
            raise db.Refusal("last_built names no package or document: it stages the last "
                             "package built (for `quarter`, when given)")
        package_id = last_built_package(conn, quarter)
        passes.check_token(conn, pass_token)
        with db.custody_lock():
            return _stage(conn, package_id, None, pass_token, as_built=True)
    if quarter is not None:
        raise db.Refusal("quarter goes with last_built")
    if (package_id is None) == (doc_id is None):
        raise db.Refusal("stage one package or one document")
    if conn.in_transaction:
        raise RuntimeError("stage_for_delivery takes the custody lock before its own transaction")
    passes.check_token(conn, pass_token)                     # early only
    if resend and package_id is not None:
        why = resend_refusal(conn, package_id)               # THE predicate, as offered
        if why is not None:
            raise db.Refusal(why)
    with db.custody_lock():
        return _stage(conn, package_id, doc_id, pass_token, resend)


def last_built_package(conn, quarter=None) -> int:
    """Issue #15: "send me the last package you built (for Q3)" — the most recently built
    package, of `quarter` when named. Refuses when none was built."""
    import dates
    if quarter:
        dates.parse_quarter(quarter)
    row = conn.execute("SELECT package_id FROM packages" + (" WHERE quarter=?" if quarter else "")
                       + " ORDER BY package_id DESC LIMIT 1",
                       (quarter,) if quarter else ()).fetchone()
    if row is None:
        what = f"a {dates.quarter_label(quarter)} package" if quarter else "a package"
        raise db.Refusal(f"I haven't built {what} yet — ask for it and I'll build it")
    return row[0]


def _stage(conn, package_id, doc_id, pass_token, resend=False, as_built=False) -> dict:
    if package_id is not None:
        pk = conn.execute("SELECT * FROM packages WHERE package_id=?", (package_id,)).fetchone()
        if pk is None:
            raise db.Refusal(f"there is no package #{package_id}")
        if pk["oversize"] or pk["size"] > package.MAX_ZIP_BYTES:
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
    for _ in range(STAGE_ATTEMPTS):
        # the staged file's own name is random and never reused; the operator sees
        # `filename` (post_package deposits it as the delivered name, S7a)
        path = _to_outbox(_fresh_path(conn, ext), data)
        note = "post it with post_package(delivery_id), then record_delivery"
        # every copy written above is this call's own: nothing in the log names it
        # until the commit below, so any failure removes it before it can be sent
        try:
            with db.tx(conn):
                passes.check_token(conn, pass_token)
                if package_id is not None and as_built:
                    # the exact file asked for by name, whatever was imported since
                    if conn.execute("SELECT 1 FROM deliveries WHERE package_id=? AND"
                                    " status='staged'", (package_id,)).fetchone():
                        raise db.Refusal(f"{name} is being sent right now — nothing to send "
                                         "again yet")
                elif package_id is not None and resend:
                    why = resend_refusal(conn, package_id)   # bound in the committing tx
                    if why is not None:
                        raise db.Refusal(why)
                elif package_id is not None:
                    _require_current_snapshot(conn, package_id)
                did = conn.execute(
                    "INSERT INTO deliveries(package_id, doc_id, channel, staged_path,"
                    " status, created_at, lease_at, as_built)"
                    " VALUES (?,?,?,?, 'staged', ?, ?, ?)",
                    (package_id, doc_id, "telegram", str(path), db.now(),
                     db.now(), int(as_built))).lastrowid
        except sqlite3.IntegrityError as exc:
            _remove_staged(path)
            if "staged_path" not in str(exc):
                raise
            continue                    # a path some delivery already named: draw again
        except BaseException:
            _remove_staged(path)
            raise
        return {"delivery_id": did, "channel": "telegram", "path": str(path),
                "filename": name, "note": note}
    raise db.Refusal("could not find a free name to stage the file under — nothing was "
                     "staged; ask again")


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
    Its package notice is raised in the same transaction. Returns the revoked rows; their staged bytes
    are withdrawn before the same commit (withdraw_revoked). A resend of a file
    already sent is never revoked."""
    import alerts
    assert conn.in_transaction
    rows = conn.execute(
        "SELECT d.delivery_id, d.channel, d.staged_path, d.package_id, p.quarter"
        " FROM deliveries d JOIN packages p"
        " ON p.package_id=d.package_id WHERE d.status='staged' AND d.revoked_at IS NULL"
        " AND d.as_built=0 AND d.posted_at IS NULL"     # r3 #1: a posted send has left
        " AND (p.snapshot_id IS NULL OR p.snapshot_id<>?)"
        " AND NOT EXISTS (SELECT 1 FROM deliveries e WHERE e.package_id=d.package_id"
        "  AND e.status IN ('delivered', 'uncertain'))", (snapshot_id,)).fetchall()
    now = db.now()
    latest = {}
    for r in rows:
        conn.execute("UPDATE deliveries SET status='failed', settled_at=coalesce(settled_at, ?),"
                     " revoked_at=? WHERE delivery_id=?", (now, now, r["delivery_id"]))
        close_offers(conn, r["package_id"])
        if r["delivery_id"] > latest.get(r["package_id"], {"delivery_id": 0})["delivery_id"]:
            latest[r["package_id"]] = r
    # every revoked send is told: once per package, keyed on its latest revoked delivery
    for r in latest.values():
        alerts.raise_package(conn, "package-revoked", f"delivery:{r['delivery_id']}:revoked",
                             quarter=r["quarter"], package_id=r["package_id"])
    return [dict(r) for r in rows]


def withdraw(conn, rows, *, refusal: str) -> None:
    """Remove the staged bytes of `rows` (deliveries) whose copy is still there —
    the outbox file. Inside the caller's transaction, under the custody lock. A path a live, staged delivery still
    names is left alone. A removal that fails raises the caller's refusal, so
    its whole transaction rolls back; copies already removed stay removed, and
    their send fails visibly."""
    assert conn.in_transaction
    for r in rows:
        target = pathlib.Path(r["staged_path"])
        if not target.exists():
            continue
        if conn.execute("SELECT 1 FROM deliveries WHERE staged_path=? AND status='staged' AND"
                        " revoked_at IS NULL AND withdrawn_at IS NULL AND delivery_id<>?",
                        (r["staged_path"], r["delivery_id"])).fetchone() is not None:
            continue
        try:
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
    between staging and record_delivery) — keyed on the DELIVERY."""
    return [r for r in conn.execute(
        "SELECT d.*, p.quarter FROM deliveries d JOIN packages p ON p.package_id=d.package_id"
        " WHERE d.status='staged' AND d.revoked_at IS NULL AND d.withdrawn_at IS NULL"
        " ORDER BY d.delivery_id").fetchall()
        if passes._age_s(r["lease_at"] or r["created_at"]) >= lease_s]


NEVER_POSTED = ("this package was never posted, so it cannot have arrived — nothing was "
                "recorded; post it first, or record it uncertain")


def posted_unrecorded(conn) -> list:
    """r3 #1: staged package sends post_package deposited (posted_at) whose outcome nobody
    recorded — they have left the plugin, whatever their lease."""
    return conn.execute(
        "SELECT d.*, p.quarter FROM deliveries d JOIN packages p ON p.package_id=d.package_id"
        " WHERE d.status='staged' AND d.posted_at IS NOT NULL AND d.revoked_at IS NULL AND"
        " d.withdrawn_at IS NULL ORDER BY d.delivery_id").fetchall()


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


def record_delivery(conn, *, delivery_id, outcome, message_id=None, pass_token=None) -> dict:
    """Settle a send. A package send recorded uncertain or failed raises its package notice
    here and returns the rendering that says it as `speak` — always including that notice,
    and the package in its scope's `offers`, so "send it again" binds to it (D3)."""
    import alerts
    if outcome not in ("delivered", "uncertain", "failed"):
        raise db.Refusal("outcome is 'delivered', 'uncertain' or 'failed'")
    with db.tx(conn):
        d = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        passes.check_token(conn, pass_token)
        if d is None:
            raise db.Refusal(f"there is no delivery #{delivery_id}")
        if d["revoked_at"] is not None:
            raise db.Refusal("the bank was re-read before this was sent — nothing was recorded")
        if d["status"] == "delivered":
            return {"delivery_id": delivery_id, "status": "delivered", "already": True}
        if (outcome == "delivered" and d["package_id"] is not None
                and d["channel"] == "telegram" and d["posted_at"] is None):
            # r3 #2 (Terra S1): a package never posted did not arrive
            raise db.Refusal(NEVER_POSTED)
        # Evidence wins over the recovery's guess: a send recovery settled `uncertain`
        # (withdrawn) that is reported delivered was delivered — Casa had taken the
        # bytes. Only that upgrade is accepted for a recovered send, by its evidence alone.
        upgrade = d["withdrawn_at"] is not None and outcome == "delivered" \
            and d["status"] == "uncertain"
        if d["withdrawn_at"] is not None and not upgrade:
            raise db.Refusal("this send was taken back before it was recorded — nothing was "
                             "written")
        conn.execute("UPDATE deliveries SET status=?, message_id=?, settled_at=? WHERE"
                     " delivery_id=?", (outcome, message_id, db.now(), delivery_id))
        changed = []
        if outcome == "delivered" and d["package_id"] is not None:
            changed = settle_delivered(conn, d)
        out = {"delivery_id": delivery_id, "status": outcome}
        if changed:
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


def settle_delivered(conn, d) -> list:
    """A package send has arrived (record_delivery's delivered outcome, and get_package
    right after its deposit, D15): inside the caller's transaction, the rows exactly as the
    package froze them become the accountant's copy, the package name counts as announced,
    no offer of the package stays open, and the copy is compared with the bank now (a late
    report may follow a newer import). Returns the new alerts' ids."""
    import ledger
    assert conn.in_transaction
    # facts_fp is db.canonical(reducer.facts_of(row)), what ledger's delivered checks
    # compare; kind is the one the accountant's copy stands for — shipped under, else the
    # last known (a row shipped unclassified or not re-read; fix wave F)
    pk = conn.execute("SELECT manifest_json FROM packages WHERE package_id=?",
                      (d["package_id"],)).fetchone()
    for r in json.loads(pk[0])["rows"]:
        conn.execute("INSERT OR REPLACE INTO delivered_rows(package_id, row_id, pid,"
                     " facts_fp, kind) VALUES (?,?,?,?,?)",
                     (d["package_id"], r["row_id"], r["pid"], r["facts_fp"],
                      r["kind"] or r.get("last_known_kind")))
    conn.execute("UPDATE binding SET package_name_announced=1 WHERE id=1")
    close_offers(conn, d["package_id"])
    return ledger.check_delivered_package(conn, d["package_id"])


def offer_lines(filename: str, status: str = "uncertain") -> list:
    """The words that offer a package whose send may not have arrived (or, for a
    send that failed, did not go out) — the same in the status view and in the
    package notice record_delivery raises."""
    if status == "failed":
        return [f"{views.field(filename)} didn't go out — I can send it again."]
    return [f"{views.field(filename)} may not have arrived — I can send it again."]


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
    would refuse, by construction. (The package note and the file's own caption carry
    their package in `offers` whatever its state (#44): resend_target answers it with
    this predicate's sentence, as any offer no longer eligible.)

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


NO_OFFER = "that message offers no package to send again"


def resend_target(conn, render_id=None) -> int:
    """What "send it again" resends (S7 §6.3, binding R4): typed words quoting a rendering
    bind `render_id`, that rendering's own `offers` (none: NO_OFFER); unquoted words bind
    the package the most recent DELIVERED rendering that offers a package offered (D3: an
    operator's words bind to what they were shown — a view, a post or a status sheet
    delivered after it offers nothing and hides nothing).
    An offered package that is no longer eligible (resend_refusal) is answered
    with its own reason. None waiting, or several, is a refusal in the operator's words —
    several are told apart by the date in their filenames (spec §"What the
    operator never has to learn")."""
    if render_id is not None:
        r = conn.execute("SELECT scope_json, delivered_at, posted_seq FROM renders WHERE"
                         " render_id=?", (render_id,)).fetchone() \
            if isinstance(render_id, str) else None
        offered = json.loads(r["scope_json"]).get("offers") or [] if db.seen_render(r) else []
        if not offered:
            raise db.Refusal(NO_OFFER)
    else:
        last = conn.execute(
            "SELECT scope_json FROM renders WHERE delivered_at IS NOT NULL AND kind NOT IN (%s)"
            " AND json_array_length(scope_json, '$.offers') > 0 ORDER BY delivered_seq DESC,"
            " delivered_at DESC, rowid DESC LIMIT 1"
            % ",".join("?" * len(db.INFORMATIONAL_KINDS)), db.INFORMATIONAL_KINDS).fetchone()
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
