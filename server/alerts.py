# server/alerts.py
"""The only two things this plugin ever says unprompted (spec §"When the
plugin may speak first"): collection stopped working, and a delivered
quarter changed underneath. Once per occurrence, never repeated while the
condition persists, never escalated, no "all better". An occurrence is keyed
by the moment the condition began, so a condition that clears and recurs is
new. An alert counts as said only when its rendering was DELIVERED
(mark_rendering_delivered sets sent_at); a send that failed is offered again."""
from __future__ import annotations

import json

import db
import views
import work

COLLECTION = {
    "gmail": "Gmail stopped letting me in{paren} — invoices aren't being searched. "
             "Re-authorise Gmail when you can.",
    "bank_sync": "The bank connection stopped{paren} — new payments aren't coming in. "
                 "Re-authorise it in bank-feed.",
    "bound_account": "The bound account is gone from bank-feed — nothing is being checked "
                     "until it is linked again.",
}


def evaluate(conn) -> None:
    for kind in COLLECTION:
        p = conn.execute("SELECT * FROM probes WHERE kind=?", (kind,)).fetchone()
        if p is None or p["ok"] or not p["failing_since"]:
            continue
        conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                     " VALUES (?,?,?,?)", (kind, f"{kind}:{p['failing_since']}",
                                          db.canonical({"detail": p["detail"] or ""}), db.now()))


def _delivered_lines(conn, rows) -> list:
    by_pkg: dict = {}
    for a in rows:
        d = json.loads(a["detail"])
        by_pkg.setdefault(d["package"], []).append(d)
    out = []
    for pkg, changes in sorted(by_pkg.items()):
        out.append(f"The package {pkg} changed underneath:")
        for c in changes:
            pid = conn.execute("SELECT pid FROM aliases WHERE row_id=?", (c["row_id"],)).fetchone()
            head = views.headline(work.describe(conn, pid[0])) if pid else f"payment #{c['row_id']}"
            word = {"corrected": "corrected by the bank", "superseded": "replaced by the bank",
                    "vanished": "withdrawn by the bank", "erased": "erased from the ledger",
                    "reclassified": "now categorised differently"}.get(c["change"], c["change"])
            out.append(f"{head} — {word}")
        q = changes[0]["quarter"].split("-")[1]
        out.append(f'Your accountant holds the old numbers. Say "rebuild {q}" if they need a '
                   "fresh one.")
    return out


def pending_rendering(conn):
    """evaluate(), the read of undelivered alerts, composition and the renders
    INSERT all run under ONE db.tx: end_pass frees the pass marker before
    calling here, so a fresh pass can begin, re-observe the same still-failing
    condition and reach this function while an earlier pass's own call is
    still in flight (fix round 1: two db.tx blocks with composition outside
    either left a window where both could SELECT the same undelivered alert
    and each INSERT a different render for it). One lock closes that window
    for the read-compose-insert sequence; by itself it would still let the
    second, later call mint a fresh duplicate render for an alert the first
    left undelivered, so an alert already parked in an existing, undelivered
    render is not composed into a second one — that render is returned again
    unchanged. That IS the "an undelivered alert is offered again" rule: the
    same offer, not a fresh one, until it is delivered (mark_rendering_delivered
    clears it) or a new, disjoint set of alerts supersedes it."""
    with db.tx(conn):
        evaluate(conn)
        rows = conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL"
                             " ORDER BY alert_id").fetchall()
        if not rows:
            return None
        pending_ids = {a["render_id"] for a in rows if a["render_id"]}
        if len(pending_ids) == 1 and all(a["render_id"] for a in rows):
            rid = next(iter(pending_ids))
            r = conn.execute("SELECT text FROM renders WHERE render_id=? AND"
                             " delivered_at IS NULL", (rid,)).fetchone()
            if r is not None:
                return {"render_id": rid, "text": r["text"]}
        lines = []
        for a in rows:
            if a["kind"] in COLLECTION:
                detail = json.loads(a["detail"])["detail"]
                paren = f" ({detail})" if detail else ""
                lines.append(COLLECTION[a["kind"]].format(paren=paren))
        lines.extend(_delivered_lines(conn, [a for a in rows if a["kind"] == "delivered-changed"]))
        text = "\n".join(w for line in lines for w in views._wrap(line))
        rid = f"r{db.next_seq(conn)}"
        conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                     " membership_json) VALUES (?, 'alert', ?, ?, ?, '[]')",
                     (rid, db.canonical({"alerts": [a["alert_id"] for a in rows]}), db.now(), text))
        conn.execute("UPDATE alerts SET render_id=? WHERE alert_id IN (%s)"
                     % ",".join("?" * len(rows)), [rid] + [a["alert_id"] for a in rows])
    return {"render_id": rid, "text": text}
