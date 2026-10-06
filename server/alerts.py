# server/alerts.py
"""The only things this plugin ever says unprompted (spec §"When the plugin
may speak first"): collection stopped working, a delivered quarter changed
underneath, what became of a package send when no turn was there to say it
(package notices: it could not be sent, it was taken back or revoked, it may
not have arrived or did not go out), and a bank-ledger write bank-feed refused (d1:
keyed by the payment and the write's payload). Once per occurrence, never repeated while the condition
persists, never escalated, no "all better". An occurrence is keyed by the
moment the condition began (a package notice: by its delivery),
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


GMAIL_RUNS = 3              # D10: Gmail's probe failed on this many runs in a row
SYNC_STALE_DAYS = 7         # D10: the last successful bank sync is older than this
STALE_SYNC = "Bank not synced since {day} · bank-feed needs attention"
STOPPED = "Accounting check stopped: {reason}."


def evaluate(conn) -> None:
    """The failures that need the operator (simple loop §1, D10), each said once per
    streak — its occurrence key names the streak, so it repeats only after a success ended
    it and a new one passed the threshold:
    - bank_sync: the store's last successful sync (max snapshots.bank_through) is more than
      SYNC_STALE_DAYS old — keyed by that date;
    - gmail: the probe failed on GMAIL_RUNS runs in a row (probes.fail_runs) — keyed by the
      streak's start (failing_since);
    - bound_account: gone from bank-feed (as before)."""
    import datetime as _dt
    through = conn.execute("SELECT max(bank_through) FROM snapshots").fetchone()[0]
    if through is not None and (dates.parse_day(db.now()[:10])
                                - dates.parse_day(through)) > _dt.timedelta(days=SYNC_STALE_DAYS):
        conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                     " VALUES ('bank_sync', ?, ?, ?)",
                     (f"bank_sync:stale:{through}", db.canonical({"since": through}), db.now()))
    g = conn.execute("SELECT * FROM probes WHERE kind='gmail'").fetchone()
    if g is not None and not g["ok"] and g["failing_since"] and g["fail_runs"] >= GMAIL_RUNS:
        detail = {"detail": g["detail"] or ""}
        if json.loads(g["data_json"] or "{}").get("absent"):
            detail["absent"] = True
        conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                     " VALUES ('gmail', ?, ?, ?)", (f"gmail:{g['failing_since']}",
                                                    db.canonical(detail), db.now()))
    p = conn.execute("SELECT * FROM probes WHERE kind='bound_account'").fetchone()
    if p is not None and not p["ok"] and p["failing_since"]:
        conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                     " VALUES ('bound_account', ?, ?, ?)",
                     (f"bound_account:{p['failing_since']}",
                      db.canonical({"detail": p["detail"] or ""}), db.now()))


def raise_stop(conn, reason: str) -> int:
    """A run's pass stopped (simple loop §2 step 1: no bank-feed tools, setup or the bank
    gate refuses): said once per streak of its reason (D10) — the streak is keyed by the
    latest pass that imported, so a pass that reads the bank again ends it and the next
    stop is said again. Inside the caller's tx."""
    last = conn.execute("SELECT pass_id FROM snapshots ORDER BY snapshot_id DESC LIMIT 1"
                        ).fetchone()
    since = (last[0] if last is not None else None) or "none"
    key = f"stop:{' '.join(str(reason).split())[:400]}:{since}"
    conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                 " VALUES ('run-stopped', ?, ?, ?)",
                 (key, db.canonical({"reason": views.clip(str(reason), DETAIL_MAX)}), db.now()))
    return conn.execute("SELECT alert_id FROM alerts WHERE occurrence_key=?", (key,)).fetchone()[0]


MIRROR_FAILED = ("{n} bank-ledger update{s} did not go through — tried again at the next "
                 "check.")


def raise_incomplete(conn, job_id, lines) -> None:
    """Rule 5 on a scheduled run with nothing new to ask (e2, Astra S2): its "search
    incomplete" lines, said once — keyed by the run. Inside the caller's tx."""
    conn.execute("INSERT OR IGNORE INTO alerts(kind, occurrence_key, detail, raised_at)"
                 " VALUES ('run-incomplete', ?, ?, ?)",
                 (f"incomplete:{job_id}", db.canonical({"lines": list(lines)[:4]}), db.now()))


def _ids(unit) -> list:
    """A unit's alert ids: one, or a tuple of them (the refused mirror writes' one line)."""
    return list(unit[0]) if isinstance(unit[0], tuple) else [unit[0]]


LINES_BUDGET = 1500         # UTF-16 units of alert lines one run message carries


def pending_lines(conn, budget=LINES_BUDGET, said=()) -> tuple:
    """(lines, alert ids): the undelivered alerts as the lines the run's one message
    carries (simple loop §1: "the line joins the end message when there is one") — whole
    occurrences in print order while they fit `budget` (the first always), so the message
    stays within its limit; the rest wait for the next run's message. The ids go into that
    message's scope["alerts"], so its delivery marks exactly them sent (D10). Inside the
    caller's transaction; evaluate() first. `said`: alert ids the message already says in
    its own words (an operator run's stop line): bound, with no line of their own."""
    assert conn.in_transaction
    evaluate(conn)
    said = [a for a in said]
    rows = [r for r in conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY"
                                    " alert_id").fetchall() if r["alert_id"] not in said]
    if not rows:
        return [], sorted(said)
    units = _units(conn, rows)
    chosen = []
    for u in units:
        trial = chosen + [u]
        if chosen and views.utf16_len("\n".join(_lines(trial)[0])) > budget:
            break
        chosen = trial
    return _lines(chosen)[0], sorted([i for u in chosen for i in _ids(u)] + said)


# The package notices (issue #2): what the operator is owed about a package send. `package-uncertain` and `package-send-failed` offer the
# package, so "send it again" binds to the rendering that printed them (D3).
PACKAGE = {
    # raised only by MIGRATIONS[11] since the simple loop (schema 12): a package an older
    # version was asked for and had not sent, and an unsaid stopped / bank-unread notice
    "package-not-sent": "I couldn't send the {quarter} package{why} — ask again when you want it.",
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
    collection alerts, then the refused mirror writes (ONE unit for all of them, d1), then
    the package notices, then each package's changes. A unit is (alert_id — a tuple of
    them for the mirror's —, (package, quarter) or None, its wrapped lines)."""
    import delivery
    out = []
    for a in rows:
        if a["kind"] in COLLECTION:
            c = json.loads(a["detail"])
            if a["kind"] == "bank_sync" and c.get("since"):
                text = STALE_SYNC.format(day=dates.short_day(c["since"]))
            else:
                detail = views.field(c.get("detail") or "", DETAIL_MAX)
                paren = f" ({detail})" if detail else ""
                text = (GMAIL_ABSENT if a["kind"] == "gmail" and c.get("absent")
                        else COLLECTION[a["kind"]].format(paren=paren))
            out.append((a["alert_id"], None, views._wrap(text)))
        elif a["kind"] == "run-stopped":
            reason = views.field(json.loads(a["detail"]).get("reason", "").rstrip(". "), 300)
            out.append((a["alert_id"], None, views._wrap(STOPPED.format(reason=reason))))
        elif a["kind"] == "run-incomplete":
            out.append((a["alert_id"], None, [w for line in json.loads(a["detail"])["lines"]
                                              for w in views._wrap(views.clip(line, 300))]))
    failed = tuple(a["alert_id"] for a in rows if a["kind"] == "mirror-failed")
    if failed:                  # d1: every refused mirror write pending, said as one line
        out.append((failed, None, views._wrap(MIRROR_FAILED.format(
            n=len(failed), s="" if len(failed) == 1 else "s"))))
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
                    else views._wrap(f"{views.field(fname)} may not have arrived — {why}.")
            elif a["kind"] == "package-send-failed" and why is not None:
                lines = views._wrap(f"The {dates.quarter_label(c['quarter'])} package didn't go "
                                    f"out — {why}.")
            else:
                reason = views.field((c.get("reason") or "").rstrip(". "), 300)
                lines = views._wrap(PACKAGE[a["kind"]].format(
                    quarter=dates.quarter_label(c["quarter"]), reason=reason,
                    why=f" ({reason})" if reason else ""))
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
    return ("\n".join(out), [i for u in units if u[0] not in cut for i in _ids(u)],
            whole == len(lines))


def _batch(units, must=None) -> tuple:
    """The first rendering: whole occurrences, in print order, while the fit
    cuts nothing (with the closing line when some are left over). The first is
    always taken; clipping makes it fit on its own. `must` (an alert_id, or a
    list of them) is taken first instead, so the occurrences a call raised are
    always in its rendering; the others follow in print order while they fit, and what
    does not fit waits for a later rendering."""
    order = {u[0]: i for i, u in enumerate(units)}
    first = [u for u in units if set(_ids(u)) & _musts(must)] or units[:1]
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


def pending_in_tx(conn, must=None, skip=()):
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
    scope's `offers`, so "send it again" binds to it (D3).

    `skip` (S7 §5): alert ids left out — the job's cursor passes the occurrences whose
    rendering this run already handed out job.OFFER_MAX times, so the next rendering
    composes the following ones."""
    assert conn.in_transaction
    evaluate(conn)
    skip = set(skip)
    rows = [r for r in conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL"
                                    " ORDER BY alert_id").fetchall()
            if r["alert_id"] not in skip]
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
        # earlier code is re-composed through the fit instead (round 3) — measured
        # against the deposit body's budget (S7 §7.6), so a pre-S7 one up to Telegram's
        # limit is re-fitted and three always join within job.POST_CHARS
        if r is not None and json.loads(r["scope_json"]) == scope \
                and views.utf16_len(r["text"]) <= views.BODY_LIMIT:
            return {"render_id": rid, "text": r["text"]}
    rid = f"r{db.next_seq(conn)}"
    conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                 " membership_json) VALUES (?, 'alert', ?, ?, ?, '[]')",
                 (rid, db.canonical(scope), db.now(), text))
    conn.execute("UPDATE alerts SET render_id=? WHERE alert_id IN (%s)"
                 % ",".join("?" * len(ids)), [rid] + ids)
    return {"render_id": rid, "text": text}
