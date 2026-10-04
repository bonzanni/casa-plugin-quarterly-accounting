"""S7: the keyed handlers — the only constructors of authority.OperatorGrant, each after its
key check (§8.1). Every answer is {"receipt": …}: Casa posts that sentence as the tap's
receipt (INV-PROP-002)."""
from __future__ import annotations

import json

import authority
import db
import keys
import matches
import reply
import views
import work

ACTIONS = ("all-good", "right", "wrong", "no-invoice")


def _stale(n) -> str:
    return (f"Nothing was applied: this sheet is out of date — {n} payment"
            f"{'s' if n != 1 else ''} changed since it was shown. Ask me for the list "
            "again to see them as they are now.")


def _item(conn, render_id, pid):
    return conn.execute("SELECT * FROM render_items WHERE render_id=? AND pid=?",
                        (render_id, pid)).fetchone()


def _changed(conn, render_id, pids) -> list:
    """The pids among `pids` whose projection revision, or any match revision the rendering
    recorded for them, differs from now (§7.3: read from the rendering, not from `shown`)."""
    out = []
    for pid in pids:
        it = _item(conn, render_id, pid)
        cur = conn.execute("SELECT revision FROM projections WHERE pid=?", (pid,)).fetchone()
        if it is None or cur is None or cur[0] != it["projection_revision"]:
            out.append(pid)
            continue
        for mid, rev in json.loads(it["match_revisions_json"]).items():
            now = conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                               (int(mid),)).fetchone()
            if now is None or now[0] != rev:
                out.append(pid)
                break
    return out


def _apply_one(conn, grant, render_id, action, d) -> tuple:
    """One payment's verdict, bound to the rendering the tapped button sits on: the
    revisions are those render_items recorded. Returns (result, receipt line)."""
    it = _item(conn, render_id, d["pid"])
    mrevs = json.loads(it["match_revisions_json"])
    cur = d["current"]
    if action in ("all-good", "right"):
        if cur is None:
            raise db.Refusal(keys.NO_LONGER)
        res = matches.confirm_in_tx(conn, grant=grant, match_id=cur["match_id"],
                                    expected_revision=mrevs.get(str(cur["match_id"]), -1),
                                    render_id=render_id, bind="rendered")
        return res, f"Confirmed {views.headline(d)}."
    if action == "wrong" and cur is not None:
        res = matches.reject_in_tx(conn, grant=grant, match_id=cur["match_id"],
                                   expected_revision=mrevs.get(str(cur["match_id"]), -1),
                                   render_id=render_id, bind="rendered")
        return res, f"Unpaired {views.headline(d)}."
    if action == "wrong":
        shown = [c["match_id"] for c in d["candidates"] if str(c["match_id"]) in mrevs]
        if not shown:
            raise db.Refusal(keys.NO_LONGER)
        effects = matches.reject_all_in_tx(conn, d["pid"], [(m, render_id) for m in shown],
                                           grant=grant)
        n = len(shown)
        return ({"set_aside": shown, "effects": effects},
                f"Set aside {'both' if n == 2 else n} candidate{'s' if n != 1 else ''} for "
                f"{views.headline(d)}.")
    res = matches.set_exemption_in_tx(conn, grant=grant, pid=d["pid"], exempt=True,
                                      expected_revision=it["projection_revision"],
                                      render_id=render_id, bind="rendered")
    dropped = [e for e in res["effects"] if e.startswith("unpaired")]
    return res, (f"{views.headline(d)}: needs no document"
                 + ("; dropped its pairing." if dropped else "."))


def verdict(conn, render_id, action, pid, key) -> dict:
    """§7.3: the only button that writes. After the key check (§7.5) and in ONE transaction:
    every affected payment must stand exactly as the tapped rendering recorded it, or
    nothing commits; `all-good` confirms exactly the pairings that rendering proposed."""
    if action not in ACTIONS or (action == "all-good") != (pid is None):
        raise db.Refusal(keys.NO_LONGER)
    with db.tx(conn):
        keys.spend_render(conn, key, render_id, action, pid)
        grant = authority.OperatorGrant("verdict", key)
        r = conn.execute("SELECT * FROM renders WHERE render_id=?", (render_id,)).fetchone()
        if r is None or r["kind"] not in views.SHEET_VIEWS + ("item",):
            raise db.Refusal(keys.NO_LONGER)
        scope = json.loads(r["scope_json"])
        affected = (scope.get("proposed") or []) if action == "all-good" else [pid]
        if not affected or any(_item(conn, render_id, p) is None for p in affected):
            raise db.Refusal(keys.NO_LONGER)
        changed = _changed(conn, render_id, affected)
        if changed:
            raise db.Refusal(_stale(len(changed)))
        lines, applied, quarters = [], [], set()
        for p in affected:
            d = work.describe(conn, p)
            res, line = _apply_one(conn, grant, render_id, action, d)
            applied.append({"pid": p, **res})
            lines.append(line)
            if d["quarter"]:
                quarters.add(d["quarter"])
        lines += reply.package_lines(conn, quarters)
    return {"receipt": views.fit_message(lines), "applied": applied}


CHANGED = ("Something changed since I read your message — nothing was applied. Say it again.")
DONE = {"applied": "That was applied already.", "cancelled": "That was cancelled — nothing "
        "was applied.", "stale": keys.NO_LONGER}


def _reading(conn, reading_id, key):
    import hmac
    row = conn.execute("SELECT * FROM readings WHERE reading_id=?", (reading_id,)).fetchone() \
        if isinstance(reading_id, int) and not isinstance(reading_id, bool) else None
    if (row is None or not isinstance(key, str) or not keys.KEY_RE.fullmatch(key)
            or not hmac.compare_digest(row["key"], key)):
        raise db.Refusal(keys.NO_LONGER)
    if row["state"] != "open":
        raise db.Refusal(DONE[row["state"]])
    return row


class _Changed(Exception):
    pass


def apply_reading(conn, reading_id, key) -> dict:
    """§8: Apply on a reading. After the key check, the stored words are read again under
    the operator's grant, bound to the same rendering, in ONE transaction; they commit only
    when that plan is exactly the stored one — every step, every revision it read, the
    binding row. Anything else: nothing applies, and the reading is stale."""
    with db.tx(conn):
        row = _reading(conn, reading_id, key)
        grant = authority.OperatorGrant("apply_reading", key)
        try:
            with db.savepoint(conn, "replay"):
                out = reply.replay(conn, row, grant)
                if db.canonical(out["plan"]) != row["plan_json"]:
                    raise _Changed
        except _Changed:
            conn.execute("UPDATE readings SET state='stale', settled_at=? WHERE reading_id=?",
                         (db.now(), reading_id))
            return {"receipt": CHANGED}
        conn.execute("UPDATE readings SET state='applied', settled_at=? WHERE reading_id=?",
                     (db.now(), reading_id))
        lines = [x for x in out["receipt"] if not x.startswith("Not rebuilding yet")]
        lines += reply.package_lines(conn, out["quarters"])
    return {"receipt": views.fit_message(lines)}


def cancel_reading(conn, reading_id, key) -> dict:
    """§8: Cancel on a reading — nothing is applied, and the key is spent. It constructs no
    grant: it writes no operator authority."""
    with db.tx(conn):
        _reading(conn, reading_id, key)
        conn.execute("UPDATE readings SET state='cancelled', settled_at=? WHERE reading_id=?",
                     (db.now(), reading_id))
    return {"receipt": "Cancelled — nothing was applied."}
