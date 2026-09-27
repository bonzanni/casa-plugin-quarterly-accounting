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


CHANGE_WORD = {"corrected": "corrected by the bank", "superseded": "replaced by the bank",
               "vanished": "withdrawn by the bank", "erased": "erased from the ledger",
               "reclassified": "now categorised differently"}
MORE_CLOSING = "More changed than fits in one message — the rest comes with the next check."
# Only free-text FIELDS are clipped (fix wave D round 3): a probe's diagnostic
# here, and in views.headline the payee text. The payment's amount and date,
# the package's name (a 32-character slug) and the change are never clipped,
# so every occurrence prints what identifies it and fits a message on its own.
DETAIL_MAX = 300


def _units(conn, rows) -> list:
    """One unit per occurrence, in the order a rendering prints them: the
    collection alerts, then each package's changes. A unit is (alert_id,
    (package, quarter) or None, its wrapped lines)."""
    out = []
    for a in rows:
        if a["kind"] in COLLECTION:
            detail = views.clip(json.loads(a["detail"])["detail"] or "", DETAIL_MAX)
            paren = f" ({detail})" if detail else ""
            out.append((a["alert_id"], None,
                        views._wrap(COLLECTION[a["kind"]].format(paren=paren))))
    changed = []
    for a in rows:
        if a["kind"] == "delivered-changed":
            changed.append((json.loads(a["detail"])["package"], a["alert_id"], a))
    for pkg, _, a in sorted(changed, key=lambda x: (x[0], x[1])):
        c = json.loads(a["detail"])
        pid = conn.execute("SELECT pid FROM aliases WHERE row_id=?", (c["row_id"],)).fetchone()
        head = views.headline(work.describe(conn, pid[0])) if pid else f"payment #{c['row_id']}"
        word = CHANGE_WORD.get(c["change"], c["change"])
        out.append((a["alert_id"], (pkg, c["quarter"]), views._wrap(f"{head} — {word}")))
    return out


def _lines(units) -> tuple:
    """The rendering's lines and, per line, the occurrence it prints (None for
    a package's heading and closing sentence)."""
    lines, owners, pkg = [], [], None

    def put(text, owner=None):
        for w in views._wrap(text):
            lines.append(w)
            owners.append(owner)

    def close():
        q = pkg[1].split("-")[1]
        put(f'Your accountant holds the old numbers. Say "rebuild {q}" if they need a fresh one.')
    for alert_id, group, wrapped in units:
        if group != pkg:
            if pkg is not None:
                close()
            pkg = group
            if group is not None:
                put(f"The package {group[0]} changed underneath:")
        lines.extend(wrapped)
        owners.extend([alert_id] * len(wrapped))
    if pkg is not None:
        close()
    return lines, owners


def _render(units, partial: bool) -> tuple:
    """(text, bound alert ids, intact) through views.fit_lines: an occurrence
    binds only when every line of it is printed in full; `intact` when the fit
    cut nothing."""
    lines, owners = _lines(units)
    out, whole = views.fit_lines(lines, MORE_CLOSING, always_close=partial)
    cut = {o for o in owners[whole:] if o is not None}
    return "\n".join(out), [u[0] for u in units if u[0] not in cut], whole == len(lines)


def _batch(units) -> tuple:
    """The first rendering: whole occurrences, in print order, while the fit
    cuts nothing (with the closing line when some are left over). The first is
    always taken; clipping makes it fit on its own."""
    chosen = units[:1]
    for u in units[1:]:
        partial = len(chosen) + 1 < len(units)
        if not _render(chosen + [u], partial)[2]:
            break
        chosen.append(u)
    return _render(chosen, len(chosen) < len(units))[:2]


def pending_rendering(conn):
    """evaluate(), the read of undelivered alerts, composition and the renders
    INSERT all run under ONE db.tx: end_pass frees the pass marker before
    calling here, so a fresh pass can begin, re-observe the same still-failing
    condition and reach this function while an earlier pass's own call is
    still in flight (fix round 1: two db.tx blocks with composition outside
    either left a window where both could SELECT the same undelivered alert
    and each INSERT a different render for it). One lock closes that window.

    A rendering never exceeds Telegram's limit (fix wave D): it prints the
    first batch of undelivered occurrences that fits, closes with a line saying
    more follows, and binds ONLY the occurrences it prints (scope "alerts"), so
    once-per-occurrence holds and the rest are offered by the next rendering.
    "An undelivered alert is offered again" is the SAME offer: when the batch
    composed now is exactly the occurrences an existing undelivered rendering
    already holds, that rendering is returned unchanged; a new occurrence that
    changes the batch supersedes it with a fresh one."""
    with db.tx(conn):
        evaluate(conn)
        rows = conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL"
                             " ORDER BY alert_id").fetchall()
        if not rows:
            return None
        text, ids = _batch(_units(conn, rows))
        parked = {a["render_id"] for a in rows if a["alert_id"] in ids}
        if len(parked) == 1 and None not in parked:
            rid = next(iter(parked))
            r = conn.execute("SELECT text, scope_json FROM renders WHERE render_id=? AND"
                             " delivered_at IS NULL", (rid,)).fetchone()
            # reused only if it is still deliverable: a rendering saved oversized by
            # earlier code is re-composed through the fit instead (round 3)
            if r is not None and sorted(json.loads(r["scope_json"]).get("alerts", [])) \
                    == sorted(ids) and views.utf16_len(r["text"]) <= views.TELEGRAM_LIMIT:
                return {"render_id": rid, "text": r["text"]}
        rid = f"r{db.next_seq(conn)}"
        conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                     " membership_json) VALUES (?, 'alert', ?, ?, ?, '[]')",
                     (rid, db.canonical({"alerts": sorted(ids)}), db.now(), text))
        conn.execute("UPDATE alerts SET render_id=? WHERE alert_id IN (%s)"
                     % ",".join("?" * len(ids)), [rid] + ids)
    return {"render_id": rid, "text": text}
