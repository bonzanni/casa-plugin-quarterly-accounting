"""#89 (OPERATOR DECISION): naming a vendor posts its own outcome. Three rounds of skill
wording did not make the desk's rename replies short, so the rename itself posts what the
operator sees and the model adds nothing:
- rename_vendor: one vendor, found from the operator's words for it, gets a given name or
  the name on its invoice; Casa posts "<old> is now called <new>." before the vendor's card;
- rename_all: every vendor gets the name on its invoice, and ONE short summary is posted.
The store's rename (kb.upsert_in_tx's new_name, #84) and its refusals are unchanged."""
from __future__ import annotations

import json

import casa_broker
import db
import kb
import views
import work

CANDIDATES_SHOWN = 8       # an ambiguous vendor's refusal names at most this many
LIST_SHOWN = 25            # a summary's name list, then "and N more"

POSTED = ("Casa posts this to the operator itself; never retell it. After its receipt, "
          "{after}your whole reply is <silent/>.")


def _texts(v) -> set:
    cp = v["cp"]
    held = set(v["texts"]) | {v["name"]}
    if cp is not None:
        held |= {cp["name"], *json.loads(cp["patterns_json"])}
    return {kb.norm(t) for t in held if kb.norm(t)}


def resolve(conn, words) -> dict:
    """The one vendor the operator's words name: the same text as its name, one of its bank
    texts or the name on its invoice (case and spacing aside; r1 Terra S1: one level, since a
    card shows an unrenamed vendor under its invoice's name), else the one vendor whose name,
    bank texts or invoice name contain the words. Several at the first level that finds any,
    or none: a refusal in words (no fuzzy matching). Payments with no payee text are nobody's
    to name."""
    w = kb.norm(words)
    if not w:
        raise db.Refusal("say which vendor: its name, part of it, or its bank text")
    vs = [v for v in work.vendors(conn) if v["texts"]]
    for hits in ([v for v in vs if w in _texts(v) | {kb.norm(v["invoice_name"])}],
                 [v for v in vs if any(w in t for t in _texts(v) | {kb.norm(v["invoice_name"])})]):
        if len(hits) == 1:
            return hits[0]
        if hits:
            names = ", ".join(views.field(v["name"]) for v in hits[:CANDIDATES_SHOWN])
            more = f" and {len(hits) - CANDIDATES_SHOWN} more" \
                if len(hits) > CANDIDATES_SHOWN else ""
            raise db.Refusal(f"'{views.field(words)}' fits {len(hits)} vendors: {names}{more}. "
                             "Nothing was renamed; name one of them")
    raise db.Refusal(f"no vendor's name or bank text contains '{views.field(words)}'; nothing "
                     "was renamed")


def _line(old, new) -> str:
    line = f"{views.field(old.strip())} is now called {views.field(new.strip())}"
    return line if line.endswith(".") else line + "."


def rename_vendor(conn, vendor, new_name=None) -> dict:
    """One rename (a given name, or with new_name None the name on its invoice) and its post:
    the line before the vendor's card (its latest payment's item card), deposited LAST."""
    import posting
    if new_name is not None and (not isinstance(new_name, str) or not new_name.strip()):
        raise db.Refusal("new_name is the name to give; leave it out for the name on its "
                         "invoice")
    with db.tx(conn):
        v = resolve(conn, vendor)
        target = new_name if new_name is not None else v["invoice_name"]
        if target is None:
            raise db.Refusal(f"{views.field(v['name'])} has no matched invoice yet, so there "
                             "is no invoice name to use; nothing was renamed")
        if target.strip() == v["name"].strip():
            lead = f"{views.field(v['name'].strip())} already has that name."
        else:
            kb.upsert_in_tx(conn, v["name"], new_name=target)
            lead = _line(v["name"], target)
        rid = posting._compose_list(conn, "item", None, max(v["pids"]), None, None, None)
        r = conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        _lead_rendering(conn, r, lead)
        value, _ = posting._view_value(conn, r, lead=lead)
        key = posting.delivery_key(conn, "rename", [rid])
    ref = casa_broker.deposit("view", value, key=key)
    return {"view": ref, "render_id": rid,
            "note": POSTED.format(after="mark_rendering_delivered(render_id); ")}


def _lead_rendering(conn, card, text) -> str:
    """The rename's line is a page posted before the card (#66's pages): stored as a rendering
    of its own with the card's kind, scope and payments, and stamped posted, so a reply
    quoting the line binds as a reply quoting the card does."""
    rid = f"r{db.next_seq(conn)}"
    conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                 " membership_json) VALUES (?,?,?,?,?,?)",
                 (rid, card["kind"], card["scope_json"], db.now(), text,
                  card["membership_json"]))
    conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                 " match_revisions_json) SELECT ?, pid, projection_revision,"
                 " match_revisions_json FROM render_items WHERE render_id=?",
                 (rid, card["render_id"]))
    conn.execute("UPDATE renders SET posted_seq=? WHERE render_id=?", (db.next_seq(conn), rid))
    return rid


def _names(names) -> str:
    shown = ", ".join(views.field(n) for n in names[:LIST_SHOWN])
    return shown + (f" and {len(names) - LIST_SHOWN} more" if len(names) > LIST_SHOWN else "")


def rename_all(conn) -> dict:
    """Every vendor gets the name on its invoice, in ONE transaction, each rename in a
    savepoint: a name the operator gave stays (named_at), a vendor whose invoice name the
    store refuses (another vendor's name or bank text) keeps its name, one with no invoice
    name is listed. Then ONE summary message, deposited LAST."""
    renamed, kept, given, none = [], [], [], []
    with db.tx(conn):
        for v in work.vendors(conn):
            if not v["texts"]:
                continue
            target = v["invoice_name"]
            if target is None:
                none.append(v["name"])
            elif target.strip() == v["name"].strip():
                continue
            elif v["cp"] is not None and v["cp"]["named_at"] is not None:
                given.append(v["name"])
            else:
                try:
                    with db.savepoint(conn, "rename_one"):
                        kb.upsert_in_tx(conn, v["name"], new_name=target)
                    renamed.append(target)
                except db.Refusal:
                    kept.append(v["name"])
    parts = []
    if renamed:
        parts.append(f"Renamed {len(renamed)} vendor{'s' if len(renamed) != 1 else ''} to the "
                     "name on their invoice.")
    else:
        parts.append("No vendor needed a new name.")
    if kept:
        parts.append(f"{len(kept)} keep{'s' if len(kept) == 1 else ''} the bank name: the "
                     f"invoice name belongs to another vendor ({_names(kept)}).")
    if given:
        parts.append(f"Kept the names you gave: {_names(given)}.")
    if none:
        parts.append(f"No invoice yet: {_names(none)}.")
    body = views.deposit_safe(views.fit_message(" ".join(parts)))
    ref = casa_broker.deposit("results", body)
    return {"results": ref, "renamed": len(renamed), "note": POSTED.format(after="")}
