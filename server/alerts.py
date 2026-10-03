# server/alerts.py
"""The only things this plugin ever says unprompted (spec §"When the plugin
may speak first"): collection stopped working, a delivered quarter changed
underneath, and what a package request the operator made came to when no
turn was there to say it (package notices: it stopped, the bank could not be
read, its send was taken back or revoked, its send may not have arrived or
did not go out). Once per occurrence, never repeated while the condition
persists, never escalated, no "all better". An occurrence is keyed by the
moment the condition began (a package notice: by its request or delivery),
so a condition that clears and recurs is new. An alert counts as said only
when its rendering was DELIVERED (mark_rendering_delivered sets sent_at); a
send that failed is offered again."""
from __future__ import annotations

import json

import dates
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
# S2 §6.4: a gmail probe recorded `absent` — finance has no Gmail tools at all. Nothing to
# re-authorise: Gmail is not connected for the finance specialist.
GMAIL_ABSENT = "Gmail isn't connected for the finance specialist — invoices aren't being searched."


def evaluate(conn) -> None:
    for kind in COLLECTION:
        p = conn.execute("SELECT * FROM probes WHERE kind=?", (kind,)).fetchone()
        if p is None or p["ok"] or not p["failing_since"]:
            continue
        detail = {"detail": p["detail"] or ""}
        if json.loads(p["data_json"] or "{}").get("absent"):
            detail["absent"] = True
        conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                     " VALUES (?,?,?,?)", (kind, f"{kind}:{p['failing_since']}",
                                          db.canonical(detail), db.now()))


# The package notices (issue #2): what a continuation owes the operator about a
# package they asked for. `package-uncertain` and `package-send-failed` offer the
# package, so "send it again" binds to the rendering that printed them (D3).
PACKAGE = {
    "package-stopped": "I couldn't build the {quarter} package: {reason}.",
    "package-failed": "I couldn't read the bank for the {quarter} package — ask for it again.",
    "package-revoked": "The bank was re-read before I could send the {quarter} package — ask "
                       "for it again and I'll rebuild it.",
    "package-send-failed": "The {quarter} package didn't go out. Say \"send it again\" and "
                           "I'll send it.",
}
OFFERING = ("package-uncertain", "package-send-failed")


def raise_package(conn, kind: str, key: str, *, quarter: str, reason: str = "",
                  package_id=None, pass_id=None) -> int:
    """Raise a package notice inside the caller's transaction. The key is UNIQUE,
    so raising one twice (a replayed claim, a retried end_pass) inserts once.
    The notice remembers the pass it was raised in (the live one, unless named):
    a pass's own notices are in that pass's end_pass message. Returns the
    occurrence's alert_id."""
    if not conn.in_transaction:
        raise RuntimeError("a package notice is raised inside the write transaction")
    if not (kind in PACKAGE or kind == "package-uncertain"):
        raise ValueError(kind)
    if pass_id is None:
        m = conn.execute("SELECT pass_id, live FROM pass_marker WHERE id=1").fetchone()
        pass_id = m["pass_id"] if m is not None and m["live"] else None
    detail = {"quarter": quarter, "reason": views.clip(reason or "", DETAIL_MAX),
              "package_id": package_id, "pass_id": pass_id}
    conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                 " VALUES (?,?,?,?)", (kind, key, db.canonical(detail), db.now()))
    return conn.execute("SELECT alert_id FROM alerts WHERE occurrence_key=?", (key,)).fetchone()[0]


def pass_notices(conn, pass_id) -> list:
    """The undelivered package notices raised during `pass_id` (by its import's
    revocations, say): the ones its end_pass, or the begin_pass that reclaims it,
    must carry."""
    return [r[0] for r in conn.execute(
        "SELECT alert_id FROM alerts WHERE sent_at IS NULL AND kind LIKE 'package-%' AND"
        " json_extract(detail, '$.pass_id')=? ORDER BY alert_id", (pass_id,))]


def _musts(must) -> set:
    if must is None:
        return set()
    return {must} if isinstance(must, int) else set(must)


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
    collection alerts, then the package notices, then each package's changes. A
    unit is (alert_id, (package, quarter) or None, its wrapped lines)."""
    import delivery
    out = []
    for a in rows:
        if a["kind"] in COLLECTION:
            c = json.loads(a["detail"])
            detail = views.clip(c["detail"] or "", DETAIL_MAX)
            paren = f" ({detail})" if detail else ""
            text = (GMAIL_ABSENT if a["kind"] == "gmail" and c.get("absent")
                    else COLLECTION[a["kind"]].format(paren=paren))
            out.append((a["alert_id"], None, views._wrap(text)))
    for a in rows:
        if a["kind"] in PACKAGE or a["kind"] == "package-uncertain":
            c = json.loads(a["detail"])
            # the outcome is told either way; the invitation only while THE predicate
            # says a resend can be staged, else its reason in one clause
            why = delivery.resend_refusal(conn, c["package_id"]) \
                if a["kind"] in OFFERING else None
            if a["kind"] == "package-uncertain":
                fname = conn.execute("SELECT filename FROM packages WHERE package_id=?",
                                     (c["package_id"],)).fetchone()[0]
                lines = delivery.offer_lines(fname) if why is None \
                    else views._wrap(f"{fname} may not have arrived — {why}.")
            elif a["kind"] == "package-send-failed" and why is not None:
                lines = views._wrap(f"The {dates.quarter_label(c['quarter'])} package didn't go "
                                    f"out — {why}.")
            else:
                reason = (c.get("reason") or "").rstrip(". ")
                lines = views._wrap(PACKAGE[a["kind"]].format(
                    quarter=dates.quarter_label(c["quarter"]), reason=reason))
            out.append((a["alert_id"], None, lines))
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


def _batch(units, must=None) -> tuple:
    """The first rendering: whole occurrences, in print order, while the fit
    cuts nothing (with the closing line when some are left over). The first is
    always taken; clipping makes it fit on its own. `must` (an alert_id, or a
    list of them) is taken first instead, so the occurrences a call raised are
    always in its rendering; the others follow in print order while they fit, and what
    does not fit waits for a later rendering."""
    order = {u[0]: i for i, u in enumerate(units)}
    first = [u for u in units if u[0] in _musts(must)] or units[:1]
    chosen = list(first)
    for u in units:
        if u in chosen:
            continue
        trial = sorted(chosen + [u], key=lambda x: order[x[0]])
        if not _render(trial, len(trial) < len(units))[2]:
            break
        chosen = trial
    return _render(chosen, len(chosen) < len(units))[:2]


def pending_rendering(conn, must=None):
    """pending_in_tx under its own write transaction (see there)."""
    with db.tx(conn):
        return pending_in_tx(conn, must)


def pending_in_tx(conn, must=None):
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
    changes the batch supersedes it with a fresh one.

    Runs inside the caller's write transaction: record_delivery raises its
    package notice and composes the rendering that says it in one commit. A
    rendering that prints a notice offering a package names that package in its
    scope's `offers`, so "send it again" binds to it (D3)."""
    assert conn.in_transaction
    evaluate(conn)
    rows = conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL"
                         " ORDER BY alert_id").fetchall()
    if not rows:
        return None
    text, ids = _batch(_units(conn, rows), must)
    import delivery
    offers = sorted({json.loads(a["detail"])["package_id"] for a in rows
                     if a["alert_id"] in ids and a["kind"] in OFFERING
                     and delivery.resend_refusal(conn, json.loads(a["detail"])["package_id"])
                     is None})
    scope = {"alerts": sorted(ids)}
    if offers:
        scope["offers"] = offers
    parked = {a["render_id"] for a in rows if a["alert_id"] in ids}
    if len(parked) == 1 and None not in parked:
        rid = next(iter(parked))
        r = conn.execute("SELECT text, scope_json FROM renders WHERE render_id=? AND"
                         " delivered_at IS NULL", (rid,)).fetchone()
        # reused only if it is still deliverable: a rendering saved oversized by
        # earlier code is re-composed through the fit instead (round 3)
        if r is not None and json.loads(r["scope_json"]) == scope \
                and views.utf16_len(r["text"]) <= views.TELEGRAM_LIMIT:
            return {"render_id": rid, "text": r["text"]}
    rid = f"r{db.next_seq(conn)}"
    conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                 " membership_json) VALUES (?, 'alert', ?, ?, ?, '[]')",
                 (rid, db.canonical(scope), db.now(), text))
    conn.execute("UPDATE alerts SET render_id=? WHERE alert_id IN (%s)"
                 % ",".join("?" * len(ids)), [rid] + ids)
    return {"render_id": rid, "text": text}
