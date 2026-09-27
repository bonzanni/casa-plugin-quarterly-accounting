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
    "gmail": "Gmail stopped letting me in ({detail}) — invoices aren't being searched. "
             "Re-authorise Gmail when you can.",
    "bank_sync": "The bank connection stopped ({detail}) — new payments aren't coming in. "
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
    with db.tx(conn):
        evaluate(conn)
    rows = conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY alert_id").fetchall()
    if not rows:
        return None
    lines = []
    for a in rows:
        if a["kind"] in COLLECTION:
            lines.append(COLLECTION[a["kind"]].format(detail=json.loads(a["detail"])["detail"]))
    lines.extend(_delivered_lines(conn, [a for a in rows if a["kind"] == "delivered-changed"]))
    text = "\n".join(w for line in lines for w in views._wrap(line))
    with db.tx(conn):
        rid = f"r{db.next_seq(conn)}"
        conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                     " membership_json) VALUES (?, 'alert', ?, ?, ?, '[]')",
                     (rid, db.canonical({"alerts": [a["alert_id"] for a in rows]}), db.now(), text))
    return {"render_id": rid, "text": text}
