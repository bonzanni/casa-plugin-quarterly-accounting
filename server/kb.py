"""Counterparty knowledge base and the expectation mapping's overrides
(spec §"Document expectation", "Counterparty KB"). Read side here; the
write side (upsert_counterparty, set_expectation) is Task 10.

A pattern is the counterparty text exactly as bank-feed shows it
(`BCK*ZAPIER`); matching is exact after case and whitespace normalisation —
the `*` is part of the bank's text, never a wildcard, and there is no fuzzy
matching anywhere in this plugin."""
from __future__ import annotations

import json
import re

import db
import expectation as ex

_TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def norm(s) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def counterparty_for(conn, bank_counterparty):
    text = norm(bank_counterparty)
    if not text:
        return None
    for r in conn.execute("SELECT * FROM counterparties ORDER BY cp_id"):
        if text == norm(r["name"]) or text in {norm(p) for p in json.loads(r["patterns_json"])}:
            return r
    return None


def override_of(cp):
    if cp is None or cp["exp_kind"] is None:
        return None
    return (cp["exp_kind"], cp["exp_tier"] if cp["exp_kind"] != "none" else None)


def is_portal(cp) -> bool:
    return cp is not None and cp["source"] == "portal"


def parse_scope(scope: str) -> frozenset:
    return frozenset(t.strip().lower() for t in (scope or "").split(",") if t.strip())


def chain_overrides(conn) -> list:
    return [(frozenset(json.loads(r["rows_json"])), frozenset(json.loads(r["key_json"])),
             r["kind"], r["tier"])
            for r in conn.execute("SELECT * FROM chain_overrides ORDER BY scope")]


def display_name(conn, bank_counterparty) -> str:
    cp = counterparty_for(conn, bank_counterparty)
    return cp["name"] if cp is not None else (bank_counterparty or "Unknown payee")


def _entry(conn, name):
    for r in conn.execute("SELECT * FROM counterparties"):
        if norm(r["name"]) == norm(name):
            return r
    return None


def get_counterparty(conn, text):
    r = counterparty_for(conn, text)
    if r is None:
        return None
    out = dict(r)
    out["patterns"] = json.loads(out.pop("patterns_json"))
    return out


def upsert_counterparty(conn, name, *, patterns=(), source=None, document_link=None,
                        link_note=None, search_hint=None, notes=None, window_days=None,
                        token=None) -> dict:
    import passes
    with db.tx(conn):
        passes.check_token(conn, token)
        return upsert_in_tx(conn, name, patterns=patterns, source=source,
                            document_link=document_link, link_note=link_note,
                            search_hint=search_hint, notes=notes, window_days=window_days)


def upsert_in_tx(conn, name, *, patterns=(), source=None, document_link=None, link_note=None,
                 search_hint=None, notes=None, window_days=None) -> dict:
    """The upsert inside the caller's transaction (apply_reply's identity clause
    checks the shown revision in the same transaction as this write)."""
    import lineage
    if not (name or "").strip():
        raise db.Refusal("a counterparty needs a name")
    if source not in (None, "email", "portal"):
        raise db.Refusal("source is 'email' or 'portal'")
    if window_days is not None and not (1 <= int(window_days) <= 60):
        raise db.Refusal("window_days is between 1 and 60")
    existing = _entry(conn, name)
    held = json.loads(existing["patterns_json"]) if existing is not None else []
    merged = sorted(set(held) | {p.strip() for p in patterns})
    # Every bank text resolves to at most one entry (fix wave B, Astra S1; round
    # B2, Terra S1): ALL of this upsert's names and patterns — the ones it adds and
    # the ones the entry already holds — are checked against every OTHER entry's,
    # on create and on update. A collision would leave a ruling stored on one
    # entry that lookup never reaches. Refused, not merged: the operator names the
    # owner, and set_expectation by that bank text already lands on it.
    _refuse_shared_bank_text(conn, existing, [name.strip()] + merged)
    if existing is None:
        conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at)"
                     " VALUES (?, '[]', ?)", (name.strip(), db.now()))
        existing = _entry(conn, name)
    fields = {"patterns_json": json.dumps(merged), "source": source,
              "document_link": document_link, "link_note": link_note,
              "search_hint": search_hint, "notes": notes, "window_days": window_days}
    sets = {k: v for k, v in fields.items() if v is not None}
    sets["updated_at"] = db.now()
    conn.execute("UPDATE counterparties SET %s WHERE cp_id=?"
                 % ", ".join(f"{k}=?" for k in sets), (*sets.values(), existing["cp_id"]))
    lineage.settle_all(conn)
    return get_counterparty(conn, name) or {}


def _texts(name, patterns) -> set:
    return {t for t in (norm(name), *(norm(p) for p in patterns)) if t}


def _refuse_shared_bank_text(conn, entry, texts) -> None:
    mine = {norm(t): t for t in texts if norm(t)}
    for r in conn.execute("SELECT * FROM counterparties ORDER BY cp_id"):
        if entry is not None and r["cp_id"] == entry["cp_id"]:
            continue
        shared = set(mine) & _texts(r["name"], json.loads(r["patterns_json"]))
        if shared:
            raise db.Refusal(f"the bank text {mine[min(shared)]!r} already belongs to "
                             f"{r['name']}; change {r['name']} instead, or give this "
                             "counterparty another name")


def _require_delivered_render(conn, render_id) -> None:
    r = conn.execute("SELECT delivered_at FROM renders WHERE render_id=?",
                     (render_id,)).fetchone() if render_id else None
    if r is None or r["delivered_at"] is None:
        raise db.Refusal("an operator decision must come from a view the operator was shown "
                         "(a delivered render_id)")


def set_expectation(conn, *, scope_type, scope, kind, tier=None, author, render_id=None,
                    token=None) -> dict:
    import passes
    if scope_type not in ("counterparty", "chain"):
        raise db.Refusal("scope_type is 'counterparty' or 'chain'")
    if author not in ("operator", "specialist"):
        raise db.Refusal("author is 'operator' or 'specialist'")
    if kind != "default":
        if kind not in ex.KINDS + ("none",):
            raise db.Refusal(f"kind is one of {', '.join(ex.KINDS)}, 'none' or 'default'")
        if kind == "none":
            tier = None
        elif tier not in ex.TIERS:
            raise db.Refusal("a document kind needs a tier: 'required' or 'optional'")
    with db.tx(conn):
        passes.check_token(conn, token)
        return set_expectation_in_tx(conn, scope_type=scope_type, scope=scope, kind=kind,
                                     tier=tier, author=author, render_id=render_id)


def set_expectation_in_tx(conn, *, scope_type, scope, kind, tier=None, author,
                          render_id=None) -> dict:
    """The override inside the caller's transaction, so apply_reply can check,
    in that same transaction, that every payment it changes was shown."""
    import lineage
    if kind == "none":
        tier = None
    if author == "operator":
        _require_delivered_render(conn, render_id)
    if scope_type == "chain":
        if author != "operator":
            raise db.Refusal("a class-level expectation is the operator's to set")
        tags = parse_scope(scope)
        if not tags or not all(_TAG.match(t) for t in tags):
            raise db.Refusal("a chain is comma-separated classification tags")
        norm = ex.normalize_scope(tags)
        if norm is None:
            raise db.Refusal("those tags select different rows of the mapping; name one chain")
        rows, okey = norm
        key = ", ".join(sorted(tags))
        # A chain override replaces any existing one whose normalized (rows, key)
        # is the same, regardless of the literal scope text it was set under
        # (carried ruling, Task 4 review): "refund" and "income, refund" both
        # normalize to the same (rows, key), and derive() refuses to guess
        # between two overrides that collide there — so the store never lets
        # two such overrides coexist; the operator's later ruling wins.
        for r in conn.execute("SELECT scope, rows_json, key_json FROM chain_overrides"):
            if (frozenset(json.loads(r["rows_json"])), frozenset(json.loads(r["key_json"]))) \
                    == (rows, okey):
                conn.execute("DELETE FROM chain_overrides WHERE scope=?", (r["scope"],))
        if kind != "default":
            conn.execute("INSERT INTO chain_overrides(scope, kind, tier, rows_json,"
                         " key_json, author, set_at) VALUES (?,?,?,?,?,?,?)",
                         (key, kind, tier, json.dumps(sorted(rows)), json.dumps(sorted(okey)),
                          author, db.now()))
    else:
        e = _entry(conn, scope) or counterparty_for(conn, scope)
        if e is None:
            conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at)"
                         " VALUES (?, '[]', ?)", (scope.strip(), db.now()))
            e = _entry(conn, scope)
        if kind == "default":
            conn.execute("UPDATE counterparties SET exp_kind=NULL, exp_tier=NULL,"
                         " exp_author=NULL, updated_at=? WHERE cp_id=?", (db.now(), e["cp_id"]))
        else:
            conn.execute("UPDATE counterparties SET exp_kind=?, exp_tier=?, exp_author=?,"
                         " updated_at=? WHERE cp_id=?", (kind, tier, author, db.now(),
                                                         e["cp_id"]))
    lineage.settle_all(conn)
    return {"scope_type": scope_type, "scope": scope, "kind": kind, "tier": tier}
