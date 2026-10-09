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


def resolve(conn, words, pool=None, done="renamed", named_first=False) -> dict:
    """The one vendor the operator's words name: the same text as its name, one of its bank
    texts or the name on its invoice (case and spacing aside; r1 Terra S1: one level, since a
    card shows an unrenamed vendor under its invoice's name), else the one vendor whose name,
    bank texts or invoice name contain the words. Several at the first level that finds any,
    or none: a refusal in words (no fuzzy matching). Payments with no payee text are nobody's
    to name."""
    w = kb.norm(words)
    if not w:
        raise db.Refusal("say which vendor: its name, part of it, or its bank text")
    vs = pool if pool is not None else [v for v in work.vendors(conn) if v["texts"]]

    def held(v):
        return _texts(v) | {kb.norm(v["invoice_name"])}
    levels = [[v for v in vs if w in held(v)]]
    if named_first:
        # #90: a merge's target is named by its own name or bank text before any vendor
        # whose invoice merely prints those words (Ryanair Mtw0's invoice says "Ryanair DAC")
        levels.insert(0, [v for v in vs if w in _texts(v)])
    levels.append([v for v in vs if any(w in t for t in held(v))])
    for hits in levels:
        if len(hits) == 1:
            return hits[0]
        if hits:
            names = ", ".join(views.field(v["name"]) for v in hits[:CANDIDATES_SHOWN])
            more = f" and {len(hits) - CANDIDATES_SHOWN} more" \
                if len(hits) > CANDIDATES_SHOWN else ""
            raise db.Refusal(f"'{views.field(words)}' fits {len(hits)} vendors: {names}{more}. "
                             f"Nothing was {done}; name one of them")
    raise db.Refusal(f"no vendor's name or bank text contains '{views.field(words)}'; nothing "
                     f"was {done}")


def _line(old, new) -> str:
    line = f"{views.field(old.strip())} is now called {views.field(new.strip())}"
    return line if line.endswith(".") else line + "."


def rename_vendor(conn, vendor, new_name=None) -> dict:
    """One rename (a given name, or with new_name None the name on its invoice) and its post:
    the line before the vendor's card (its latest payment's item card), deposited LAST."""
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
            if v["cp"] is None or v["cp"]["named_at"] is None:
                # #92 r1 (Astra): asked for, the name is kept from now on (named_at), also
                # when it is already the one shown
                kb.upsert_in_tx(conn, v["name"], new_name=target)
        else:
            kb.upsert_in_tx(conn, v["name"], new_name=target)
            lead = _line(v["name"], target)
        out = _line_and_card(conn, max(v["pids"]), lead, "rename")
    return _deposit(out)


def _line_and_card(conn, pid, lead, what) -> dict:
    """The line before the item card of payment `pid` (inside the caller's transaction): the
    card composed, the line stored as its page; the deposit's value and key — or, when the
    card has nothing to act on (#93), the plain post of the line and the card."""
    import posting
    rid = posting._compose_list(conn, "item", None, pid, None, None, None)
    r = conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
    line = _lead_rendering(conn, r, lead)
    if not posting.has_actions(conn, r):
        return {"view": None, **posting.plain_post(conn, r, line)}
    value, _ = posting._view_value(conn, r, lead=lead)
    return {"rid": rid, "value": value, "key": posting.delivery_key(conn, what, [rid])}


def _deposit(out) -> dict:
    """The deposit, LAST (after the transaction); a plain post is returned as it is."""
    if "post" in out:
        return out
    ref = casa_broker.deposit("view", out["value"], key=out["key"])
    return {"view": ref, "render_id": out["rid"],
            "note": POSTED.format(after="mark_rendering_delivered(render_id); ")}


def _entries_without_payments(conn, vendors) -> list:
    """#90: a KB entry no live payment resolves to, as a vendor `into` may name (an entry made
    for a name, e.g. "Ryanair DAC", before any payment of its own)."""
    held = {v["cp"]["cp_id"] for v in vendors if v["cp"] is not None}
    return [{"key": kb.norm(r["name"]), "name": r["name"], "texts": [], "pids": [], "cp": r,
             "invoice_name": None}
            for r in conn.execute("SELECT * FROM counterparties ORDER BY name")
            if r["cp_id"] not in held]


def merge_vendors(conn, vendor, into) -> dict:
    """#90: the operator says two vendors are one: X (`vendor`) joins Y (`into`). Every bank
    text and name X had resolves to Y's entry from now on (kb.merge_in_tx), so X's payments,
    documents, rules and a running job's work follow; then "<X> is now part of <Y>." before
    the merged vendor's card, deposited LAST."""
    with db.tx(conn):
        vs = [v for v in work.vendors(conn) if v["texts"]]
        x = resolve(conn, vendor, vs, done="merged")
        if kb.norm(into) in _texts(x):
            raise db.Refusal(f"{views.field(x['name'].strip())} and "
                             f"{views.field(into.strip())} are already one vendor; nothing "
                             "was merged")
        pool = [v for v in vs if v["key"] != x["key"]] + _entries_without_payments(conn, vs)
        if kb.norm(into) == kb.norm(x["invoice_name"]) and not any(
                kb.norm(into) in _texts(v) for v in pool):
            # r1 (Astra S1): the words are X's own invoice name and no other vendor bears it —
            # never a containment guess at a third vendor
            raise db.Refusal(f"no other vendor is called '{views.field(into.strip())}'; "
                             "nothing was merged")
        y = resolve(conn, into, pool, done="merged", named_first=True)
        kb.merge_in_tx(conn, x, y)
        lead = (f"{views.field(x['name'].strip())} is now part of "
                f"{views.field(y['name'].strip())}")
        lead += "" if lead.endswith(".") else "."
        out = _line_and_card(conn, max(x["pids"] + y["pids"]), lead, "merge")
    return _deposit(out)


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
                    # #90: with the vendor whose name (or bank text) the invoice name is
                    owner = kb.counterparty_for(conn, target)
                    kept.append((v["name"], owner["name"] if owner is not None else target))
    parts = []
    if renamed:
        parts.append(f"Renamed {len(renamed)} vendor{'s' if len(renamed) != 1 else ''} to the "
                     "name on their invoice.")
    else:
        parts.append("No vendor needed a new name.")
    if kept:
        pairs = [f"{views.field(a)} → {views.field(b)}" for a, b in kept[:LIST_SHOWN]]
        more = f" and {len(kept) - LIST_SHOWN} more" if len(kept) > LIST_SHOWN else ""
        parts.append(f"{len(kept)} keep{'s' if len(kept) == 1 else ''} the bank name: the "
                     f"invoice name belongs to another vendor ({', '.join(pairs)}{more}). "
                     "Ask to merge them if they are the same vendor.")
    if given:
        parts.append(f"Kept the names you gave: {_names(given)}.")
    if none:
        parts.append(f"No invoice yet: {_names(none)}.")
    body = views.deposit_safe(views.fit_message(" ".join(parts)))
    ref = casa_broker.deposit("results", body)
    return {"results": ref, "renamed": len(renamed), "note": POSTED.format(after="")}
